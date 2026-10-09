"""Live bid / ask ticks for the simulated trading accounts (ADR-112).

Source: Dukascopy ticks via dukascopy-python (``INTERVAL_TICK``: timestamp, bidPrice, askPrice, bid / ask volume), the
same index CFDs the Charts tab draws (USATECH.IDX/USD for NQ / MNQ, USA500.IDX/USD for ES / MES). A buy is filled at the
ASK and a sell at the BID of a real tick, so the spread is paid as in a real fill. Ticks arrive with Dukascopy's own
delay; an order is only ever filled by a tick stamped AFTER it was placed (never by a price seen before the order).
Ticks are processed and dropped (never stored, never a dataset).
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Callable, NamedTuple

import numpy as np
import pandas as pd


class Tick(NamedTuple):
    ts: int          # epoch milliseconds (UTC)
    bid: float
    ask: float


def dukascopy_ticks(code: str, start: datetime, end: datetime) -> pd.DataFrame:
    import dukascopy_python as dp
    return dp.fetch(code, dp.INTERVAL_TICK, dp.OFFER_SIDE_BID, start, end)


def to_ticks(df: pd.DataFrame | None, after_ms: int) -> list[Tick]:
    """DataFrame (index = UTC time, bidPrice / askPrice) -> ticks strictly after ``after_ms``, time ordered, no broken quotes."""
    if df is None or not len(df):
        return []
    idx = pd.DatetimeIndex(df.index)
    idx = (idx.tz_localize("UTC") if idx.tz is None else idx.tz_convert("UTC")).as_unit("ms")
    ts = idx.asi8
    bid = pd.to_numeric(df["bidPrice"], errors="coerce").to_numpy(float)
    ask = pd.to_numeric(df["askPrice"], errors="coerce").to_numpy(float)
    ok = (ts > after_ms) & np.isfinite(bid) & np.isfinite(ask) & (bid > 0) & (ask >= bid)
    order = np.argsort(ts[ok], kind="mergesort")
    return [Tick(int(t), float(b), float(a)) for t, b, a in zip(ts[ok][order], bid[ok][order], ask[ok][order])]


class TickSource:
    """Fetches ticks of one feed code from a cursor up to now (+1 s)."""

    def __init__(self, fetch: Callable | None = None, now: Callable[[], datetime] | None = None):
        self.fetch = fetch or dukascopy_ticks
        self.now = now or (lambda: datetime.now(timezone.utc))

    def since(self, code: str, after_ms: int) -> list[Tick]:
        start = datetime.fromtimestamp(after_ms / 1000, timezone.utc)
        end = self.now() + timedelta(seconds=1)
        if end <= start:
            return []
        return to_ticks(self.fetch(code, start, end), after_ms)
