"""Bars of every timeframe for the market simulator, built from 1-minute BID bars (NQ: the protocol's research dataset;
ES: the imported USA500 reference series). Read-only; never a dataset, never the holdout.

Conventions (explicit, tested):
* time zone America/New_York; the TRADING DATE of a bar = the NY date of (bar open + 6 hours), i.e. a session runs
  18:00 -> 16:15 the next day and carries the later date;
* a bar of timeframe ``tf`` minutes covers [anchor + k*tf, anchor + (k+1)*tf) with the anchor at 18:00 NY of its
  session; it is KNOWN (complete) at its nominal end ``known_ns`` = bucket start + tf (4h buckets: 18-22-02-06-10-14);
* the daily bar of a trading date is known at the end of its last minute;
* missing minutes are never filled: a bar is built from the minutes that exist (``n`` = how many).
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pandas as pd

NY = "America/New_York"
MIN_NS = 60_000_000_000
TIMEFRAMES = (1, 2, 3, 4, 5, 10, 15, 30, 60, 240)     # minutes; plus the daily bar (DAY)
DAY = 1440
TF_LABEL = {1: "1m", 2: "2m", 3: "3m", 4: "4m", 5: "5m", 10: "10m", 15: "15m", 30: "30m", 60: "1h", 240: "4h", DAY: "1D"}
SESSION_OPEN_MIN = 18 * 60                              # minutes after NY midnight


class MarketDataError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass
class Minute:
    """1-minute bars with their New York clock (all arrays the same length, sorted, unique timestamps)."""
    ts: np.ndarray            # bar open, UTC ns
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    ny_min: np.ndarray = field(init=False)      # minutes after NY midnight of the bar open
    sess_min: np.ndarray = field(init=False)    # minutes after the 18:00 NY session open (0 .. 1439)
    day: np.ndarray = field(init=False)         # trading date as datetime64[D]
    weekday: np.ndarray = field(init=False)     # 0 = Monday (of the trading date)

    def __post_init__(self):
        self.ts = np.asarray(self.ts, dtype=np.int64)
        for k in ("o", "h", "l", "c"):
            setattr(self, k, np.asarray(getattr(self, k), dtype=np.float64))
        n = len(self.ts)
        if not (len(self.o) == len(self.h) == len(self.l) == len(self.c) == n):
            raise MarketDataError("BAR_SHAPES", "price arrays differ in length")
        if n > 1 and not (np.diff(self.ts) > 0).all():
            raise MarketDataError("BAR_ORDER", "timestamps must be strictly increasing")
        if n and (self.ts % MIN_NS).any():
            raise MarketDataError("BAR_NOT_1M", "timestamps must be whole minutes")
        idx = pd.DatetimeIndex(self.ts, tz="UTC").tz_convert(NY)
        self.ny_min = np.asarray(idx.hour * 60 + idx.minute, dtype=np.int32)
        self.sess_min = ((self.ny_min - SESSION_OPEN_MIN) % 1440).astype(np.int32)
        shifted = (idx + pd.Timedelta(hours=6)).tz_localize(None).normalize()
        self.day = shifted.values.astype("datetime64[D]")
        self.weekday = np.asarray(shifted.weekday, dtype=np.int8)

    def __len__(self):
        return len(self.ts)

    def cut(self, start_ns: int | None = None, end_ns: int | None = None) -> "Minute":
        m = np.ones(len(self.ts), bool)
        if start_ns is not None:
            m &= self.ts >= start_ns
        if end_ns is not None:
            m &= self.ts <= end_ns
        return Minute(self.ts[m], self.o[m], self.h[m], self.l[m], self.c[m])


@dataclass
class Bars:
    """Bars of one timeframe built from ``Minute`` (``i0``/``i1`` = first / last minute index, inclusive)."""
    tf: int
    ts: np.ndarray            # bucket start, UTC ns
    known_ns: np.ndarray      # when the bar is complete (usable by a live decision at or after this time)
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    n: np.ndarray             # minutes present
    i0: np.ndarray
    i1: np.ndarray
    day: np.ndarray           # trading date (datetime64[D])
    slot: np.ndarray          # bucket number inside the session (0 = the bucket starting 18:00)

    def __len__(self):
        return len(self.ts)

    @property
    def label(self) -> str:
        return TF_LABEL[self.tf]


def resample(m: Minute, tf: int) -> Bars:
    """Aggregate 1-minute bars into ``tf``-minute buckets anchored at the 18:00 NY session open (or DAY)."""
    n = len(m)
    if n == 0:
        z = np.zeros(0)
        zi = np.zeros(0, dtype=np.int64)
        return Bars(tf, zi, zi, z, z, z, z, zi, zi, zi, np.zeros(0, "datetime64[D]"), zi)
    dayi = m.day.astype(np.int64)
    if tf == DAY:
        slot = np.zeros(n, dtype=np.int64)
    else:
        slot = (m.sess_min // tf).astype(np.int64)
    key = dayi * 10_000 + slot
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    ends = np.r_[starts[1:], n] - 1
    h = np.maximum.reduceat(m.h, starts)
    lo = np.minimum.reduceat(m.l, starts)
    o = m.o[starts]
    c = m.c[ends]
    if tf == DAY:
        bstart = m.ts[starts]
        known = m.ts[ends] + MIN_NS
    else:
        off = (m.sess_min[starts] % tf).astype(np.int64)
        bstart = m.ts[starts] - off * MIN_NS
        known = bstart + tf * MIN_NS
    return Bars(tf, bstart, known, o, h, lo, c, (ends - starts + 1).astype(np.int64), starts.astype(np.int64),
                ends.astype(np.int64), m.day[starts], slot[starts])


def all_timeframes(m: Minute, tfs=TIMEFRAMES + (DAY,)) -> dict[int, Bars]:
    return {tf: resample(m, tf) for tf in tfs}


def atr(b: Bars, n: int = 14) -> np.ndarray:
    """Causal average true range: value at bar k uses bars k-n+1 .. k (known when bar k is complete). NaN before n bars."""
    if len(b) == 0:
        return np.zeros(0)
    pc = np.r_[np.nan, b.c[:-1]]
    tr = np.nanmax(np.vstack([b.h - b.l, np.abs(b.h - pc), np.abs(b.l - pc)]), axis=0)
    cs = np.cumsum(np.r_[0.0, tr])
    out = np.full(len(tr), np.nan)
    if len(tr) >= n:
        out[n - 1:] = (cs[n:] - cs[:-n]) / n
    return out


def typical_by_slot(b: Bars, values: np.ndarray, days: int = 20, stat: str = "median") -> np.ndarray:
    """For every bar: the median (or mean) of ``values`` at the SAME slot over the previous ``days`` trading dates that
    had that slot (strictly earlier dates: known before the day starts). NaN with fewer than 5 earlier values."""
    import warnings
    from numpy.lib.stride_tricks import sliding_window_view
    out = np.full(len(b), np.nan)
    if len(b) == 0:
        return out
    order = np.lexsort((b.ts, b.slot))
    slots = b.slot[order]
    vals = np.asarray(values, float)[order]
    bounds = np.flatnonzero(np.r_[True, slots[1:] != slots[:-1], True])
    fn = np.nanmedian if stat == "median" else np.nanmean
    for a, z in zip(bounds[:-1], bounds[1:]):
        v = vals[a:z]
        win = sliding_window_view(np.r_[np.full(days, np.nan), v], days)[: z - a]   # row k = v[k-days : k]
        cnt = np.isfinite(win).sum(axis=1)
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", RuntimeWarning)
            res = fn(win, axis=1)
        res[cnt < 5] = np.nan
        out[order[a:z]] = res
    return out


def trading_day(ts_ns) -> np.ndarray:
    """Trading date (datetime64[D]) of UTC ns timestamps: the NY date of (time + 6 hours)."""
    idx = pd.DatetimeIndex(np.asarray(ts_ns, dtype=np.int64), tz="UTC").tz_convert(NY)
    return (idx + pd.Timedelta(hours=6)).tz_localize(None).normalize().values.astype("datetime64[D]")


# =============================================================================================== loading
@dataclass
class Market:
    """NQ and ES minutes on the same window (ES may be None), the window and where they came from."""
    nq: Minute
    es: Minute | None
    start: pd.Timestamp
    end: pd.Timestamp
    source: dict


def _minute_from_dataset(ds) -> Minute:
    b = ds.bars
    return Minute(b.ts_ns, b.open, b.high, b.low, b.close)


def load_es(data_root, start_ns: int, end_ns: int) -> tuple[Minute | None, dict | None]:
    from edgelab.mystrategy import es as ES
    try:
        s = ES.load(data_root)                                   # hash-checked
    except ES.EsError as e:
        raise MarketDataError(e.code, e.message) from e
    if s is None:
        return None, None
    with np.load(ES.folder(data_root) / s.manifest["file"]) as z:
        ts, o, h, lo, c = (z[k] for k in ("ts", "open", "high", "low", "close"))
    keep = (ts >= start_ns) & (ts <= end_ns)
    if not keep.any():
        return None, {"content_hash": s.content_hash, "bars_in_window": 0}
    return Minute(ts[keep], o[keep], h[keep], lo[keep], c[keep]), {"content_hash": s.content_hash,
                                                                   "bars_in_window": int(keep.sum())}


def discovery(svc, lock=None) -> Market:
    """NQ 1m BID of the protocol's DISCOVERY window (re-validated cut) and ES cut to the same window."""
    from edgelab.mystrategy import runner as R
    parent = R._parent(svc)
    if parent is None:
        raise MarketDataError("NO_PROTOCOL", "The market simulator needs the workspace's active research protocol (its "
                                             "data and discovery dates). None, or more than one, is active.")
    w = R.windows(parent["material"])
    start, end = R._ts(w["discovery"]["start"]), R._ts(w["discovery"]["end"])
    hold = R._ts(w["holdout"]["start"])
    ds = svc._cell_dataset(R.dataset_1m(svc, parent), (start, end), lock)
    nq = _minute_from_dataset(ds)
    if len(nq) and int(nq.ts[-1]) >= hold.value:
        raise MarketDataError("HOLDOUT_REACHED", "The discovery cut reaches the holdout; refused.")
    es, es_info = load_es(svc.data_root, start.value, min(end.value, hold.value - 1))
    src = {"nq": {"dataset_id": ds.manifest.dataset_id, "content_hash": ds.manifest.content_hash,
                  "instrument": ds.instrument.symbol, "provider": ds.manifest.provider, "price_basis": "bid"},
           "es": es_info, "window": {"start": start.isoformat(), "end": end.isoformat()},
           "holdout_start": hold.isoformat(), "protocol_id": parent["protocol_id"]}
    return Market(nq, es, start, end, src)


