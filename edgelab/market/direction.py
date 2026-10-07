"""Direction calls (ADR-109): which way does the REST of a 15-minute candle go - called at its open and again at
minute 5 and minute 10 - and only when confident.

Why: the ADR-106 / 107 forecaster gives every candle a direction probability at its open; on discovery and on the holdout
it is right ~51 % of the time (the 'always up' baseline: 51.3 %). The discovery edge scan did find direction information
INSIDE the 15-minute candle (after a 1-5 minute FVG / IFVG / BOS the rest of the candle went against it ~54-55 %, found
on the first 70 %, confirmed on the last 30 %) and in rare higher-timeframe situations. So:

* DECISIONS at minute 0, 5 and 10 of every complete 15-minute candle; the target is the REST of the candle: does it close
  above the price at that moment (the open of that minute)? Never 'does the candle close green' from the middle of the
  candle (that is mostly the move already made).
* INPUTS (everything known before the decision; a bar / pattern counts from the end of its last minute):
  - every forecast input (forecast.features: trends 15m ... 1D, levels, FVGs, ES, SMT, news, shocks, last candles);
  - INSIDE-CANDLE micro reactions: FVGs, inverse FVGs (closed through within 10 bars), BOS, CHoCH and FVG fills (traded
    through within 30 minutes) of the 1m, 3m and 5m charts - signed sums inside the current candle so far and inside the
    previous candle; the last micro event and its age; the candle so far (move, range, where price sits in it, a sweep
    of the previous candle's high / low); the previous candle's wicks and close position and whether it swept the
    candle before it;
  - SWEEP -> SHIFT -> FVG sequences: a liquidity level (previous week / day / session high or low, Asia, London, untaken
    1h / 4h swing, equal highs / lows of 15m / 1h) traded through and a 1-minute close back inside within 15 minutes
    (= swept), then a 1-5 minute BOS / CHoCH the other way, then an FVG that way: stage (1-3, signed), the level's weight,
    age, how far price moved from the level; swept liquidity of the last 2 hours (weighted);
  - RESTING FVGs as magnets: per chart (5m, 15m, 1h, 4h, 1D) the distance to the nearest open FVG above / below (its next
    untraded part, like the level map) and how many lie within 3 ATR; the balance of the nearest above vs below.
* MODELS, walk-forward, baseline (the training period's up-rate) and the 70 / 30 model choice: forecast.py (per stage).
* CALLS: a call is made only when the chosen model's probability is at least tau away from 50 %. tau per stage is chosen
  on the EARLY 70 % of the predicted months among 55 % ... 80 % (the best lower Wilson bound of the call accuracy, at
  least 100 calls, one-sided 5 % corrected for the thresholds tried; no calls at all for a stage when that bound is not
  above 50 %);
  the later 30 % report how often it calls and how often it is right, with a day-bootstrap interval, against chance and
  against the baseline on the same candles.
Nothing here is a run, a try or a holdout look; the second holdout look (holdout_run) is separate and recorded.
"""
from __future__ import annotations

import json
import math
from pathlib import Path

import numpy as np

from edgelab.market import data as D
from edgelab.market import forecast as F
from edgelab.market import patterns as P

DIRECTION_VERSION = 1
STAGES = (0, 5, 10)                                         # minutes into the 15-minute candle
STAGE_WORDS = {0: "at the open", 5: "at minute 5", 10: "at minute 10"}
MICRO_TFS = (1, 3, 5)
STREAMS = ("fvg", "ifvg", "bos", "choch", "fill")
SEQ_WINDOW_MIN = 120
SWEEP_BACK_MIN = 15
MAGNET_TFS = (5, 15, 60, 240, D.DAY)
TAU_GRID = tuple(round(float(x), 3) for x in np.arange(0.05, 0.301, 0.01))  # a call = 55 % or more one way
TAU_Z = float(__import__('statistics').NormalDist().inv_cdf(1 - 0.05 / len(TAU_GRID)))  # corrected for the thresholds tried
MIN_CALLS = 100
GBM_ROWS = 60_000
SIM_INPUTS = ("stage_5", "stage_10", "so_move", "so_pos", "prev_upper_wick", "prev_lower_wick", "prev_close_pos",
              "seq_signal", "cur_fvg_1m", "cur_bos_1m", "prev_fvg_1m", "magnet_balance", "trend_1h", "move60")
WORDS = {"stage_5": "called at minute 5", "stage_10": "called at minute 10", "so_move": "the candle's move so far",
         "so_range": "the candle's range so far", "so_pos": "where price sits in the candle so far",
         "so_sweep": "the candle swept the previous candle's high / low", "prev_upper_wick": "previous candle's upper wick",
         "prev_lower_wick": "previous candle's lower wick", "prev_close_pos": "where the previous candle closed",
         "prev_sweep": "previous candle swept the one before", "last_micro_dir": "the last 1-5 min pattern",
         "last_micro_age": "age of the last 1-5 min pattern", "seq_signal": "sweep -> shift -> FVG sequence",
         "seq_weight": "weight of the swept liquidity", "seq_age": "age of the sweep", "seq_step_age": "age of its last step",
         "seq_from_level": "move away from the swept level", "swept_2h": "liquidity swept in the last 2 hours",
         "magnet_balance": "nearest resting FVG above vs below", "magnet_stack": "resting FVGs within 3 ATR above vs below"}
BIG = np.iinfo(np.int64).max


def input_word(name: str) -> str:
    if name in WORDS:
        return WORDS[name]
    for w in ("cur_", "prev_"):
        if name.startswith(w) and name.count("_") == 2:
            _, kind, tf = name.split("_")
            return f"{tf} {kind.upper() if kind != 'fill' else 'FVG fills'} {'this candle' if w == 'cur_' else 'previous candle'}"
    if name.startswith("mag_"):
        _, tf, what = name.split("_", 2)
        return {"above": f"nearest open {tf} FVG above", "below": f"nearest open {tf} FVG below",
                "n_above": f"open {tf} FVGs within 3 ATR above", "n_below": f"open {tf} FVGs within 3 ATR below"}.get(what, name)
    return F.input_word(name)


