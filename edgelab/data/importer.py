"""Dataset import pipeline (futures, CFD, or any OHLC source).

    raw provider file -> inspect -> normalize timestamps -> validate -> manifest
    -> content hash -> store immutable dataset -> derive timeframes -> build features

Nothing here is vendor-specific: file layouts are described by options (or presets in
configs/import_profiles.yaml). Rules inherited from Phase 1 are enforced, not repeated:
tz-naive timestamps are never guessed (source_timezone is REQUIRED), close-stamped bars
are shifted to bar-open, validation is mandatory, missing bars are never created.

Broker-server clocks (MT4/MT5) are not IANA zones: they typically run at New York time
plus 7 hours so that midnight = 17:00 NY. Express this as ``America/New_York+7h``:
local = server - 7h, interpreted in New York (DST-correct).

Dataset ids are content-addressed: <NAME>_<TF>_<first 10 hex of content hash>, e.g.
NAS100_CFD_DUKASCOPY_1M_3FA9C1D2E4. Re-importing identical data is idempotent; a
different file under the same name gets a different id - datasets are never overwritten
or merged.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from edgelab.core.logging import get_logger
from edgelab.data.calendar import load_calendars
from edgelab.data.exclusions import ExclusionError, apply_exclusions, exclusion_set_from_config
from edgelab.data.resample import resample_bars
from edgelab.data.schema import (ASSET_TYPES, PRICE_BASES, VOLUME_TYPES, BarArrays,
                                 DataRequiredError, timeframe_minutes)
from edgelab.data.store import ResultStore
from edgelab.data.validation import (DataIntegrityError, ValidatedDataset, clean_bars,
                                     validate_and_freeze)
from edgelab.instruments import load_instruments

log = get_logger("importer")
IMPORT_VERSION = "edgelab-import/1"
_OFFSET_RE = re.compile(r"^(?P<tz>.+?)(?P<sign>[+-])(?P<h>\d{1,2})(?::(?P<m>\d{2}))?h$")


class ImportFailed(RuntimeError):
    def __init__(self, stage: str, message: str, report: Any = None):
        self.stage, self.report = stage, report
        super().__init__(f"[{stage}] {message}")


@dataclass
class ImportOptions:
    file: str
    instrument: str                       # key in configs/instruments.yaml, e.g. NAS100_CFD
    provider: str                         # e.g. DUKASCOPY, MYBROKER, CME_VIA_VENDOR
    asset_type: str                       # CFD | FUTURE | ...
    timeframe: str                        # bar size of the file, e.g. 1m
    source_timezone: str | None = None    # REQUIRED: IANA zone, or IANA+Nh for broker-server time
    timestamp_convention: str = "open"    # open | close (as stamped in the file)
    symbol: str = ""                      # provider's symbol, e.g. USA100IDXUSD
    dataset_name: str | None = None       # default <INSTRUMENT>_<PROVIDER> e.g. NAS100_CFD_DUKASCOPY
    calendar: str | None = None           # default: the instrument's calendar
    price_basis: str = "unknown"          # bid | ask | mid | last | unknown
    volume_type: str | None = None        # exchange | tick | none (default: none if no volume column)
    delimiter: str = ","
    columns: dict = field(default_factory=dict)   # canonical -> file column: ts, open, high, low, close, volume
    date_column: str | None = None        # separate date + time columns (MT5)
    time_column: str | None = None
    datetime_format: str | None = None
    epoch_unit: str | None = None         # s | ms | us | ns for numeric timestamps
    spread_column: str | None = None
    spread_multiplier: float | None = None  # file spread units -> price points (default 1)
    bid_close_column: str | None = None   # alternative: spread = ask_close - bid_close
    ask_close_column: str | None = None
    notes: str = ""
    contract: str = "unspecified"
    adjustment: str = "n/a"
    derive_timeframes: list = field(default_factory=list)
    build_features: bool = True
    profile: str | None = None
    source_exclusions: str | None = None  # name of an audited set in configs/data.yaml (ADR-44); opt-in

    def to_dict(self) -> dict:
        return asdict(self)


def apply_profile(opts: ImportOptions, cfg: Mapping) -> ImportOptions:
    """Fill layout options from a named preset; explicit options win. REQUIRED placeholders
    must be supplied by the user."""
    if not opts.profile:
        return opts
    prof = (cfg.get("import_profiles") or {}).get(opts.profile)
    if prof is None:
        raise ImportFailed("options", f"unknown import profile {opts.profile!r}")
    d = opts.to_dict()
    defaults = ImportOptions(file="", instrument="", provider="", asset_type="", timeframe="").to_dict()
    for k, v in prof.items():
        if k == "columns":
            d["columns"] = {**v, **(opts.columns or {})}
        elif d.get(k) == defaults.get(k):
            d[k] = v
    return ImportOptions(**d)


def _check_options(o: ImportOptions) -> None:
    for k, v in o.to_dict().items():
        if v == "REQUIRED":
            raise ImportFailed("options", f"'{k}' must be set explicitly for profile {o.profile!r}")
    if not o.source_timezone:
        raise ImportFailed("options", "source_timezone is required: timestamps are never guessed "
                                      "(e.g. UTC, Europe/London, America/New_York+7h)")
    if o.asset_type not in ASSET_TYPES:
        raise ImportFailed("options", f"asset_type must be one of {ASSET_TYPES}")
    if o.price_basis not in PRICE_BASES:
        raise ImportFailed("options", f"price_basis must be one of {PRICE_BASES}")
    if o.volume_type is not None and o.volume_type not in VOLUME_TYPES:
        raise ImportFailed("options", f"volume_type must be one of {VOLUME_TYPES}")
    if o.timestamp_convention not in ("open", "close"):
        raise ImportFailed("options", "timestamp_convention must be open|close")


def parse_source_timezone(spec: str) -> tuple[str, pd.Timedelta]:
    """'UTC' -> ('UTC', 0); 'America/New_York+7h' -> ('America/New_York', +7h)."""
    m = _OFFSET_RE.match(spec)
    if m:
        off = pd.Timedelta(hours=int(m["h"]), minutes=int(m["m"] or 0))
        return m["tz"], off if m["sign"] == "+" else -off
    return spec, pd.Timedelta(0)


def file_sha256(path: str | Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def read_raw(o: ImportOptions) -> pd.DataFrame:
    p = Path(o.file)
    if not p.exists():
        raise DataRequiredError(o.provider, o.instrument, "?", o.timeframe, note=f"file not found: {p}")
    return pd.read_csv(p, sep=o.delimiter, dtype=str, keep_default_na=False, na_values=[""])


def _col(raw: pd.DataFrame, name: str | None, what: str) -> pd.Series:
    if name is None or name not in raw.columns:
        raise ImportFailed("normalize", f"{what} column {name!r} not found; file has {list(raw.columns)}")
    return raw[name]


_EXPLICIT_OFFSET = re.compile(r"\d{1,2}:\d{2}(?::\d{2}(?:\.\d+)?)?\s*(?:Z|[+-]\d{2}(?::?\d{2})?)$", re.IGNORECASE)


def parse_timestamps(raw: pd.DataFrame, o: ImportOptions) -> pd.DatetimeIndex:
    if o.date_column or o.time_column:
        s = _col(raw, o.date_column, "date").str.strip() + " " + _col(raw, o.time_column, "time").str.strip()
    else:
        s = _col(raw, o.columns.get("ts", "ts"), "timestamp")
    if o.epoch_unit:
        return pd.DatetimeIndex(pd.to_datetime(pd.to_numeric(s), unit=o.epoch_unit, utc=True))
    # Values that carry their own UTC offset ("Z", "+02:00", "-0400") are absolute instants and may
    # differ in offset (e.g. a feed that switches from Z to local offsets across DST); they convert
    # to UTC exactly. utc=True is used ONLY when every present value is explicit - on naive values
    # it would silently assume UTC. A mix of explicit and naive values is refused, never guessed.
    present = s.notna() & (s.str.strip() != "")
    explicit = s[present].str.strip().str.contains(_EXPLICIT_OFFSET, na=False)
    if explicit.any() and not explicit.all():
        raise ImportFailed("normalize", f"{int((~explicit).sum())} of {len(explicit)} timestamps have no UTC offset "
                           "while the others do; refusing to guess their timezone")
    all_explicit = bool(len(explicit)) and bool(explicit.all())
    try:
        ts = pd.to_datetime(s, format=o.datetime_format, utc=all_explicit)
    except (ValueError, TypeError) as exc:
        raise ImportFailed("normalize", f"cannot parse timestamps ({exc}); set datetime_format") from exc
    ts = pd.DatetimeIndex(ts)
    iana, offset = parse_source_timezone(o.source_timezone)
    if ts.tz is not None:
        if offset != pd.Timedelta(0):
            raise ImportFailed("normalize", "timestamps carry their own UTC offset; do not add +Nh")
        return ts.tz_convert("UTC")
    try:
        local = (ts - offset).tz_localize(iana, ambiguous="raise", nonexistent="raise")
    except Exception as exc:          # ambiguous/nonexistent local times, bad zone
        raise ImportFailed("normalize", f"cannot localize timestamps in {o.source_timezone!r}: {exc}") from exc
    return local.tz_convert("UTC")


def normalize(raw: pd.DataFrame, o: ImportOptions) -> tuple[pd.DataFrame, dict]:
    """Raw provider frame -> canonical frame (ts = bar OPEN, UTC, ns) + facts about it."""
    tf = timeframe_minutes(o.timeframe)
    ts = parse_timestamps(raw, o)
    if o.timestamp_convention == "close":
        ts = ts - pd.Timedelta(minutes=tf)
    cols = {c: o.columns.get(c, c) for c in ("open", "high", "low", "close")}
    df = pd.DataFrame({"ts": ts.as_unit("ns")})
    for c, src in cols.items():
        df[c] = pd.to_numeric(_col(raw, src, c).str.replace(",", ""), errors="coerce").to_numpy()
    vol_src = o.columns.get("volume", "volume")
    has_vol_col = vol_src in raw.columns
    df["volume"] = pd.to_numeric(raw[vol_src], errors="coerce").to_numpy() if has_vol_col else np.nan
    volume_type = o.volume_type or ("unknown" if has_vol_col else "none")
    if volume_type == "none":
        df["volume"] = np.nan                       # declared unusable: never treated as zero
    spread_source = "none"
    if o.spread_column:
        mult = 1.0 if o.spread_multiplier is None else o.spread_multiplier
        if not float(mult) > 0:
            raise ImportFailed("options", "spread_multiplier must be > 0 (file spread units -> points)")
        df["spread"] = pd.to_numeric(_col(raw, o.spread_column, "spread"), errors="coerce").to_numpy() * float(mult)
        spread_source = "column"
    elif o.bid_close_column and o.ask_close_column:
        bid = pd.to_numeric(_col(raw, o.bid_close_column, "bid close"), errors="coerce").to_numpy()
        ask = pd.to_numeric(_col(raw, o.ask_close_column, "ask close"), errors="coerce").to_numpy()
        df["spread"] = ask - bid
        spread_source = "bid_ask_close"
    facts = {"volume_type": volume_type, "spread_source": spread_source,
             "has_bid_ask": bool(o.bid_close_column and o.ask_close_column),
             "raw_rows": int(len(raw)), "raw_columns": list(raw.columns)}
    return df, facts


def price_decimals(x: np.ndarray, max_dec: int = 6) -> int:
    x = x[np.isfinite(x)][:10000]
    for d in range(max_dec + 1):
        if np.allclose(np.round(x, d), x, atol=10 ** -(max_dec + 2)):
            return d
    return max_dec


def session_profile(ts_utc: pd.DatetimeIndex, tz: str) -> pd.DataFrame:
    """Bar counts by local weekday x hour: shows when the feed actually has data (use it to
    choose / verify the calendar)."""
    loc = ts_utc.tz_convert(tz)
    t = pd.crosstab(loc.dayofweek, loc.hour).reindex(index=range(7), columns=range(24), fill_value=0)
    t.index = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return t


def inspect_file(o: ImportOptions, cfg: Mapping | None = None, view_tz: str = "America/New_York") -> dict:
    """Dry run: what is in this file and how would it be interpreted? Never stores anything."""
    if cfg is not None:
        o = apply_profile(o, cfg)
    raw = read_raw(o)
    out: dict[str, Any] = {"file": str(o.file), "bytes": Path(o.file).stat().st_size,
                           "file_sha256": file_sha256(o.file), "rows": int(len(raw)),
                           "columns": list(raw.columns), "head": raw.head(5).to_dict("records")}
    try:
        _check_options(o)
        df, facts = normalize(raw, o)
    except (ImportFailed, DataRequiredError) as exc:
        out["normalize_error"] = str(exc)
        return out
    ts = pd.DatetimeIndex(df["ts"])
    diffs = np.diff(ts.asi8) / 60e9 if len(ts) > 1 else np.array([])
    pos = diffs[diffs > 0]
    out.update({
        "facts": facts,
        "first_bar_open_utc": str(ts.min()), "last_bar_open_utc": str(ts.max()),
        "monotonic": bool((diffs > 0).all()) if len(diffs) else True,
        "duplicate_timestamps": int(ts.duplicated().sum()),
        "inferred_bar_minutes": float(pd.Series(pos).mode().iloc[0]) if len(pos) else None,
        "declared_bar_minutes": timeframe_minutes(o.timeframe),
        "price_decimals": price_decimals(df["close"].to_numpy(float)),
        "volume_present": bool(np.isfinite(df["volume"]).any()),
        "spread_present": "spread" in df.columns,
        "session_profile_tz": view_tz,
        "session_profile": session_profile(ts, view_tz).to_dict(),
    })
    if out["inferred_bar_minutes"] and out["inferred_bar_minutes"] != out["declared_bar_minutes"]:
        out["warning"] = (f"declared timeframe {o.timeframe} but most common spacing is "
                          f"{out['inferred_bar_minutes']:g} minutes")
    return out


@dataclass
class ImportResult:
    dataset_id: str
    created: bool
    manifest: dict
    report_status: str
    stages: list = field(default_factory=list)
    derived: list = field(default_factory=list)
    features: dict = field(default_factory=dict)
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _dataset_id(name: str, tf: str, content_hash: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", f"{name}_{tf}").strip("_").upper()
    return f"{safe}_{content_hash[:10].upper()}"


def import_dataset(o: ImportOptions, cfg: Mapping, store: ResultStore, cache=None,
                   reports_dir: str | Path | None = None) -> ImportResult:
    """Run the full pipeline. Raises ImportFailed (with the validation report) on failure."""
    stages: list = []
    warnings: list = []

    def stage(name, fn):
        t0 = time.perf_counter()
        try:
            r = fn()
        except ImportFailed:
            stages.append({"stage": name, "status": "FAILED", "seconds": round(time.perf_counter() - t0, 3)})
            raise
        stages.append({"stage": name, "status": "ok", "seconds": round(time.perf_counter() - t0, 3)})
        return r

    o = apply_profile(o, cfg)
    stage("options", lambda: _check_options(o))
    instruments = load_instruments(cfg)
    if o.instrument not in instruments:
        raise ImportFailed("options", f"unknown instrument {o.instrument!r}; add it to configs/instruments.yaml")
    inst = instruments[o.instrument]
    cal_name = o.calendar or inst.calendar
    calendars = load_calendars(cfg)
    if cal_name not in calendars:
        raise ImportFailed("options", f"unknown calendar {cal_name!r}")
    cal = calendars[cal_name]
    tf = timeframe_minutes(o.timeframe)
    tf_label = f"{tf}m"
    # default family name: NAS100_CFD_<PROVIDER>, NQ_FUTURE_<PROVIDER>
    asset_tag = "" if o.asset_type.upper() in o.instrument.upper() else f"_{o.asset_type.upper()}"
    name = o.dataset_name or f"{o.instrument}{asset_tag}_{o.provider}"

    src_hash = stage("hash_source", lambda: file_sha256(o.file))
    raw = stage("read", lambda: read_raw(o))
    df, facts = stage("normalize", lambda: normalize(raw, o))
    exclusion = None
    if o.source_exclusions:
        def _exclude():
            try:
                return apply_exclusions(df, cal, o.source_exclusions,
                                        exclusion_set_from_config(cfg, o.source_exclusions))
            except ExclusionError as exc:
                raise ImportFailed("exclude", f"{exc} - nothing was stored") from exc
        df, exclusion = stage("exclude", _exclude)
    cleaned, _ = clean_bars(df)
    content_hash = BarArrays.from_frame(cleaned, tf).content_hash()
    dataset_id = _dataset_id(name, tf_label, content_hash)
    provenance = dict(
        dataset_name=name, asset_type=o.asset_type, symbol=o.symbol or o.instrument,
        source_timezone=o.source_timezone, source_timestamp_convention=o.timestamp_convention,
        source_file_sha256=src_hash, volume_type=facts["volume_type"], price_basis=o.price_basis,
        has_bid_ask=facts["has_bid_ask"], spread_source=facts["spread_source"],
        provider_notes=o.notes, import_version=IMPORT_VERSION)
    if exclusion:
        provenance["derivation"] = (f"source exclusions {exclusion['set']} ({exclusion['rows_excluded']} of "
                                    f"{exclusion['rows_before']} source rows removed; set {exclusion['set_hash'][:12]})")
    thresholds = cfg.get("validation")

    def _validate():
        try:
            return validate_and_freeze(df, inst, cal, o.timeframe, tf, o.provider, dataset_id, thresholds,
                                       source_detail={"file": str(o.file), "options": o.to_dict(),
                                                      "raw_rows": facts["raw_rows"],
                                                      **({"source_exclusions": exclusion} if exclusion else {})},
                                       contract=o.contract, adjustment=o.adjustment, **provenance)
        except DataIntegrityError as exc:
            _write_report(reports_dir, f"FAILED_{dataset_id}", exc.report)
            raise ImportFailed("validate", "validation FAILED - nothing was stored. See report.",
                               exc.report) from exc
    ds = stage("validate", _validate)
    if ds.manifest.content_hash != content_hash:
        raise ImportFailed("validate", "internal: content hash changed during validation")
    for other in store.find_by_content_hash(content_hash):
        if other != dataset_id:
            warnings.append(f"identical bar content already stored as {other} (kept separate)")
    created = stage("store", lambda: store.save_dataset(ds.manifest, ds.bars, ds.report))
    if not created:
        warnings.append("dataset already imported (idempotent re-import); existing copy kept")
    _write_report(reports_dir, dataset_id, ds.report, extra={"manifest": ds.manifest.to_dict(),
                                                              "manifest_hash": ds.manifest.manifest_hash()})
    datasets = [ds]
    derived_ids = []
    for dtf in o.derive_timeframes or []:
        dm = timeframe_minutes(dtf)
        if dm <= tf or dm % tf:
            raise ImportFailed("derive", f"{dtf} is not a multiple of {o.timeframe}")

        def _derive(dm=dm):
            rdf = resample_bars(cleaned, cal, dm).drop(columns=["n_subbars"])
            h = BarArrays.from_frame(rdf, dm).content_hash()
            did = _dataset_id(name, f"{dm}m", h)
            dds = validate_and_freeze(rdf, inst, cal, f"{dm}m", dm, o.provider, did, thresholds,
                                      source_detail={"derived_from": dataset_id,
                                                     **({"source_exclusions": exclusion} if exclusion else {})},
                                      contract=o.contract,
                                      adjustment=o.adjustment,
                                      **{**provenance, "parent_dataset_id": dataset_id,
                                         "derivation": f"session-anchored resample {o.timeframe}->{dm}m"})
            store.save_dataset(dds.manifest, dds.bars, dds.report)
            return dds
        dds = stage(f"derive_{dm}m", _derive)
        datasets.append(dds)
        derived_ids.append(dds.manifest.dataset_id)
    features = {}
    if o.build_features and cache is not None:
        from edgelab.features.engine import FeatureEngine
        from edgelab.features.sessions import load_sessions
        from edgelab.features.spec import FeatureSpec
        sessions = load_sessions(cfg)
        specs = [FeatureSpec.from_dict(d) for d in (cfg.get("features") or {}).get("default_set", [])]

        def _features():
            for d in datasets:
                eng = FeatureEngine.for_dataset(d, sessions, cache)
                built, skipped = [], {}
                for s in specs:
                    ok, why = eng.availability(s)
                    if ok:
                        eng.compute(s)
                        built.append(s.label)
                    else:
                        skipped[s.label] = why
                features[d.manifest.dataset_id] = {"built": built, "skipped": skipped}
        stage("features", _features)
    log.event("dataset_imported", dataset=dataset_id, created=created, bars=len(ds.bars))
    return ImportResult(dataset_id, bool(created), ds.manifest.to_dict(), ds.report.status, stages,
                        derived_ids, features, warnings)


def _write_report(reports_dir, name, report, extra=None) -> None:
    if not reports_dir:
        return
    d = Path(reports_dir)
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{name}_quality.txt").write_text(report.to_text())
    (d / f"{name}.json").write_text(json.dumps({"report": report.to_dict(), **(extra or {})},
                                               default=str, indent=1))


def load_validated(store: ResultStore, cfg: Mapping, dataset_id: str,
                   allow_calendar_change: bool = False) -> ValidatedDataset:
    """Stored dataset -> ValidatedDataset. Re-runs the validation gate (data is never trusted
    blindly) and verifies the content hash and calendar definition against the manifest."""
    m = store.get_manifest(dataset_id)
    tf = timeframe_minutes(m.timeframe)
    _, bars = store.load_dataset(dataset_id, tf)
    inst = load_instruments(cfg)[m.instrument]
    cal = load_calendars(cfg)[m.calendar or inst.calendar]
    if m.calendar_fingerprint and cal.fingerprint() != m.calendar_fingerprint and not allow_calendar_change:
        raise ImportFailed("load", f"calendar {cal.name!r} changed since import of {dataset_id}; "
                                   "re-import or pass allow_calendar_change=True")
    keep = {k: getattr(m, k) for k in ("dataset_name", "asset_type", "symbol", "source_timezone",
                                       "source_timestamp_convention", "source_file_sha256", "volume_type",
                                       "price_basis", "has_bid_ask", "spread_source", "provider_notes",
                                       "import_version", "parent_dataset_id", "derivation")}
    ds = validate_and_freeze(bars.to_frame(), inst, cal, m.timeframe, tf, m.provider, dataset_id,
                             cfg.get("validation"), source_detail=m.source_detail, contract=m.contract,
                             adjustment=m.adjustment, **keep)
    if ds.manifest.content_hash != m.content_hash:
        raise ImportFailed("load", f"{dataset_id}: content hash mismatch after reload")
    ds.manifest.imported_at = m.imported_at
    # The stored bars were cleaned at import, so re-validating them finds nothing to sort or drop.
    # Keep the import-time facts about the RAW source instead of overwriting them with empty/zero
    # values; otherwise the reloaded manifest (and its hash) differs from the stored one (ADR-45).
    ds.manifest.source_detail = dict(m.source_detail)
    ds.manifest.duplicate_bars = m.duplicate_bars
    return ds
