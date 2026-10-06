"""ICT / SMC concepts on every timeframe, with what happened afterwards (ADR-106). Everything is CAUSAL: a pattern
exists from the moment its last bar is complete (``known_ns``); every outcome is measured on 1-minute bars from that
moment on. Definitions are fixed here (single choices, written down, not tuned):

* FVG: three bars; bullish when bar 3's low > bar 1's high (zone = bar 1 high .. bar 3 low), bearish mirrored. Bars
  must be consecutive in time (a session break never makes a gap).
* Zone outcomes: TOUCHED (price enters the zone), CE (reaches its middle, the 'consequent encroachment'), FILLED
  (trades through its far side); minutes to each; LEFT BEHIND = never filled by the end of the data. HELD = after the
  first touch price moved 1 ATR away in the zone's direction before trading through the far side.
* IFVG: an FVG that a bar CLOSES through becomes an inverse FVG (opposite direction); same zone outcomes from then on.
* BPR: a bullish and a bearish FVG within 10 bars whose zones overlap; the overlap is the zone (direction = the later).
* Swings: a high (low) with 2 lower highs (higher lows) on each side; known when the 2nd bar after it completes.
* Sweep vs break of a level (swing, equal highs / lows, previous day / week, Asia, London, previous regular session):
  the first bar trading beyond it; SWEEP when that bar closes back inside, BREAK when it closes beyond.
* BOS / CHoCH: a close beyond the last swing high (low); BOS continues the current structure, CHoCH flips it.
  DISPLACEMENT = that bar's body >= 1.5 ATR.
* Order block: the last opposite-colour bar (within 10 bars) before a displacement BOS / CHoCH; its high-low is the
  zone. A close through it turns it into a BREAKER (opposite direction).
* OTE: after a BOS, the 62-79 % retracement of the leg (last opposite swing -> break bar extreme).
* Opening gaps: NDOG = previous session's last close -> the 18:00 open; NWOG = Friday's last close -> Sunday's open;
  RTH gap = previous 15:59 close -> 9:30 open.
* Opening range (9:30-9:45 and 9:30-10:00), Judas swing (first 30 min after 9:30 against the regular session's
  direction), Power of 3 (on up days the low before the high / on down days the high before the low), time of the
  day's high / low, 20 / 40 / 60-day range breaks (IPDA ranges).

Every event carries an EDGE OUTCOME for the edge scan: from its entry moment, did price move 1 ATR (of the pattern's
timeframe) in the pattern's direction before 1 ATR against it (+1 / -1; 0 = neither in time or both in one minute)?
"""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.market import data as D

SESSION_NAMES = ("Asia", "London", "NY pre-open", "NY AM", "NY lunch", "NY PM")
SILVER_BULLET = ((3 * 60, 4 * 60, "3-4 AM"), (10 * 60, 11 * 60, "10-11 AM"), (14 * 60, 15 * 60, "2-3 PM"))
DISPLACEMENT_ATR = 1.5
EQ_TOL_ATR = 0.1
OB_LOOKBACK = 10
SWEEP_LOOKAHEAD = 50          # bars a swing stays a target for the turtle-soup check
BPR_BARS = 10


def session_code(ny_min: np.ndarray) -> np.ndarray:
    m = np.asarray(ny_min)
    out = np.zeros(len(m), dtype=np.int8)                       # Asia 18:00 - 02:00
    out[(m >= 120) & (m < 420)] = 1                             # London 02:00 - 07:00
    out[(m >= 420) & (m < 570)] = 2                             # NY pre-open 07:00 - 09:30
    out[(m >= 570) & (m < 720)] = 3                             # NY AM 09:30 - 12:00
    out[(m >= 720) & (m < 810)] = 4                             # NY lunch 12:00 - 13:30
    out[(m >= 810) & (m < 1080)] = 5                            # NY PM 13:30 - 16:15
    return out


def ny_minutes(ts_ns) -> np.ndarray:
    idx = pd.DatetimeIndex(np.asarray(ts_ns, dtype=np.int64), tz="UTC").tz_convert(D.NY)
    return np.asarray(idx.hour * 60 + idx.minute, dtype=np.int32)


# =============================================================================================== event table
COLS = ("kind", "tf", "dir", "known_ns", "i_min", "entry_i", "day", "session", "top", "bottom", "atr", "size_atr",
        "touch_min", "ce_min", "fill_min", "held", "edge", "flag")


class Events:
    """Column store of pattern events (one row per pattern instance)."""

    def __init__(self):
        self.parts: list[dict] = []

    def add(self, **cols):
        n = len(cols["known_ns"])
        if n == 0:
            return
        row = {}
        for c in COLS:
            v = cols.get(c)
            if v is None:
                v = {"touch_min": np.nan, "ce_min": np.nan, "fill_min": np.nan, "held": 0, "edge": 0, "flag": "",
                     "entry_i": -1, "top": np.nan, "bottom": np.nan, "size_atr": np.nan, "atr": np.nan}.get(c, 0)
            row[c] = np.broadcast_to(np.asarray(v), (n,)).copy() if np.ndim(v) == 0 else np.asarray(v)
        self.parts.append(row)

    def frame(self) -> pd.DataFrame:
        if not self.parts:
            return pd.DataFrame(columns=list(COLS))
        f = pd.DataFrame({c: np.concatenate([p[c] for p in self.parts]) for c in COLS})
        f["kind"] = f["kind"].astype(str).astype("category")           # compact: few distinct names
        f["flag"] = f["flag"].astype(str).astype("category")
        return f