# =============================================================================================== micro events
def _breaks(b: D.Bars, a: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """BOS / CHoCH of one chart exactly as patterns.add_structure (close beyond the last confirmed swing), without its
    outcome measurements: (bar index, direction, is_choch)."""
    sw = P.swings(b)
    n = len(b)
    conf_hi = np.full(n + 3, -1, np.int64)
    conf_lo = np.full(n + 3, -1, np.int64)
    conf_hi[sw["hi"] + 2] = sw["hi"]
    conf_lo[sw["lo"] + 2] = sw["lo"]
    last_hi = last_lo = -1
    trend = 0
    oi, od, oc = [], [], []
    c, h, lo = b.c, b.h, b.l
    fin = np.isfinite(a)
    for i in range(n):
        if conf_hi[i] >= 0:
            last_hi = conf_hi[i]
        if conf_lo[i] >= 0:
            last_lo = conf_lo[i]
        if not fin[i]:
            continue
        if last_hi >= 0 and c[i] > h[last_hi] and last_hi < i:
            oi.append(i), od.append(1), oc.append(trend < 0)
            trend, last_hi = 1, -1
        elif last_lo >= 0 and c[i] < lo[last_lo] and last_lo < i:
            oi.append(i), od.append(-1), oc.append(trend > 0)
            trend, last_lo = -1, -1
    return np.array(oi, np.int64), np.array(od, np.int8), np.array(oc, bool)


def _sorted(kn, val):
    kn = np.asarray(kn, np.int64)
    val = np.asarray(val, float)
    o = np.argsort(kn, kind="stable")
    return kn[o], val[o], np.r_[0.0, np.cumsum(val[o])]


def _wsum(stream, lo, hi) -> np.ndarray:
    """Sum of the stream's values known in [lo, hi] (known at or before hi)."""
    kn, _, cs = stream
    if not len(kn):
        return np.zeros(len(lo))
    return cs[np.searchsorted(kn, hi, side="right")] - cs[np.searchsorted(kn, lo, side="left")]


class Micro:
    """Micro events (1m / 3m / 5m), liquidity sweeps and resting FVG zones, computed once for a minute series."""

    def __init__(self, cx: F.Context, src=None):
        from edgelab.market import levelmap as L
        m = cx.m
        self.cx = cx
        self.src = src if src is not None else L.Source(cx)
        self.streams: dict = {}
        brk_up, brk_dn, fv_up, fv_dn = [], [], [], []
        allk, alld = [], []
        for tf in MICRO_TFS:
            b = cx.bars[tf] if tf in cx.bars else D.resample(m, tf)
            a = cx.atr[tf] if tf in cx.atr else D.atr(b)
            f = P.fvgs(b, a)
            k, d = f["k"], f["dir"].astype(float)
            kn = b.known_ns[k].astype(np.int64)
            self.streams[("fvg", tf)] = _sorted(kn, d)
            fv_up.append(kn[d > 0]), fv_dn.append(kn[d < 0])
            allk.append(kn), alld.append(d)
            # FVG fills within 30 minutes (traded through its far edge): known when that minute ends; value -dir
            far = np.where(d > 0, f["bottom"], f["top"])
            i0 = np.searchsorted(m.ts, kn, side="left")
            fill = np.full(len(k), -1, np.int64)
            for off in range(30):
                j = i0 + off
                okj = (j < len(m)) & (fill < 0)
                jc = np.minimum(j, len(m) - 1)
                hit = okj & (m.day[jc] == m.day[np.minimum(i0, len(m) - 1)]) & \
                    np.where(d > 0, m.l[jc] <= far, m.h[jc] >= far)
                fill[hit] = jc[hit]
            okf = fill >= 0
            self.streams[("fill", tf)] = _sorted(m.ts[fill[okf]] + D.MIN_NS, -d[okf])
            # inverse FVGs: a bar of this chart closes through the far edge within 10 bars
            inv = np.full(len(k), -1, np.int64)
            for off in range(1, 11):
                j = k + off
                okj = (j < len(b)) & (inv < 0)
                jc = np.minimum(j, len(b) - 1)
                hit = okj & ((b.ts[jc] - b.ts[k]) == off * tf * D.MIN_NS) & np.where(d > 0, b.c[jc] < far, b.c[jc] > far)
                inv[hit] = jc[hit]
            oki = inv >= 0
            self.streams[("ifvg", tf)] = _sorted(b.known_ns[inv[oki]], -d[oki])
            allk.append(b.known_ns[inv[oki]]), alld.append(-d[oki])
            bi, bd, bc = _breaks(b, a)
            bkn = b.known_ns[bi].astype(np.int64) if len(bi) else np.zeros(0, np.int64)
            self.streams[("bos", tf)] = _sorted(bkn[~bc], bd[~bc])
            self.streams[("choch", tf)] = _sorted(bkn[bc], bd[bc])
            brk_up.append(bkn[bd > 0]), brk_dn.append(bkn[bd < 0])
            allk.append(bkn), alld.append(bd.astype(float))
        self.brk = {1: np.sort(np.concatenate(brk_up)), -1: np.sort(np.concatenate(brk_dn))}
        self.fvg = {1: np.sort(np.concatenate(fv_up)), -1: np.sort(np.concatenate(fv_dn))}
        ak = np.concatenate(allk)
        o = np.argsort(ak, kind="stable")
        self.any_kn, self.any_dir = ak[o], np.concatenate(alld)[o]
        self._sweeps()

    def _sweeps(self) -> None:
        """Liquidity traded through and closed back inside (1-minute close) within 15 minutes = swept: known when that
        minute ends; direction = against the swept side (a swept high is bearish); weight by the level's importance."""
        cx, m = self.cx, self.cx.m
        lv = cx.lv
        days = lv["days"]
        dstart = np.searchsorted(m.day, days, side="left")
        dend = np.searchsorted(m.day, days, side="right")
        st, lvl, side, w = [], [], [], []
        for key, sd, wt, from_min in (("pwh", 1, 4, 1080), ("pwl", -1, 4, 1080), ("pdh", 1, 3, 1080), ("pdl", -1, 3, 1080),
                                      ("prth_hi", 1, 3, 1080), ("prth_lo", -1, 3, 1080), ("asia_hi", 1, 2, 120),
                                      ("asia_lo", -1, 2, 120), ("lon_hi", 1, 2, 420), ("lon_lo", -1, 2, 420)):
            vals = lv[key]
            for d in range(len(days)):
                if not np.isfinite(vals[d]) or dstart[d] >= dend[d]:
                    continue
                sm = m.sess_min[dstart[d]:dend[d]]
                ok = np.flatnonzero(sm >= (from_min - 1080) % 1440)
                if not len(ok):
                    continue
                st.append(dstart[d] + ok[0]), lvl.append(vals[d]), side.append(sd), w.append(wt)
        j = D.first_touch(m, np.array(st, np.int64), np.array(lvl), np.array(side, np.int8), horizon_days=0) if st else \
            np.zeros(0, np.int64)
        taken_i, t_lvl, t_side, t_w = [j], [np.array(lvl)], [np.array(side)], [np.array(w, float)]
        for tf, wt in ((60, 1.0), (240, 2.0)):
            sw = self.src.swing.get(tf)
            if not sw:
                continue
            for key, sd in (("hi", 1), ("lo", -1)):
                s = sw[key]
                ok = s["taken"] < BIG
                taken_i.append(np.searchsorted(m.ts, s["taken"][ok]))
                t_lvl.append(s["price"][ok]), t_side.append(np.full(int(ok.sum()), sd)), t_w.append(np.full(int(ok.sum()), wt))
        for tf, wt in ((15, 1.0), (60, 2.0)):
            eq = self.src.equal.get(tf)
            if not eq:
                continue
            for key, sd in (("hi", 1), ("lo", -1)):
                e = eq[key]
                ok = e["taken"] < BIG
                taken_i.append(np.searchsorted(m.ts, e["taken"][ok]))
                t_lvl.append(e["price"][ok]), t_side.append(np.full(int(ok.sum()), sd)), t_w.append(np.full(int(ok.sum()), wt))
        ti = np.concatenate(taken_i).astype(np.int64)
        tl, ts_, tw = np.concatenate(t_lvl), np.concatenate(t_side), np.concatenate(t_w)
        good = (ti >= 0) & (ti < len(m))
        ti, tl, ts_, tw = ti[good], tl[good], ts_[good], tw[good]
        back = np.full(len(ti), -1, np.int64)
        for off in range(SWEEP_BACK_MIN):
            jj = ti + off
            okj = (jj < len(m)) & (back < 0)
            jc = np.minimum(jj, len(m) - 1)
            hit = okj & (m.day[jc] == m.day[ti]) & np.where(ts_ > 0, m.c[jc] < tl, m.c[jc] > tl)
            back[hit] = jc[hit]
        ok = back >= 0
        kn = m.ts[back[ok]] + D.MIN_NS
        o = np.argsort(kn, kind="stable")
        self.sw_kn, self.sw_dir = kn[o], (-ts_[ok]).astype(float)[o]
        self.sw_w, self.sw_lvl = tw[ok][o], tl[ok][o]
        self.sw_stream = (self.sw_kn, self.sw_dir * self.sw_w, np.r_[0.0, np.cumsum(self.sw_dir * self.sw_w)])


# =============================================================================================== rows + inputs
def rows(cx: F.Context, start_ns: int | None = None) -> dict:
    """Decision rows: minute 0 / 5 / 10 of every complete 15-minute candle (12+ minutes) whose decision minute exists."""
    m, b = cx.m, cx.bars[15]
    a15 = cx.atr[15]
    ok = (b.n >= 12) & np.r_[False, np.isfinite(a15[:-1])] if len(b) else np.zeros(0, bool)
    out = {k: [] for k in ("t", "stage", "k", "i", "ref", "y")}
    for s in STAGES:
        t = b.ts + s * D.MIN_NS
        i = np.searchsorted(m.ts, t, side="left")
        ic = np.minimum(i, len(m) - 1)
        good = ok & (i < len(m)) & (m.ts[ic] == t) & (i <= b.i1) & (i >= b.i0)
        if start_ns is not None:
            good &= t >= start_ns
        kk = np.flatnonzero(good)
        out["t"].append(t[kk]), out["stage"].append(np.full(len(kk), s)), out["k"].append(kk), out["i"].append(ic[kk])
        out["ref"].append(m.o[ic[kk]]), out["y"].append(np.sign(b.c[kk] - m.o[ic[kk]]))
    r = {k: np.concatenate(v) for k, v in out.items()}
    o = np.lexsort((r["stage"], r["t"]))
    r = {k: v[o] for k, v in r.items()}
    r["day"] = b.day[r["k"]].astype("datetime64[D]")
    r["session"] = P.session_code(P.ny_minutes(r["t"]))
    r["slot"] = np.array([STAGES.index(int(x)) for x in r["stage"]]) * 6 + r["session"]
    return r


def features(mi: Micro, r: dict) -> tuple[np.ndarray, list[str], dict]:
    cx, m = mi.cx, mi.cx.m
    t = r["t"]
    n = len(t)
    X0, names0, info = F.features(cx, t)
    a15 = info["a15"]
    px = info["px"]
    cols: dict = {}
    cols["stage_5"] = (r["stage"] == 5).astype(float)
    cols["stage_10"] = (r["stage"] == 10).astype(float)
    b15 = cx.bars[15]
    k = r["k"]
    i0 = b15.i0[k]
    cstart = b15.ts[k]
    # the candle so far (minutes i0 .. i-1)
    hi = np.full(n, -np.inf)
    lo = np.full(n, np.inf)
    for off in range(10):
        j = i0 + off
        use = j < r["i"]
        jc = np.minimum(j, len(m) - 1)
        hi = np.where(use, np.maximum(hi, m.h[jc]), hi)
        lo = np.where(use, np.minimum(lo, m.l[jc]), lo)
    so = r["stage"] > 0
    rng = np.where(so, hi - lo, 0.0)
    cols["so_move"] = np.where(so, (px - m.o[i0]) / a15, 0.0)
    cols["so_range"] = np.where(so, rng / a15, 0.0)
    cols["so_pos"] = np.where(so & (rng > 0), (px - lo) / np.where(rng > 0, rng, 1) - 0.5, 0.0)
    # previous candle (the last complete 15-minute bar) and the one before
    j = np.searchsorted(b15.known_ns, t, side="right") - 1
    jc = np.maximum(j, 0)
    jp = np.maximum(j - 1, 0)
    okp = (j >= 0) & (b15.day[jc] == r["day"])
    okpp = okp & (j >= 1) & ((b15.ts[jc] - b15.ts[jp]) == 15 * D.MIN_NS)
    ph, pl, po, pc = b15.h[jc], b15.l[jc], b15.o[jc], b15.c[jc]
    prng = ph - pl
    cols["prev_upper_wick"] = np.where(okp, (ph - np.maximum(po, pc)) / a15, 0.0)
    cols["prev_lower_wick"] = np.where(okp, (np.minimum(po, pc) - pl) / a15, 0.0)
    cols["prev_close_pos"] = np.where(okp & (prng > 0), (pc - pl) / np.where(prng > 0, prng, 1) - 0.5, 0.0)
    swh = okpp & (ph > b15.h[jp]) & (pc < b15.h[jp])
    swl = okpp & (pl < b15.l[jp]) & (pc > b15.l[jp])
    cols["prev_sweep"] = swl.astype(float) - swh.astype(float)
    cs_h = so & okp & (hi > ph) & (px < ph)
    cs_l = so & okp & (lo < pl) & (px > pl)
    cols["so_sweep"] = cs_l.astype(float) - cs_h.astype(float)
    # micro events: this candle so far and the previous candle
    for tf in MICRO_TFS:
        for s in STREAMS:
            st = mi.streams[(s, tf)]
            cols[f"cur_{s}_{tf}m"] = _wsum(st, cstart, t)
            cols[f"prev_{s}_{tf}m"] = _wsum(st, cstart - 15 * D.MIN_NS, cstart - 1)
    jl = np.searchsorted(mi.any_kn, t, side="right") - 1
    okl = (jl >= 0) & (t - mi.any_kn[np.maximum(jl, 0)] <= 30 * D.MIN_NS)
    cols["last_micro_dir"] = np.where(okl, mi.any_dir[np.maximum(jl, 0)], 0.0)
    cols["last_micro_age"] = np.where(okl, (t - mi.any_kn[np.maximum(jl, 0)]) / (30 * D.MIN_NS), 1.0)
    # sweep -> shift -> FVG
    js = np.searchsorted(mi.sw_kn, t, side="right") - 1
    jsc = np.maximum(js, 0)
    oks = (js >= 0) & (t - mi.sw_kn[jsc] <= SEQ_WINDOW_MIN * D.MIN_NS) if len(mi.sw_kn) else np.zeros(n, bool)
    d = np.where(oks, mi.sw_dir[jsc], 0.0) if len(mi.sw_kn) else np.zeros(n)
    s0 = mi.sw_kn[jsc] if len(mi.sw_kn) else np.zeros(n, np.int64)
    stage = np.where(oks, 1, 0)
    last = s0.copy()
    for dd in (1, -1):
        sel = oks & (d == dd)
        bk, fk = mi.brk[dd], mi.fvg[dd]
        if not sel.any() or not len(bk):
            continue
        ib = np.searchsorted(bk, s0, side="right")
        has_b = sel & (ib < len(bk)) & (bk[np.minimum(ib, len(bk) - 1)] <= t)
        bt = bk[np.minimum(ib, len(bk) - 1)]
        stage = np.where(has_b, 2, stage)
        last = np.where(has_b, bt, last)
        if len(fk):
            iff = np.searchsorted(fk, bt, side="left")
            has_f = has_b & (iff < len(fk)) & (fk[np.minimum(iff, len(fk) - 1)] <= t)
            stage = np.where(has_f, 3, stage)
            last = np.where(has_f, fk[np.minimum(iff, len(fk) - 1)], last)
    cols["seq_signal"] = d * stage
    cols["seq_weight"] = d * (mi.sw_w[jsc] if len(mi.sw_kn) else 0.0)
    cols["seq_age"] = np.where(oks, (t - s0) / (SEQ_WINDOW_MIN * D.MIN_NS), 1.0)
    cols["seq_step_age"] = np.where(oks, (t - last) / (SEQ_WINDOW_MIN * D.MIN_NS), 1.0)
    cols["seq_from_level"] = np.where(oks, np.clip(d * (px - (mi.sw_lvl[jsc] if len(mi.sw_kn) else 0.0)) / a15, -10, 10), 0.0)
    cols["swept_2h"] = _wsum(mi.sw_stream, t - SEQ_WINDOW_MIN * D.MIN_NS, t)
    # resting FVGs as magnets
    from edgelab.market import levelmap as L
    dA_all = np.full(n, 10.0)
    dB_all = np.full(n, 10.0)
    stack = np.zeros(n)
    for tf in MAGNET_TFS:
        z = mi.src.zones.get(tf)
        lab = D.TF_LABEL[tf]
        dA = np.full(n, 10.0)
        dB = np.full(n, 10.0)
        nA = np.zeros(n)
        nB = np.zeros(n)
        if z is not None:
            lb = L.LOOKBACK_DAYS[tf] * 86_400_000_000_000
            a_ = np.searchsorted(z["known"], t - lb, side="left")
            b_ = np.searchsorted(z["known"], t, side="right")
            for q in range(n):
                if b_[q] <= a_[q] or not np.isfinite(a15[q]):
                    continue
                sl = slice(a_[q], b_[q])
                tq = t[q]
                op = z["t_fill"][sl] >= tq
                if not op.any():
                    continue
                dz, top, bot = z["dir"][sl][op], z["top"][sl][op], z["bottom"][sl][op]
                fresh = z["t_touch"][sl][op] >= tq
                ce = z["t_ce"][sl][op] >= tq
                lvl = np.where(fresh, np.where(dz > 0, top, bot), np.where(ce, (top + bot) / 2, np.where(dz > 0, bot, top)))
                dist = (lvl - px[q]) / a15[q]
                up, dn = dist[dist > 0], -dist[dist < 0]
                if len(up):
                    dA[q] = min(10.0, up.min())
                    nA[q] = (up <= 3).sum()
                if len(dn):
                    dB[q] = min(10.0, dn.min())
                    nB[q] = (dn <= 3).sum()
        cols[f"mag_{lab}_above"], cols[f"mag_{lab}_below"] = dA, dB
        cols[f"mag_{lab}_n_above"], cols[f"mag_{lab}_n_below"] = nA, nB
        dA_all, dB_all = np.minimum(dA_all, dA), np.minimum(dB_all, dB)
        stack += nA - nB
    cols["magnet_balance"] = np.log((dB_all + 0.25) / (dA_all + 0.25))
    cols["magnet_stack"] = stack
    names = list(cols) + list(names0)
    X = np.column_stack([np.nan_to_num(np.asarray(cols[c], float)) for c in cols] + [X0])
    return X, names, info


# =============================================================================================== calls
def wilson_low(k: float, n: int, z: float = 1.96) -> float:
    if n <= 0:
        return 0.0
    p = k / n
    den = 1 + z * z / n
    return (p + z * z / (2 * n) - z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n))) / den


