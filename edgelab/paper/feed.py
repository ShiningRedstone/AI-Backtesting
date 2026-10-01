"""The paper-trading data feed (ADR-81): completed Dukascopy trading days, downloaded with ``dukascopy-python``.

The same source as the user's research files (``fetch(INSTRUMENT_IDX_AMERICA_E_NQ_100, INTERVAL_MIN_1, OFFER_SIDE_BID /
OFFER_SIDE_ASK, START, END)``, see DATA_IMPORT.md "Source identity"). One file per trading date under
``<data>/paper/feed/days/YYYY-MM-DD.csv``: the BID rows plus ``ask_open/ask_high/ask_low/ask_close`` of the ASK row
with the identical timestamp (the combine rules of scripts/dukascopy_bid_ask_check.py: nothing filled, a one-sided
timestamp inside the day refuses the day, a negative spread refuses). A trading date (DUKASCOPY_USATECH_OBSERVED,
New York 18:00 of the previous day -> 16:15) is downloaded only once it is complete (close + SAFETY). Day files are
never overwritten; each is recorded with its SHA-256 in ``manifest.json``.

``build_feed`` turns a date range of day files into validated datasets with the PRODUCTION import pipeline (the same
profile, instrument, provider, ASK-OHLC options and derived timeframes as the research import) into a throwaway
in-memory store: the feed is never a research dataset, never governed by a research protocol, never a trial.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Mapping

import pandas as pd

from edgelab.core.fsutil import atomic_write_text, replace_with_retry

INSTRUMENT_CODE = "E_NQ-100"                    # dukascopy_python.INSTRUMENT_IDX_AMERICA_E_NQ_100
CALENDAR = "DUKASCOPY_USATECH_OBSERVED"
INSTRUMENT = "NQ_DUKASCOPY"
PROVIDER = "DUKASCOPY"
SAFETY = timedelta(minutes=45)                  # after the 16:15 New York close before a date counts as complete
WARMUP_TRADING_DAYS = 40                         # indicator history before the earliest paper start
FEED_DATASET_NAME = "NQ_DUKASCOPY_PAPER_FEED"
DERIVED = ["5m", "15m", "30m", "60m"]
ASK_COLS = ("ask_open", "ask_high", "ask_low", "ask_close")
CSV_COLS = ("timestamp", "open", "high", "low", "close", "volume") + ASK_COLS


class FeedError(RuntimeError):
    """A day could not be accepted (inconsistent BID/ASK, negative spread, download failure). Nothing is guessed."""


def feed_dir(data_root: Path | str) -> Path:
    return Path(data_root) / "paper" / "feed"


def _calendar(cfg: Mapping):
    from edgelab.data.calendar import load_calendars
    return load_calendars(cfg)[CALENDAR]


def trading_dates(cfg: Mapping, start: date, end: date) -> list[date]:
    """Scheduled trading dates in [start, end] (calendar weekdays minus listed holidays)."""
    cal = _calendar(cfg)
    out, d = [], start
    while d <= end:
        if d.weekday() in cal.trading_weekdays and d not in cal.holidays:
            out.append(d)
        d += timedelta(days=1)
    return out


def last_completed_date(cfg: Mapping, now: datetime | None = None) -> date:
    """The newest trading date whose session has closed (plus SAFETY) at ``now`` (UTC)."""
    cal = _calendar(cfg)
    now = now or datetime.now(timezone.utc)
    d = pd.Timestamp(now).tz_convert(cal.timezone).date()
    for _ in range(10):
        if d.weekday() in cal.trading_weekdays and d not in cal.holidays:
            _, close = cal.session_bounds(d)
            if pd.Timestamp(now) >= (close + SAFETY).tz_convert("UTC"):
                return d
        d -= timedelta(days=1)
    return d


def first_unstarted_date(cfg: Mapping, now: datetime | None = None) -> date:
    """The first trading date whose session has NOT started yet at ``now``: a paper account starting "next trading
    day" never sees a bar that existed when it was created."""
    cal = _calendar(cfg)
    now = pd.Timestamp(now or datetime.now(timezone.utc))
    d = now.tz_convert(cal.timezone).date()
    for _ in range(14):
        if d.weekday() in cal.trading_weekdays and d not in cal.holidays:
            o, _ = cal.session_bounds(d)
            if o.tz_convert("UTC") > now:
                return d
        d += timedelta(days=1)
    raise ValueError("no upcoming trading date within two weeks")