def align(nq: Minute, es: Minute) -> tuple[np.ndarray, np.ndarray]:
    """Indices (i_nq, i_es) of the minutes both series have (inner join on the bar open)."""
    common, a, b = np.intersect1d(nq.ts, es.ts, assume_unique=True, return_indices=True)
    return a, b


def first_hit(hi: np.ndarray, lo: np.ndarray, day: np.ndarray, start_idx, level, side,
              horizon_days: int | None = None) -> np.ndarray:
    """For each query: the first index j >= start_idx where ``hi[j] >= level`` (side +1) or ``lo[j] <= level`` (side -1)
    on any bar series (``day`` = its trading date per bar). -1 = never (within the data / ``horizon_days`` further
    trading dates). Searches the rest of the start day directly, then later days through each day's extreme."""
    start_idx = np.asarray(start_idx, dtype=np.int64)
    level = np.asarray(level, float)
    side = np.asarray(side, dtype=np.int8)
    out = np.full(len(start_idx), -1, dtype=np.int64)
    n = len(hi)
    if not n or not len(start_idx):
        return out
    dayi = np.asarray(day).astype(np.int64)
    dstarts = np.flatnonzero(np.r_[True, dayi[1:] != dayi[:-1]])
    dends = np.r_[dstarts[1:], n]
    dhigh = np.maximum.reduceat(hi, dstarts)
    dlow = np.minimum.reduceat(lo, dstarts)
    dpos = np.searchsorted(dstarts, np.clip(start_idx, 0, n - 1), side="right") - 1
    for q in range(len(start_idx)):
        i, x, s, d = start_idx[q], level[q], side[q], dpos[q]
        if i < 0 or i >= n or not np.isfinite(x):
            continue
        seg = hi[i:dends[d]] >= x if s > 0 else lo[i:dends[d]] <= x
        k = np.flatnonzero(seg)
        if len(k):
            out[q] = i + k[0]
            continue
        last = len(dstarts) if horizon_days is None else min(len(dstarts), d + 1 + horizon_days)
        if d + 1 >= last:
            continue
        hit = dhigh[d + 1:last] >= x if s > 0 else dlow[d + 1:last] <= x
        kd = np.flatnonzero(hit)
        if not len(kd):
            continue
        dd = d + 1 + kd[0]
        seg = hi[dstarts[dd]:dends[dd]] >= x if s > 0 else lo[dstarts[dd]:dends[dd]] <= x
        out[q] = dstarts[dd] + np.flatnonzero(seg)[0]
    return out


