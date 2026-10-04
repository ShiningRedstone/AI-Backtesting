"""Higher-timeframe candles, swing points and fair value gaps built from 1-minute bars (My strategy, ADR-93).

CAUSALITY: every higher-timeframe (HTF) candle carries ``comp`` = the index of the 1-minute bar at whose CLOSE the
candle is known to be complete. A candle is complete at its last member bar when that bar closes exactly at the
bucket's scheduled end (or at the session close); otherwise only once a bar of a LATER bucket exists (its first
member). A truncated history therefore never treats its partial last candle as complete. Swing points are known at the
completion of the confirming candle, FVGs at the completion of their third candle. Callers only use objects whose
``comp`` index is <= the decision bar.

Buckets are anchored to the session open of the trading date (18:00 New York on the Dukascopy calendar), which gives
New York clock alignment for 1-60 minute candles, 4h candles at 18/22/02/06/10/14 and one daily candle per trading date
(18:00-16:15), like the CME charts the videos use.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from edgelab.data.schema import BarArrays

NEVER = np.int64(2**62)
TFS = (1, 2, 3, 4, 5, 15, 30, 60, 240, 1440)
TF_LABEL = {1: "1m", 2: "2m", 3: "3m", 4: "4m", 5: "5m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", 1440: "1D"}
LABEL_TF = {v: k for k, v in TF_LABEL.items()}


@dataclass
class Base:
    """Per 1-minute bar: trading-date ordinal, New York minute of day, minutes since the session open."""
    n: int
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    ts: np.ndarray            # bar open, UTC ns
    td: np.ndarray            # trading date as int64 days since epoch
    mod: np.ndarray           # New York minute of day of the bar OPEN (0..1439)
    since_open: np.ndarray    # minutes since the session open (0..)
    session_len: int
    ask: tuple | None = None  # (o, h, l, c) ASK arrays when present


def build_base(bars: BarArrays, calendar, series: str = "bid") -> Base:
    import pandas as pd
    td = calendar.trading_dates_of(bars).astype("datetime64[D]").astype(np.int64)
    loc = pd.DatetimeIndex(bars.ts_ns.astype("datetime64[ns]")).tz_localize("UTC").tz_convert(calendar.timezone)
    mod = np.asarray(loc.hour * 60 + loc.minute, dtype=np.int64)
    since_open = (mod - calendar.open_min) % 1440
    session_len = (calendar.close_min - calendar.open_min) % 1440 or 1440
    o, h, l, c = bars.open, bars.high, bars.low, bars.close
    ask = (bars.ask_open, bars.ask_high, bars.ask_low, bars.ask_close) if bars.has_ask_ohlc else None
    if series == "mid":
        if ask is None:
            raise ValueError("price series 'mid' needs ASK OHLC in the dataset")
        o, h, l, c = ((o + ask[0]) / 2, (h + ask[1]) / 2, (l + ask[2]) / 2, (c + ask[3]) / 2)
    return Base(len(bars), o, h, l, c, bars.ts_ns, td, mod, since_open, int(session_len), ask)


@dataclass
class TF:
    """Candles of one timeframe. Index arrays point into the 1-minute base."""
    tf: int
    start: np.ndarray         # first member bar
    end: np.ndarray           # last member bar
    comp: np.ndarray          # bar index at whose close the candle is known complete (NEVER = not yet)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    t0: np.ndarray            # scheduled bucket start, UTC ns
    td: np.ndarray            # trading date of the candle
    of_bar: np.ndarray        # per 1-minute bar: index of its candle (int32)
    _swings: dict = field(default_factory=dict)
    _fvgs: dict = field(default_factory=dict)

    def __len__(self) -> int:
        return len(self.start)

    @property
    def label(self) -> str:
        return TF_LABEL[self.tf]


def build_tf(b: Base, tf: int) -> TF:
    n = b.n
    if tf == 1:
        idx = np.arange(n)
        comp = idx.copy()
        return TF(1, idx, idx, comp, b.o, b.h, b.l, b.c, b.ts.copy(), b.td, idx.astype(np.int32))
    if tf >= 1440:
        key = b.td
    else:
        key = b.td * 1440 + (b.since_open // tf) * tf
    if n == 0:
        z = np.zeros(0, np.int64)
        return TF(tf, z, z, z, *(np.zeros(0),) * 4, z, z, np.zeros(0, np.int32))
    chg = np.r_[True, key[1:] != key[:-1]]
    start = np.flatnonzero(chg)
    end = np.r_[start[1:] - 1, n - 1]
    o = b.o[start]
    c = b.c[end]
    h = np.maximum.reduceat(b.h, start)
    l = np.minimum.reduceat(b.l, start)
    so_last = b.since_open[end]
    if tf >= 1440:
        at_end = so_last + 1 >= b.session_len
        off = b.since_open[start]
    else:
        at_end = ((so_last + 1) % tf == 0) | (so_last + 1 >= b.session_len)
        off = b.since_open[start] % tf
    nxt = np.r_[start[1:], NEVER]
    comp = np.where(at_end, end, nxt).astype(np.int64)
    t0 = b.ts[start] - off.astype(np.int64) * 60_000_000_000
    of_bar = (np.cumsum(chg) - 1).astype(np.int32)
    return TF(tf, start, end, comp, o, h, l, c, t0, b.td[start], of_bar)


# ---------------------------------------------------------------------------------------------- swings
def swings(t: TF, strength: int) -> dict:
    """Fractal swing points: a high higher than the ``strength`` candles before it and not exceeded by the
    ``strength`` candles after it (lows mirrored). Known at the completion of the last confirming candle.
    Returns {'hi': (idx, price, known), 'lo': (...)} sorted by candle index."""
    hit = t._swings.get(strength)
    if hit is not None:
        return hit
    s = max(1, int(strength))
    m = len(t)
    out = {}
    for side, x in (("hi", t.h), ("lo", t.l)):
        if m < 2 * s + 1:
            out[side] = (np.zeros(0, np.int64), np.zeros(0), np.zeros(0, np.int64))
            continue
        from numpy.lib.stride_tricks import sliding_window_view as win
        w = win(x, 2 * s + 1)                         # window j-s .. j+s centred at j = s..m-s-1
        mid = x[s:m - s]
        left, right = w[:, :s], w[:, s + 1:]
        if side == "hi":
            ok = (mid > left.max(axis=1)) & (mid >= right.max(axis=1))
        else:
            ok = (mid < left.min(axis=1)) & (mid <= right.min(axis=1))
        j = np.flatnonzero(ok) + s
        out[side] = (j.astype(np.int64), x[j].astype(np.float64), t.comp[j + s])
    t._swings[strength] = out
    return out


# ------------------------------------------------------------------------------------------------ FVGs
@dataclass
class Gaps:
    """Fair value gaps of one timeframe and side. Index k = the third candle."""
    k: np.ndarray
    top: np.ndarray
    bottom: np.ndarray
    known: np.ndarray        # bar index at which the gap exists (completion of candle k)


def fvgs(t: TF, side: str, min_points: float = 0.0, same_session: bool = True) -> Gaps:
    """side 'bull': low[k] > high[k-2] (zone high[k-2]..low[k]); 'bear': high[k] < low[k-2] (zone high[k]..low[k-2]).
    Intraday gaps that span the daily session break (16:15 -> 18:00) are left out when ``same_session``."""
    key = (side, float(min_points), bool(same_session))
    hit = t._fvgs.get(key)
    if hit is not None:
        return hit
    m = len(t)
    if m < 3:
        g = Gaps(*(np.zeros(0, np.int64),), np.zeros(0), np.zeros(0), np.zeros(0, np.int64))
        t._fvgs[key] = g
        return g
    k = np.arange(2, m)
    if side == "bull":
        bottom, top = t.h[:-2], t.l[2:]
    else:
        bottom, top = t.h[2:], t.l[:-2]
    ok = (top - bottom) > max(min_points, 0.0)
    if same_session and t.tf < 1440:
        ok &= t.td[2:] == t.td[:-2]
    sel = np.flatnonzero(ok)
    g = Gaps(k[sel].astype(np.int64), top[sel].astype(np.float64), bottom[sel].astype(np.float64), t.comp[k[sel]])
    t._fvgs[key] = g
    return g


# ------------------------------------------------------------------------------------- range queries
class RangeQ:
    """Range min / max over a 1-minute array with block summaries (no lookahead: callers pass the range)."""
    B = 256

    def __init__(self, x: np.ndarray):
        self.x = x
        n = len(x)
        nb = (n + self.B - 1) // self.B
        pad = np.full(nb * self.B, np.nan)
        pad[:n] = x
        blk = pad.reshape(nb, self.B)
        self.bmin = np.nanmin(blk, axis=1) if nb else np.zeros(0)
        self.bmax = np.nanmax(blk, axis=1) if nb else np.zeros(0)

    # ADR-97 speed: ndarray methods instead of np.min / np.max (no dispatch overhead); the same values. The parts of a
    # long range are combined with np.min / np.max as before (NaN-propagating, identical results).
    def min(self, a: int, b: int) -> float:
        """Inclusive range [a, b]."""
        if b < a:
            return np.nan
        x, B = self.x, self.B
        if b - a < 2 * B:
            return float(x[a:b + 1].min())
        ba, bb = a // B + 1, b // B           # whole blocks ba .. bb-1
        parts = [x[a:ba * B].min(), x[bb * B:b + 1].min()]
        if bb > ba:
            parts.append(self.bmin[ba:bb].min())
        return float(np.min(np.asarray(parts)))

    def max(self, a: int, b: int) -> float:
        if b < a:
            return np.nan
        x, B = self.x, self.B
        if b - a < 2 * B:
            return float(x[a:b + 1].max())
        ba, bb = a // B + 1, b // B
        parts = [x[a:ba * B].max(), x[bb * B:b + 1].max()]
        if bb > ba:
            parts.append(self.bmax[ba:bb].max())
        return float(np.max(np.asarray(parts)))


def first_where(x: np.ndarray, a: int, b: int, below: float | None = None, above: float | None = None) -> int:
    """First index j in [a, b] with x[j] <= below (or x[j] >= above); -1 if none. Chunked so short searches are cheap."""
    if b < a:
        return -1
    k0, size = a, 64
    while k0 <= b:
        k1 = min(b, k0 + size - 1)
        seg = x[k0:k1 + 1]
        m = (seg <= below) if below is not None else (seg >= above)
        if m.any():
            return k0 + int(np.argmax(m))
        k0, size = k1 + 1, size * 4
    return -1
