"""Timeframe aggregation, anchored to the session open (not midnight UTC).

Anchoring matters for non-divisors of 60 (45m) and multi-hour bars (240m): a
240m NQ bar should start at 18:00 ET, 22:00 ET, ... regardless of DST. Each
output bar carries ``n_subbars`` so partial bars (session end, data gaps) are
visible rather than silently looking like full bars.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.data.calendar import SessionCalendar
from edgelab.data.schema import ASK_COLUMNS, BarArrays, canonicalize

NS_PER_MIN = 60_000_000_000


def resample_bars(df: pd.DataFrame, calendar: SessionCalendar, tf_minutes: int) -> pd.DataFrame:
    bars = canonicalize(df)
    ts_idx = pd.DatetimeIndex(bars["ts"]).as_unit("ns")
    if not ts_idx.is_monotonic_increasing:
        raise ValueError("resample requires sorted bars (run clean_bars first)")
    td = calendar.trading_dates(ts_idx)
    uniq = np.unique(td)
    open_ns = {d: calendar.session_bounds(pd.Timestamp(d).date())[0].tz_convert("UTC").as_unit("ns").value
               for d in uniq}
    sess_open = np.array([open_ns[d] for d in td], dtype=np.int64) if len(td) else np.array([], np.int64)
    since = ts_idx.asi8 - sess_open
    if (since < 0).any():
        raise ValueError("bar precedes its session open - calendar/timezone mismatch")
    step = np.int64(tf_minutes) * NS_PER_MIN
    bucket = sess_open + (since // step) * step
    g = bars.assign(_b=bucket).groupby("_b", sort=True)
    size = g.size()
    # Volume is only defined when EVERY sub-bar has it. pandas' sum() would turn an all-NaN
    # bucket into 0.0 and a partially-missing bucket into an under-count (fixed in Phase 2).
    vol = g["volume"].sum(min_count=1).where(g["volume"].count() == size)
    out = pd.DataFrame({
        "open": g["open"].first(), "high": g["high"].max(), "low": g["low"].min(),
        "close": g["close"].last(), "volume": vol, "n_subbars": size,
    })
    if "spread" in bars.columns:   # mean spread of the sub-bars; NaN if any sub-bar lacks it
        out["spread"] = g["spread"].mean().where(g["spread"].count() == size)
    if any(k in bars.columns for k in ASK_COLUMNS):
        # Observed ASK OHLC over the SAME present sub-bars as BID (ADR-55). The whole ASK side of
        # a bucket is NaN if any present sub-bar lacks any ASK value (pandas first/max/min/last
        # would otherwise skip NaN and fabricate a quote); validation then FAILs the bucket.
        missing = [k for k in ASK_COLUMNS if k not in bars.columns]
        if missing:
            raise ValueError(f"ASK OHLC is all-or-none: missing {missing}")
        ask = pd.DataFrame({"ask_open": g["ask_open"].first(), "ask_high": g["ask_high"].max(),
                            "ask_low": g["ask_low"].min(), "ask_close": g["ask_close"].last()})
        complete = pd.concat([g[k].count() == size for k in ASK_COLUMNS], axis=1).all(axis=1)
        for k in ASK_COLUMNS:
            out[k] = ask[k].where(complete)
    out.insert(0, "ts", pd.DatetimeIndex(out.index.to_numpy().astype("datetime64[ns]")).tz_localize("UTC"))
    return out.reset_index(drop=True)


def map_intrabar(htf: BarArrays, ltf: BarArrays) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """For each HTF bar return (ltf_start, ltf_end_exclusive, reliable).

    ``reliable`` is True only where the LTF bars reproduce the HTF bar exactly
    (same open, high, low, close). Intrabar resolution is only trusted there.
    """
    step = np.int64(htf.tf_minutes) * NS_PER_MIN
    start = np.searchsorted(ltf.ts_ns, htf.ts_ns, side="left")
    end = np.searchsorted(ltf.ts_ns, htf.ts_ns + step, side="left")
    reliable = np.zeros(len(htf), dtype=bool)
    nonempty = end > start
    idx = np.flatnonzero(nonempty)
    if len(idx):
        # vectorized reductions over variable-length segments
        hi = np.maximum.reduceat(ltf.high, start[idx]) if len(ltf) else np.array([])
        lo = np.minimum.reduceat(ltf.low, start[idx]) if len(ltf) else np.array([])
        # reduceat reduces to the next start; clip segments that overrun their end
        seg_ok = np.ones(len(idx), bool)
        nxt = np.r_[start[idx][1:], len(ltf)]
        overrun = nxt > end[idx]
        for j in np.flatnonzero(overrun):
            s, e = start[idx[j]], end[idx[j]]
            hi[j], lo[j] = ltf.high[s:e].max(), ltf.low[s:e].min()
        tol = 1e-9
        seg_ok &= np.abs(ltf.open[start[idx]] - htf.open[idx]) < tol
        seg_ok &= np.abs(ltf.close[end[idx] - 1] - htf.close[idx]) < tol
        seg_ok &= np.abs(hi - htf.high[idx]) < tol
        seg_ok &= np.abs(lo - htf.low[idx]) < tol
        reliable[idx] = seg_ok
    return start, end, reliable
