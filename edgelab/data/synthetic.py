"""Synthetic market data with KNOWN properties - used to prove the engine is correct
before it is ever pointed at real data.

Construction: every bar is built from ``substeps`` Gaussian increments of a
latent price path; OHLC are taken from the tick-rounded path, so bars are
internally consistent by construction and consecutive in-session bars are gap-free
(open == previous close) unless ``session_gap_sigma`` > 0, which injects a gap
at each new trading session.

Why this gives testable answers: for a driftless walk, a trade with stop distance
a and target distance b (filled exactly at those levels) hits the target first
with probability a/(a+b) and has zero expected gross P&L (optional stopping on a
martingale). Any systematic deviation beyond sampling error is an engine bug.
"""
from __future__ import annotations

from typing import Iterable, Sequence

import numpy as np
import pandas as pd

from edgelab.data.calendar import SessionCalendar


def generate_bars(calendar: SessionCalendar, start: str, end: str, tf_minutes: int = 1,
                  start_price: float = 18000.0, sigma_per_bar: float = 3.0,
                  drift_per_bar: float = 0.0, tick_size: float = 0.25, substeps: int = 12,
                  session_gap_sigma: float = 0.0, base_volume: float = 500.0,
                  seed: int = 0) -> tuple[pd.DataFrame, dict]:
    """Random-walk bars on the calendar's session grid over [start, end) in calendar-local
    time. Returns (bars, truth)."""
    rng = np.random.default_rng(seed)
    end_ts = pd.Timestamp(end, tz=calendar.timezone)
    ts = calendar.expected_bar_opens(pd.Timestamp(start, tz=calendar.timezone), end_ts, tf_minutes)
    ts = ts[ts < end_ts.tz_convert("UTC")]
    n = len(ts)
    if n == 0:
        raise ValueError("calendar produced no bars for the requested range")
    inc = rng.normal(drift_per_bar / substeps, sigma_per_bar / np.sqrt(substeps), size=(n, substeps))
    td = calendar.trading_dates(ts)
    new_session = np.r_[False, td[1:] != td[:-1]]
    gaps = np.where(new_session, rng.normal(0.0, session_gap_sigma, n), 0.0) \
        if session_gap_sigma > 0 else np.zeros(n)
    bar_move = inc.sum(axis=1)
    latent_open = start_price + np.r_[0.0, np.cumsum(bar_move)[:-1]] + np.cumsum(gaps)
    path = latent_open[:, None] + np.cumsum(inc, axis=1)

    def rt(x):
        return np.round(x / tick_size) * tick_size

    o = rt(latent_open)
    p = rt(path)
    h = np.maximum(o, p.max(axis=1))
    l = np.minimum(o, p.min(axis=1))
    c = p[:, -1]
    vol = np.round(base_volume * rng.lognormal(0.0, 0.5, n))
    bars = pd.DataFrame({"ts": ts, "open": o, "high": h, "low": l, "close": c, "volume": vol})
    truth = dict(generator="random_walk", seed=seed, sigma_per_bar=sigma_per_bar,
                 drift_per_bar=drift_per_bar, substeps=substeps, tick_size=tick_size,
                 session_gap_sigma=session_gap_sigma, start_price=start_price,
                 tf_minutes=tf_minutes, calendar=calendar.name)
    return bars, truth


def bars_from_ohlc(rows: Sequence[Sequence[float]], start: str = "2024-01-02 00:00",
                   tf_minutes: int = 1, tz: str = "UTC",
                   timestamps: Iterable | None = None) -> pd.DataFrame:
    """Explicit bars for hand-checked unit tests. rows = [(o, h, l, c), ...]."""
    arr = np.asarray(rows, dtype=float)
    if timestamps is None:
        ts = pd.date_range(pd.Timestamp(start, tz=tz), periods=len(arr),
                           freq=f"{tf_minutes}min").tz_convert("UTC")
    else:
        ts = pd.DatetimeIndex(list(timestamps)).tz_convert("UTC")
    return pd.DataFrame({"ts": ts, "open": arr[:, 0], "high": arr[:, 1], "low": arr[:, 2],
                         "close": arr[:, 3], "volume": np.full(len(arr), 100.0)})
