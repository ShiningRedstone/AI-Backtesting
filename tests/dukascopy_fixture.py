"""SYNTHETIC Dukascopy-shaped 1m CSV for tests (never market data; written to temp dirs only).

Layout of the user's real file: ``timestamp,open,high,low,close,volume`` with an explicit UTC offset
on every row, decimal (provider-defined) volume, 1m bars, real gaps. Bars follow the provisional
calendar DUKASCOPY_NQ_PROVISIONAL (Sun 18:00 - Fri 17:00 New York, pause 17:00-18:00), so the
UTC offset of the session boundary moves with US DST - which is what the tests check."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd

HEADER = "timestamp,open,high,low,close,volume"


def session_minutes(start: str, end: str) -> pd.DatetimeIndex:
    """Bar-open times (UTC) inside the provisional calendar between two New York dates."""
    idx = pd.date_range(pd.Timestamp(start, tz="America/New_York"), pd.Timestamp(end, tz="America/New_York"),
                        freq="1min", inclusive="left")
    wd, hr = idx.weekday, idx.hour
    keep = ((wd <= 3) & (hr != 17)) | ((wd == 4) & (hr < 17)) | ((wd == 6) & (hr >= 18))
    return idx[keep].tz_convert("UTC")


def write_fixture(path: Path, start: str = "2024-03-03", end: str = "2024-03-16", *, seed: int = 7,
                  gap_minutes: int = 25, drop: pd.DatetimeIndex | None = None, extra_rows: list[str] | None = None,
                  offset_style: str = "+00:00") -> dict:
    """Write the fixture; returns facts (rows, the gap it contains). The 2024-03-10 US DST switch
    falls inside the default range."""
    ts = session_minutes(start, end)
    gap = ts[len(ts) // 3: len(ts) // 3 + gap_minutes]           # a real in-session gap (no rows)
    ts = ts.difference(gap)
    if drop is not None:
        ts = ts.difference(drop)
    rng = np.random.default_rng(seed)
    close = 18000 + np.cumsum(rng.normal(0, 2.0, len(ts)))
    close = np.round(close, 3)
    opn = np.round(np.r_[close[0], close[:-1]], 3)
    hi = np.round(np.maximum(opn, close) + rng.uniform(0, 1.5, len(ts)), 3)
    lo = np.round(np.minimum(opn, close) - rng.uniform(0, 1.5, len(ts)), 3)
    vol = np.round(rng.uniform(0.01, 0.9, len(ts)), 4)                 # decimal, provider-defined
    stamps = ts.strftime("%Y-%m-%dT%H:%M:%S") + offset_style
    lines = [HEADER] + [f"{t},{o:.3f},{h:.3f},{l:.3f},{c:.3f},{v:.4f}"
                        for t, o, h, l, c, v in zip(stamps, opn, hi, lo, close, vol)]
    lines += extra_rows or []
    path.write_text("\n".join(lines) + "\n")
    return {"rows": len(ts), "gap": gap, "first": ts[0], "last": ts[-1]}