def _i_min(m: D.Minute, known_ns) -> np.ndarray:
    """First minute that opens at or after ``known_ns`` (the first moment a live decision can act)."""
    return np.searchsorted(m.ts, np.asarray(known_ns, dtype=np.int64), side="left").astype(np.int64)


def race(m: D.Minute, start: np.ndarray, up: np.ndarray, down: np.ndarray, max_minutes) -> np.ndarray:
    """+1 if high >= up before low <= down (from minute ``start`` on, within ``max_minutes``), -1 the other way,
    0 neither in time or both inside one minute."""
    start = np.asarray(start, dtype=np.int64)
    mm = np.broadcast_to(np.asarray(max_minutes, dtype=np.int64), start.shape)
    out = np.zeros(len(start), dtype=np.int8)
    n = len(m)
    for q in range(len(start)):
        i = start[q]
        if i < 0 or i >= n or not (np.isfinite(up[q]) and np.isfinite(down[q])):
            continue
        j = min(n, i + mm[q])
        a = np.flatnonzero(m.h[i:j] >= up[q])
        b = np.flatnonzero(m.l[i:j] <= down[q])
        fa = a[0] if len(a) else -1
        fb = b[0] if len(b) else -1
        if fa < 0 and fb < 0:
            continue
        if fb < 0 or (0 <= fa < fb):
            out[q] = 1
        elif fa < 0 or fb < fa:
            out[q] = -1
    return out


def edge_outcome(m: D.Minute, entry_i: np.ndarray, direction: np.ndarray, atr: np.ndarray, tf: int) -> np.ndarray:
    """+1 / -1 / 0: 1 ATR in ``direction`` before 1 ATR against it, from the OPEN of minute ``entry_i``, within
    max(30, 8 x tf) minutes (signed by direction: +1 = the pattern was right)."""
    entry_i = np.asarray(entry_i, dtype=np.int64)
    ok = (entry_i >= 0) & (entry_i < len(m))
    px = np.full(len(entry_i), np.nan)
    px[ok] = m.o[entry_i[ok]]
    r = race(m, entry_i, px + atr, px - atr, max(30, 8 * min(tf, 240)))
    return (r * np.sign(direction)).astype(np.int8)


def zone_outcomes(m: D.Minute, known_ns, direction, top, bottom, atr, tf: int) -> dict:
    """Touch / CE / fill minutes after ``known_ns`` for zones expected to hold (dir +1: support below, -1: resistance
    above), HELD (+1 / -1 / 0) after the first touch, and the edge outcome entered at the first touch."""
    known_ns = np.asarray(known_ns, dtype=np.int64)
    d = np.asarray(direction, dtype=np.int8)
    top, bottom, atr = (np.asarray(x, float) for x in (top, bottom, atr))
    i0 = _i_min(m, known_ns)
    near = np.where(d > 0, top, bottom)
    far = np.where(d > 0, bottom, top)
    mid = (top + bottom) / 2
    side = (-d).astype(np.int8)                     # bull zone is reached from above: low <= level
    out = {}
    for name, lvl in (("touch", near), ("ce", mid), ("fill", far)):
        j = D.first_touch(m, i0, lvl, side)
        mins = np.where(j >= 0, (m.ts[np.maximum(j, 0)] - known_ns) / D.MIN_NS, np.nan)
        out[name + "_min"] = mins
        out[name + "_i"] = j
    tj = out["touch_i"]
    # act AFTER the touch minute: that minute was chosen because it moved to the zone (measuring from its open would
    # build that move into the result); a live trader can act at the next minute's open.
    after = np.where((tj >= 0) & (tj + 1 < len(m)), tj + 1, -1)
    eps = np.maximum(0.25, 0.01 * np.abs(top - bottom))
    up = np.where(d > 0, near + atr, far + eps)
    dn = np.where(d > 0, far - eps, near - atr)
    held = race(m, after, up, dn, max(60, 20 * min(tf, 240)))
    out["held"] = (held * np.sign(d)).astype(np.int8)
    out["edge"] = edge_outcome(m, after, d, atr, tf)
    out["entry_i"] = after
    return out


def _consecutive(b: D.Bars, k: np.ndarray, span: int) -> np.ndarray:
    """Bars k-span .. k are consecutive buckets (no time gap; a session break is a gap)."""
    if b.tf == D.DAY:
        return np.ones(len(k), bool)
    return (b.ts[k] - b.ts[k - span]) == span * b.tf * D.MIN_NS


# =============================================================================================== FVG family
def fvgs(b: D.Bars, a: np.ndarray) -> dict:
    """Raw FVGs of one timeframe: index of the 3rd bar, direction, zone."""
    if len(b) < 3:
        return {"k": np.zeros(0, int), "dir": np.zeros(0, np.int8), "top": np.zeros(0), "bottom": np.zeros(0)}
    k = np.arange(2, len(b))
    bull = (b.l[k] > b.h[k - 2]) & _consecutive(b, k, 2)
    bear = (b.h[k] < b.l[k - 2]) & _consecutive(b, k, 2)
    kb, ks = k[bull], k[bear]
    kk = np.r_[kb, ks]
    order = np.argsort(kk, kind="stable")
    return {"k": kk[order], "dir": np.r_[np.ones(len(kb), np.int8), -np.ones(len(ks), np.int8)][order],
            "top": np.r_[b.l[kb], b.l[ks - 2]][order], "bottom": np.r_[b.h[kb - 2], b.h[ks]][order]}