def session_open_utc(cfg: Mapping, d: date) -> pd.Timestamp:
    return _calendar(cfg).session_bounds(d)[0].tz_convert("UTC")


def next_trading_date(cfg: Mapping, after: date) -> date:
    return trading_dates(cfg, after + timedelta(days=1), after + timedelta(days=10))[0]


def shift_trading_days(cfg: Mapping, d: date, n: int) -> date:
    """The trading date n scheduled trading days before ``d`` (n >= 0)."""
    dates = trading_dates(cfg, d - timedelta(days=int(n * 1.6) + 10), d)
    return dates[max(0, len(dates) - 1 - n)]


# ------------------------------------------------------------------------------------------- download
def dukascopy_fetcher(side: str, start: datetime, end: datetime) -> pd.DataFrame:
    """1-minute bars of one side ("BID"/"ASK") via dukascopy-python, indexed by UTC bar-open time."""
    import dukascopy_python as dp
    return dp.fetch(INSTRUMENT_CODE, dp.INTERVAL_MIN_1, dp.OFFER_SIDE_BID if side == "BID" else dp.OFFER_SIDE_ASK,
                    start, end)


def _window(cfg: Mapping, d: date) -> tuple[datetime, datetime]:
    o, c = _calendar(cfg).session_bounds(d)
    return o.tz_convert("UTC").to_pydatetime(), c.tz_convert("UTC").to_pydatetime()


def combine_day(bid: pd.DataFrame, ask: pd.DataFrame, start: datetime, end: datetime) -> pd.DataFrame | None:
    """BID rows + the ASK OHLC of the identical timestamp, for bars opening in [start, end). None = no data that day.
    Refuses (FeedError) on a one-sided timestamp inside the overlap or a negative spread; never fills anything."""
    def clip(df):
        if df is None or not len(df):
            return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
        idx = pd.DatetimeIndex(df.index)
        idx = idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")
        df = df.copy()
        df.index = idx
        return df[(df.index >= pd.Timestamp(start)) & (df.index < pd.Timestamp(end))].sort_index()
    b, a = clip(bid), clip(ask)
    if not len(b) and not len(a):
        return None
    if not len(b) or not len(a):
        raise FeedError(f"only one side has data ({len(b)} BID, {len(a)} ASK rows)")
    if b.index.has_duplicates or a.index.has_duplicates:
        raise FeedError("duplicate timestamps in the download")
    common = b.index.intersection(a.index)
    if not len(common):
        raise FeedError("BID and ASK share no timestamps")
    lo, hi = common.min(), common.max()
    inner_one_sided = [t for t in b.index.symmetric_difference(a.index) if lo <= t <= hi]
    if inner_one_sided:
        raise FeedError(f"{len(inner_one_sided)} timestamp(s) present on only one side inside the day "
                        f"(first {inner_one_sided[0].isoformat()}); nothing is filled")
    b, a = b.loc[common], a.loc[common]
    out = pd.DataFrame({"timestamp": [t.isoformat() for t in common],
                        "open": b["open"].to_numpy(), "high": b["high"].to_numpy(), "low": b["low"].to_numpy(),
                        "close": b["close"].to_numpy(), "volume": b["volume"].to_numpy(),
                        "ask_open": a["open"].to_numpy(), "ask_high": a["high"].to_numpy(),
                        "ask_low": a["low"].to_numpy(), "ask_close": a["close"].to_numpy()})
    if (out["ask_close"] < out["close"]).any() or (out["ask_low"] < out["low"]).any():
        raise FeedError("negative spread (ASK below BID) in the download")
    return out