def choose_tau(p: np.ndarray, y: np.ndarray) -> dict:
    """tau (a call = probability >= 55 % one way) with the best lower Wilson bound of the call accuracy (>= MIN_CALLS
    calls; one-sided 5 % corrected for the number of thresholds tried); chosen on early months."""
    best = None
    for tau in TAU_GRID:
        c = np.abs(p - 0.5) >= tau
        nc = int(c.sum())
        if nc < MIN_CALLS:
            break
        k = float(((p[c] > 0.5) == (y[c] > 0.5)).sum())
        lb = wilson_low(k, nc, TAU_Z)
        if best is None or lb > best["wilson_low"]:
            best = {"tau": tau, "calls": nc, "accuracy": k / nc, "wilson_low": lb, "share": nc / len(p)}
    if best is None:
        return {"tau": None, "why": "fewer than %d confident candles" % MIN_CALLS}
    if best["wilson_low"] <= 0.5:                  # not even the best threshold is clearly above a coin flip: no calls
        return {**best, "tau": None, "why": "no threshold was clearly better than 50 % on the early months"}
    return best


def _boot(vals: np.ndarray, days: np.ndarray, reps: int = 2000) -> list | None:
    if len(vals) < 30:
        return None
    ud, inv = np.unique(days, return_inverse=True)
    s = np.bincount(inv, weights=vals, minlength=len(ud))
    c = np.bincount(inv, minlength=len(ud)).astype(float)
    rng = np.random.default_rng(109)
    idx = rng.integers(0, len(ud), size=(reps, len(ud)))
    bs = s[idx].sum(1) / np.maximum(c[idx].sum(1), 1)
    return [float(np.quantile(bs, 0.025)), float(np.quantile(bs, 0.975))]