def _close_through(b: D.Bars, k: np.ndarray, d: np.ndarray, top: np.ndarray, bottom: np.ndarray) -> np.ndarray:
    """First bar index after k whose CLOSE is beyond the zone's far side (bull: close < bottom). -1 = never."""
    far = np.where(d > 0, bottom, top)
    side = (-d).astype(np.int8)
    return D.first_hit(b.c, b.c, b.day, k + 1, far - np.where(d > 0, 1e-9, -1e-9), side)


def add_fvg_family(ev: Events, m: D.Minute, b: D.Bars, a: np.ndarray) -> None:
    f = fvgs(b, a)
    k, d, top, bot = f["k"], f["dir"], f["top"], f["bottom"]
    if not len(k):
        return
    at = a[k]
    known = b.known_ns[k]
    sess = session_code(ny_minutes(b.ts[k]))
    o = zone_outcomes(m, known, d, top, bot, at, b.tf)
    sb = np.full(len(k), "", dtype=object)
    nym = ny_minutes(b.ts[k])
    for lo_, hi_, name in SILVER_BULLET:
        sb[(nym >= lo_) & (nym < hi_)] = name
    disp = np.abs(b.c[k - 1] - b.o[k - 1]) / at
    flag = np.where(disp >= DISPLACEMENT_ATR, "displacement", "")
    flag = np.where(sb != "", np.char.add(np.char.add(flag.astype(str), np.where(flag != "", " ", "")), "silver bullet " + sb.astype(str)), flag)
    ev.add(kind="FVG", tf=b.tf, dir=d, known_ns=known, i_min=_i_min(m, known), day=b.day[k].astype(np.int64),
           session=sess, top=top, bottom=bot, atr=at, size_atr=(top - bot) / at, touch_min=o["touch_min"],
           ce_min=o["ce_min"], fill_min=o["fill_min"], held=o["held"], edge=o["edge"], entry_i=o["entry_i"], flag=flag)
    # inverse FVGs: closed through -> opposite direction from the close-through bar on
    j = _close_through(b, k, d, top, bot)
    ok = j >= 0
    if ok.any():
        jj, dd = j[ok], -d[ok]
        kn = b.known_ns[jj]
        o2 = zone_outcomes(m, kn, dd, top[ok], bot[ok], a[jj], b.tf)
        ev.add(kind="IFVG", tf=b.tf, dir=dd, known_ns=kn, i_min=_i_min(m, kn), day=b.day[jj].astype(np.int64),
               session=session_code(ny_minutes(b.ts[jj])), top=top[ok], bottom=bot[ok], atr=a[jj],
               size_atr=(top[ok] - bot[ok]) / a[jj], touch_min=o2["touch_min"], ce_min=o2["ce_min"],
               fill_min=o2["fill_min"], held=o2["held"], edge=o2["edge"], entry_i=o2["entry_i"])
    # balanced price ranges: opposite FVGs within BPR_BARS bars with overlapping zones
    rows = []
    for x in range(len(k)):
        for y in range(x + 1, len(k)):
            if k[y] - k[x] > BPR_BARS:
                break
            if d[y] == d[x]:
                continue
            t, bt = min(top[x], top[y]), max(bot[x], bot[y])
            if t > bt:
                rows.append((k[y], d[y], t, bt))
    if rows:
        kk, dd, tt, bb = (np.array(v) for v in zip(*rows))
        kn = b.known_ns[kk]
        o3 = zone_outcomes(m, kn, dd, tt, bb, a[kk], b.tf)
        ev.add(kind="BPR", tf=b.tf, dir=dd.astype(np.int8), known_ns=kn, i_min=_i_min(m, kn),
               day=b.day[kk].astype(np.int64), session=session_code(ny_minutes(b.ts[kk])), top=tt, bottom=bb,
               atr=a[kk], size_atr=(tt - bb) / a[kk], touch_min=o3["touch_min"], ce_min=o3["ce_min"],
               fill_min=o3["fill_min"], held=o3["held"], edge=o3["edge"], entry_i=o3["entry_i"])


# =============================================================================================== structure
def swings(b: D.Bars, n: int = 2) -> dict:
    """Swing highs / lows (fractal n each side), known when bar k+n is complete."""
    if len(b) < 2 * n + 1:
        e = np.zeros(0, int)
        return {"hi": e, "lo": e}
    k = np.arange(n, len(b) - n)
    hi = np.ones(len(k), bool)
    lo = np.ones(len(k), bool)
    for s in range(1, n + 1):
        hi &= (b.h[k] > b.h[k - s]) & (b.h[k] > b.h[k + s])
        lo &= (b.l[k] < b.l[k - s]) & (b.l[k] < b.l[k + s])
    return {"hi": k[hi], "lo": k[lo]}