def first_touch(m: Minute, start_idx, level, side, horizon_days: int | None = None) -> np.ndarray:
    """``first_hit`` on 1-minute bars (high / low)."""
    return first_hit(m.h, m.l, m.day, start_idx, level, side, horizon_days)


def first_passage(m: Minute, start_idx: np.ndarray, entry: np.ndarray, up: np.ndarray, down: np.ndarray,
                  max_minutes: int) -> np.ndarray:
    """+1 if price reaches ``entry + up`` before ``entry - down`` (from minute start_idx on, within max_minutes),
    -1 if the lower level first, 0 if neither in time or both inside the same minute (unknown order)."""
    out = np.zeros(len(start_idx), dtype=np.int8)
    n = len(m)
    for q, i in enumerate(np.asarray(start_idx, dtype=np.int64)):
        if i < 0 or i >= n:
            continue
        j = min(n, i + max_minutes)
        hi = m.h[i:j] >= entry[q] + up[q]
        lo = m.l[i:j] <= entry[q] - down[q]
        a = np.flatnonzero(hi)
        b = np.flatnonzero(lo)
        fa = a[0] if len(a) else None
        fb = b[0] if len(b) else None
        if fa is None and fb is None:
            continue
        if fb is None or (fa is not None and fa < fb):
            out[q] = 1
        elif fa is None or fb < fa:
            out[q] = -1
    return out
