"""Data integrity validation - the gate every dataset must pass before any strategy runs.

Status levels: PASS < INFO < WARN < FAIL. A FAIL blocks backtesting.

Cleaning policy: ``clean_bars`` may only (a) sort and (b) drop EXACT duplicate
rows. It never edits prices and never fills missing bars - fabricated bars would
create fabricated trades.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from edgelab.data.calendar import SessionCalendar
from edgelab.data.schema import ASK_COLUMNS, BarArrays, DatasetManifest, canonicalize
from edgelab.instruments import Instrument

_ORDER = {"PASS": 0, "INFO": 1, "WARN": 2, "FAIL": 3}
DEFAULT_THRESHOLDS = {
    "max_missing_bar_ratio_warn": 0.001,
    "max_missing_bar_ratio_fail": 0.05,
    "max_outside_session_ratio_fail": 0.001,
    "spike_range_multiple": 25.0,
    "tick_misaligned_ratio_warn": 0.0,
    "allow_nonpositive_prices": False,
}


class DataIntegrityError(RuntimeError):
    def __init__(self, report: "DataQualityReport"):
        self.report = report
        super().__init__("dataset failed validation:\n" + report.to_text())


@dataclass
class Check:
    name: str
    status: str
    count: int = 0
    message: str = ""
    examples: list = field(default_factory=list)


@dataclass
class DataQualityReport:
    instrument: str
    timeframe_minutes: int
    n_bars: int
    start: str | None
    end: str | None
    checks: list[Check] = field(default_factory=list)

    @property
    def status(self) -> str:
        worst = max((_ORDER[c.status] for c in self.checks), default=0)
        return {v: k for k, v in _ORDER.items()}[worst]

    def get(self, name: str) -> Check:
        for c in self.checks:
            if c.name == name:
                return c
        raise KeyError(name)

    @property
    def missing_bars(self) -> int:
        return self.get("missing_bars").count

    @property
    def duplicate_bars(self) -> int:
        return self.get("exact_duplicates").count + self.get("conflicting_duplicates").count

    def to_dict(self) -> dict:
        return {"instrument": self.instrument, "timeframe_minutes": self.timeframe_minutes,
                "n_bars": self.n_bars, "start": self.start, "end": self.end,
                "status": self.status,
                "checks": [c.__dict__ for c in self.checks]}

    def to_text(self) -> str:
        lines = [f"DATA QUALITY REPORT - {self.instrument} {self.timeframe_minutes}m  "
                 f"[{self.status}]",
                 f"  bars={self.n_bars:,}  range={self.start} -> {self.end}"]
        for c in self.checks:
            ex = f"  e.g. {c.examples[:3]}" if c.examples and c.status != "PASS" else ""
            lines.append(f"  {c.status:<4}  {c.name:<26} n={c.count:<8,} {c.message}{ex}")
        return "\n".join(lines)


def _ts_ns(series) -> np.ndarray:
    return pd.DatetimeIndex(series).as_unit("ns").asi8


def _fmt(ts_ns: np.ndarray, k: int = 5) -> list[str]:
    return [str(pd.Timestamp(int(t), tz="UTC")) for t in ts_ns[:k]]


def _check_ask(bars, have_ask, o, h, l, c, ts, add) -> None:
    """Observed ASK OHLC must be complete, finite, internally consistent and never below the
    BID primary series. Violations FAIL (never repaired, never partially stored)."""
    if len(have_ask) != len(ASK_COLUMNS):
        add(Check("ask_ohlc_columns", "FAIL", len(have_ask),
                  f"ASK OHLC is all-or-none; only {have_ask} present"))
        return
    add(Check("ask_ohlc_columns", "PASS", len(have_ask), "ask_open/high/low/close present"))
    ao, ah, al, ac = (bars[k].to_numpy() for k in ASK_COLUMNS)
    bad = ~np.isfinite(np.c_[ao, ah, al, ac]).all(axis=1)
    add(Check("ask_finite_prices", "FAIL" if bad.any() else "PASS", int(bad.sum()),
              "missing/NaN/inf ASK OHLC value (a quote side is never partially stored)", _fmt(ts[bad])))
    with np.errstate(invalid="ignore"):
        bad_ohlc = (ah < np.maximum(ao, ac)) | (al > np.minimum(ao, ac)) | (ah < al) | (np.c_[ao, ah, al, ac] <= 0).any(axis=1)
        below = (ao < o) | (ah < h) | (al < l) | (ac < c)
    add(Check("ask_ohlc_integrity", "FAIL" if bad_ohlc.any() else "PASS", int(bad_ohlc.sum()),
              "ASK high < max(open,close), low > min(open,close), high < low, or non-positive",
              _fmt(ts[bad_ohlc])))
    add(Check("ask_not_below_bid", "FAIL" if below.any() else "PASS", int(below.sum()),
              "ASK below BID on open, high, low or close (crossed quotes / misaligned feeds)",
              _fmt(ts[below])))


def validate_bars(df: pd.DataFrame, instrument: Instrument, calendar: SessionCalendar,
                  tf_minutes: int, thresholds: Mapping[str, Any] | None = None) -> DataQualityReport:
    th = {**DEFAULT_THRESHOLDS, **(thresholds or {})}
    bars = canonicalize(df)
    n = len(bars)
    ts = _ts_ns(bars["ts"])
    rep = DataQualityReport(instrument.symbol, tf_minutes, n,
                            str(bars["ts"].min()) if n else None,
                            str(bars["ts"].max()) if n else None)
    add = rep.checks.append
    if n == 0:
        add(Check("non_empty", "FAIL", 0, "dataset is empty"))
        # keep the named checks present so accessors work
        for nm in ("exact_duplicates", "conflicting_duplicates", "missing_bars"):
            add(Check(nm, "PASS"))
        return rep
    add(Check("non_empty", "PASS", n))

    o, h, l, c, v = (bars[k].to_numpy() for k in ("open", "high", "low", "close", "volume"))

    # --- NaN / non-finite ------------------------------------------------------
    bad = ~np.isfinite(np.c_[o, h, l, c]).all(axis=1)
    add(Check("finite_prices", "FAIL" if bad.any() else "PASS", int(bad.sum()),
              "NaN/inf OHLC values", _fmt(ts[bad])))

    # --- ordering ---------------------------------------------------------------
    back = np.flatnonzero(np.diff(ts) < 0) + 1
    add(Check("timestamp_order", "FAIL" if len(back) else "PASS", len(back),
              "timestamps going backwards (run clean_bars to sort)", _fmt(ts[back])))

    # --- duplicates -------------------------------------------------------------
    dup_ts_mask = bars.duplicated("ts", keep=False).to_numpy()
    exact_mask = bars.duplicated(list(bars.columns), keep="first").to_numpy()
    n_exact = int(exact_mask.sum())
    if dup_ts_mask.any():
        ask_cols = [k for k in ASK_COLUMNS if k in bars.columns]
        distinct = (bars.loc[dup_ts_mask, ["ts", "open", "high", "low", "close", "volume"] + ask_cols]
                    .drop_duplicates().groupby("ts").size())
        conflicting = list(_ts_ns(pd.Series(distinct.index[distinct.to_numpy() > 1])))
    else:
        conflicting = []
    add(Check("exact_duplicates", "WARN" if n_exact else "PASS", n_exact,
              "identical repeated rows (clean_bars removes these)", _fmt(ts[exact_mask])))
    add(Check("conflicting_duplicates", "FAIL" if conflicting else "PASS", len(conflicting),
              "same timestamp, different values - cannot resolve automatically",
              _fmt(np.array(conflicting, dtype=np.int64))))

    # --- OHLC integrity ---------------------------------------------------------
    with np.errstate(invalid="ignore"):
        bad_ohlc = (h < np.maximum(o, c)) | (l > np.minimum(o, c)) | (h < l)
    add(Check("ohlc_integrity", "FAIL" if bad_ohlc.any() else "PASS", int(bad_ohlc.sum()),
              "high < max(open,close) or low > min(open,close) or high < low",
              _fmt(ts[bad_ohlc])))
    if not th["allow_nonpositive_prices"]:
        nonpos = (np.c_[o, h, l, c] <= 0).any(axis=1)
        add(Check("positive_prices", "FAIL" if nonpos.any() else "PASS", int(nonpos.sum()),
                  "non-positive prices (set allow_nonpositive_prices for e.g. CL 2020)",
                  _fmt(ts[nonpos])))
    negv = v < 0
    add(Check("volume_non_negative", "FAIL" if negv.any() else "PASS", int(negv.sum()),
              "negative volume", _fmt(ts[negv])))
    nanv = ~np.isfinite(v)
    if nanv.all():
        add(Check("volume_availability", "INFO", n, "no volume in dataset: volume-based "
                  "features (VWAP, relative volume) will be unavailable"))
    elif nanv.any():
        add(Check("volume_availability", "WARN", int(nanv.sum()),
                  "volume missing on some bars only (inconsistent feed); volume features refuse "
                  "to run until resolved", _fmt(ts[nanv])))
    else:
        add(Check("volume_availability", "PASS", 0, "volume present on every bar"))

    # --- spread (optional column, CFDs) ---------------------------------------------
    if "spread" in bars.columns:
        sp = bars["spread"].to_numpy()
        neg = sp < 0
        nan_sp = ~np.isfinite(sp)
        add(Check("spread_non_negative", "FAIL" if neg.any() else "PASS", int(neg.sum()),
                  "negative spread (bid > ask?)", _fmt(ts[neg])))
        add(Check("spread_availability", "WARN" if nan_sp.any() else "PASS", int(nan_sp.sum()),
                  "bars without a spread value; dataset-spread cost mode refuses trades on them",
                  _fmt(ts[nan_sp])))

    # --- ASK OHLC (optional second quote side, ADR-55) ------------------------------
    have_ask = [k for k in ASK_COLUMNS if k in bars.columns]
    if have_ask:
        _check_ask(bars, have_ask, o, h, l, c, ts, add)

    # --- tick alignment ---------------------------------------------------------
    px = np.c_[o, h, l, c]
    ticks = px / instrument.tick_size
    off = (np.abs(ticks - np.round(ticks)) > 1e-6).any(axis=1)
    ratio = off.mean()
    add(Check("tick_alignment", "WARN" if ratio > th["tick_misaligned_ratio_warn"] else "PASS",
              int(off.sum()), f"prices not on {instrument.tick_size} tick grid "
              f"({ratio:.2%}; expected for adjusted continuous or CFD data)", _fmt(ts[off])))

    # --- session / calendar -----------------------------------------------------
    uniq_ts = np.unique(ts)
    idx = pd.DatetimeIndex(uniq_ts.astype("datetime64[ns]")).tz_localize("UTC")
    inside = calendar.in_session(idx)
    outside = ~inside
    out_ratio = outside.mean()
    status = ("FAIL" if out_ratio > th["max_outside_session_ratio_fail"]
              else "WARN" if outside.any() else "PASS")
    add(Check("bars_outside_session", status, int(outside.sum()),
              f"bars where calendar '{calendar.name}' says market closed ({out_ratio:.3%}); "
              "a cluster after a DST change means a timezone error", _fmt(uniq_ts[outside])))

    expected = calendar.expected_bar_opens(idx[0], idx[-1], tf_minutes).asi8
    grid_ok = np.isin(uniq_ts[inside], expected)
    n_misaligned = int((~grid_ok).sum())
    add(Check("grid_alignment", "FAIL" if n_misaligned else "PASS", n_misaligned,
              f"in-session bars not on the session-anchored {tf_minutes}m grid "
              "(wrong timestamp convention or timeframe?)", _fmt(uniq_ts[inside][~grid_ok])))

    missing = np.setdiff1d(expected, uniq_ts, assume_unique=True)
    miss_ratio = len(missing) / max(len(expected), 1)
    status = ("FAIL" if miss_ratio > th["max_missing_bar_ratio_fail"]
              else "WARN" if miss_ratio > th["max_missing_bar_ratio_warn"]
              else "INFO" if len(missing) else "PASS")
    add(Check("missing_bars", status, int(len(missing)),
              f"expected in-session bars absent ({miss_ratio:.3%} of {len(expected):,}); "
              "never auto-filled", _fmt(missing)))

    # whole trading days missing -> unlisted holiday or outage
    if len(expected):
        exp_td = calendar.trading_dates(pd.DatetimeIndex(expected.astype("datetime64[ns]")).tz_localize("UTC"))
        have_td = np.unique(calendar.trading_dates(idx))
        missing_days = np.setdiff1d(np.unique(exp_td), have_td)
    else:
        missing_days = np.array([], dtype="datetime64[D]")
    add(Check("missing_trading_days", "WARN" if len(missing_days) else "PASS", len(missing_days),
              "entire trading days absent - add to calendar holidays if exchange was closed",
              [str(d) for d in missing_days[:5]]))

    # --- spikes / bad ticks -----------------------------------------------------
    rng = h - l
    med = np.nanmedian(rng[rng > 0]) if (rng > 0).any() else 0.0
    spikes = rng > th["spike_range_multiple"] * med if med > 0 else np.zeros(n, bool)
    add(Check("range_spikes", "WARN" if spikes.any() else "PASS", int(spikes.sum()),
              f"bar range > {th['spike_range_multiple']}x median ({med:g}); inspect for bad ticks",
              _fmt(ts[spikes])))

    # --- DST transitions (informational context) ----------------------------------
    loc = idx.tz_convert(calendar.timezone)
    offs = np.array([t.utcoffset().total_seconds() for t in loc[:: max(1, len(loc) // 20000)]]) \
        if len(loc) else np.array([])
    n_offsets = len(np.unique(offs))
    add(Check("dst_transitions", "INFO" if n_offsets > 1 else "PASS", max(n_offsets - 1, 0),
              f"{calendar.timezone} UTC offsets present: {sorted(float(x) for x in set(offs / 3600))}; "
              "sessions computed in local time"))

    # --- contract roll ------------------------------------------------------------
    if "contract" in bars.columns:
        chg = np.flatnonzero(bars["contract"].to_numpy()[1:] != bars["contract"].to_numpy()[:-1]) + 1
        jumps = np.abs(o[chg] - c[chg - 1])
        big = jumps > 5 * med if med > 0 else np.zeros(len(chg), bool)
        add(Check("contract_rolls", "WARN" if big.any() else "INFO", len(chg),
                  f"{len(chg)} roll(s); {int(big.sum())} with open-vs-prior-close jump > 5x "
                  "median range (unadjusted roll gap would create fake P&L)", _fmt(ts[chg])))
    else:
        add(Check("contract_rolls", "INFO", 0,
                  "no 'contract' column: roll behaviour unverifiable from data; see manifest.adjustment"))
    return rep


def clean_bars(df: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """Sort and drop exact duplicates only. Returns (clean_df, change_log)."""
    bars = canonicalize(df)
    log = []
    if (np.diff(_ts_ns(bars["ts"])) < 0).any():
        bars = bars.sort_values("ts", kind="mergesort").reset_index(drop=True)
        log.append("sorted by timestamp")
    before = len(bars)
    bars = bars.drop_duplicates().reset_index(drop=True)
    if len(bars) != before:
        log.append(f"dropped {before - len(bars)} exact duplicate row(s)")
    return bars, log


_TOKEN = object()


@dataclass(frozen=True)
class ValidatedDataset:
    """The only data object the backtester accepts. Construct via validate_and_freeze()."""
    bars: BarArrays
    instrument: Instrument
    calendar: SessionCalendar
    report: DataQualityReport
    manifest: DatasetManifest
    _token: Any = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._token is not _TOKEN:
            raise TypeError("ValidatedDataset must be created with validate_and_freeze()")

    def verify_unchanged(self) -> None:
        if self.bars.content_hash() != self.manifest.content_hash:
            raise RuntimeError("bar data changed after validation (hash mismatch)")


def validate_and_freeze(df: pd.DataFrame, instrument: Instrument, calendar: SessionCalendar,
                        timeframe: str, tf_minutes: int, provider: str, dataset_id: str,
                        thresholds: Mapping[str, Any] | None = None,
                        source_detail: dict | None = None, contract: str = "unspecified",
                        adjustment: str = "unspecified", **manifest_fields) -> ValidatedDataset:
    """Validate raw data, apply the (non-fabricating) cleaning policy, re-validate, freeze.

    Raises DataIntegrityError if the raw data has unfixable problems (conflicting
    duplicates, bad OHLC, ...) or if the cleaned data still fails.
    ``manifest_fields`` sets Phase 2 provenance fields (asset_type, volume_type, ...).
    """
    raw = validate_bars(df, instrument, calendar, tf_minutes, thresholds)
    unfixable = [c for c in raw.checks
                 if c.status == "FAIL" and c.name not in ("timestamp_order",)]
    if unfixable:
        raise DataIntegrityError(raw)
    cleaned, change_log = clean_bars(df)
    report = validate_bars(cleaned, instrument, calendar, tf_minutes, thresholds)
    if report.status == "FAIL":
        raise DataIntegrityError(report)
    bars = BarArrays.from_frame(cleaned, tf_minutes)
    detail = dict(source_detail or {})
    detail["cleaning"] = change_log
    detail["raw_duplicate_bars"] = raw.duplicate_bars
    manifest = DatasetManifest(
        dataset_id=dataset_id, provider=provider, instrument=instrument.symbol,
        timeframe=timeframe, timezone=calendar.timezone, timestamp_convention="bar_open_utc",
        start=report.start, end=report.end, n_bars=len(bars), content_hash=bars.content_hash(),
        source_detail=detail, contract=contract, adjustment=adjustment,
        missing_bars=report.missing_bars, duplicate_bars=raw.duplicate_bars,
        quality_status=report.status, calendar=calendar.name,
        calendar_fingerprint=calendar.fingerprint(),
        has_spread=bars.spread is not None, has_ask_ohlc=bars.has_ask_ohlc, **manifest_fields)
    if manifest.volume_type == "unknown" and not bars.has_volume:
        manifest.volume_type = "none"
    return ValidatedDataset(bars, instrument, calendar, report, manifest, _token=_TOKEN)