def add_structure(ev: Events, m: D.Minute, b: D.Bars, a: np.ndarray) -> list[dict]:
    """BOS / CHoCH (with displacement flag), order blocks, breakers, OTE. Returns the breaks (for other uses)."""
    sw = swings(b)
    n = 2
    conf_hi = {int(k) + n: int(k) for k in sw["hi"]}     # confirmation bar -> swing bar
    conf_lo = {int(k) + n: int(k) for k in sw["lo"]}
    last_hi = last_lo = None
    trend = 0
    breaks = []
    for i in range(len(b)):
        if i in conf_hi:
            last_hi = conf_hi[i]
        if i in conf_lo:
            last_lo = conf_lo[i]
        if not np.isfinite(a[i]):
            continue
        if last_hi is not None and b.c[i] > b.h[last_hi] and last_hi < i:
            kind = "BOS" if trend >= 0 else "CHOCH"
            breaks.append({"i": i, "dir": 1, "kind": kind, "level": b.h[last_hi], "swing": last_hi,
                           "leg_from": last_lo, "disp": abs(b.c[i] - b.o[i]) / a[i] >= DISPLACEMENT_ATR})
            trend, last_hi = 1, None
        elif last_lo is not None and b.c[i] < b.l[last_lo] and last_lo < i:
            kind = "BOS" if trend <= 0 else "CHOCH"
            breaks.append({"i": i, "dir": -1, "kind": kind, "level": b.l[last_lo], "swing": last_lo,
                           "leg_from": last_hi, "disp": abs(b.c[i] - b.o[i]) / a[i] >= DISPLACEMENT_ATR})
            trend, last_lo = -1, None
    if not breaks:
        return breaks
    bi = np.array([x["i"] for x in breaks])
    bd = np.array([x["dir"] for x in breaks], dtype=np.int8)
    kn = b.known_ns[bi]
    ent = _i_min(m, kn)
    kinds = np.array([x["kind"] for x in breaks])
    flag = np.array(["displacement" if x["disp"] else "" for x in breaks], dtype=object)
    ev.add(kind=kinds, tf=b.tf, dir=bd, known_ns=kn, i_min=ent, entry_i=ent, day=b.day[bi].astype(np.int64),
           session=session_code(ny_minutes(b.ts[bi])), top=np.array([x["level"] for x in breaks]),
           bottom=np.array([x["level"] for x in breaks]), atr=a[bi], size_atr=np.abs(b.c[bi] - b.o[bi]) / a[bi],
           edge=edge_outcome(m, ent, bd, a[bi], b.tf), flag=flag)
    # order blocks (displacement breaks only) and breakers
    obs = []
    for x in breaks:
        if not x["disp"]:
            continue
        i = x["i"]
        for j in range(i - 1, max(-1, i - 1 - OB_LOOKBACK), -1):
            opp = b.c[j] < b.o[j] if x["dir"] > 0 else b.c[j] > b.o[j]
            if opp:
                obs.append((i, j, x["dir"]))
                break
    if obs:
        oi, oj, od = (np.array(v) for v in zip(*obs))
        od = od.astype(np.int8)
        top, bot = b.h[oj], b.l[oj]
        kn = b.known_ns[oi]
        o = zone_outcomes(m, kn, od, top, bot, a[oi], b.tf)
        ev.add(kind="OB", tf=b.tf, dir=od, known_ns=kn, i_min=_i_min(m, kn), day=b.day[oi].astype(np.int64),
               session=session_code(ny_minutes(b.ts[oi])), top=top, bottom=bot, atr=a[oi], size_atr=(top - bot) / a[oi],
               touch_min=o["touch_min"], ce_min=o["ce_min"], fill_min=o["fill_min"], held=o["held"], edge=o["edge"],
               entry_i=o["entry_i"])
        j = _close_through(b, oi, od, top, bot)
        ok = j >= 0
        if ok.any():
            jj, dd = j[ok], -od[ok]
            kn2 = b.known_ns[jj]
            o2 = zone_outcomes(m, kn2, dd, top[ok], bot[ok], a[jj], b.tf)
            ev.add(kind="BREAKER", tf=b.tf, dir=dd, known_ns=kn2, i_min=_i_min(m, kn2), day=b.day[jj].astype(np.int64),
                   session=session_code(ny_minutes(b.ts[jj])), top=top[ok], bottom=bot[ok], atr=a[jj],
                   size_atr=(top[ok] - bot[ok]) / a[jj], touch_min=o2["touch_min"], ce_min=o2["ce_min"],
                   fill_min=o2["fill_min"], held=o2["held"], edge=o2["edge"], entry_i=o2["entry_i"])
    # OTE: retracement into 62-79 % of the leg, then continuation (new extreme) before the leg's origin
    rows = []
    for x in breaks:
        if x["leg_from"] is None:
            continue
        i = x["i"]
        if x["dir"] > 0:
            hi_, lo_ = float(b.h[x["swing"]:i + 1].max()), float(b.l[x["leg_from"]])
            if hi_ <= lo_:
                continue
            rows.append((i, 1, hi_ - 0.62 * (hi_ - lo_), hi_ - 0.79 * (hi_ - lo_), hi_, lo_))
        else:
            lo_, hi_ = float(b.l[x["swing"]:i + 1].min()), float(b.h[x["leg_from"]])
            if hi_ <= lo_:
                continue
            rows.append((i, -1, lo_ + 0.79 * (hi_ - lo_), lo_ + 0.62 * (hi_ - lo_), lo_, hi_))
    if rows:
        ri, rd, ztop, zbot, ext, origin = (np.array(v) for v in zip(*rows))
        rd = rd.astype(np.int8)
        kn = b.known_ns[ri]
        i0 = _i_min(m, kn)
        near = np.where(rd > 0, ztop, zbot)
        tj = D.first_touch(m, i0, near, (-rd).astype(np.int8))
        nj = D.first_touch(m, i0, ext + np.where(rd > 0, 0.25, -0.25), rd)     # new extreme first = no retracement
        entered = (tj >= 0) & ((nj < 0) | (tj < nj)) & (tj + 1 < len(m))
        tj = np.where(entered, tj + 1, -1)                 # act after the touch minute (see zone_outcomes)
        up = np.where(rd > 0, ext, origin)
        dn = np.where(rd > 0, origin, ext)
        res = race(m, tj, up, dn, 5 * 1440)
        cont = (res * rd).astype(np.int8)
        ev.add(kind="OTE", tf=b.tf, dir=rd, known_ns=kn, i_min=i0, entry_i=tj,
               day=b.day[ri].astype(np.int64), session=session_code(ny_minutes(b.ts[ri])), top=ztop, bottom=zbot,
               atr=a[ri], size_atr=np.abs(ext - origin) / a[ri],
               touch_min=np.where(entered, (m.ts[np.maximum(tj - 1, 0)] - kn) / D.MIN_NS, np.nan), held=cont,
               edge=edge_outcome(m, tj, rd, a[ri], b.tf))
    return breaks