def _sha(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _read_manifest(fd: Path) -> dict:
    try:
        return json.loads((fd / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"days": {}, "skipped": {}, "errors": {}}


def _write_manifest(fd: Path, m: dict) -> None:
    fd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(fd / "manifest.json", json.dumps(m, indent=1, sort_keys=True))   # the page reads it (ADR-80)


def update(cfg: Mapping, data_root: Path | str, start: date, fetcher: Callable | None = None,
           now: datetime | None = None, max_days: int = 400) -> dict:
    """Download every missing completed trading date from ``start`` on, in date order (never overwrites a stored day).
    Stops at the first date that fails or is refused, so the stored days never have a hole after it. Returns a
    summary: downloaded / skipped (no data, e.g. a holiday) / errors per date and the newest stored date."""
    fetcher = fetcher or dukascopy_fetcher
    fd = feed_dir(data_root)
    days = fd / "days"
    days.mkdir(parents=True, exist_ok=True)
    m = _read_manifest(fd)
    last = last_completed_date(cfg, now)
    got, skipped, errors = [], [], {}
    for d in trading_dates(cfg, start, last)[-max_days:]:
        key = d.isoformat()
        p = days / f"{key}.csv"
        if p.exists() or key in m.get("skipped", {}):
            continue
        s, e = _window(cfg, d)
        try:
            frame = combine_day(fetcher("BID", s, e), fetcher("ASK", s, e), s, e)
        except FeedError as exc:                              # refused data: paper trading pauses here, visibly
            errors[key] = str(exc)
            break
        except Exception as exc:                              # network or package failure: retried next time
            errors[key] = f"download failed: {type(exc).__name__}: {exc}"
            break                                             # never a hole: later days wait for this one
        if frame is None:
            if d < last:                                      # a closed date with no bars (e.g. an exchange holiday)
                m.setdefault("skipped", {})[key] = "no bars from the source"
                skipped.append(key)
            continue
        tmp = days / f"{key}.{os.getpid()}.tmp"
        frame.to_csv(tmp, index=False, columns=list(CSV_COLS))
        replace_with_retry(tmp, p)
        m.setdefault("days", {})[key] = {"rows": int(len(frame)), "sha256": _sha(p),
                                         "fetched_at": datetime.now(timezone.utc).isoformat(),
                                         "first_bar": frame["timestamp"].iloc[0], "last_bar": frame["timestamp"].iloc[-1]}
        got.append(key)
    m["errors"] = {**{k: v for k, v in m.get("errors", {}).items() if k not in got}, **errors}
    m["checked_at"] = datetime.now(timezone.utc).isoformat()
    m["last_completed_date"] = last.isoformat()
    _write_manifest(fd, m)
    stored = sorted(m.get("days", {}))
    return {"downloaded": got, "skipped": skipped, "errors": errors, "last_completed_date": last.isoformat(),
            "newest_day": stored[-1] if stored else None, "n_days": len(stored)}


def status(data_root: Path | str) -> dict:
    m = _read_manifest(feed_dir(data_root))
    stored = sorted(m.get("days", {}))
    return {"n_days": len(stored), "first_day": stored[0] if stored else None, "newest_day": stored[-1] if stored else None,
            "checked_at": m.get("checked_at"), "last_completed_date": m.get("last_completed_date"),
            "errors": m.get("errors", {}), "skipped": m.get("skipped", {}), "source": "Dukascopy USATECH.IDX/USD "
            "(feed E_NQ-100), 1-minute BID + ASK, via dukascopy-python", "downloader_available": downloader_available()}


def downloader_available() -> bool:
    """True when dukascopy-python can be imported (bundled in the desktop app; listed in requirements.txt)."""
    try:
        import dukascopy_python  # noqa: F401
        return True
    except Exception:
        return False


# ------------------------------------------------------------------------------------------- build
@dataclass
class Feed:
    datasets: dict                 # timeframe label ("1m", "5m", ...) -> ValidatedDataset
    first_day: str
    last_day: str
    days: list
    content_hash: str              # of the 1m feed bars


def build_feed(cfg: Mapping, data_root: Path | str, start: date, end: date | None = None) -> Feed | None:
    """Validated feed datasets for the stored days in [start, end] through the production import pipeline (in a
    throwaway in-memory store). None when no day is stored. Validation FAIL raises ImportFailed (nothing guessed)."""
    from edgelab.data.importer import ImportOptions, import_dataset, load_validated
    from edgelab.data.store import SQLiteStore
    fd = feed_dir(data_root)
    m = _read_manifest(fd)
    keys = sorted(k for k in m.get("days", {}) if k >= start.isoformat() and (end is None or k <= end.isoformat()))
    if not keys:
        return None
    for k in keys:                                            # a day file changed after download: refuse
        if _sha(fd / "days" / f"{k}.csv") != m["days"][k]["sha256"]:
            raise FeedError(f"day file {k} does not match its recorded SHA-256")
    tmpdir = Path(tempfile.mkdtemp(prefix="munyun_feed_"))
    try:
        combined = tmpdir / "feed.csv"
        with open(combined, "w", encoding="utf-8", newline="") as out:
            for i, k in enumerate(keys):
                with open(fd / "days" / f"{k}.csv", encoding="utf-8") as fh:
                    head = fh.readline()
                    if i == 0:
                        out.write(head)
                    out.write(fh.read())
        store = SQLiteStore(":memory:")
        try:
            opts = ImportOptions(file=str(combined), instrument=INSTRUMENT, provider=PROVIDER, asset_type="CFD",
                                 timeframe="1m", profile="dukascopy_utc_csv", symbol="USATECH.IDX/USD",
                                 price_basis="bid", bid_close_column="close", ask_close_column="ask_close",
                                 ask_open_column="ask_open", ask_high_column="ask_high", ask_low_column="ask_low",
                                 dataset_name=FEED_DATASET_NAME, derive_timeframes=list(DERIVED),
                                 build_features=False, notes="paper-trading forward feed (never a research dataset)")
            r = import_dataset(opts, cfg, store)
            ids = [r.dataset_id] + list(r.derived)
            dss = {}
            for did in ids:
                ds = load_validated(store, cfg, did)
                dss[ds.manifest.timeframe] = ds
        finally:
            store.close()
    finally:
        for p in tmpdir.glob("*"):
            p.unlink(missing_ok=True)
        tmpdir.rmdir()
    return Feed(dss, keys[0], keys[-1], keys, dss["1m"].manifest.content_hash)


# ------------------------------------------------------------------------------------------- identity
SOURCE_CHECK_DAYS = 3
PRICE_COLS = ("open", "high", "low", "close") + ASK_COLS


def verify_against(ds_research, day_frame: pd.DataFrame) -> dict:
    """Compare a downloaded day (combine_day output) with the user's research dataset on the shared timestamps:
    BID OHLC and ASK OHLC must be identical. Proves the feed is the research source. ``max_abs_diff`` per column tells
    a rounding difference from a different source; bars present on one side only are counted, never filled."""
    import numpy as np
    b = ds_research.bars
    ts = pd.to_datetime(day_frame["timestamp"], utc=True).dt.as_unit("ns").astype("int64").to_numpy()
    rts = np.asarray(b.ts_ns, dtype="int64")
    lo, hi = np.searchsorted(rts, ts.min()), np.searchsorted(rts, ts.max(), side="right")
    ri = np.searchsorted(rts, ts).clip(0, len(rts) - 1)
    hit = rts[ri] == ts
    if not hit.any():
        return {"compared": 0, "identical": None, "note": "the research data does not cover this day"}
    fi, ri = np.nonzero(hit)[0], ri[hit]
    arrs = {"open": b.open, "high": b.high, "low": b.low, "close": b.close}
    if b.has_ask_ohlc:
        arrs.update(ask_open=b.ask_open, ask_high=b.ask_high, ask_low=b.ask_low, ask_close=b.ask_close)
    diffs, maxd = {}, {}
    any_diff = np.zeros(len(fi), dtype=bool)
    for c, arr in arrs.items():
        d = np.abs(day_frame[c].to_numpy(float)[fi] - np.asarray(arr, float)[ri])
        any_diff |= d > 1e-9
        diffs[c] = int((d > 1e-9).sum())
        maxd[c] = round(float(d.max()), 6) if len(d) else 0.0
    return {"compared": int(len(fi)), "identical": all(v == 0 for v in diffs.values()), "differences": diffs,
            "max_abs_diff": maxd, "bars_different": int(any_diff.sum()), "only_in_download": int((~hit).sum()), "only_in_research": int(hi - lo - len(fi)),
            "ask_compared": bool(b.has_ask_ohlc)}


def check_days(cfg: Mapping, ds_research, n_days: int = SOURCE_CHECK_DAYS) -> list[date]:
    """The last ``n_days`` trading dates whose whole session lies inside the research dataset's bars."""
    import numpy as np
    rts = np.asarray(ds_research.bars.ts_ns, dtype="int64")
    first, last = pd.Timestamp(int(rts[0]), tz="UTC"), pd.Timestamp(int(rts[-1]), tz="UTC")
    out = []
    for d in reversed(trading_dates(cfg, first.date(), last.date())):
        s, e = _window(cfg, d)
        if pd.Timestamp(s) >= first and pd.Timestamp(e) <= last + pd.Timedelta(minutes=1):
            out.append(d)
            if len(out) == n_days:
                break
    return sorted(out)


def source_check(cfg: Mapping, ds_research, fetcher: Callable | None = None,
                 n_days: int = SOURCE_CHECK_DAYS) -> dict:
    """Download the last complete trading days INSIDE the research dataset with the paper downloader and compare them
    bar by bar (verify_against). The downloaded days are never stored in the paper feed. Verdict: match (every shared
    price identical), mismatch (any price differs), no_overlap (nothing comparable), error (download failed)."""
    fetcher = fetcher or dukascopy_fetcher
    m = ds_research.manifest
    out = {"dataset_id": m.dataset_id, "dataset_name": getattr(m, "dataset_name", "") or m.dataset_id,
           "dataset_content_hash": m.content_hash, "checked_at": datetime.now(timezone.utc).isoformat(), "days": []}
    days = check_days(cfg, ds_research, n_days)
    if not days:
        return {**out, "verdict": "no_overlap", "compared": 0,
                "note": "the research dataset has no complete trading day to compare"}
    errors = 0
    for d in days:
        s, e = _window(cfg, d)
        try:
            frame = combine_day(fetcher("BID", s, e), fetcher("ASK", s, e), s, e)
        except Exception as exc:                          # FeedError or a download failure: reported, never guessed
            out["days"].append({"date": d.isoformat(), "error": f"{type(exc).__name__}: {exc}"})
            errors += 1
            continue
        if frame is None:
            out["days"].append({"date": d.isoformat(), "compared": 0, "identical": None, "note": "no bars from the source"})
            continue
        out["days"].append({"date": d.isoformat(), **verify_against(ds_research, frame)})
    compared = sum(x.get("compared", 0) for x in out["days"])
    differ = any(x.get("identical") is False for x in out["days"])
    verdict = ("mismatch" if differ else "error" if errors else "match" if compared else "no_overlap")
    return {**out, "verdict": verdict, "compared": compared,
            "bars_different": sum(x.get("bars_different", 0) for x in out["days"]),
            "max_abs_diff": {c: max((x.get("max_abs_diff", {}).get(c, 0.0) for x in out["days"]), default=0.0)
                             for c in PRICE_COLS}}


def read_source_check(data_root: Path | str) -> dict | None:
    try:
        return json.loads((feed_dir(data_root) / "source_check.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def write_source_check(data_root: Path | str, rec: dict) -> None:
    fd = feed_dir(data_root)
    fd.mkdir(parents=True, exist_ok=True)
    atomic_write_text(fd / "source_check.json", json.dumps(rec, indent=1, sort_keys=True))
