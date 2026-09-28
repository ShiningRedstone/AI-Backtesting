"""Example strategies for Phase 1 (hand-written; Phase 3 introduces the DSL).

``Breakout`` is a deliberately simple concept, used to exercise the engine - not
a claim of edge. ``RandomEntry`` is the null model every real strategy must beat
with comparable trade count, holding period, risk and session constraints.
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.data.schema import BarArrays
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy


def decision_minutes_local(bars: BarArrays, tz: str) -> np.ndarray:
    """Local minute-of-day at each bar's CLOSE (= decision time). Causal."""
    close_ts = pd.DatetimeIndex(bars.ts_close_ns.astype("datetime64[ns]")).tz_localize("UTC").tz_convert(tz)
    return np.asarray(close_ts.hour * 60 + close_ts.minute)


def _window_mask(bars: BarArrays, window: tuple[str, str] | None, tz: str) -> np.ndarray:
    if not window:
        return np.ones(len(bars), bool)
    to_min = lambda s: int(s[:2]) * 60 + int(s[3:5])
    m = decision_minutes_local(bars, tz)
    return (m >= to_min(window[0])) & (m < to_min(window[1]))


class Breakout(Strategy):
    """Close breaks the prior N-bar high (long) / low (short); market entry next bar."""
    family = "breakout"

    def __init__(self, order: OrderSpec, lookback: int = 20, side: str = "both",
                 window: tuple[str, str] | None = None, tz: str = "America/New_York"):
        if side not in ("long", "short", "both"):
            raise ValueError("side must be long|short|both")
        super().__init__(order, lookback=lookback, side=side, window=window, tz=tz)

    def generate_signals(self, bars: BarArrays) -> SignalSet:
        n, N = len(bars), self.params["lookback"]
        sig = SignalSet.empty(n)
        if n <= N:
            return sig
        win_h = np.lib.stride_tricks.sliding_window_view(bars.high, N)[:-1].max(axis=1)
        win_l = np.lib.stride_tricks.sliding_window_view(bars.low, N)[:-1].min(axis=1)
        prev_hi = np.r_[np.full(N, np.nan), win_h]    # max(high[i-N:i]) at index i
        prev_lo = np.r_[np.full(N, np.nan), win_l]
        ok = _window_mask(bars, self.params["window"], self.params["tz"])
        side = self.params["side"]
        with np.errstate(invalid="ignore"):
            if side in ("long", "both"):
                sig.direction[(bars.close > prev_hi) & ok] = 1
            if side in ("short", "both"):
                sig.direction[(bars.close < prev_lo) & ok] = -1
        return sig


class RandomEntry(Strategy):
    """Null model: enters with probability p at each eligible bar, random direction.
    The random stream is a pure function of (seed, bar index), so it is causal and
    reproducible."""
    family = "random_control"

    def __init__(self, order: OrderSpec, p: float = 0.05, seed: int = 0, side: str = "both",
                 window: tuple[str, str] | None = None, tz: str = "America/New_York"):
        super().__init__(order, p=p, seed=seed, side=side, window=window, tz=tz)

    def generate_signals(self, bars: BarArrays) -> SignalSet:
        n = len(bars)
        sig = SignalSet.empty(n)
        u = np.random.default_rng(self.params["seed"]).random((n, 2))
        fire = (u[:, 0] < self.params["p"]) & _window_mask(bars, self.params["window"], self.params["tz"])
        side = self.params["side"]
        d = np.where(u[:, 1] < 0.5, 1, -1) if side == "both" else (1 if side == "long" else -1)
        sig.direction[fire] = (d[fire] if side == "both" else d)
        return sig