def _sweep_events(ev: Events, m: D.Minute, b: D.Bars, a: np.ndarray, kind: str, start_bar, end_bar, level, side,
                  known_ns) -> None:
    """For each level (side +1 = a high above, -1 = a low below): the first bar in [start_bar, end_bar] trading beyond
    it; SWEEP (closed back inside -> reversal expected) or BREAK (closed beyond -> continuation expected)."""
    start_bar, end_bar = np.asarray(start_bar, np.int64), np.asarray(end_bar, np.int64)
    level, side = np.asarray(level, float), np.asarray(side, np.int8)
    j = D.first_hit(b.h, b.l, b.day, start_bar, level + side * 1e-9, side)
    ok = (j >= 0) & (j <= end_bar)
    if not ok.any():
        return
    j, lv, sd, kn0 = j[ok], level[ok], side[ok], np.asarray(known_ns, np.int64)[ok]
    closed_back = np.where(sd > 0, b.c[j] < lv, b.c[j] > lv)
    d = np.where(closed_back, -sd, sd).astype(np.int8)                 # sweep: reversal; break: continuation
    kn = b.known_ns[j]
    ent = _i_min(m, kn)
    flag = np.where(closed_back, "sweep", "break")
    ev.add(kind=kind, tf=b.tf, dir=d, known_ns=kn, i_min=ent, entry_i=ent, day=b.day[j].astype(np.int64),
           session=session_code(ny_minutes(b.ts[j])), top=lv, bottom=lv, atr=a[j],
           size_atr=np.abs(np.where(sd > 0, b.h[j], b.l[j]) - lv) / a[j],
           touch_min=(b.ts[j] - kn0) / D.MIN_NS, edge=edge_outcome(m, ent, d, a[j], b.tf), flag=flag)


def add_swing_sweeps(ev: Events, m: D.Minute, b: D.Bars, a: np.ndarray) -> None:
    sw = swings(b)
    for key, side in (("hi", 1), ("lo", -1)):
        k = sw[key]
        if not len(k):
            continue
        lvl = b.h[k] if side > 0 else b.l[k]
        start = k + 3
        _sweep_events(ev, m, b, a, "SWING_SWEEP", start, start + SWEEP_LOOKAHEAD, lvl, np.full(len(k), side),
                      b.known_ns[np.minimum(k + 2, len(b) - 1)])
        # equal highs / lows: two consecutive swings within 0.1 ATR
        if len(k) >= 2:
            k1, k2 = k[:-1], k[1:]
            close = np.abs(lvl[1:] - lvl[:-1]) <= EQ_TOL_ATR * a[k2]
            if close.any():
                kk = k2[close]
                lv = np.maximum(lvl[1:], lvl[:-1])[close] if side > 0 else np.minimum(lvl[1:], lvl[:-1])[close]
                _sweep_events(ev, m, b, a, "EQUAL_HL", kk + 3, kk + 3 + 5 * SWEEP_LOOKAHEAD, lv,
                              np.full(len(kk), side), b.known_ns[np.minimum(kk + 2, len(b) - 1)])


# =============================================================================================== daily references
def day_levels(m: D.Minute) -> dict:
    """Per trading date (aligned to the unique days of ``m``): reference levels known BEFORE (or during) the day."""
    days = np.unique(m.day)
    di = np.searchsorted(days, m.day)
    nd = len(days)

    def agg(mask):
        hi = np.full(nd, np.nan)
        lo = np.full(nd, np.nan)
        if mask.any():
            np.fmax.at(hi, di[mask], m.h[mask])
            np.fmin.at(lo, di[mask], m.l[mask])
        return hi, lo
    full_hi, full_lo = agg(np.ones(len(m), bool))
    asia_hi, asia_lo = agg((m.ny_min >= 1080) | (m.ny_min < 120))
    lon_hi, lon_lo = agg((m.ny_min >= 120) & (m.ny_min < 420))
    rth = (m.ny_min >= 570) & (m.ny_min < 960)
    rth_hi, rth_lo = agg(rth)
    week = (days.astype("datetime64[D]").astype(np.int64) - 4) // 7          # Monday-based week number (1970-01-05 = Monday)
    pw_hi, pw_lo = np.full(nd, np.nan), np.full(nd, np.nan)
    wk_hi, wk_lo = {}, {}
    for i in range(nd):
        w = week[i]
        prev = wk_hi.get(w - 1)
        if prev is not None:
            pw_hi[i], pw_lo[i] = prev, wk_lo[w - 1]
        wk_hi[w] = max(wk_hi.get(w, -np.inf), full_hi[i])
        wk_lo[w] = min(wk_lo.get(w, np.inf), full_lo[i])
    shift = lambda x: np.r_[np.nan, x[:-1]]
    return {"days": days, "di": di, "pdh": shift(full_hi), "pdl": shift(full_lo), "pwh": pw_hi, "pwl": pw_lo,
            "asia_hi": asia_hi, "asia_lo": asia_lo, "lon_hi": lon_hi, "lon_lo": lon_lo,
            "prth_hi": shift(rth_hi), "prth_lo": shift(rth_lo), "hi": full_hi, "lo": full_lo}


