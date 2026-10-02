"""Multi-timeframe features without lookahead.

A higher-timeframe (HTF) bar is built from base bars by session-anchored resampling.
Its values become known at its EFFECTIVE CLOSE = min(open + timeframe, close of its
trading session): a 45m bar starting 16:30 ET in a session ending 17:00 is complete at
17:00, not 17:15.

Mapping onto base bars: base bar t may see HTF bar k only if
    effective_close[k] <= base_close[t]
i.e. the HTF bar must be COMPLETE when the base bar's decision is made. The value of
an unfinished HTF bar is never visible - not even its partial, "so far" value - so the
classic resample-then-forward-fill leak (exposing the HTF bar at its OPEN) cannot occur.
Base bars before the first completed HTF bar receive NaN.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.data.calendar import SessionCalendar
from edgelab.data.resample import resample_bars
from edgelab.data.schema import BarArrays

NS_PER_MIN = 60_000_000_000


def build_htf(bars: BarArrays, calendar: SessionCalendar, tf_minutes: int) -> tuple[BarArrays, np.ndarray]:
    """Resample base bars to ``tf_minutes``; return (htf_bars, effective_close_ns). ADR-91: remembered by the base bars'
    exact content hash + calendar + timeframe, so every strategy (and every causality cut) shares one result."""
    if tf_minutes % bars.tf_minutes:
        raise ValueError(f"{tf_minutes}m is not a multiple of the base {bars.tf_minutes}m")
    from edgelab.core.memo import derived
    return derived().get_or_compute(("htf", bars.content_hash(), calendar.fingerprint(), int(tf_minutes)),
                                    lambda: _build_htf(bars, calendar, tf_minutes))


def _build_htf(bars: BarArrays, calendar: SessionCalendar, tf_minutes: int) -> tuple[BarArrays, np.ndarray]:
    if tf_minutes % bars.tf_minutes:
        raise ValueError(f"{tf_minutes}m is not a multiple of the base {bars.tf_minutes}m")
    df = bars.to_frame()
    if "spread" in df.columns:
        df = df.drop(columns=["spread"])
    htf = BarArrays.from_frame(resample_bars(df, calendar, tf_minutes), tf_minutes)
    nominal = htf.ts_close_ns
    td = calendar.trading_dates(htf.ts)
    uniq = np.unique(td)
    closes = np.array([calendar.session_bounds(pd.Timestamp(d).date())[1].tz_convert("UTC").as_unit("ns").value
                       for d in uniq], dtype=np.int64)
    sess_close = closes[np.searchsorted(uniq, td)] if len(td) else np.array([], np.int64)   # ADR-91: vectorized lookup
    return htf, np.minimum(nominal, sess_close)


def map_to_base(values: np.ndarray, htf_known_ns: np.ndarray, base_close_ns: np.ndarray) -> np.ndarray:
    """Value of the latest COMPLETED HTF bar for every base bar (NaN before the first)."""
    k = np.searchsorted(htf_known_ns, base_close_ns, side="right") - 1
    out = np.full(len(base_close_ns), np.nan)
    ok = k >= 0
    out[ok] = values[k[ok]]
    return out
