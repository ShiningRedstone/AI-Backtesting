"""Phase 2 test helpers: synthetic provider files written through the REAL import path.
All data here is synthetic (random walk) and labelled as such."""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.core.config import load_config
from edgelab.data.synthetic import generate_bars
from edgelab.features.sessions import load_sessions
from tests.helpers import CME

CFG = load_config(environ={})
SESSIONS = load_sessions(CFG)


def synthetic_canonical(start="2024-03-04", end="2024-03-16", tf=1, seed=11, **kw) -> pd.DataFrame:
    df, _ = generate_bars(CME, start, end, tf_minutes=tf, seed=seed, **kw)
    return df


def write_mt5_like(df: pd.DataFrame, path, spread_points: np.ndarray | None = None,
                   server_offset_h: int = 7) -> None:
    """MT5-style export: tab-separated, <DATE> <TIME> in broker server time = New York + 7h,
    tick volume, integer spread in 0.01-price 'points'."""
    ny = pd.DatetimeIndex(df["ts"]).tz_convert("America/New_York").tz_localize(None)
    server = ny + pd.Timedelta(hours=server_offset_h)
    sp = spread_points if spread_points is not None else np.full(len(df), 150)
    out = pd.DataFrame({"<DATE>": server.strftime("%Y.%m.%d"), "<TIME>": server.strftime("%H:%M:%S"),
                        "<OPEN>": df["open"].round(2), "<HIGH>": df["high"].round(2),
                        "<LOW>": df["low"].round(2), "<CLOSE>": df["close"].round(2),
                        "<TICKVOL>": df["volume"].astype(int), "<VOL>": 0, "<SPREAD>": sp})
    out.to_csv(path, sep="\t", index=False)


def write_generic_utc(df: pd.DataFrame, path, with_volume: bool = True, close_stamped: bool = False,
                      tf: int = 1) -> None:
    ts = pd.DatetimeIndex(df["ts"])
    if close_stamped:
        ts = ts + pd.Timedelta(minutes=tf)
    out = pd.DataFrame({"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": df["open"].round(2),
                        "high": df["high"].round(2), "low": df["low"].round(2),
                        "close": df["close"].round(2)})
    if with_volume:
        out["volume"] = df["volume"]
    out.to_csv(path, index=False)