REF_LEVELS = (  # (kind, high key, low key, NY minute from which the level is known)
    ("PREV_DAY", "pdh", "pdl", 18 * 60),
    ("PREV_WEEK", "pwh", "pwl", 18 * 60),
    ("PREV_RTH", "prth_hi", "prth_lo", 18 * 60),
    ("ASIA", "asia_hi", "asia_lo", 120),
    ("LONDON", "lon_hi", "lon_lo", 420),
)


def add_level_sweeps(ev: Events, m: D.Minute, b: D.Bars, a: np.ndarray, lv: dict) -> None:
    """Sweeps / breaks of the day's reference levels on this timeframe (only bars complete after the level is known)."""
    if b.tf == D.DAY:
        return
    days = lv["days"]
    bdi = np.searchsorted(days, b.day)
    dstart = np.searchsorted(bdi, np.arange(len(days)), side="left")
    dend = np.searchsorted(bdi, np.arange(len(days)), side="right") - 1
    bny = ny_minutes(b.ts)
    for kind, khi, klo, from_min in REF_LEVELS:
        for key, side in ((khi, 1), (klo, -1)):
            vals = lv[key]
            st, en, lvls, known = [], [], [], []
            for d in range(len(days)):
                if not np.isfinite(vals[d]) or dstart[d] > dend[d]:
                    continue
                seg = np.arange(dstart[d], dend[d] + 1)
                # bars that START at or after the level is final (session clock: 18:00 = 0)
                sm = (bny[seg] - 1080) % 1440
                ok = seg[sm >= (from_min - 1080) % 1440]
                if not len(ok):
                    continue
                st.append(ok[0])
                en.append(dend[d])
                lvls.append(vals[d])
                known.append(b.ts[ok[0]])
            if st:
                _sweep_events(ev, m, b, a, "SWEEP_" + kind, np.array(st), np.array(en), np.array(lvls),
                              np.full(len(st), side), np.array(known))