def call_scores(p: np.ndarray, base: np.ndarray, y: np.ndarray, days: np.ndarray, tau: float | None) -> dict:
    """How often it calls, how often a call is right (interval by resampling days), against chance and the baseline's
    direction on the same candles. 'real' = the interval of (call right - baseline right) stays above 0."""
    ok = np.isfinite(p) & np.isfinite(base)
    p, base, y, days = p[ok], base[ok], y[ok], days[ok]
    out = {"candles": int(len(p))}
    if tau is None or not len(p):
        return {**out, "calls": 0}
    c = np.abs(p - 0.5) >= tau
    out.update(calls=int(c.sum()), share=float(c.mean()), tau=tau)
    if c.sum() < 30:
        return out
    right = ((p[c] > 0.5) == (y[c] > 0.5)).astype(float)
    bright = ((base[c] > 0.5) == (y[c] > 0.5)).astype(float)
    out.update(accuracy=float(right.mean()), accuracy_ci=_boot(right, days[c]), baseline_accuracy=float(bright.mean()),
               up_calls=float((p[c] > 0.5).mean()))
    ci = _boot(right - bright, days[c])
    out["vs_baseline_ci"] = ci
    out["real"] = bool(ci and ci[0] > 0)
    out["beats_chance"] = bool(out["accuracy_ci"] and out["accuracy_ci"][0] > 0.5)
    return out


