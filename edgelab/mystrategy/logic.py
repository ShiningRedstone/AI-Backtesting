"""The mechanical rules of My strategy (ADR-93): bias -> key level -> manipulation leg -> inversion gap -> execution.

Every rule reads only bars whose index is <= the decision bar (candles/gaps/swings via their ``comp``/``known`` index;
see frames.py). Long and short are one implementation: a short is a long in a MIRRORED price world (prices negated,
highs <-> lows), so both directions are guaranteed to follow exactly the same rules. Prices are mapped back to real
prices when signals and explanations are written.

Trade management (breakeven / trailing) is handed to the engine as per-bar stop CANDIDATES (TrailSpec mode "level"):
the engine owns the ratchet and the fills. The strategy also tracks its own copy of each trade (same fill and
stop/target rules, same daily limits) only to know when it is flat again and to keep its candidates tied to the open
trade; the engine's result is authoritative.
"""
from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

import numpy as np

from edgelab.engine.signals import SignalSet, TrailSpec
from edgelab.mystrategy.frames import (NEVER, TF, TF_LABEL, Base, Gaps, RangeQ, build_base, build_tf, first_where,
                                       fvgs, swings)
from edgelab.mystrategy.params import minutes

IFG_TFS = (1, 2, 3, 4, 5)
KEY_TFS = {"3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240}
BIAS_TFS = {"1d": 1440, "4h": 240, "1h": 60, "15m": 15}
LBL = {"3m": 3, "5m": 5, "15m": 15, "30m": 30, "1h": 60, "4h": 240, "1m": 1}


def _neg_tf(t: TF) -> TF:
    return TF(t.tf, t.start, t.end, t.comp, -t.o, -t.l, -t.h, -t.c, t.t0, t.td, t.of_bar)


@dataclass
class View:
    """One direction's price world (d = +1 real prices, d = -1 mirrored)."""
    d: int
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    ent_o: np.ndarray        # entry side open (long buys on ASK)
    ent_l: np.ndarray        # entry side low (limit buy fills)
    ent_c: np.ndarray        # entry side close (the price a market entry is planned from)
    ex_h: np.ndarray         # exit side (long sells on BID)
    ex_l: np.ndarray
    ex_c: np.ndarray
    tf: dict = field(default_factory=dict)
    rq_l: RangeQ | None = None
    rq_h: RangeQ | None = None
    memo: dict = field(default_factory=dict)

    def px(self, v: float) -> float:
        return float(self.d * v) if v == v else v

    def zone(self, top: float, bottom: float) -> tuple[float, float]:
        a, b = self.d * top, self.d * bottom
        return (float(max(a, b)), float(min(a, b)))


@dataclass
class Zone:
    kind: str                # fvg | cisd | rejection_block | bpr
    tf: int
    top: float
    bottom: float
    known: int               # bar index from which the zone exists
    inval: int               # bar index at which it is known invalid (NEVER if not by the window end)
    k: int                   # candle index (of its timeframe) that completed it
    t_from: int              # ns: start of the first candle of the pattern (chart box start)
    extra: dict = field(default_factory=dict)


@dataclass
class Setup:
    model: str
    m: int                   # bar of the manipulation extreme
    start: int               # bar of the leg start
    low: float
    zones: list
    info: dict


class Rules:
    def __init__(self, bars, calendar, s: dict, skip: set | None = None, trade_from_td: int | None = None, es=None):
        self.s, self.cal = s, calendar
        self.trade_from_td = trade_from_td   # earlier trading dates are warm-up history only (no signals)
        self.skip = set(skip or ())          # signal bars the trader declined (holdout review): stay flat there
        self.bars = bars
        self.b: Base = build_base(bars, calendar, s["models.price_series"])
        b = self.b
        self.n = b.n
        self.open_min = calendar.open_min
        so = lambda hhmm: (minutes(hhmm) - calendar.open_min) % 1440
        self.so_manip, self.so_from = so(s["session.manip_from"]), so(s["session.entry_from"])
        self.so_until, self.so_force = so(s["session.entry_until"]), so(s["session.force_exit"])
        self.so_930 = so("09:30")
        self.so_judas_end = self.so_930 + int(s["session.judas_minutes"])
        need = {1, 2, 3, 4, 5, 15, 30, 60, 240, 1440}
        base_tf = {t: build_tf(b, t) for t in sorted(need)}
        ask = b.ask
        bid = (bars.open, bars.high, bars.low, bars.close)
        ent = ask if ask is not None else bid
        ex_s = ask if ask is not None else bid
        self.views = {
            1: View(1, b.o, b.h, b.l, b.c, ent[0], ent[2], ent[3], bid[1], bid[2], bid[3], base_tf),
            -1: View(-1, -b.o, -b.l, -b.h, -b.c, -bid[0], -bid[1], -bid[3], -ex_s[2], -ex_s[1], -ex_s[3],
                     {t: _neg_tf(x) for t, x in base_tf.items()}),
        }
        for v in self.views.values():
            v.rq_l, v.rq_h = RangeQ(v.l), RangeQ(v.h)
        # ES (ADR-95): aligned to these bars by exact bar-open minute (NaN = no ES bar). Index i only ever holds the ES
        # bar of the same minute, so a truncated history never sees later ES prices. Per view: the "low" side.
        self.es_low = None
        if es is not None:
            es_h, es_l = es.align(bars.ts_ns)
            self.es_low = {1: es_l, -1: -es_h}
        self.stats: Counter = Counter()
        self.explain: dict[int, dict] = {}
        self.days: list[dict] = []

    # ------------------------------------------------------------------ helpers
    def _first_touch(self, v: View, tf: int, side: str, gi: int, g: Gaps, below: bool) -> int:
        """First 1m bar after the gap exists that trades into it (memoized; the caller compares with its decision
        bar, so a touch found after it never influences a decision)."""
        key = ("touch", tf, side, gi)
        hit = v.memo.get(key)
        if hit is None:
            a = int(g.known[gi]) + 1
            if below:       # price coming down into a gap below
                j = first_where(v.l, a, self.n - 1, below=float(g.top[gi]))
            else:
                j = first_where(v.h, a, self.n - 1, above=float(g.bottom[gi]))
            hit = v.memo[key] = (j if j >= 0 else int(NEVER))
        return hit

    def _close_through(self, v: View, tf: int, side: str, gi: int, g: Gaps, down: bool) -> int:
        """Bar index at which a candle of the gap's timeframe is known to have CLOSED beyond its far edge."""
        key = ("thru", tf, side, gi)
        hit = v.memo.get(key)
        if hit is None:
            t = v.tf[tf]
            k = int(g.k[gi])
            if down:
                q = first_where(t.c, k + 1, len(t) - 1, below=float(g.bottom[gi]) - 1e-9)
            else:
                q = first_where(t.c, k + 1, len(t) - 1, above=float(g.top[gi]) + 1e-9)
            hit = v.memo[key] = (int(t.comp[q]) if q >= 0 else int(NEVER))
        return hit

    def _ts(self, i: int) -> int:
        return int(self.b.ts[i])

    # ------------------------------------------------------------------ bias (real prices)
    def _bias(self, d0: int, prev: int) -> dict:
        s = self.s
        ov = s["bias.override"]
        if ov != "auto":
            return {"direction": 1 if ov == "long" else -1, "score": None, "method": "forced " + ov, "per_tf": {}}
        v = self.views[1]
        total, per = 0.0, {}
        for name, tf in BIAS_TFS.items():
            if not s[f"bias.tf_{name}"]:
                continue
            w = float(s[f"bias.weight_{name}"])
            t = v.tf[tf]
            cur = int(t.of_bar[d0])
            info: dict = {"weight": w}
            sc = 0.0
            if s["bias.method"] in ("fvg_respect", "both"):
                events = []
                for side in ("bull", "bear"):
                    g = fvgs(t, side, s["bias.fvg_min_points"])
                    lo = np.searchsorted(g.k, cur - int(s["bias.fvg_lookback"]))
                    hi = np.searchsorted(g.k, cur, side="right")
                    for gi in range(lo, hi):
                        if g.known[gi] > d0:
                            continue
                        bull = side == "bull"
                        thru = self._close_through(v, tf, side, gi, g, down=bull)
                        if thru <= d0:
                            events.append((thru, -1 if bull else 1, side, gi, "disrespected"))
                            continue
                        tch = self._first_touch(v, tf, side, gi, g, below=bull)
                        if tch <= d0:
                            events.append((tch, 1 if bull else -1, side, gi, "respected"))
                        elif not s["bias.respect_needs_touch"]:
                            events.append((int(g.known[gi]), 1 if bull else -1, side, gi, "respected"))
                events.sort()
                events = events[-int(s["bias.events_per_tf"]):]
                if events:
                    sc += sum(e[1] for e in events) / len(events)
                info["gaps"] = [{"side": e[2], "state": e[4], "event_ts": self._ts(e[0]),
                                 "top": float(fvgs(t, e[2], s["bias.fvg_min_points"]).top[e[3]]),
                                 "bottom": float(fvgs(t, e[2], s["bias.fvg_min_points"]).bottom[e[3]]),
                                 "t_from": int(t.t0[int(fvgs(t, e[2], s["bias.fvg_min_points"]).k[e[3]]) - 2]),
                                 "tf": TF_LABEL[tf]} for e in events]
            if s["bias.method"] in ("structure", "both"):
                sw = swings(t, s["bias.structure_strength"])
                hi_ = sw["hi"]
                lo_ = sw["lo"]
                hs = hi_[1][hi_[2] <= d0][-2:]
                ls = lo_[1][lo_[2] <= d0][-2:]
                st = 0
                if len(hs) == 2 and len(ls) == 2:
                    if hs[1] > hs[0] and ls[1] > ls[0]:
                        st = 1
                    elif hs[1] < hs[0] and ls[1] < ls[0]:
                        st = -1
                info["structure"] = st
                sc += st
            info["score"] = sc
            per[TF_LABEL[tf]] = info
            total += w * sc
        direction = 1 if total >= s["bias.min_score"] and total > 0 else -1 if total <= -s["bias.min_score"] and total < 0 else 0
        how = s["bias.method"]
        if direction == 0:
            tb = s["bias.tie_break"]
            if tb == "previous_day" and prev:
                direction, how = prev, how + " (kept previous day)"
            elif tb == "daily_trend":
                t = v.tf[1440]
                done = np.flatnonzero(t.comp[: int(t.of_bar[d0]) + 1] <= d0)
                if len(done) > 20:
                    diff = t.c[done[-1]] - t.c[done[-21]]
                    direction = 1 if diff > 0 else -1 if diff < 0 else 0
                    how += " (daily trend)"
        return {"direction": direction, "score": round(total, 4), "method": how, "per_tf": per}

    # ------------------------------------------------------------------ day filters (real prices)
    def _chop(self, d0: int) -> dict:
        s = self.s
        v = self.views[1]
        lo_bar = max(0, d0 - int(s["filters.chop_minutes"]))
        created = flipped = 0
        for tf in (5, 15):
            t = v.tf[tf]
            for side in ("bull", "bear"):
                g = fvgs(t, side, 0.0)
                sel = np.flatnonzero((g.known >= lo_bar) & (g.known <= d0))
                for gi in sel:
                    created += 1
                    if self._close_through(v, tf, side, int(gi), g, down=side == "bull") <= d0:
                        flipped += 1
        ratio = flipped / created if created else 0.0
        ok = created >= s["filters.chop_min_gaps"] and ratio <= s["filters.chop_max_flip"]
        return {"gaps_created": created, "gaps_closed_through": flipped, "share": round(ratio, 3), "ok": bool(ok)}

    def _ath_block(self, d0: int) -> bool:
        s = self.s
        t = self.views[1].tf[1440]
        cur = int(t.of_bar[d0])
        lo = max(0, cur - int(s["bias.ath_days"]))
        hh = float(t.h[lo:cur].max()) if cur > lo else -math.inf
        ds = int(t.start[cur])
        hh = max(hh, float(self.b.h[ds:d0 + 1].max()))
        return bool(self.b.c[d0] >= hh * (1 - s["bias.ath_pct"] / 100.0))

    # ------------------------------------------------------------------ draw (view world: above price)
    def _liquidity_above(self, v: View, i: int, ref: float, tfs: list, strength: int, gap_tfs: list,
                         sessions: dict | None, ds: int, include_nwog: bool) -> list[dict]:
        out = []
        for tf in tfs:
            t = v.tf[tf]
            sw = swings(t, strength)["hi"]
            sel = np.flatnonzero(sw[2] <= i)[-60:]
            for j in sel[::-1]:
                price = float(sw[1][j])
                if price <= ref:
                    continue
                bar_end = int(t.end[int(sw[0][j])])
                if bar_end + 1 <= i and v.rq_h.max(bar_end + 1, i) >= price:
                    continue          # already taken
                out.append({"kind": "swing_high", "tf": TF_LABEL[tf], "price": price,
                            "ts": int(t.t0[int(sw[0][j])])})
        for tf in gap_tfs:
            t = v.tf[tf]
            g = fvgs(t, "bear", 1.0)
            lo = np.searchsorted(g.k, int(t.of_bar[i]) - 400)
            for gi in range(lo, len(g.k)):
                if g.known[gi] > i:
                    break
                if g.bottom[gi] <= ref:
                    continue
                if self._first_touch(v, tf, "bear", gi, g, below=False) <= i:
                    continue
                out.append({"kind": "unfilled_gap", "tf": TF_LABEL[tf], "price": float(g.bottom[gi]),
                            "top": float(g.top[gi]), "ts": int(t.t0[int(g.k[gi]) - 2])})
        if sessions:
            for name, price in sessions.items():
                if price == price and price > ref:
                    out.append({"kind": name, "tf": "", "price": float(price), "ts": None})
        if include_nwog:
            nw = self._nwog(v, i)
            if nw and nw["bottom"] > ref:
                out.append({"kind": "nwog", "tf": "1W", "price": nw["bottom"], "top": nw["top"], "ts": nw["ts"]})
        return out

    def _nwog(self, v: View, i: int) -> dict | None:
        td = self.b.td
        wk = (td[i] + 3) // 7                  # Monday-based week number of the trading date
        key = ("nwog", int(wk))
        if key not in v.memo:
            res = None
            lo = 0 if i < 8000 else i - 8000
            seg = (td[lo:i + 1] + 3) // 7
            first = lo + int(np.argmax(seg == wk))
            if first > 0 and (td[first - 1] + 3) // 7 != wk:
                a, b = float(v.c[first - 1]), float(v.o[first])
                if abs(a - b) > 0:
                    res = {"top": max(a, b), "bottom": min(a, b), "ts": self._ts(first), "first": first}
            v.memo[key] = res
        res = v.memo[key]
        if res is None or v.rq_h.max(res["first"], i) >= res["bottom"]:
            return None                      # no gap, or price already traded into it this week
        return res

    def _draw(self, v: View, d0: int, ds: int) -> dict | None:
        s = self.s
        tfs = [tf for name, tf in (("1d", 1440), ("4h", 240), ("1h", 60), ("15m", 15)) if s[f"draw.tf_{name}"]]
        sess = {}
        if s["draw.prev_day"]:
            sess["previous_day_high"] = self._prev_day(v, d0)
        ref = float(v.c[d0])
        cands = self._liquidity_above(v, d0, ref, tfs, s["draw.swing_strength"],
                                      [60, 240] if s["draw.unfilled_fvg"] else [], sess, ds, s["draw.nwog"])
        ok = [c for c in cands if s["draw.min_points"] <= c["price"] - ref <= s["draw.max_points"]]
        if not ok:
            return None
        best = min(ok, key=lambda c: c["price"])
        return {**best, "distance": round(best["price"] - ref, 2)}

    def _prev_day(self, v: View, i: int) -> float:
        t = v.tf[1440]
        cur = int(t.of_bar[i])
        return float(t.h[cur - 1]) if cur >= 1 and t.comp[cur - 1] <= i else math.nan

    # ------------------------------------------------------------------ key levels of a day (view world)
    def _key_tfs(self) -> list[int]:
        return [tf for name, tf in KEY_TFS.items() if self.s[f"key.tf_{name}"]]

    def _day_zones(self, v: View, mf: int, eu: int) -> list[Zone]:
        s = self.s
        tol = s["key.tolerance_points"]
        win_low = v.rq_l.min(mf, eu)
        zones: list[Zone] = []
        max_age = int(s["key.fvg_max_age"])
        for tf in self._key_tfs():
            t = v.tf[tf]
            cur_lo = int(t.of_bar[mf]) - max_age
            cur_hi = int(t.of_bar[eu])
            bull = fvgs(t, "bull", s["key.fvg_min_points"])
            lo = np.searchsorted(bull.k, cur_lo)
            hi = np.searchsorted(bull.k, cur_hi, side="right")
            if s["key.fvg"]:
                for gi in range(lo, hi):
                    if bull.known[gi] > eu or bull.top[gi] + tol < win_low:
                        continue
                    inval = self._close_through(v, tf, "bull", gi, bull, down=True) if s["key.invalidate_on_close"] else int(NEVER)
                    if inval < mf:
                        continue
                    zones.append(Zone("fvg", tf, float(bull.top[gi]), float(bull.bottom[gi]), int(bull.known[gi]),
                                      inval, int(bull.k[gi]), int(t.t0[int(bull.k[gi]) - 2])))
            if s["key.cisd"]:
                zones += self._cisd_zones(v, t, cur_lo, cur_hi, mf, eu, win_low)
            if s["key.rejection_block"] and tf >= LBL[s["key.rb_min_tf"]]:
                zones += self._rb_zones(v, t, cur_lo, cur_hi, mf, eu, win_low)
            if s["key.bpr"]:
                zones += self._bpr_zones(v, t, bull, lo, hi, mf, eu, win_low)
        return zones

    def _gap_at(self, v: View, price: float, at: int) -> tuple | None:
        """A bullish key-timeframe gap (any enabled key timeframe) that ``price`` traded into, existing at bar ``at``."""
        for tf in self._key_tfs():
            t = v.tf[tf]
            g = fvgs(t, "bull", self.s["key.fvg_min_points"])
            hi = np.searchsorted(g.known, at, side="left")       # known < at  (g.known is non-decreasing with k)
            lo = max(0, hi - 40)
            for gi in range(hi - 1, lo - 1, -1):
                if g.bottom[gi] <= price <= g.top[gi] and self._close_through(v, tf, "bull", gi, g, True) >= at:
                    return tf, gi, float(g.top[gi]), float(g.bottom[gi])
        return None

    def _cisd_at(self, v: View, t: TF, q: int) -> dict | None:
        """The CISD completed by candle ``q`` of ``t`` (q = first candle after a down-close series), or None. Depends
        only on q (memoized across days); callers compare its ``known`` / ``inval`` indices with their own bars."""
        key = ("cisd", t.tf, q)
        if key in v.memo:
            return v.memo[key]
        s = self.s
        res = None
        if t.c[q - 1] < t.o[q - 1]:
            maxn = int(s["key.cisd_max_candles"])
            f = q - 1
            while f - 1 >= 0 and t.c[f - 1] < t.o[f - 1] and q - f < maxn:
                f -= 1
            level = float(t.o[f])
            series_low = float(t.l[f:q].min())
            hit = -1            # the candle closing above the series' open (within 10 candles, before a close below its low)
            for r in range(q, min(len(t), q + 10)):
                if t.c[r] < series_low:
                    break
                if t.c[r] > level:
                    hit = r
                    break
            if hit >= 0:
                inval = int(NEVER)
                if s["key.invalidate_on_close"]:
                    qq = first_where(t.c, hit + 1, len(t) - 1, below=series_low - 1e-9)
                    inval = int(t.comp[qq]) if qq >= 0 else int(NEVER)
                res = {"f": f, "level": level, "low": series_low, "hit": hit, "known": int(t.comp[hit]),
                       "ctx": self._gap_at(v, series_low, int(t.comp[q - 1])), "inval": inval}
        v.memo[key] = res
        return res

    def _cisd_zones(self, v: View, t: TF, cur_lo: int, cur_hi: int, mf: int, eu: int, win_low: float) -> list[Zone]:
        s = self.s
        out = []
        q = max(1, cur_lo)
        while q <= cur_hi and q < len(t):
            c = self._cisd_at(v, t, q)
            if c is None or c["known"] > eu or c["level"] + s["key.tolerance_points"] < win_low or \
                    (s["key.cisd_needs_fvg"] and c["ctx"] is None):
                q += 1
                continue
            if c["inval"] >= mf:
                ctx = c["ctx"]
                out.append(Zone("cisd", t.tf, c["level"], c["low"], c["known"], c["inval"], c["hit"], int(t.t0[c["f"]]),
                                {"series_candles": int(q - c["f"]), "from_gap": None if ctx is None else
                                 {"tf": TF_LABEL[ctx[0]], "top": ctx[2], "bottom": ctx[3]}}))
            q = c["hit"] + 1
        return out

    def _rb_at(self, v: View, t: TF, q: int) -> tuple | None:
        key = ("rb", t.tf, q)
        if key in v.memo:
            return v.memo[key]
        s = self.s
        res = None
        rng = t.h[q] - t.l[q]
        body_lo = min(t.o[q], t.c[q])
        if rng > 0 and (body_lo - t.l[q]) / rng >= s["key.rb_wick_ratio"]:
            ctx = self._gap_at(v, float(t.l[q]), int(t.comp[q]))
            if ctx is not None:
                inval = int(NEVER)
                if s["key.invalidate_on_close"]:
                    qq = first_where(t.c, q + 1, len(t) - 1, below=float(t.l[q]) - 1e-9)
                    inval = int(t.comp[qq]) if qq >= 0 else int(NEVER)
                res = (float(body_lo), float(t.l[q]), ctx, inval)
        v.memo[key] = res
        return res

    def _rb_zones(self, v: View, t: TF, cur_lo: int, cur_hi: int, mf: int, eu: int, win_low: float) -> list[Zone]:
        s = self.s
        out = []
        for q in range(max(0, cur_lo), min(cur_hi + 1, len(t))):
            if t.comp[q] > eu:
                break
            r = self._rb_at(v, t, q)
            if r is None or r[0] + s["key.tolerance_points"] < win_low or r[3] < mf:
                continue
            body_lo, low, ctx, inval = r
            out.append(Zone("rejection_block", t.tf, body_lo, low, int(t.comp[q]), inval, q, int(t.t0[q]),
                            {"ce": (body_lo + low) / 2, "from_gap": {"tf": TF_LABEL[ctx[0]], "top": ctx[2], "bottom": ctx[3]}}))
        return out

    def _bpr_zones(self, v: View, t: TF, bull: Gaps, lo: int, hi: int, mf: int, eu: int, win_low: float) -> list[Zone]:
        out = []
        bear = fvgs(t, "bear", self.s["key.fvg_min_points"])
        for gi in range(lo, hi):
            if bull.known[gi] > eu or bull.top[gi] < win_low:
                continue
            k = int(bull.k[gi])
            j0 = np.searchsorted(bear.k, k - 30)
            for bj in range(j0, len(bear.k)):
                if bear.k[bj] >= k:
                    break
                inv = self._close_through(v, t.tf, "bear", bj, bear, down=False)
                if inv > int(bull.known[gi]):
                    continue
                top, bottom = min(bull.top[gi], bear.top[bj]), max(bull.bottom[gi], bear.bottom[bj])
                if top > bottom:
                    inval = self._close_through(v, t.tf, "bull", gi, bull, down=True) \
                        if self.s["key.invalidate_on_close"] else int(NEVER)
                    if inval >= mf:
                        out.append(Zone("bpr", t.tf, float(top), float(bottom), int(bull.known[gi]), inval, k,
                                        int(t.t0[int(bear.k[bj]) - 2])))
                    break
        return out

    # ------------------------------------------------------------------ setups
    def _leg_start(self, v: View, m: int, i: int, judas: bool, open_idx: int, ds: int) -> int:
        s = self.s
        lo = max(ds, m - int(s["leg.max_minutes"]))
        if judas and s["leg.judas_from_open"] and open_idx >= 0:
            a = max(open_idx, lo)
            return a + int(np.argmax(v.h[a:m + 1]))
        t = v.tf[LBL[s["leg.start_tf"]]]
        sw = swings(t, s["leg.swing_strength"])["hi"]
        hi = np.searchsorted(sw[0], int(t.of_bar[m]))     # swing candles before the extreme's candle
        for jj in range(hi - 1, -1, -1):
            k = int(sw[0][jj])
            a, b = int(t.start[k]), int(t.end[k])
            if b < lo:
                break
            if sw[2][jj] > i or b >= m:
                continue
            j = a + int(np.argmax(v.h[a:b + 1]))          # the 1m bar of that swing's high
            if j >= lo and v.h[j] >= v.rq_h.max(j + 1, m):
                return j
        return lo + int(np.argmax(v.h[lo:m + 1]))

    def _liquidity_check(self, v: View, start: int, m: int, ds: int) -> dict:
        """Prior swing lows (sweep timeframe) the leg extreme swept, or merely matched (relative equal lows)."""
        s = self.s
        tf = LBL[s["leg.sweep_tf"]]
        t = v.tf[tf]
        sw = swings(t, 2)["lo"]
        low = float(v.l[m])
        swept, equal = None, None
        sel = np.flatnonzero((sw[2] < start) & (t.end[sw[0]] >= start - 2880))     # the last two days
        for jj in sel[::-1][:30]:
            price = float(sw[1][jj])
            bar_end = int(t.end[int(sw[0][jj])])
            if bar_end + 1 <= start - 1 and v.rq_l.min(bar_end + 1, start - 1) < price:
                continue      # already taken before the leg
            if low < price and swept is None:
                swept = {"price": price, "tf": TF_LABEL[tf], "ts": int(t.t0[int(sw[0][jj])])}
            elif low >= price and low - price <= s["leg.equal_points"] and equal is None:
                equal = {"price": price, "tf": TF_LABEL[tf], "ts": int(t.t0[int(sw[0][jj])])}
        return {"swept": swept, "equal": equal}

    def _smt(self, v: View, start: int, m: int) -> dict:
        """SMT divergence at the manipulation leg's extreme ``m`` (view world: lows; shorts are mirrored).

        Reference = the most recent prior swing low of the sweep timeframe that was still untaken at the leg start (the
        same swings as the liquidity check, last two days). NQ took it if its extreme went below it; ES took its own
        low of that same swing candle if any ES low from after that candle up to ``m`` went below it. Divergence =
        exactly one of the two took its low. Unknown (None) without ES data or when ES has no bar for any minute from
        the swing candle to ``m``. Reads bars <= m only."""
        if self.es_low is None:
            return {"divergence": None, "reason": "no ES data"}
        tf = LBL[self.s["leg.sweep_tf"]]
        t = v.tf[tf]
        sw = swings(t, 2)["lo"]
        sel = np.flatnonzero((sw[2] < start) & (t.end[sw[0]] >= start - 2880))
        ref = None
        for jj in sel[::-1][:30]:
            k = int(sw[0][jj])
            price, bar_end = float(sw[1][jj]), int(t.end[k])
            if bar_end + 1 <= start - 1 and v.rq_l.min(bar_end + 1, start - 1) < price:
                continue      # already taken before the leg
            ref = (k, price)
            break
        if ref is None:
            return {"divergence": False, "reason": "no untaken prior swing to compare"}
        k, nq_ref = ref
        a, b = int(t.start[k]), int(t.end[k])
        el = self.es_low[v.d]
        seg = el[a:m + 1]
        if np.isnan(seg).any():
            return {"divergence": None, "reason": "ES minutes missing", "ref_ts": int(t.t0[k])}
        es_ref = float(seg[:b - a + 1].min())
        es_ext = float(seg[b - a + 1:].min()) if m > b else math.inf
        nq_ext = float(v.l[m])
        nq_took, es_took = nq_ext < nq_ref, es_ext < es_ref
        return {"divergence": bool(nq_took != es_took), "nq_took": bool(nq_took), "es_took": bool(es_took),
                "tf": TF_LABEL[tf], "ref_ts": int(t.t0[k]), "nq_ref": nq_ref, "nq_extreme": nq_ext, "es_ref": es_ref,
                "es_extreme": es_ext if es_ext != math.inf else None,
                "reason": "NQ took its low, ES held" if nq_took and not es_took else
                          "ES took its low, NQ held" if es_took and not nq_took else
                          "both took their lows" if nq_took else "neither took its low"}

    def _eq(self, v: View, m: int, ds: int, mf: int) -> dict | None:
        s = self.s
        mode = s["eq.range"]
        if mode == "previous_day":
            t = v.tf[1440]
            cur = int(t.of_bar[m])
            if cur < 1 or t.comp[cur - 1] > m:
                return None
            hi, lo = float(t.h[cur - 1]), float(t.l[cur - 1])
        elif mode == "overnight":
            if mf <= ds:
                return None
            hi, lo = float(v.h[ds:mf].max()), float(v.l[ds:mf].min())
        else:
            t = v.tf[LBL[s["eq.tf"]]]
            sw = swings(t, s["eq.swing_strength"])
            hs, ls = sw["hi"], sw["lo"]
            ih = np.flatnonzero(hs[2] < m)
            il = np.flatnonzero(ls[2] < m)
            if not len(ih) or not len(il):
                return None
            hi, lo = float(hs[1][ih[-1]]), float(ls[1][il[-1]])
        if not hi > lo:
            return None
        eq = lo + s["eq.level"] * (hi - lo)
        return {"high": hi, "low": lo, "eq": eq, "ok": bool(v.l[m] <= eq + s["eq.tolerance"] * (hi - lo)),
                "position": round(float((v.l[m] - lo) / (hi - lo)), 3)}

    def _zones_hit(self, v: View, zones: list, m: int, start: int, judas: bool) -> list:
        s = self.s
        low = float(v.l[m])
        tol = s["key.tolerance_points"]
        min_tf = LBL[s["key.judas_min_tf"]] if judas else 0
        hit = []
        for z in zones:
            if z.known >= m or z.inval <= m or z.tf < min_tf or low > z.top + tol:
                continue
            info: dict = {}
            if z.kind == "fvg" and s["key.fvg_sweep_rule"]:
                a, b = z.known + 1, start
                if b >= a:
                    prior = v.rq_l.min(a, b)
                    if prior <= z.top:
                        if not low < prior:
                            continue
                        info["swept_inside_low"] = prior
            if z.kind == "cisd" and s["key.cisd_first_touch"]:
                a, b = z.known + 1, start - 1
                if b >= a and v.rq_l.min(a, b) <= z.top:
                    continue
            if z.kind == "rejection_block" and s["key.rb_needs_ce"] and low > z.extra["ce"]:
                continue
            hit.append((z, info))
        return hit

    def _make_setup(self, v: View, m: int, i: int, day: dict, zones: list) -> Setup | None:
        s = self.s
        st = self.stats
        so = int(self.b.since_open[m])
        judas = s["models.judas"] and self.so_930 <= so < self.so_judas_end
        model = "judas" if judas else "ny_4step"
        if not judas and (not s["models.ny_4step"] or so < self.so_manip):
            return None
        start = self._leg_start(v, m, i, judas, day["open_idx"], day["ds"])
        if v.rq_l.min(start, m) < v.l[m]:
            return None                                   # not the extreme of its leg
        size = float(v.h[start] - v.l[m])
        if size < s["leg.min_points"]:
            st["setup_rejected_leg_too_small"] += 1
            return None
        if judas and s["leg.judas_open_side"] and day["open_idx"] >= 0 and not v.l[m] < v.o[day["open_idx"]]:
            st["setup_rejected_no_open_manipulation"] += 1
            return None
        hits = self._zones_hit(v, zones, m, start, judas)
        if len(hits) < s["key.min_levels"]:
            return None
        eq = self._eq(v, m, day["ds"], day["mf"])
        if s["eq.enabled"] and eq is not None and not eq["ok"]:
            st["setup_rejected_not_in_discount_premium"] += 1
            return None
        liq = self._liquidity_check(v, start, m, day["ds"])
        if s["leg.require_sweep"] and liq["swept"] is None:
            st["setup_rejected_no_sweep"] += 1
            return None
        if s["leg.no_equal_extremes"] and liq["swept"] is None and liq["equal"] is not None:
            st["setup_rejected_equal_extremes"] += 1
            return None
        smt = self._smt(v, start, m)
        if s["filters.smt"] and smt["divergence"] is not True:
            st["setup_rejected_smt_unknown" if smt["divergence"] is None else "setup_rejected_no_smt"] += 1
            return None
        return Setup(model, m, start, float(v.l[m]), hits, {"eq": eq, "liq": liq, "leg_size": size, "smt": smt})

    # ------------------------------------------------------------------ confirmation
    def _ifg(self, v: View, su: Setup, i: int) -> dict | None:
        s = self.s
        per = {}
        for tf in IFG_TFS:
            if not s[f"ifg.tf_{tf}m"]:
                continue
            t = v.tf[tf]
            g = fvgs(t, "bear", s["ifg.min_gap_points"])
            k_lo, k_hi = int(t.of_bar[su.start]) + 2, int(t.of_bar[su.m])
            cur = int(t.of_bar[i])
            k_done = cur if t.comp[cur] <= i else cur - 1          # never read a candle that is still forming
            a, b = np.searchsorted(g.k, k_lo), np.searchsorted(g.k, k_hi, side="right")
            gaps = []
            for gi in range(a, b):
                if g.known[gi] > i:
                    continue
                k = int(g.k[gi])
                # still a gap at the extreme: no candle closed above it inside the leg
                up = min(k_hi, k_done)
                if k + 1 <= up and t.c[k + 1:up + 1].max(initial=-np.inf) > g.top[gi]:
                    continue
                gaps.append((k, float(g.top[gi]), float(g.bottom[gi])))
            if gaps:
                per[tf] = gaps
        if not per:
            return None
        rule = s["ifg.rule_judas"] if su.model == "judas" else s["ifg.rule_ny"]
        if rule == "highest":
            tf = max(per)
        elif rule == "lowest":
            tf = min(per)
        else:
            singles = [x for x in sorted(per) if len(per[x]) == 1]
            tf = singles[0] if singles else min(per)
        gaps = per[tf]
        level = max(gp[1] for gp in gaps) if s["ifg.all_gaps"] else min(gp[1] for gp in gaps)
        return {"tf": tf, "rule": rule, "gaps": gaps, "level": level, "per_tf": {TF_LABEL[k]: len(x) for k, x in per.items()}}

    def _trigger(self, v: View, su: Setup, i: int) -> dict | None:
        s = self.s
        f = self._ifg(v, su, i)
        if f is None:
            return None
        t = v.tf[f["tf"]]
        q = int(t.of_bar[i])
        if t.comp[q] != i:
            return None
        last_k = max(gp[0] for gp in f["gaps"])
        lvl = f["level"] + s["ifg.close_buffer"]
        if q <= last_k or not t.c[q] > lvl:
            return None
        if q - 1 > last_k and t.c[q - 1] > lvl:
            return None                                   # crossed earlier: not a fresh confirmation
        rng = float(t.h[q] - t.l[q])
        body = abs(float(t.c[q] - t.o[q]))
        prev = t.h[max(0, q - 20):q] - t.l[max(0, q - 20):q]
        avg = float(prev.mean()) if len(prev) else rng
        disp = {"body_ratio": round(body / rng, 3) if rng > 0 else 0.0,
                "range_x_avg": round(rng / avg, 3) if avg > 0 else 0.0}
        disp["ok"] = bool(disp["body_ratio"] >= s["ifg.body_ratio"] and disp["range_x_avg"] >= s["ifg.range_x_avg"])
        if s["ifg.displacement"] and not disp["ok"]:
            self.stats["confirmation_rejected_no_displacement"] += 1
            return None
        return {**f, "q": q, "candle": (float(t.o[q]), float(t.h[q]), float(t.l[q]), float(t.c[q])),
                "t_candle": int(t.t0[q]), "displacement": disp}

    # ------------------------------------------------------------------ orders
    def _targets(self, v: View, i: int, entry: float, risk: float, day: dict) -> tuple[float | None, dict]:
        s = self.s
        if s["target.mode"] == "fixed_r":
            return entry + s["target.fixed_r"] * risk, {"source": "fixed_r", "r": s["target.fixed_r"]}
        tfs = [tf for name, tf in (("1m", 1), ("5m", 5), ("15m", 15), ("1h", 60)) if s[f"target.swings_{name}"]]
        sess = {}
        if s["target.session_levels"]:
            sess["previous_day_high"] = self._prev_day(v, i)
            if day["mf"] > day["ds"]:
                sess["overnight_high"] = float(v.h[day["ds"]:day["mf"]].max())
        cands = self._liquidity_above(v, i, entry, tfs, s["target.swing_strength"],
                                      [5, 15, 60] if s["target.unfilled_fvg"] else [], sess, day["ds"], False)
        if s["target.draw"] and day.get("draw") and day["draw"]["price"] > entry:
            cands.append({**day["draw"], "kind": "draw:" + day["draw"]["kind"]})
        cands.sort(key=lambda c: c["price"])
        lo_px, hi_px = entry + s["target.min_r"] * risk, entry + s["target.max_r"] * risk
        info: dict = {"candidates": [{**c, "r": round((c["price"] - entry) / risk, 2)} for c in cands[:8]]}
        if not cands:
            if s["target.no_level"] == "skip":
                return None, {**info, "why": "no liquidity found"}
            return lo_px, {**info, "source": "min_r (no liquidity found)"}
        first = cands[0]
        chosen, how = first, "nearest liquidity"
        if first["price"] < lo_px:
            mode = s["target.below_min"]
            if mode == "skip":
                return None, {**info, "why": "nearest liquidity below the minimum R"}
            if mode == "extend":
                chosen, how = None, "extended to the minimum R"
            else:
                nxt = [c for c in cands if c["price"] >= lo_px]
                if not nxt:
                    chosen, how = None, "extended to the minimum R (no level beyond it)"
                else:
                    chosen, how = nxt[0], "next liquidity beyond the minimum R"
        price = lo_px if chosen is None else chosen["price"]
        if price > hi_px:
            if s["target.above_max"] == "skip":
                return None, {**info, "why": "liquidity beyond the maximum R"}
            price, how = hi_px, how + ", capped at the maximum R"
        eqs = [c for c in cands if c["kind"] == "swing_high" and abs(c["price"] - price) <= s["leg.equal_points"] * 2]
        return price, {**info, "source": how, "level": chosen, "stacked_liquidity": len(eqs) >= 2}

    def _sim(self, v: View, sig: int, entry_kind: str, level: float, stop: float, target: float, be_level: float,
             level_arr: np.ndarray) -> tuple[int, str]:
        """The strategy's own copy of the trade (see module docstring). Returns (exit bar, exit class)."""
        s = self.s
        n, b = self.n, self.b
        td0 = b.td[sig]
        f = sig + 1
        if f >= n or b.td[f] != td0:
            return sig, "none"
        if entry_kind == "market":
            fill = float(v.ent_o[f])
        else:
            last = min(n - 1, sig + int(s["entry.limit_minutes"]))
            f = -1
            for k in range(sig + 1, last + 1):
                if b.td[k] != td0 or self._force(k - 1):
                    break
                if v.ent_o[k] <= level:
                    f, fill = k, float(v.ent_o[k])
                    break
                if v.ent_l[k] <= level:
                    f, fill = k, float(level)
                    break
            if f < 0:
                return sig, "none"
        risk = fill - stop
        if not risk > 0:
            return sig, "none"
        cur = stop
        be_mode, tr = s["manage.be"], s["manage.trail"]
        extreme = max(fill, float(v.ex_h[f])) if entry_kind == "market" else fill
        tf_tr = {"swing_1m": 1, "swing_5m": 5}.get(tr)
        for k in range(f, n):
            if b.td[k] != td0:
                return k - 1, "close"
            # stop / target on this bar (stop first when both: the engine's conservative policy)
            if v.ex_l[k] <= cur:
                return k, "stop" if cur == stop else "trail"
            if v.ex_h[k] >= target:
                return k, "target"
            if self._force(k):
                return k + 1, "close"
            if k > f:
                extreme = max(extreme, float(v.ex_h[k]))
            cands = []
            if be_mode == "leg_swing" and be_level == be_level and extreme >= be_level:
                cands.append(fill + s["manage.be_offset"])
            elif be_mode == "r" and extreme - fill >= s["manage.be_r"] * risk:
                cands.append(fill + s["manage.be_offset"])
            if tf_tr and extreme - fill >= s["manage.trail_start_r"] * risk:
                sw = swings(v.tf[tf_tr], 2)["lo"]
                j = np.searchsorted(sw[2], k, side="right") - 1
                if j >= 0 and v.tf[tf_tr].end[int(sw[0][j])] >= f:
                    cands.append(float(sw[1][j]) - s["manage.trail_buffer"])
            if cands:
                cand = max(cands)
                level_arr[k] = cand
                if v.ex_c[k] - cand > 0 and cand - cur > 0:
                    cur = cand
        return n - 1, "close"

    def _force(self, k: int) -> bool:
        so = int(self.b.since_open[k])
        return so + 1 >= self.so_force and so < self.b.session_len

    # ------------------------------------------------------------------ main
    def run(self) -> tuple[SignalSet, dict, Counter]:
        s, b, n = self.s, self.b, self.n
        sig = SignalSet.empty(n)
        lvl = {1: np.full(n, np.nan), -1: np.full(n, np.nan)}
        manage = s["manage.be"] != "off" or s["manage.trail"] != "off"
        if n == 0:
            return sig, {}, self.stats
        so = b.since_open
        bounds = np.flatnonzero(np.r_[True, b.td[1:] != b.td[:-1]])
        ends = np.r_[bounds[1:] - 1, n - 1]
        prev_bias = 0
        for ds, de in zip(bounds, ends):
            ds, de = int(ds), int(de)
            seg = so[ds:de + 1]
            mf = ds + int(np.searchsorted(seg, self.so_manip))
            eu = ds + int(np.searchsorted(seg + 1, self.so_until, side="right")) - 1
            if mf > de or eu < mf or mf <= ds:
                continue
            if self.trade_from_td is not None and b.td[ds] < self.trade_from_td:
                continue
            wd = int((b.td[ds] + 3) % 7)
            day_info = {"date": str(np.datetime64(int(b.td[ds]), "D")), "trades": 0}
            self.stats["days"] += 1
            if (wd == 0 and s["session.skip_monday"]) or (wd == 4 and s["session.skip_friday"]):
                self.stats["days_skipped_weekday"] += 1
                continue
            d0 = mf - 1
            bias = self._bias(d0, prev_bias)
            prev_bias = bias["direction"] or prev_bias
            day_info["bias"] = bias["direction"]
            if bias["direction"] == 0:
                self.stats["days_no_bias"] += 1
                self.days.append(day_info)
                continue
            d = bias["direction"]
            allowed = s["models.direction"]
            if (allowed == "long_only" and d < 0) or (allowed == "short_only" and d > 0):
                self.stats["days_bias_direction_not_allowed"] += 1
                continue
            if d < 0 and s["bias.ath_rule"] and self._ath_block(d0):
                self.stats["days_short_blocked_near_high"] += 1
                continue
            chop = self._chop(d0) if s["filters.chop"] else None
            if chop is not None and not chop["ok"]:
                self.stats["days_choppy"] += 1
                continue
            v = self.views[d]
            open_idx = ds + int(np.searchsorted(seg, self.so_930))
            if open_idx > de or so[open_idx] != self.so_930:
                open_idx = -1
            day = {"ds": ds, "mf": mf, "eu": eu, "open_idx": open_idx}
            draw = self._draw(v, d0, ds)
            if draw is None and s["draw.required"]:
                self.stats["days_no_draw"] += 1
                self.days.append(day_info)
                continue
            day["draw"] = draw
            self.stats["days_with_bias_and_draw"] += 1
            self._scan_day(v, day, bias, chop, sig, lvl[d], day_info)
            self.days.append(day_info)
        trail = TrailSpec(mode="level") if manage else None
        if manage:
            sig.trail = trail
            sig.trail_level_long, sig.trail_level_short = lvl[1], -lvl[-1]
        exit_flags = (so + 1 >= self.so_force) & (so < b.session_len)
        sig.exit_long = exit_flags.copy()
        sig.exit_short = exit_flags.copy()
        sig.max_trades_per_day = int(s["day.max_trades"])
        win, loss = s["day.stop_after_win"], s["day.stop_after_loss"]
        sig.block_after = "any" if (win and loss) else "target" if win else "stop" if loss else None
        sig.exit_cooldown_bars = int(s["day.cooldown"])
        return sig, self.explain, self.stats

    def _scan_day(self, v: View, day: dict, bias: dict, chop, sig: SignalSet, lvl: np.ndarray, day_info: dict) -> None:
        s, b = self.s, self.b
        mf, eu, ds = day["mf"], day["eu"], day["ds"]
        zones = self._day_zones(v, mf, eu)
        if not zones:
            self.stats["days_no_key_level"] += 1
            return
        z_top = np.array([z.top for z in zones]) + s["key.tolerance_points"]
        z_known = np.array([z.known for z in zones])
        z_inval = np.array([z.inval for z in zones])
        su: Setup | None = None
        busy_until = -1
        trades = wins = losses = 0
        last_exit = -10**9
        win_block, loss_block = s["day.stop_after_win"], s["day.stop_after_loss"]
        for i in range(mf, eu + 1):
            if i <= busy_until:
                continue
            if trades >= s["day.max_trades"] or (win_block and wins) or (loss_block and losses):
                break
            low = v.l[i]
            if su is None or low < su.low:
                near = i > last_exit and bool(((z_known < i) & (z_inval > i) & (low <= z_top)).any())
                cand = self._make_setup(v, i, i, day, zones) if near else None
                if cand is not None:
                    if su is None:
                        self.stats["setups"] += 1
                    su = cand
                elif su is not None:
                    su = None
                    self.stats["setups_broken"] += 1
            if su is None:
                continue
            if i - su.m > s["ifg.max_wait"]:
                su = None
                self.stats["setups_no_confirmation_in_time"] += 1
                continue
            live = [(z, inf) for z, inf in su.zones if z.inval > i]
            if len(live) < s["key.min_levels"]:
                su = None
                self.stats["setups_level_invalidated"] += 1
                continue
            su.zones = live
            if b.since_open[i] + 1 < self.so_from:
                continue
            trg = self._trigger(v, su, i)
            if trg is None:
                continue
            res = self._order(v, su, trg, i, day, bias, chop)
            su = None
            if res is None:
                continue
            if i in self.skip:
                self.stats["signals_declined_in_review"] += 1
                last_exit = i
                continue
            entry_kind, entry_level, stop, target, be_level, expl = res
            sig.direction[i] = v.d
            sig.stop_price[i] = v.px(stop)
            sig.target_price[i] = v.px(target)
            if entry_kind != "market":
                sig.entry_price[i] = v.px(entry_level)
            exit_bar, cls = self._sim(v, i, entry_kind, entry_level, stop, target, be_level, lvl)
            if cls == "none":
                self.stats["signals_not_filled_in_own_tracking"] += 1
            else:
                trades += 1
                wins += cls == "target"
                losses += cls in ("stop", "trail")
            busy_until = max(i, exit_bar)
            last_exit = exit_bar
            self.explain[i] = expl
            self.stats["signals"] += 1
            day_info["trades"] += 1

    def _order(self, v: View, su: Setup, trg: dict, i: int, day: dict, bias: dict, chop) -> tuple | None:
        s, st = self.s, self.stats
        gaps = trg["gaps"]
        if s["entry.type"] == "market":
            entry_kind, entry = "market", float(v.ent_c[i])      # long: ASK close (what a buy pays), short: BID
        else:
            entry_kind, entry = "limit", float(trg["level"])
        o, h, l, c = trg["candle"]
        mode = s["stop.mode"]
        base = {"leg_extreme": su.low, "confirm_candle": l, "confirm_body": min(o, c),
                "gap_edge": min(gp[2] for gp in gaps)}[mode]
        stop = base - s["stop.buffer"]
        risk = entry - stop
        widened = False
        if risk < s["stop.min_points"]:
            stop, risk, widened = entry - s["stop.min_points"], s["stop.min_points"], True
        if risk > s["stop.max_points"]:
            st["signal_rejected_stop_too_wide"] += 1
            return None
        target, tinfo = self._targets(v, i, entry, risk, day)
        if target is None:
            st["signal_rejected_target"] += 1
            return None
        be_level = math.nan
        if s["manage.be"] == "leg_swing":
            lvl_ = float(v.h[su.start])
            if lvl_ - entry >= s["manage.be_min_r"] * risk and lvl_ < target:
                be_level = lvl_
        liq = su.info["liq"]
        eq = su.info["eq"]
        kinds = {z.kind for z, _ in su.zones}
        judas_open = day["open_idx"] >= 0 and v.l[su.m] < v.o[day["open_idx"]]
        checklist = {
            "bias_clear": bias["score"] is None or abs(bias["score"]) >= s["bias.min_score"],
            "draw_on_liquidity": day.get("draw") is not None,
            "discount_premium": None if eq is None else eq["ok"],
            "key_fvg": "fvg" in kinds,
            "key_cisd": "cisd" in kinds,
            "key_rejection_block": "rejection_block" in kinds,
            "key_bpr": "bpr" in kinds,
            "fresh_gap_or_swept_inside": any(z.kind == "fvg" for z, _ in su.zones),
            "several_key_levels": len(su.zones) >= 2,
            "liquidity_swept": liq["swept"] is not None,
            "no_equal_lows_highs": liq["equal"] is None or liq["swept"] is not None,
            "open_manipulation": bool(judas_open),
            "inversion_gap": True,
            "highest_timeframe_gap": trg["tf"] == max(int(k.rstrip("m")) for k in trg["per_tf"]),
            "displacement": trg["displacement"]["ok"],
            "smt": su.info["smt"]["divergence"],
            "target_at_liquidity": "liquidity" in tinfo.get("source", ""),
            "stacked_liquidity_at_target": bool(tinfo.get("stacked_liquidity")),
            "not_choppy": None if chop is None else chop["ok"],
        }
        scored = ("liquidity_swept", "several_key_levels", "displacement", "discount_premium",
                  "stacked_liquidity_at_target", "open_manipulation") + (("smt",) if s["filters.smt_in_score"] else ())
        quality = sum(bool(checklist[k]) for k in scored)
        if quality < s["filters.min_quality"]:
            st["signal_rejected_quality"] += 1
            return None
        P = v.px
        zones = []
        for z, inf in su.zones:
            top, bottom = v.zone(z.top, z.bottom)
            zz = {"kind": z.kind, "tf": TF_LABEL[z.tf], "top": top, "bottom": bottom, "t_from": z.t_from,
                  "known_ts": self._ts(z.known)}
            if "ce" in z.extra:
                zz["ce"] = P(z.extra["ce"])
            if z.extra.get("from_gap"):
                fg = z.extra["from_gap"]
                t2, b2 = v.zone(fg["top"], fg["bottom"])
                zz["from_gap"] = {"tf": fg["tf"], "top": t2, "bottom": b2}
            if "swept_inside_low" in inf:
                zz["swept_inside_level"] = P(inf["swept_inside_low"])
            zones.append(zz)

        def lv(x):
            if not x:
                return None
            out = {**x, "price": P(x["price"])}
            if "top" in out:
                out["top"] = P(x["top"])
            return out
        draw = day.get("draw")
        expl = {
            "model": su.model, "direction": v.d, "signal_bar": i, "signal_ts": self._ts(i),
            "bias": bias, "draw": lv(draw) if draw else None,
            "eq": None if eq is None else {"high": P(eq["high"]) if v.d > 0 else P(eq["low"]),
                                           "low": P(eq["low"]) if v.d > 0 else P(eq["high"]),
                                           "eq": P(eq["eq"]), "ok": eq["ok"], "position": eq["position"]},
            "key_levels": zones,
            "leg": {"start_ts": self._ts(su.start), "start_price": P(v.h[su.start]), "end_ts": self._ts(su.m),
                    "end_price": P(su.low), "size_points": round(su.info["leg_size"], 2),
                    "swept": lv(liq["swept"]), "equal": lv(liq["equal"])},
            "confirmation": {"tf": TF_LABEL[trg["tf"]], "rule": trg["rule"], "level": P(trg["level"]),
                             "gaps_per_tf": trg["per_tf"],
                             "gaps": [{"top": v.zone(gp[1], gp[2])[0], "bottom": v.zone(gp[1], gp[2])[1],
                                       "t_from": int(v.tf[trg["tf"]].t0[gp[0] - 2])} for gp in gaps],
                             "candle_ts": trg["t_candle"], "candle": [P(x) for x in trg["candle"]],
                             "displacement": trg["displacement"]},
            "entry": {"type": entry_kind, "reference_price": P(entry)},
            "stop": {"mode": mode, "price": P(stop), "widened_to_minimum": widened, "risk_points": round(risk, 2)},
            "target": {"price": P(target), "r_planned": round((target - entry) / risk, 2),
                       "source": tinfo.get("source"), "level": lv(tinfo.get("level")),
                       "candidates": [lv(c) for c in tinfo.get("candidates", [])]},
            "breakeven": {"mode": s["manage.be"], "level": None if be_level != be_level else P(be_level)},
            "chop": chop, "checklist": checklist, "quality": quality,
            "smt": {k: (P(x) if k in ("nq_ref", "nq_extreme", "es_ref", "es_extreme") and x is not None else
                        x.replace("low", "high") if k == "reason" and v.d < 0 else x)
                    for k, x in su.info["smt"].items()},
        }
        return entry_kind, entry, stop, target, be_level, expl