def add_day_patterns(ev: Events, m: D.Minute, lv: dict, a_day: dict) -> dict:
    """Opening gaps, opening range, Judas swing, Power of 3, time of high / low, IPDA range breaks. Returns per-day
    facts for the trend statistics."""
    days, di = lv["days"], lv["di"]
    nd = len(days)
    starts = np.searchsorted(di, np.arange(nd), side="left")
    ends = np.searchsorted(di, np.arange(nd), side="right") - 1
    atr15 = a_day["atr15"]                      # 15m ATR per minute index (for edge outcomes of daily patterns)
    facts = {"day": days, "rth_open": np.full(nd, np.nan), "rth_close": np.full(nd, np.nan),
             "rth_hi": np.full(nd, np.nan), "rth_lo": np.full(nd, np.nan), "t_hi": np.full(nd, np.nan),
             "t_lo": np.full(nd, np.nan), "open": np.full(nd, np.nan), "close": np.full(nd, np.nan)}
    gaps = []
    orb = []
    judas = []
    for d in range(nd):
        s, e = starts[d], ends[d]
        if s > e:
            continue
        facts["open"][d], facts["close"][d] = m.o[s], m.c[e]
        seg = slice(s, e + 1)
        ny = m.ny_min[seg]
        rth = np.flatnonzero((ny >= 570) & (ny < 960)) + s
        if d > 0 and starts[d - 1] <= ends[d - 1]:                    # NDOG / NWOG
            pc = m.c[ends[d - 1]]
            kind = "NWOG" if m.weekday[s] == 0 else "NDOG"
            if m.o[s] != pc:
                gaps.append((kind, s, pc, m.o[s]))
        if len(rth) >= 350:
            r0, r1 = rth[0], rth[-1]
            if m.ny_min[r0] != 570:
                continue
            facts["rth_open"][d], facts["rth_close"][d] = m.o[r0], m.c[r1]
            hi_i = r0 + int(np.argmax(m.h[r0:r1 + 1]))
            lo_i = r0 + int(np.argmin(m.l[r0:r1 + 1]))
            facts["rth_hi"][d], facts["rth_lo"][d] = m.h[hi_i], m.l[lo_i]
            facts["t_hi"][d], facts["t_lo"][d] = m.ny_min[hi_i], m.ny_min[lo_i]
            if d > 0 and np.isfinite(facts["rth_close"][d - 1]) and m.o[r0] != facts["rth_close"][d - 1]:
                gaps.append(("RTH_GAP", r0, facts["rth_close"][d - 1], m.o[r0]))
            for width in (15, 30):                                     # opening range
                w = rth[:width]
                if len(w) < width or m.ny_min[w[-1]] != 570 + width - 1:
                    continue
                oh, ol = m.h[w].max(), m.l[w].min()
                orb.append((width, w[-1] + 1, oh, ol, r1))
            j30 = rth[:30]
            if len(j30) == 30:
                judas.append((d, r0, j30[-1], r1))
    # gaps: toward the fill is the expected direction (dir = sign of previous close - open)
    if gaps:
        kinds = np.array([g[0] for g in gaps])
        gi = np.array([g[1] for g in gaps])
        pc = np.array([g[2] for g in gaps])
        op = np.array([g[3] for g in gaps])
        dd = np.sign(pc - op).astype(np.int8)
        kn = m.ts[gi]
        fill_j = D.first_touch(m, gi, pc, dd)
        ce_j = D.first_touch(m, gi, (pc + op) / 2, dd)                  # half the gap closed
        top, bot = np.maximum(pc, op), np.minimum(pc, op)
        ev.add(kind=kinds, tf=1, dir=dd, known_ns=kn, i_min=gi, entry_i=gi, day=m.day[gi].astype(np.int64),
               session=session_code(m.ny_min[gi]), top=top, bottom=bot, atr=atr15[gi], size_atr=(top - bot) / atr15[gi],
               fill_min=np.where(fill_j >= 0, (m.ts[np.maximum(fill_j, 0)] - kn) / D.MIN_NS, np.nan),
               ce_min=np.where(ce_j >= 0, (m.ts[np.maximum(ce_j, 0)] - kn) / D.MIN_NS, np.nan),
               touch_min=np.zeros(len(gi)), edge=edge_outcome(m, gi, dd, atr15[gi], 15))
    if orb:
        width, i0, oh, ol, r1 = (np.array(v) for v in zip(*orb))
        up_j = np.array([D.first_touch(m, [i], [h_ + 1e-9], [1])[0] for i, h_ in zip(i0, oh)])
        dn_j = np.array([D.first_touch(m, [i], [l_ - 1e-9], [-1])[0] for i, l_ in zip(i0, ol)])
        up_j = np.where((up_j >= 0) & (up_j <= r1), up_j, -1)
        dn_j = np.where((dn_j >= 0) & (dn_j <= r1), dn_j, -1)
        first_up = (up_j >= 0) & ((dn_j < 0) | (up_j < dn_j))
        first_dn = (dn_j >= 0) & ((up_j < 0) | (dn_j < up_j))
        broke = first_up | first_dn
        d = np.where(first_up, 1, -1).astype(np.int8)
        bj = np.where(first_up, up_j, dn_j)
        both = (up_j >= 0) & (dn_j >= 0)
        ent = np.where(broke, np.minimum(bj + 1, len(m) - 1), -1)
        kinds = np.where(width == 15, "OPENING_RANGE_15", "OPENING_RANGE_30")
        flag = np.where(both, "both sides broken", np.where(broke, "one side only", "no break"))
        kn = np.where(broke, m.ts[np.maximum(bj, 0)] + D.MIN_NS, m.ts[np.minimum(i0, len(m) - 1)])
        ev.add(kind=kinds, tf=15, dir=d, known_ns=kn, i_min=ent, entry_i=ent, day=m.day[np.minimum(i0, len(m) - 1)].astype(np.int64),
               session=3, top=oh, bottom=ol, atr=atr15[np.minimum(i0, len(m) - 1)],
               size_atr=(oh - ol) / atr15[np.minimum(i0, len(m) - 1)],
               held=np.where(both, -1, np.where(broke, 1, 0)).astype(np.int8),
               edge=edge_outcome(m, ent, d, atr15[np.minimum(i0, len(m) - 1)], 15), flag=flag)
    if judas:
        dd_, r0, j1, r1 = (np.array(v) for v in zip(*judas))
        first = np.sign(m.c[j1] - m.o[r0])
        day_dir = np.sign(m.c[r1] - m.o[r0])
        ok = (first != 0) & (day_dir != 0)
        ent = np.minimum(j1 + 1, len(m) - 1)
        d = (-first).astype(np.int8)                                   # Judas: the first 30 min fake the day's move
        ev.add(kind=np.full(int(ok.sum()), "JUDAS_30"), tf=30, dir=d[ok], known_ns=m.ts[ent[ok]], i_min=ent[ok],
               entry_i=ent[ok], day=m.day[r0[ok]].astype(np.int64), session=3, atr=atr15[ent[ok]],
               held=(first[ok] != day_dir[ok]).astype(np.int8) * 2 - 1, edge=edge_outcome(m, ent[ok], d[ok],
                                                                                          atr15[ent[ok]], 15))
    # IPDA ranges: first trade beyond the previous 20 / 40 / 60 trading dates' high / low (daily references)
    for look in (20, 40, 60):
        hi = pd.Series(lv["hi"]).rolling(look, min_periods=look).max().shift(1).to_numpy()
        lo = pd.Series(lv["lo"]).rolling(look, min_periods=look).min().shift(1).to_numpy()
        for vals, side in ((hi, 1), (lo, -1)):
            sel = [d for d in range(nd) if np.isfinite(vals[d]) and starts[d] <= ends[d]]
            if not sel:
                continue
            st = starts[sel]
            j = D.first_touch(m, st, vals[sel] + side * 1e-9, np.full(len(sel), side), horizon_days=0)
            ok = (j >= 0) & (j <= ends[sel])
            if not ok.any():
                continue
            jj = j[ok]
            ent = np.minimum(jj + 1, len(m) - 1)
            dd = np.full(len(jj), side, dtype=np.int8)
            ev.add(kind=f"IPDA_{look}", tf=D.DAY, dir=dd, known_ns=m.ts[jj] + D.MIN_NS, i_min=ent, entry_i=ent,
                   day=m.day[jj].astype(np.int64), session=session_code(m.ny_min[jj]), top=vals[sel][ok],
                   bottom=vals[sel][ok], atr=atr15[jj], edge=edge_outcome(m, ent, dd, atr15[jj], 15))
    return facts