# =============================================================================================== walk-forward job
def home(data_root, key: str) -> Path:
    from edgelab.market import analysis as A
    return A.home(data_root) / f"analysis_{key}"


def latest(data_root, key: str) -> dict | None:
    try:
        d = json.loads((home(data_root, key) / "direction.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if d.get("version") == DIRECTION_VERSION and d.get("analysis_key") == key else None


def build(cx: F.Context, start_ns: int | None = None, progress=None) -> dict:
    step = progress or (lambda s: None)
    step("Direction: 1m / 3m / 5m patterns, liquidity sweeps and resting FVGs")
    mi = Micro(cx)
    r = rows(cx, start_ns)
    step(f"Direction: live inputs of {len(r['t']):,} decision moments (open, minute 5, minute 10)")
    X, names, info = features(mi, r)
    good = np.isfinite(info["a15"]) & np.isfinite(info["px"])
    r = {k: v[good] for k, v in r.items()}
    return {"r": r, "X": X[good], "names": names}


def _buckets(b: dict, vol_cuts: list, calls: np.ndarray | None = None) -> dict:
    from edgelab.market import levelmap as L
    r, X, names = b["r"], b["X"], b["names"]
    g = L.candle_buckets(X, names, r["t"], vol_cuts)
    g["When"] = np.array([STAGE_WORDS[int(s)] for s in r["stage"]])
    seq = X[:, names.index("seq_signal")]
    g["Sweep -> shift -> FVG"] = np.select([np.abs(seq) == 3, np.abs(seq) == 2, np.abs(seq) == 1],
                                           ["full sequence", "sweep + shift", "sweep only"], "none")
    mic = sum(np.abs(X[:, names.index(f"cur_{s}_{tf}m")]) for s in ("fvg", "ifvg", "bos", "choch") for tf in MICRO_TFS)
    g["1-5 min patterns this candle"] = np.where(r["stage"] == 0, "(at the open)", np.where(mic > 0, "some", "none"))
    if calls is not None:
        g["Called?"] = np.where(calls, "called", "no call")
    return g


def analyse(b: dict, progress=None) -> tuple[dict, dict, np.ndarray]:
    """Walk-forward of every model, per stage: scores, the chosen model, the call rule (tau on early months), call
    scores on later months, the mistakes report and the inputs the boosting model leaned on."""
    from edgelab.market import levelmap as L
    step = progress or (lambda s: None)
    r, X, names = b["r"], b["X"], b["names"]
    m_ = r["y"] != 0
    y = (r["y"] > 0).astype(float)
    sim = [names.index(c) for c in SIM_INPUTS if c in names]
    onehot = np.array([n.startswith(F.ONEHOT_PREFIX) for n in names])
    wf = F.walk_forward(X, np.where(m_, y, 0.0), r["day"], r["slot"], "binary", step, "Direction",
                        onehot=onehot, sim_cols=sim, sim_exact=True, gbm_rows=GBM_ROWS, fit_mask=m_)
    pred = wf["pred"]
    months = r["day"].astype("datetime64[M]")
    res = {"version": DIRECTION_VERSION, "rows": int(len(y)), "inputs": names, "stages": {}}
    vol_cuts = [float(np.quantile(X[:, names.index("size60")], q)) for q in (1 / 3, 2 / 3)]
    res["volatility_cuts"] = vol_cuts
    called = np.zeros(len(y), bool)
    for s in STAGES:
        ss = (r["stage"] == s) & m_
        ev = F.evaluate({k: v[ss] for k, v in pred.items()}, y[ss], months[ss], "binary", r["day"][ss])
        ch = ev.get("chosen") or "logistic"
        cut = (ev.get("reported_on") or "").replace("months from ", "")
        p = pred[ch]
        have_p = ss & np.isfinite(p)
        early = have_p & (r["day"] < np.datetime64(cut, "D")) if cut else have_p
        late = have_p & ~early
        rule = choose_tau(p[early], y[early]) if early.sum() else {"tau": None}
        ev["calls"] = {"rule": rule, "late": call_scores(p[late], pred["baseline"][late], y[late], r["day"][late], rule["tau"]),
                       "all": call_scores(p[have_p], pred["baseline"][have_p], y[have_p], r["day"][have_p], rule["tau"])}
        if rule["tau"] is not None:
            called |= have_p & (np.abs(p - 0.5) >= rule["tau"])
        res["stages"][str(s)] = ev
    chosen = {str(s): res["stages"][str(s)].get("chosen") or "logistic" for s in STAGES}
    p_off = np.full(len(y), np.nan)
    for s in STAGES:
        sel = r["stage"] == s
        p_off[sel] = pred[chosen[str(s)]][sel]
    late_from = min(((res["stages"][str(s)].get("reported_on") or "").replace("months from ", "") or "9999-12")
                    for s in STAGES)
    late_mask = r["day"] >= np.datetime64(late_from, "D") if late_from != "9999-12" else np.ones(len(y), bool)
    mres = L.mistakes_core(p_off, pred["baseline"], y, "binary", _buckets(b, vol_cuts, called), m_ & late_mask)
    mres.update(target="direction", model="chosen per stage", **{"from": late_from})
    res["mistakes"] = mres
    res["top_inputs"] = _top_inputs(X[m_], y[m_], names)
    return res, pred, called


def run(svc, *, lock=None, progress=None, force: bool = False) -> dict:
    """Walk-forward direction calls on the discovery period (cached per analysis + DIRECTION_VERSION)."""
    from edgelab.core.fsutil import atomic_write_text
    from edgelab.market import analysis as A
    from edgelab.market import news as N
    from edgelab.mystrategy import runner as R
    step = progress or (lambda s: None)
    la = A.latest(svc.data_root)
    if not la:
        from edgelab.market.holdout import HoldoutTestError
        raise HoldoutTestError("NO_ANALYSIS", "Run the analysis first.")
    key = la["key"]
    have = latest(svc.data_root, key)
    if have and not force:
        return have
    step("Loading NQ and ES (discovery period only)")
    mk = D.discovery(svc, lock)
    cal = N.calendar(svc.data_root)
    news_ok = bool(cal and not cal.get("refused"))
    news = N.events(svc.data_root, mk.start.value, mk.end.value) if news_ok else []
    cx = F.Context(mk.nq, mk.es, news)
    b = build(cx, progress=step)
    res, pred, called = analyse(b, step)
    res.update(analysis_key=key, computed_at=R._now())
    r = b["r"]
    out_dir = home(svc.data_root, key)
    np.savez_compressed(out_dir / "direction.npz", t=r["t"], stage=r["stage"].astype(np.int8), y=r["y"].astype(np.int8),
                        ref=r["ref"], day=r["day"].astype(np.int64), called=called,
                        **{f"p_{k}": v.astype(np.float32) for k, v in pred.items()})
    res = A._jsonable(res)
    atomic_write_text(out_dir / "direction.json", json.dumps(res))
    return res


def _top_inputs(X, y, names, k: int = 15) -> list:
    """Inputs the boosting model leaned on most (whole discovery fit; descriptive, for the 'why')."""
    from edgelab.market import gbm as G
    sub = np.unique(np.linspace(0, len(y) - 1, min(len(y), GBM_ROWS)).astype(np.int64))
    g = G.GBM(n_trees=60, seed=3).fit(X[sub], y[sub])
    tot = g.gain.sum() or 1.0
    o = np.argsort(-g.gain)[:k]
    return [{"input": names[i], "words": input_word(names[i]), "share": float(g.gain[i] / tot)} for i in o if g.gain[i] > 0]


# =============================================================================================== frozen models (holdout / new days)
def fit_frozen(b: dict, gbm_trees: int = 90) -> dict:
    from edgelab.market import gbm as G
    r, X, names = b["r"], b["X"], b["names"]
    m_ = r["y"] != 0
    y = (r["y"] > 0).astype(float)
    sub = np.flatnonzero(m_)
    gsub = sub[np.unique(np.linspace(0, len(sub) - 1, min(len(sub), GBM_ROWS)).astype(np.int64))]
    sim = [names.index(c) for c in SIM_INPUTS if c in names]
    return {"names": names, "p_all": (y[m_].sum() + 1) / (m_.sum() + 2),
            "logistic": G.Logistic(l2=1.0).fit(X[m_], y[m_]),
            "boosting": G.GBM(loss="logloss", n_trees=gbm_trees, seed=9).fit(X[gsub], y[gsub]),
            "sim": sim, "X": X[m_][:, sim], "y": y[m_], "slot": r["slot"][m_]}


def predict_frozen(fm: dict, b: dict) -> dict:
    X, r = b["X"], b["r"]
    return {"baseline": np.full(len(X), fm["p_all"]), "logistic": fm["logistic"].predict(X),
            "boosting": fm["boosting"].predict(X),
            "similar": F._similar(fm["X"], fm["y"], fm["slot"], X[:, fm["sim"]], r["slot"], "binary", exact=True)}


def score_period(b: dict, preds: dict, summary: dict) -> dict:
    """Scores of frozen models on a period they never saw (per stage: every model's skill, the official calls)."""
    r = b["r"]
    m_ = r["y"] != 0
    y = (r["y"] > 0).astype(float)
    out = {"stages": {}}
    for s in STAGES:
        st = summary["stages"][str(s)]
        ch = st.get("chosen") or "logistic"
        tau = ((st.get("calls") or {}).get("rule") or {}).get("tau")
        ss = (r["stage"] == s) & m_
        dd = r["day"][ss]
        models = {k: F.score_binary(v[ss], y[ss], preds["baseline"][ss], dd) for k, v in preds.items()}
        months = dd.astype("datetime64[M]")
        bym = []
        for mo in np.unique(months):
            mm = months == mo
            c = call_scores(preds[ch][ss][mm], preds["baseline"][ss][mm], y[ss][mm], dd[mm], tau)
            bym.append({"month": str(mo), "calls": c.get("calls"), "accuracy": c.get("accuracy")})
        out["stages"][str(s)] = {"official_model": ch, "models": models, "official": models.get(ch),
                                 "calls": call_scores(preds[ch][ss], preds["baseline"][ss], y[ss], dd, tau), "by_month": bym}
    return out


def official(preds: dict, b: dict, summary: dict) -> tuple[np.ndarray, np.ndarray]:
    """The official probability per row (chosen model of its stage) and whether it is a call."""
    r = b["r"]
    p = np.full(len(r["t"]), np.nan)
    called = np.zeros(len(r["t"]), bool)
    for s in STAGES:
        st = summary["stages"][str(s)]
        sel = r["stage"] == s
        p[sel] = preds[st.get("chosen") or "logistic"][sel]
        tau = ((st.get("calls") or {}).get("rule") or {}).get("tau")
        if tau is not None:
            called |= sel & (np.abs(p - 0.5) >= tau)
    return p, called


def save_period(path, b: dict, preds: dict, called: np.ndarray) -> None:
    r = b["r"]
    np.savez_compressed(path, t=r["t"], stage=r["stage"].astype(np.int8), y=r["y"].astype(np.int8), ref=r["ref"],
                        day=r["day"].astype(np.int64), called=called, **{f"p_{k}": v.astype(np.float32) for k, v in preds.items()})


def day_calls(npz: dict | None, summary: dict | None, lo_ns: int, hi_ns: int) -> dict:
    """Per 15-minute candle open (UTC seconds): the official call of each stage (probability, called, what happened)."""
    if npz is None or summary is None or not len(npz.get("t", [])):
        return {}
    t = npz["t"]
    sel = np.flatnonzero((t >= lo_ns) & (t < hi_ns))
    out: dict = {}
    for q in sel:
        s = int(npz["stage"][q])
        ch = summary["stages"][str(s)].get("chosen") or "logistic"
        p = F._f(npz[f"p_{ch}"][q])
        cstart = (int(t[q]) - s * D.MIN_NS) // 1_000_000_000          # seconds: exact in JavaScript
        out.setdefault(str(cstart), {})[str(s)] = {"p": p, "called": bool(npz["called"][q]), "ref": float(npz["ref"][q]),
                                                  "actual": int(npz["y"][q])}
    return out


# =============================================================================================== second holdout look
def holdout_home(data_root) -> Path:
    p = Path(data_root) / "market" / "holdout_direction"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _first_look(svc) -> dict | None:
    from edgelab.market import holdout as H
    try:
        st = H.status(svc)
    except Exception:                                              # noqa: BLE001 - only used for the exposure note
        return None
    return st.get("look") if st.get("used") else None


def holdout_protocol(svc, lock=None, create: bool = True):
    from edgelab.market import holdout as H
    from edgelab.research import protocol as rp
    first = _first_look(svc)
    seen = (f"the first Market simulator holdout look ({first['access_id']}, {str(first.get('created_at'))[:10]}) was "
            f"used and its results were SEEN by the user and Claude" if first else
            "no earlier Market simulator holdout look was recorded")
    return H.protocol(
        svc, lock, create, role=rp.MARKET_DIRECTION_ROLE, suffix=rp.MARKET_DIRECTION_SCOPE_SUFFIX,
        name="Market simulator SECOND holdout look: direction calls only (one look)",
        constraints={"strategies": "none: frozen direction models (rest of the 15-minute candle at minute 0 / 5 / 10, "
                                   "calls only above a threshold chosen on discovery); no trading strategy, no trials",
                     "evaluation_windows": "the holdout window only (discovery minutes are the history of live inputs)",
                     "stages": {"discovery": "walk-forward direction analysis (ADR-109)",
                                "holdout": "ONE look: every holdout 15-minute candle's direction calls scored"}},
        exposure=(f"Created at the user's request for a SECOND look at the same holdout. Before this direction "
                  f"forecaster was designed, {seen}: the forecaster was built after knowing that the first one's "
                  f"direction forecasts had no skill there. Its result is therefore NOT a clean first look and is "
                  f"labelled so everywhere; new days after the research data remain the clean test."))


def holdout_status(svc) -> dict:
    from edgelab.market import holdout as H
    from edgelab.mystrategy import runner as R
    try:
        parent, mine = holdout_protocol(svc, create=False)
    except H.HoldoutTestError as e:
        return {"available": False, "problem": e.message}
    w = R.windows(parent["material"])
    looks = H._looks(svc, mine)
    res = R._read_json(holdout_home(svc.data_root) / "result.json")
    first = _first_look(svc)
    return {"available": True, "used": bool(looks), "look": looks[0] if looks else None,
            "first_look": first, "holdout": {"start": str(w["holdout"]["start"]), "end": str(w["holdout"]["end"])},
            "result": res}


def holdout_run(svc, *, lock=None, progress=None) -> dict:
    """The second look: checks -> frozen direction models -> look RECORDED -> holdout loaded -> scored."""
    from contextlib import nullcontext

    from edgelab.core.fsutil import atomic_write_text
    from edgelab.core.identity import hash_obj
    from edgelab.market import analysis as A
    from edgelab.market import holdout as H
    from edgelab.market import levelmap as L
    from edgelab.market import news as N
    from edgelab.mystrategy import runner as R
    step = progress or (lambda s: None)
    guard = lock if lock is not None else nullcontext()
    step("Checking: current analysis and direction analysis, protocol, settings, unused look")
    la = A.latest(svc.data_root)
    if not la:
        raise H.HoldoutTestError("NO_ANALYSIS", "Run the analysis first.")
    mk = D.discovery(svc, lock)
    cal = N.calendar(svc.data_root)
    news_ok = bool(cal and not cal.get("refused"))
    if A.cache_key(mk.source, cal["meta"]["sha256"] if news_ok else None, svc._config_hash()) != la["key"]:
        raise H.HoldoutTestError("ANALYSIS_OUTDATED", "The data, news or settings changed since the last analysis. Run "
                                                      "the analysis and the direction analysis again first.")
    summ = latest(svc.data_root, la["key"])
    if not summ:
        raise H.HoldoutTestError("NO_DIRECTION", "Run the direction analysis first: the look freezes its models and "
                                                 "call thresholds.")
    parent, mine = holdout_protocol(svc, lock)
    if svc._config_hash() != mine["material"]["config_hash"]:
        raise H.HoldoutTestError("PROTOCOL_CONFIG_CHANGED", "The research settings differ from the protocol's.")
    if H._looks(svc, mine):
        raise H.HoldoutTestError("HOLDOUT_LOOK_USED", "The second holdout look is already used (one look only).")
    rules = {s: {"model": summ["stages"][s].get("chosen"), "tau": ((summ["stages"][s].get("calls") or {}).get("rule") or {}).get("tau")}
             for s in summ["stages"]}
    step("Training the frozen direction models on the whole discovery period")
    news_d = N.events(svc.data_root, mk.start.value, mk.end.value) if news_ok else []
    bd = build(F.Context(mk.nq, mk.es, news_d), progress=step)
    fm = fit_frozen(bd)
    vol_cuts = summ.get("volatility_cuts") or [0.0, 0.0]
    fp = hash_obj({"analysis": la["key"], "direction": DIRECTION_VERSION, "rules": rules, "inputs": bd["names"],
                   "code": R._code_version()}, 16)
    del bd
    w = R.windows(parent["material"])
    hs, he = R._ts(w["holdout"]["start"]), R._ts(w["holdout"]["end"])
    with guard:
        if H._looks(svc, mine):
            raise H.HoldoutTestError("HOLDOUT_LOOK_USED", "The second holdout look is already used.")
        access_id = "HA_MARKETDIR_" + R.new_id("X")[2:]
        svc.store.add_holdout_access({"access_id": access_id, "protocol_id": mine["protocol_id"],
                                      "strategy_id": "market_sim_direction:" + fp, "logic_hash": fp, "definition_hash": fp,
                                      "frozen_hash": fp, "search_id": None, "status": "granted", "reason_code": None,
                                      "reason": None, "run_id": None, "result_json": None, "created_at": R._now(),
                                      "completed_at": None})
    try:
        step("Loading the holdout (the look is recorded)")
        ds = svc._cell_dataset(R.dataset_1m(svc, parent), (hs, he), lock)
        hb = ds.bars
        nq = D.Minute(np.r_[mk.nq.ts, hb.ts_ns], np.r_[mk.nq.o, hb.open], np.r_[mk.nq.h, hb.high],
                      np.r_[mk.nq.l, hb.low], np.r_[mk.nq.c, hb.close])
        es, _ = D.load_es(svc.data_root, mk.start.value, he.value)
        news_all = N.events(svc.data_root, mk.start.value, he.value) if news_ok else []
        step("Direction calls on every holdout 15-minute candle (live knowledge only)")
        bh = build(F.Context(nq, es, news_all), start_ns=hs.value, progress=step)
        preds = predict_frozen(fm, bh)
        sc = score_period(bh, preds, summ)
        p_off, called = official(preds, bh, summ)
        r = bh["r"]
        m_ = r["y"] != 0
        y = (r["y"] > 0).astype(float)
        mres = L.mistakes_core(p_off, preds["baseline"], y, "binary", _buckets(bh, vol_cuts, called), m_)
        mres.update(target="direction", model="chosen per stage")
        ref = summ.get("mistakes") or {}
        H._compare(mres, ref)
        res = {"access_id": access_id, "fingerprint": fp, "analysis_key": la["key"], "second_look": True,
               "first_look": _first_look(svc), "rules": rules, "computed_at": R._now(),
               "holdout": {"start": hs.isoformat(), "end": he.isoformat()}, "candles": int((r["stage"] == 0).sum()),
               "days": int(len(np.unique(r["day"]))), "stages": sc["stages"], "mistakes": mres,
               "discovery": {s: {"calls": (summ["stages"][s].get("calls") or {}).get("late"),
                                 "skill": ((summ["stages"][s].get("scores") or {}).get(summ["stages"][s].get("chosen") or "")
                                           or {}).get("late")} for s in summ["stages"]}}
        res = A._jsonable(res)
        save_period(holdout_home(svc.data_root) / "predictions.npz", bh, preds, called)
        atomic_write_text(holdout_home(svc.data_root) / "result.json", json.dumps(res))
        with guard:
            svc.store.update_holdout_access(access_id, status="completed", completed_at=R._now(), result_json=json.dumps(
                {s: {"calls": v["calls"].get("calls"), "accuracy": v["calls"].get("accuracy")} for s, v in res["stages"].items()}))
        return res
    except Exception as exc:                                       # the look stays spent; the failure is recorded
        with guard:
            svc.store.update_holdout_access(access_id, status="failed", completed_at=R._now(),
                                            reason_code=type(exc).__name__, reason=str(exc)[:500])
        raise