def detect_tf(m: D.Minute, b: D.Bars, a: np.ndarray, lv: dict) -> tuple[pd.DataFrame, list[dict]]:
    """Every pattern of one timeframe (FVG family, structure, order blocks, breakers, OTE, sweeps). Returns the events
    and the breaks of structure (for the context of lower timeframes)."""
    ev = Events()
    add_fvg_family(ev, m, b, a)
    breaks = add_structure(ev, m, b, a)
    add_swing_sweeps(ev, m, b, a)
    add_level_sweeps(ev, m, b, a, lv)
    for x in breaks:
        x["known_ns"] = int(b.known_ns[x["i"]])
    return _clean(ev.frame()), breaks


def atr15_per_minute(m: D.Minute, b15: D.Bars) -> np.ndarray:
    """The ATR of the last COMPLETE 15-minute bar at every minute (NaN before the first)."""
    a15 = D.atr(b15)
    if not len(b15):
        return np.full(len(m), np.nan)
    j = np.searchsorted(b15.known_ns, m.ts, side="right") - 1
    return np.where(j >= 0, a15[np.maximum(j, 0)], np.nan)


def _clean(f: pd.DataFrame) -> pd.DataFrame:
    a = f["atr"].astype(float)
    return f[np.isfinite(a) & (a > 0)].reset_index(drop=True)


def detect_all(m: D.Minute, tfs=D.TIMEFRAMES, progress=None) -> tuple[pd.DataFrame, dict, dict]:
    """Every pattern on every timeframe in one table (small data / tests). Returns (events, per-day facts, bars)."""
    bars = D.all_timeframes(m, tuple(tfs) + (D.DAY,))
    lv = day_levels(m)
    parts = []
    for tf in tuple(tfs) + (D.DAY,):
        if progress:
            progress(f"Patterns on the {D.TF_LABEL[tf]} chart")
        f, _ = detect_tf(m, bars[tf], D.atr(bars[tf]), lv)
        parts.append(f)
    ev = Events()
    facts = add_day_patterns(ev, m, lv, {"atr15": atr15_per_minute(m, bars[15])})
    parts.append(_clean(ev.frame()))
    return pd.concat(parts, ignore_index=True), facts, bars


# =============================================================================================== statistics
def summarize(ev: pd.DataFrame, end_ns: int, n_days: int) -> list[dict]:
    """One row per (kind, timeframe, direction): how often, fill / hold rates, time to fill, left behind."""
    rows = []
    if ev.empty:
        return rows
    for (kind, tf, d), g in ev.groupby(["kind", "tf", "dir"], sort=True):
        n = len(g)
        fill = g["fill_min"].astype(float)
        touch = g["touch_min"].astype(float)
        ce = g["ce_min"].astype(float)
        held = g["held"].astype(int)
        edge = g["edge"].astype(int)
        flags = g["flag"].astype(str)
        r = {"kind": kind, "tf": int(tf), "dir": int(d), "n": int(n), "per_day": n / max(1, n_days),
             "touched": float(np.isfinite(touch).mean()), "ce": float(np.isfinite(ce).mean()),
             "filled": float(np.isfinite(fill).mean()),
             "filled_1h": float((fill <= 60).mean()), "filled_1d": float((fill <= 1440).mean()),
             "filled_5d": float((fill <= 7200).mean()),
             "median_touch_min": _med(touch), "median_fill_min": _med(fill),
             "left_behind": int((~np.isfinite(fill)).sum()),
             "left_behind_median_age_days": _med((end_ns - g["known_ns"][~np.isfinite(fill)].astype(np.int64)) / 864e11),
             "held": _rate(held), "held_n": int((held != 0).sum()),
             "edge": _rate(edge), "edge_n": int((edge != 0).sum()),
             "median_size_atr": _med(g["size_atr"].astype(float)),
             "by_session": [{"session": SESSION_NAMES[int(s)], "n": int(len(gs)), "edge": _rate(gs["edge"].astype(int)),
                             "filled": float(np.isfinite(gs["fill_min"].astype(float)).mean()),
                             "held": _rate(gs["held"].astype(int))}
                            for s, gs in g.groupby("session")]}
        fl = flags[flags != ""]
        if len(fl):
            r["flags"] = {k: {"n": int(v), "edge": _rate(g["edge"][flags == k].astype(int))}
                          for k, v in fl.value_counts().head(8).items()}
        rows.append(r)
    return rows


def _rate(x) -> float | None:
    x = np.asarray(x)
    nz = x[x != 0]
    return float(np.mean(nz > 0)) if len(nz) else None


def _med(x) -> float | None:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    return float(np.median(x)) if len(x) else None
