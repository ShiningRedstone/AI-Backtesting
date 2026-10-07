"""Level map (ADR-108): at a 15-minute candle open in New York hours, which price levels are ahead, which one price taps
first, whether it reacts there, and where price lands. LIVE knowledge only, scored walk-forward against honest
baselines, never on the holdout (except the one recorded look in holdout.py).

Decision times: 9:30, 10:00 ... 15:30 New York (every 30 minutes, opens of 15-minute candles).

Candidate levels at a decision (everything known before it; fixed definitions, not tuned):
* open FVGs of the 5m, 15m, 1h, 4h and daily charts (formed and not yet filled): the next untraded part of the zone -
  its near edge while untouched, its middle (CE) once tapped, its far edge once past the middle. Same-direction zones
  that overlap or lie within 0.25 x ATR(15m) of each other form a STACK (size, rank inside it, timeframes in it);
* the dealing range of the 15m, 1h and 4h charts = the last confirmed swing high and swing low (2 bars each side):
  its 50 % equilibrium (EQ) and the OTE retracements 0.618 / 0.705 / 0.79 of the leg (measured back from the later
  swing);
* liquidity: untaken swing highs / lows (15m, 1h, 4h), equal highs / lows (15m, 1h), previous day / week /
  regular-session high and low, Asia and London high and low, today's high and low so far, the 18:00, midnight and
  9:30 opens.
Kept: levels within 10 ATR(15m), per side the 5 nearest plus the nearest of every kind.

Targets (1-minute bars after the decision, until the trading date ends at 16:15):
* REACH 2H / REACH SESSION: is the level traded within 2 hours / before the session ends;
* REACTS: after its first touch, does price move 1 ATR(15m) away from the level before 1 ATR through it (the
  touch minute counts as 'through' when it already went 1 ATR through; otherwise the race starts at the next minute;
  both in one minute = unknown, left out). On a random walk this is 50 %;
* FIRST: is the nearest level above traded before the nearest level below;
* LANDS 2H / LANDS SESSION: where price is 2 hours later / at the session's last minute (in ATR(15m) from now);
* TURNING LEVEL (derived): per side, the first level price reaches AND reacts at (or none).

Baselines (the opponents a model must beat): distance-only logistic for REACH (how far, how much time), the
random-walk 'gambler's ruin' probability for FIRST (P(up first) = distance below / (distance above + below)), the
training period's reaction rate for REACTS, 'no change' for LANDS. Models, walk-forward, scoring, 70 / 30 choice:
forecast.py. The MISTAKES report breaks the scored predictions down (session, news, volatility, level kind, chart,
distance, with / against the 1h trend, weekday) - descriptive, never fed back into the models.
"""
from __future__ import annotations

import math

import numpy as np

from edgelab.market import data as D
from edgelab.market import forecast as F
from edgelab.market import gbm as G
from edgelab.market import patterns as P

DECISION_TIMES = tuple(range(570, 960, 30))                # 9:30 ... 15:30 New York
HORIZON_MIN = 120
OTE = (0.618, 0.705, 0.79)
MAX_DIST = 10.0                                            # ATR(15m)
NEAREST = 5                                                # per side, plus the nearest of every kind
STACK_GAP = 0.25                                           # ATR(15m)
NEAR = 0.25                                                # confluence distance, ATR(15m)
ZONE_TFS = (5, 15, 60, 240, D.DAY)
RANGE_TFS = (15, 60, 240)
EQUAL_TFS = (15, 60)
LOOKBACK_DAYS = {5: 3, 15: 7, 60: 25, 240: 60, D.DAY: 260}
GROUPS = ("fvg", "eq", "ote", "swing", "equal", "prev_day", "prev_week", "prev_rth", "asia", "london", "today", "open")
GROUP_WORDS = {"fvg": "FVG", "eq": "Equilibrium (50 %)", "ote": "OTE fib", "swing": "Swing high / low",
               "equal": "Equal highs / lows", "prev_day": "Previous day", "prev_week": "Previous week",
               "prev_rth": "Previous regular session", "asia": "Asia session", "london": "London session",
               "today": "Today's high / low", "open": "Session opens"}
TF_KEYS = (5, 15, 60, 240, D.DAY, 0)                       # 0 = not tied to a chart
LEVEL_TARGETS = ("reach2h", "reach", "react")
DECISION_TARGETS = ("first", "land2h", "land")
TARGET_WORDS = {"reach2h": "Level traded within 2 hours", "reach": "Level traded before the session ends",
                "react": "Reacts at the level (1 ATR away before 1 ATR through)",
                "first": "Nearest level above traded before the nearest below",
                "land2h": "Where price is 2 hours later", "land": "Where price is at the session end",
                "turn": "The level price turns at"}
BASE_COLS = 8                                              # the random-walk baseline's columns: distance, time left and the
                                                           # current volatility (first in the matrix)
GBM_EVERY = 6
GBM_ROWS = 40_000
DIRECTIONAL = ("move15_lag", "move60", "move_day", "trend_", "es_move", "nq_minus_es60", "smt_last_hour",
               "sweeps_last_hour", "shock_last_30min", "news_last_surprise_z", "fvg15m_inside", "fvg1h_inside",
               "fvg4h_inside")
BIG = np.iinfo(np.int64).max


# =============================================================================================== level sources
class Source:
    """Zones, swings, equal highs / lows of every chart with the times they were touched (computed once)."""

    def __init__(self, cx: F.Context):
        m = cx.m
        self.cx = cx
        self.zones, self.swing, self.equal = {}, {}, {}

        def when(i0, lvl, side):
            j = D.first_touch(m, i0, lvl, side)
            return np.where(j >= 0, m.ts[np.maximum(j, 0)], BIG).astype(np.int64)
        for tf in ZONE_TFS:
            b, a = cx.bars[tf], cx.atr[tf]
            if len(b) < 4:
                continue
            f = P.fvgs(b, a)
            if not len(f["k"]):
                continue
            kn = b.known_ns[f["k"]].astype(np.int64)
            d, top, bot = f["dir"], f["top"], f["bottom"]
            i0 = np.searchsorted(m.ts, kn, side="left")
            side = (-d).astype(np.int8)                     # a bullish zone (below) is reached from above
            near, mid, far = np.where(d > 0, top, bot), (top + bot) / 2, np.where(d > 0, bot, top)
            self.zones[tf] = {"known": kn, "dir": d, "top": top, "bottom": bot, "t_touch": when(i0, near, side),
                              "t_ce": when(i0, mid, side), "t_fill": when(i0, far, side)}
        for tf in RANGE_TFS:
            b = cx.bars[tf]
            s = P.swings(b, 2)
            out = {}
            for key, side, px in (("hi", 1, b.h), ("lo", -1, b.l)):
                k = s[key]
                kn = b.known_ns[np.minimum(k + 2, len(b) - 1)].astype(np.int64) if len(k) else np.zeros(0, np.int64)
                lvl = px[k]
                i0 = np.searchsorted(m.ts, kn, side="left")
                out[key] = {"k": k, "known": kn, "price": lvl,
                            "taken": when(i0, lvl, np.full(len(k), side, np.int8)) if len(k) else np.zeros(0, np.int64)}
            self.swing[tf] = out
            if tf in EQUAL_TFS:
                a = cx.atr[tf]
                eq = {}
                for key, side in (("hi", 1), ("lo", -1)):
                    sw = out[key]
                    if len(sw["k"]) < 2:
                        eq[key] = {"known": np.zeros(0, np.int64), "price": np.zeros(0), "taken": np.zeros(0, np.int64)}
                        continue
                    p1, p2 = sw["price"][:-1], sw["price"][1:]
                    tol = P.EQ_TOL_ATR * np.nan_to_num(a[sw["k"][1:]], nan=0.0)
                    ok = np.abs(p1 - p2) <= tol
                    lvl = (np.maximum(p1, p2) if side > 0 else np.minimum(p1, p2))[ok]
                    kn = sw["known"][1:][ok]
                    i0 = np.searchsorted(m.ts, kn, side="left")
                    eq[key] = {"known": kn, "price": lvl, "taken": when(i0, lvl, np.full(len(kn), side, np.int8))}
                self.equal[tf] = eq


def _candidates(src: Source, t: int, px: float, a15: float, di: int, hi_so: float, lo_so: float,
                day_first_i: int) -> list[tuple]:
    """(price, group, tf, ote ratio, zone low, zone high, state, known_ns, label) of every level known before t."""
    cx, m = src.cx, src.cx.m
    out = []
    for tf, z in src.zones.items():
        a = np.searchsorted(z["known"], t - LOOKBACK_DAYS[tf] * 86_400_000_000_000, side="left")
        b = np.searchsorted(z["known"], t, side="right")
        q = np.arange(a, b)
        q = q[z["t_fill"][q] >= t]                          # filled before t: gone
        if not len(q):
            continue
        d, top, bot = z["dir"][q], z["top"][q], z["bottom"][q]
        st = np.where(z["t_touch"][q] >= t, 0, np.where(z["t_ce"][q] >= t, 1, 2))
        lvl = np.where(st == 0, np.where(d > 0, top, bot), np.where(st == 1, (top + bot) / 2, np.where(d > 0, bot, top)))
        tfl = D.TF_LABEL[tf]
        for n in range(len(q)):
            out.append((lvl[n], "fvg", tf, 0.0, bot[n], top[n], int(st[n]), int(z["known"][q[n]]),
                        f"{tfl} {'bullish' if d[n] > 0 else 'bearish'} FVG {('edge', 'CE', 'far edge')[st[n]]}"))
    for tf, sw in src.swing.items():
        tfl = D.TF_LABEL[tf]
        last = {}
        for key in ("hi", "lo"):
            s = sw[key]
            j = np.searchsorted(s["known"], t, side="right")
            last[key] = j - 1 if j > 0 else None
            lo_j = np.searchsorted(s["known"], t - LOOKBACK_DAYS[tf] * 86_400_000_000_000, side="left")
            for q in range(lo_j, j):
                if s["taken"][q] >= t:                      # untaken liquidity
                    out.append((s["price"][q], "swing", tf, 0.0, s["price"][q], s["price"][q], 0, int(s["known"][q]),
                                f"{tfl} swing {'high' if key == 'hi' else 'low'} (untaken)"))
        if last["hi"] is not None and last["lo"] is not None:
            h, lo = sw["hi"]["price"][last["hi"]], sw["lo"]["price"][last["lo"]]
            if h > lo:
                up_leg = sw["hi"]["k"][last["hi"]] > sw["lo"]["k"][last["lo"]]
                kn = int(max(sw["hi"]["known"][last["hi"]], sw["lo"]["known"][last["lo"]]))
                out.append(((h + lo) / 2, "eq", tf, 0.5, lo, h, 0, kn, f"{tfl} equilibrium (50 %)"))
                for r in OTE:
                    lvl = h - r * (h - lo) if up_leg else lo + r * (h - lo)
                    out.append((lvl, "ote", tf, r, lo, h, 0, kn, f"{tfl} OTE {r:g}"))
    for tf, eq in src.equal.items():
        for key in ("hi", "lo"):
            e = eq[key]
            j = np.searchsorted(e["known"], t, side="right")
            lo_j = np.searchsorted(e["known"], t - LOOKBACK_DAYS[tf] * 86_400_000_000_000, side="left")
            for q in range(lo_j, j):
                if e["taken"][q] >= t:
                    out.append((e["price"][q], "equal", tf, 0.0, e["price"][q], e["price"][q], 0, int(e["known"][q]),
                                f"{D.TF_LABEL[tf]} equal {'highs' if key == 'hi' else 'lows'}"))
    lv = cx.lv
    for key, grp in (("pdh", "prev_day"), ("pdl", "prev_day"), ("pwh", "prev_week"), ("pwl", "prev_week"),
                     ("prth_hi", "prev_rth"), ("prth_lo", "prev_rth"), ("asia_hi", "asia"), ("asia_lo", "asia"),
                     ("lon_hi", "london"), ("lon_lo", "london")):
        v = lv[key][di]
        if np.isfinite(v):
            out.append((v, grp, 0, 0.0, v, v, 0, 0, F.LEVEL_WORDS[key]))
    if np.isfinite(hi_so):
        out.append((hi_so, "today", 0, 0.0, hi_so, hi_so, 0, 0, "Today's high so far"))
        out.append((lo_so, "today", 0, 0.0, lo_so, lo_so, 0, 0, "Today's low so far"))
    i_t = int(np.searchsorted(m.ts, t, side="left"))
    if 0 <= day_first_i < i_t:
        out.append((m.o[day_first_i], "open", 0, 0.0, m.o[day_first_i], m.o[day_first_i], 0, 0, "18:00 open"))
        seg = m.ny_min[day_first_i:i_t]
        mid = np.flatnonzero(seg < 1080)
        if len(mid):
            v = m.o[day_first_i + mid[0]]
            out.append((v, "open", 0, 0.0, v, v, 0, 0, "Midnight open"))
        r = np.flatnonzero(seg == 570)
        if len(r):
            v = m.o[day_first_i + r[0]]
            out.append((v, "open", 0, 0.0, v, v, 0, 0, "9:30 open"))
    return out


# =============================================================================================== rows
def _decisions(cx: F.Context, start_ns: int | None = None) -> list[tuple]:
    m = cx.m
    days = np.unique(m.day)
    ends = np.searchsorted(m.day, days, side="right") - 1
    starts = np.searchsorted(m.day, days, side="left")
    pts = []
    for d, s, e in zip(days, starts, ends):
        for mm in DECISION_TIMES:
            tt = F._ny_ts(np.datetime64(d, "D"), mm)
            if tt is None or (start_ns is not None and tt < start_ns):
                continue
            i = np.searchsorted(m.ts, tt, side="left")
            if i < len(m) and m.ts[i] == tt and m.day[i] == d and i < e:
                pts.append((tt, int(i), int(e), int(s)))
    return pts


def build(cx: F.Context, start_ns: int | None = None, progress=None) -> dict:
    """Every decision (``t`` >= start_ns) with its candidate levels, features and outcomes."""
    step = progress or (lambda s: None)
    m = cx.m
    step("Level map: finding every FVG, dealing range and liquidity level on 5m ... 1D")
    src = Source(cx)
    pts = _decisions(cx, start_ns)
    if not pts:
        return {"n_dec": 0}
    t_dec = np.array([p[0] for p in pts], np.int64)
    step(f"Level map: live inputs of {len(pts)} decision moments")
    Xg, gnames, info = F.features(cx, t_dec)
    labels: dict[str, int] = {}
    R = {k: [] for k in ("dec", "price", "side", "dist", "group", "tf", "ote", "width", "state", "age", "stack_size",
                         "stack_rank", "stack_tfs", "confluence", "traded_today", "room", "rank", "label",
                         "reach2h", "reach", "react", "touch_ns")}
    Dd = {k: [] for k in ("t", "px", "a15", "day", "first", "gambler", "d_above", "d_below", "n_above3", "n_below3",
                          "above_htf", "below_htf", "land2h", "land", "news_ahead", "news_before")}
    hi_news = cx.news_ts[np.array([e["impact"] for e in cx.news], np.int8) >= 3] if cx.news else np.zeros(0, np.int64)
    step("Level map: what happened at every level (touched, reacted) and where price landed")
    for q, (t, i, e, s) in enumerate(pts):
        px, a15 = info["px"][q], info["a15"][q]
        if not (np.isfinite(px) and np.isfinite(a15) and a15 > 0):
            continue
        cand = _candidates(src, t, px, a15, int(info["di"][q]), info["hi_so"][q], info["lo_so"][q], s)
        if not cand:
            continue
        price = np.array([c[0] for c in cand], float)
        ok = np.isfinite(price) & (price != px) & (np.abs(price - px) / a15 <= MAX_DIST)
        if not ok.any():
            continue
        cand = [c for c, k in zip(cand, ok) if k]
        price = price[ok]
        dist = (price - px) / a15
        side = np.sign(dist).astype(np.int8)
        grp = np.array([GROUPS.index(c[1]) for c in cand])
        tfk = np.array([TF_KEYS.index(c[2]) for c in cand])
        zlo = np.array([c[4] for c in cand], float)
        zhi = np.array([c[5] for c in cand], float)
        # stacks: same-direction FVG zones overlapping or within STACK_GAP of each other (per side, outward from price)
        stack_size = np.ones(len(cand))
        stack_rank = np.zeros(len(cand))
        stack_tfs = np.ones(len(cand))
        for sd in (1, -1):
            fz = np.flatnonzero((side == sd) & (grp == 0))
            if len(fz) < 2:
                continue
            fz = fz[np.argsort(np.abs(dist[fz]), kind="stable")]
            groups, cur, edge = [], [fz[0]], (zhi[fz[0]] if sd > 0 else zlo[fz[0]])
            for z in fz[1:]:
                near_edge = zlo[z] if sd > 0 else zhi[z]
                if (near_edge - edge) * sd <= STACK_GAP * a15:
                    cur.append(z)
                    edge = max(edge, zhi[z]) if sd > 0 else min(edge, zlo[z])
                else:
                    groups.append(cur)
                    cur, edge = [z], (zhi[z] if sd > 0 else zlo[z])
            groups.append(cur)
            for gidx in groups:
                tfs = len({tfk[z] for z in gidx})
                for r, z in enumerate(gidx):
                    stack_size[z], stack_rank[z], stack_tfs[z] = len(gidx), r, tfs
        conf = (np.abs(price[:, None] - price[None, :]) <= NEAR * a15).sum(1) - 1
        # keep: per side the NEAREST plus the nearest of every kind
        keep = np.zeros(len(cand), bool)
        rank = np.zeros(len(cand))
        for sd in (1, -1):
            ss = np.flatnonzero(side == sd)
            if not len(ss):
                continue
            ss = ss[np.argsort(np.abs(dist[ss]), kind="stable")]
            rank[ss] = np.arange(len(ss))
            keep[ss[:NEAREST]] = True
            for g in np.unique(grp[ss]):
                keep[ss[grp[ss] == g][0]] = True
        kk = np.flatnonzero(keep)
        # outcomes
        lvl = price[kk]
        sd_k = side[kk]
        j = D.first_touch(m, np.full(len(kk), i), lvl, sd_k, horizon_days=0)
        touched = (j >= 0) & (j <= e)
        t2h = t + HORIZON_MIN * D.MIN_NS
        reach2h = touched & (m.ts[np.maximum(j, 0)] < t2h)
        react = np.full(len(kk), np.nan)
        if touched.any():
            jj = j[touched]
            sdt = sd_k[touched]
            lt = lvl[touched]
            through_now = np.where(sdt > 0, m.h[jj] >= lt + a15, m.l[jj] <= lt - a15)
            r = P.race(m, jj + 1, lt + a15, lt - a15, np.maximum(e - jj, 0))
            ok_r = (jj + 1 <= e)
            away = np.where(sdt > 0, r == -1, r == 1)
            res = np.where(through_now, 0.0, np.where(ok_r & (r != 0), away.astype(float), np.nan))
            react[touched] = res
        dec_id = len(Dd["t"])
        for n_, z in enumerate(kk):
            c = cand[z]
            lab = labels.setdefault(c[8], len(labels))
            beyond = np.abs(dist[(side == side[z]) & (np.abs(dist) > abs(dist[z]))])
            R["dec"].append(dec_id)
            R["price"].append(lvl[n_])
            R["side"].append(int(side[z]))
            R["dist"].append(dist[z])
            R["group"].append(int(grp[z]))
            R["tf"].append(int(tfk[z]))
            R["ote"].append(c[3])
            R["width"].append((c[5] - c[4]) / a15 if c[1] == "fvg" else 0.0)
            R["state"].append(c[6])
            R["age"].append(math.log1p(max(0.0, (t - c[7]) / 3.6e12)) if c[7] else 0.0)
            R["stack_size"].append(stack_size[z])
            R["stack_rank"].append(stack_rank[z])
            R["stack_tfs"].append(stack_tfs[z])
            R["confluence"].append(conf[z])
            R["traded_today"].append(float(np.isfinite(info["hi_so"][q]) and info["lo_so"][q] <= lvl[n_] <= info["hi_so"][q]))
            R["room"].append(min(10.0, beyond.min() - abs(dist[z])) if len(beyond) else 10.0)
            R["rank"].append(rank[z])
            R["label"].append(lab)
            R["reach2h"].append(bool(reach2h[n_]))
            R["reach"].append(bool(touched[n_]))
            R["react"].append(react[n_])
            R["touch_ns"].append(int(m.ts[j[n_]]) if touched[n_] else 0)
        # decision outcomes
        ab = np.flatnonzero(side[kk] > 0)
        be = np.flatnonzero(side[kk] < 0)
        da = np.abs(dist[kk][ab]).min() if len(ab) else np.nan
        db = np.abs(dist[kk][be]).min() if len(be) else np.nan
        first = np.nan
        if len(ab) and len(be):
            ja = j[ab[np.argmin(np.abs(dist[kk][ab]))]]
            jb = j[be[np.argmin(np.abs(dist[kk][be]))]]
            ja = ja if 0 <= ja <= e else -1
            jb = jb if 0 <= jb <= e else -1
            if ja >= 0 and (jb < 0 or ja < jb):
                first = 1.0
            elif jb >= 0 and (ja < 0 or jb < ja):
                first = 0.0
        Dd["t"].append(t)
        Dd["px"].append(px)
        Dd["a15"].append(a15)
        Dd["day"].append(m.day[i])
        Dd["first"].append(first)
        Dd["gambler"].append(db / (da + db) if np.isfinite(da) and np.isfinite(db) else 0.5)
        Dd["d_above"].append(da if np.isfinite(da) else MAX_DIST)
        Dd["d_below"].append(db if np.isfinite(db) else MAX_DIST)
        Dd["n_above3"].append(float(((dist > 0) & (dist <= 3)).sum()))
        Dd["n_below3"].append(float(((dist < 0) & (dist >= -3)).sum()))
        Dd["above_htf"].append(float(len(ab) > 0 and TF_KEYS[tfk[kk][ab[np.argmin(np.abs(dist[kk][ab]))]]] in (60, 240, D.DAY)))
        Dd["below_htf"].append(float(len(be) > 0 and TF_KEYS[tfk[kk][be[np.argmin(np.abs(dist[kk][be]))]]] in (60, 240, D.DAY)))
        i2 = min(e, int(np.searchsorted(m.ts, t2h, side="left")) - 1)
        Dd["land2h"].append(float(np.clip((m.c[i2] - px) / a15, -20, 20)))
        Dd["land"].append(float(np.clip((m.c[e] - px) / a15, -20, 20)))
        Dd["news_ahead"].append(float(((hi_news >= t) & (hi_news < t2h)).any()) if len(hi_news) else 0.0)
        Dd["news_before"].append(float(((hi_news >= t - 60 * D.MIN_NS) & (hi_news < t)).any()) if len(hi_news) else 0.0)
        Dd.setdefault("q", []).append(q)
    rows = {k: np.array(v) for k, v in R.items()}
    dec = {k: np.array(v) for k, v in Dd.items()}
    if not len(dec.get("t", [])):
        return {"n_dec": 0}
    dec["day"] = dec["day"].astype("datetime64[D]")
    dec["X"] = Xg[dec["q"].astype(int)]
    dec["minutes_left"] = np.array([(m.ts[e] - t) / D.MIN_NS for (t, i, e, s) in pts])[dec["q"].astype(int)]
    rows["dec"] = rows["dec"].astype(np.int64)
    rows["label_words"] = np.array(sorted(labels, key=labels.get) or [""], dtype=str)
    return {"n_dec": len(dec["t"]), "rows": rows, "dec": dec, "gnames": gnames}


# =============================================================================================== matrices
def level_matrix(b: dict) -> tuple[np.ndarray, list[str]]:
    r, dec = b["rows"], b["dec"]
    di = r["dec"]
    Xg = dec["X"][di]
    gn = b["gnames"]
    left = dec["minutes_left"][di]
    ad = np.abs(r["dist"])
    left2 = np.minimum(left, HORIZON_MIN)
    vol = np.exp(np.clip(Xg[:, gn.index("size60")], -3, 3))          # the last hour's size vs usual
    cols = [ad, np.log1p(ad), r["side"].astype(float), ad / np.sqrt(np.maximum(left2, 1) / 15),
            ad / np.sqrt(np.maximum(left, 1) / 15), left / 405, np.log(vol), ad / vol]
    names = ["abs_dist", "log_dist", "side", "dist_per_time_2h", "dist_per_time_session", "time_left",
             "volatility_now", "dist_per_volatility"]
    for k in ("rank", "width", "state", "age", "ote", "stack_size", "stack_rank", "stack_tfs", "confluence",
              "traded_today", "room"):
        cols.append(r[k].astype(float))
        names.append(k)
    for gi, g in enumerate(GROUPS):
        cols.append((r["group"] == gi).astype(float))
        names.append("kind_" + g)
    for ti, tf in enumerate(TF_KEYS):
        cols.append((r["tf"] == ti).astype(float))
        names.append("chart_" + (D.TF_LABEL[tf] if tf else "none"))
    for j, n in enumerate(gn):
        if n.startswith(DIRECTIONAL):
            cols.append(r["side"] * Xg[:, j])
            names.append("toward_" + n)
    X = np.column_stack(cols + [Xg])
    return X, names + list(gn)


def decision_matrix(b: dict) -> tuple[np.ndarray, list[str]]:
    d = b["dec"]
    cols = [d["d_above"], d["d_below"], np.log((d["d_above"] + 0.05) / (d["d_below"] + 0.05)), d["n_above3"],
            d["n_below3"], d["above_htf"], d["below_htf"], d["gambler"]]
    names = ["dist_nearest_above", "dist_nearest_below", "above_vs_below", "levels_above_3atr", "levels_below_3atr",
             "nearest_above_htf", "nearest_below_htf", "random_walk_up_first"]
    return np.column_stack(cols + [d["X"]]), names + list(b["gnames"])


SIM_LEVEL = ("abs_dist", "log_dist", "dist_per_time_session", "time_left", "rank", "stack_size", "confluence",
             "toward_trend_1h", "toward_move_day", "size60")
SIM_DEC = ("dist_nearest_above", "dist_nearest_below", "above_vs_below", "move_day", "trend_1h", "size60", "move60")


def _targets(b: dict) -> dict:
    r, d = b["rows"], b["dec"]
    return {"reach2h": r["reach2h"].astype(float), "reach": r["reach"].astype(float), "react": r["react"].astype(float),
            "first": d["first"].astype(float), "land2h": d["land2h"].astype(float), "land": d["land"].astype(float)}


def _masks(b: dict, y: dict) -> dict:
    """Rows a target is defined on (reaction: touched levels with a known outcome; first: both sides, one first)."""
    return {k: np.isfinite(v) for k, v in y.items()}


# =============================================================================================== walk-forward
def run(cx: F.Context, out_dir, progress=None) -> dict:
    """Walk-forward predictions of every level-map target on the discovery period; writes levelmap.npz."""
    from pathlib import Path
    step = progress or (lambda s: None)
    b = build(cx, progress=step)
    if not b["n_dec"]:
        return {"missing": True}
    r, d = b["rows"], b["dec"]
    XL, lnames = level_matrix(b)
    XD, dnames = decision_matrix(b)
    y = _targets(b)
    mk = _masks(b, y)
    lday = d["day"][r["dec"]]
    lslot = r["group"]
    lsim = [lnames.index(c) for c in SIM_LEVEL if c in lnames]
    dsim = [dnames.index(c) for c in SIM_DEC if c in dnames]
    dslot = P.ny_minutes(d["t"]) // 30
    preds, res = {}, {"decisions": int(b["n_dec"]), "levels": int(len(r["dec"])), "inputs_level": lnames,
                      "inputs_decision": dnames, "targets": {}}
    for tgt in LEVEL_TARGETS + DECISION_TARGETS:
        lev = tgt in LEVEL_TARGETS
        msk = mk[tgt]
        X, days = (XL, lday) if lev else (XD, d["day"])
        kind = "real" if tgt.startswith("land") else "binary"
        kw = {"gbm_every": GBM_EVERY, "gbm_rows": GBM_ROWS, "fit_mask": msk}
        if tgt in ("reach2h", "reach"):
            kw["base_X"] = X[:, :BASE_COLS]
        elif tgt == "first":
            kw["base_p"] = d["gambler"]
        # every row is PREDICTED (live: nobody knows yet whether a level will be touched); only rows with a known
        # outcome are learned from and scored
        yy = np.where(msk, y[tgt], 0.0)
        wf = F.walk_forward(X, yy, days, lslot if lev else dslot, kind, step, "Level map: " + TARGET_WORDS[tgt],
                            sim_cols=lsim if lev else dsim, sim_exact=lev, **kw)
        preds[tgt] = wf["pred"]
        ev = F.evaluate({k: v[msk] for k, v in wf["pred"].items()}, y[tgt][msk], days[msk].astype("datetime64[M]"), kind,
                        days[msk])
        res["targets"][tgt] = ev
    q2 = {}
    for tgt in ("land2h", "land"):
        ch = res["targets"][tgt].get("chosen") or "logistic"
        q, cover = F.bands(preds[tgt][ch], y[tgt], d["day"].astype("datetime64[M]"))
        q2[tgt] = q
        res["targets"][tgt]["bands"] = cover
    chosen = {t: res["targets"][t].get("chosen") or "logistic" for t in res["targets"]}
    res["targets"]["turn"] = turn_scores(b, preds, chosen, y)
    res["by_kind"] = describe(b)
    res["volatility_cuts"] = volatility_cuts(b)
    res["mistakes"] = {t: mistakes(b, preds[t], y[t], t, chosen[t], res["volatility_cuts"],
                                   late_from=_cut_month(res["targets"][t])) for t in LEVEL_TARGETS + DECISION_TARGETS}
    save(Path(out_dir) / "levelmap.npz", b, preds, q2)
    return res


def _cut_month(ev: dict) -> str | None:
    rep = ev.get("reported_on") or ""
    return rep.replace("months from ", "") or None


def save(path, b: dict, preds: dict, q2: dict) -> None:
    r, d = b["rows"], b["dec"]
    np.savez_compressed(
        path, dec_t=d["t"], dec_px=d["px"], dec_a15=d["a15"], dec_day=d["day"].astype(np.int64),
        dec_first=d["first"], dec_gambler=d["gambler"], dec_land2h=d["land2h"], dec_land=d["land"],
        row_dec=r["dec"], row_price=r["price"], row_side=r["side"], row_dist=r["dist"].astype(np.float32),
        row_group=r["group"].astype(np.int8), row_tf=r["tf"].astype(np.int8), row_label=r["label"].astype(np.int32),
        row_stack=r["stack_size"].astype(np.int16), row_stack_rank=r["stack_rank"].astype(np.int16),
        row_reach2h=r["reach2h"], row_reach=r["reach"], row_react=r["react"].astype(np.float32),
        row_touch_ns=r["touch_ns"], label_words=r["label_words"],
        **{f"p_{t}_{k}": np.asarray(v, np.float32) for t, pm in preds.items() for k, v in pm.items()},
        **{f"q_{t}": np.asarray(q, np.float32) for t, q in q2.items()})


# =============================================================================================== turning level
def _turn_actual(b: dict, y: dict) -> dict:
    """Per (decision, side): index (row) of the first level price reached and reacted at, -1 = none, -2 = unknown."""
    r = b["rows"]
    out = {}
    order = np.lexsort((np.abs(r["dist"]), r["side"], r["dec"]))
    for key in np.unique(np.c_[r["dec"], r["side"]], axis=0):
        out[(int(key[0]), int(key[1]))] = None
    cur, res = None, None
    for z in order:
        k = (int(r["dec"][z]), int(r["side"][z]))
        if k != cur:
            if cur is not None:
                out[cur] = -1 if res is None else res
            cur, res = k, None
        if res is not None:
            continue
        if not r["reach"][z]:
            res = -1
            continue
        if not np.isfinite(y["react"][z]):
            res = -2
        elif y["react"][z] == 1:
            res = int(z)
    if cur is not None:
        out[cur] = -1 if res is None else res
    return out


def turn_predict(b: dict, p_reach: np.ndarray, p_react: np.ndarray) -> dict:
    """Per (decision, side): the most likely turning level (row) or -1 = none. P(turn at i) = P(reach i) x P(react i)
    x the product of (1 - P(react j)) over the nearer levels j (a combined estimate: the parts are assumed
    independent, said so in the UI)."""
    r = b["rows"]
    out, prob = {}, {}
    order = np.lexsort((np.abs(r["dist"]), r["side"], r["dec"]))
    cur, best, bp, keep_no = None, -1, 0.0, 1.0
    for z in list(order) + [None]:
        k = None if z is None else (int(r["dec"][z]), int(r["side"][z]))
        if k != cur:
            if cur is not None:
                none_p = max(0.0, 1.0 - tot)
                out[cur] = best if bp >= none_p else -1
                prob[cur] = bp if bp >= none_p else none_p
            if z is None:
                break
            cur, best, bp, keep_no, tot = k, -1, 0.0, 1.0, 0.0
        pr, pc = p_reach[z], p_react[z]
        if not (np.isfinite(pr) and np.isfinite(pc)):
            continue
        pt = pr * pc * keep_no
        tot += pt
        if pt > bp:
            best, bp = int(z), pt
        keep_no *= (1 - pc)
    return {"pick": out, "prob": prob}


def turn_scores(b: dict, preds: dict, chosen: dict, y: dict, mask_dec: np.ndarray | None = None) -> dict:
    """Accuracy of the predicted turning level (per decision and side) for the chosen models, the baselines (distance
    reach x the training reaction rate) and 'always the nearest level'."""
    act = _turn_actual(b, y)
    r, d = b["rows"], b["dec"]
    model = turn_predict(b, preds["reach"][chosen["reach"]], preds["react"][chosen["react"]])["pick"]
    base = turn_predict(b, preds["reach"]["baseline"], preds["react"]["baseline"])["pick"]
    nearest = {}
    for z in np.lexsort((-np.abs(r["dist"]), r["side"], r["dec"])):
        nearest[(int(r["dec"][z]), int(r["side"][z]))] = int(z)
    hit = {"model": [], "baseline": [], "nearest": []}
    days = []
    for k, a in act.items():
        if a == -2 or (mask_dec is not None and not mask_dec[k[0]]):
            continue
        if k not in model or not np.isfinite(preds["reach"][chosen["reach"]][nearest[k]]):
            continue
        hit["model"].append(model[k] == a)
        hit["baseline"].append(base.get(k, -1) == a)
        hit["nearest"].append(nearest[k] == a)
        days.append(d["day"][k[0]])
    if len(days) < 50:
        return {"n": len(days)}
    days = np.array(days)
    out = {"n": len(days), "none_share": float(np.mean([act[k] == -1 for k in act if act[k] != -2]))}
    for k, v in hit.items():
        out[k] = float(np.mean(v))
    diff = np.array(hit["model"], float) - np.array(hit["baseline"], float)
    ud, inv = np.unique(days, return_inverse=True)
    s = np.bincount(inv, weights=diff, minlength=len(ud))
    c = np.bincount(inv, minlength=len(ud))
    rng = np.random.default_rng(108)
    idx = rng.integers(0, len(ud), size=(2000, len(ud)))
    bs = s[idx].sum(1) / c[idx].sum(1)
    out["model_minus_baseline_ci"] = [float(np.quantile(bs, 0.025)), float(np.quantile(bs, 0.975))]
    out["real"] = bool(out["model_minus_baseline_ci"][0] > 0)
    return out


# =============================================================================================== descriptions
def describe(b: dict) -> list[dict]:
    """How often each kind of level (per chart) was reached and reacted at (descriptive, whole discovery)."""
    r = b["rows"]
    out = []
    for gi, g in enumerate(GROUPS):
        for ti, tf in enumerate(TF_KEYS):
            s = (r["group"] == gi) & (r["tf"] == ti)
            if s.sum() < 30:
                continue
            rc = r["react"][s & r["reach"]]
            rc = rc[np.isfinite(rc)]
            out.append({"kind": g, "kind_name": GROUP_WORDS[g], "chart": D.TF_LABEL[tf] if tf else None, "n": int(s.sum()),
                        "reach2h": float(r["reach2h"][s].mean()), "reach": float(r["reach"][s].mean()),
                        "react_n": int(len(rc)), "react": float(rc.mean()) if len(rc) else None,
                        "median_dist": float(np.median(np.abs(r["dist"][s])))})
    st = r["group"] == 0
    for k in (1, 2, 3):
        s = st & (np.minimum(r["stack_size"], 3) == k)
        if s.sum() >= 30:
            rc = r["react"][s & r["reach"]]
            rc = rc[np.isfinite(rc)]
            out.append({"kind": "stack", "kind_name": f"FVG in a stack of {k}{'+' if k == 3 else ''}", "chart": None,
                        "n": int(s.sum()), "reach2h": float(r["reach2h"][s].mean()), "reach": float(r["reach"][s].mean()),
                        "react_n": int(len(rc)), "react": float(rc.mean()) if len(rc) else None,
                        "median_dist": float(np.median(np.abs(r["dist"][s])))})
    return out


def volatility_cuts(b: dict) -> list[float]:
    """Terciles of the last hour's size (input size60) over the discovery decisions: quiet / normal / busy."""
    j = b["gnames"].index("size60")
    v = b["dec"]["X"][:, j]
    return [float(np.quantile(v, 1 / 3)), float(np.quantile(v, 2 / 3))]


# =============================================================================================== mistakes report
SESSION_BUCKETS = ((570, 660, "9:30-11:00"), (660, 810, "11:00-13:30"), (810, 960, "13:30-16:00"))


def _buckets(b: dict, level: bool, vol_cuts: list) -> dict:
    """Group labels per row: where / when / what kind of situation the prediction was made in."""
    d = b["dec"]
    di = b["rows"]["dec"] if level else np.arange(len(d["t"]))
    ny = P.ny_minutes(d["t"])[di]
    out = {"Time of day": np.select([(ny >= lo) & (ny < hi) for lo, hi, _ in SESSION_BUCKETS],
                                    [w for _, _, w in SESSION_BUCKETS], "other")}
    na, nb = d["news_ahead"][di], d["news_before"][di]
    out["News"] = np.where(na > 0, "red news in the next 2 h", np.where(nb > 0, "red news in the last hour", "no red news near"))
    j = b["gnames"].index("size60")
    v = d["X"][di, j]
    out["Volatility (last hour)"] = np.where(v <= vol_cuts[0], "quiet", np.where(v <= vol_cuts[1], "normal", "busy"))
    jt = b["gnames"].index("trend_1h")
    tr = d["X"][di, jt]
    wd = (d["day"][di].astype(np.int64) + 3) % 7
    out["Weekday"] = np.array(["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"])[wd]
    if level:
        r = b["rows"]
        out["Level kind"] = np.array([GROUP_WORDS[g] for g in GROUPS])[r["group"]]
        out["Chart"] = np.array([D.TF_LABEL[t] if t else "no chart" for t in TF_KEYS])[r["tf"]]
        ad = np.abs(r["dist"])
        out["Distance"] = np.select([ad < 1, ad < 3, ad < 6], ["under 1 ATR", "1-3 ATR", "3-6 ATR"], "6-10 ATR")
        tw = r["side"] * tr
        out["1h trend"] = np.where(tw > 0, "level in the 1h trend's direction",
                                   np.where(tw < 0, "level against the 1h trend", "no 1h trend yet"))
        st = np.minimum(r["stack_size"], 3).astype(int)
        out["FVG stack"] = np.where(r["group"] == 0, np.array(["", "single FVG", "stack of 2", "stack of 3+"])[st], "not an FVG")
    else:
        out["1h trend"] = np.where(tr > 0, "1h trend up", np.where(tr < 0, "1h trend down", "no 1h trend yet"))
    return out


def mistakes(b: dict, pm: dict, y: np.ndarray, target: str, chosen: str, vol_cuts: list, late_from: str | None = None,
             mask: np.ndarray | None = None, top: int = 15) -> dict:
    """Where the official model's predictions of a level-map target went wrong, per situation bucket. ``late_from``:
    only months from then on (the discovery months the model's choice did NOT see). Descriptive only."""
    level = target in LEVEL_TARGETS
    kind = "real" if target.startswith("land") else "binary"
    d = b["dec"]
    days = d["day"][b["rows"]["dec"]] if level else d["day"]
    ok = np.ones(len(y), bool)
    if late_from:
        ok &= days >= np.datetime64(late_from, "D")
    if mask is not None:
        ok &= mask
    groups = _buckets(b, level, vol_cuts)
    words = b["rows"].get("label_words") if level else None

    def ctx(z):
        dz = int(b["rows"]["dec"][z]) if level else int(z)
        row = {"t": int(d["t"][dz])}
        if level:
            row.update(level=str(words[int(b["rows"]["label"][z])]), price=float(b["rows"]["price"][z]),
                       dist=float(b["rows"]["dist"][z]))
        return row
    out = mistakes_core(pm[chosen], pm["baseline"] if kind == "binary" else None, y, kind, groups, ok, ctx, top)
    out.update(target=target, model=chosen, **{"from": late_from})
    return out


def mistakes_core(p: np.ndarray, base: np.ndarray | None, y: np.ndarray, kind: str, groups: dict, ok: np.ndarray,
                  ctx=None, top: int = 15) -> dict:
    """Per bucket of every grouping: n, skill against the baseline (binary: Brier; real: error vs 'no change' / the
    usual), accuracy and what was said vs what happened; plain-English findings (worse than the baseline, or
    said / happened apart by 8+ points; buckets of 50+ predictions) and the most confident misses."""
    base = np.zeros(len(y)) if base is None else base
    ok = ok & np.isfinite(p) & np.isfinite(y) & np.isfinite(base)
    if ok.sum() < 30:
        return {"n": int(ok.sum())}
    if kind == "binary":
        loss, lb = (np.clip(p, 0, 1) - y) ** 2, (np.clip(base, 0, 1) - y) ** 2
    else:
        loss, lb = np.abs(p - y), np.abs(y - base)
    out = {"n": int(ok.sum()), "skill": float(1 - loss[ok].sum() / lb[ok].sum()) if lb[ok].sum() > 0 else None,
           "groups": [], "findings": []}
    for gname, lab in groups.items():
        rows = []
        for v in np.unique(lab[ok]):
            if v == "":
                continue
            s = ok & (lab == v)
            if s.sum() < 20:
                continue
            row = {"bucket": str(v), "n": int(s.sum()),
                   "skill": float(1 - loss[s].sum() / lb[s].sum()) if lb[s].sum() > 0 else None}
            if kind == "binary":
                row.update(accuracy=float(np.mean((p[s] > 0.5) == (y[s] > 0.5))), said=float(p[s].mean()),
                           happened=float(y[s].mean()))
            else:
                row.update(error=float(loss[s].mean()), error_baseline=float(lb[s].mean()))
            rows.append(row)
        out["groups"].append({"group": gname, "rows": rows})
        for row in rows:
            if row["n"] < 50 or row["skill"] is None:
                continue
            if row["skill"] < -0.02:
                out["findings"].append({"group": gname, "bucket": row["bucket"], "n": row["n"], "skill": row["skill"],
                                        "text": f"{gname} = {row['bucket']}: worse than the baseline "
                                                f"({row['skill'] * 100:+.1f} % skill, {row['n']} predictions)"})
            if kind == "binary" and abs(row["said"] - row["happened"]) >= 0.08:
                out["findings"].append({"group": gname, "bucket": row["bucket"], "n": row["n"], "skill": row["skill"],
                                        "text": f"{gname} = {row['bucket']}: said {row['said'] * 100:.0f} % on average, "
                                                f"{row['happened'] * 100:.0f} % happened ({row['n']} predictions)"})
    out["findings"].sort(key=lambda f: f["skill"])
    if kind == "binary":
        conf = np.where(ok & ((p > 0.5) != (y > 0.5)), np.abs(p - 0.5), -1.0)
        floor = 0.0
    else:
        conf = np.where(ok, loss - lb, -np.inf)
        floor = -np.inf
    miss = []
    for z in np.argsort(-conf)[:top]:
        if not conf[z] > floor:
            continue
        row = ctx(z) if ctx else {}
        row.update(said=float(p[z]), happened=float(y[z]), baseline=float(base[z]),
                   context={g: str(groups[g][z]) for g in groups})
        miss.append(row)
    out["worst"] = miss
    return out


def candle_buckets(X: np.ndarray, names: list, t: np.ndarray, vol_cuts: list) -> dict:
    """Situation buckets of 15-minute candle predictions (forecast.features inputs at the candle open)."""
    col = {n: X[:, i] for i, n in enumerate(names)}
    sess = P.session_code(P.ny_minutes(t))
    v = col["size60"]
    tr = col["trend_1h"]
    news = np.where(col["news_in_this_candle"] > 0, "news inside the candle",
                    np.where(col["news_since_high_min"] < 0.25, "red news in the last hour", "no news near"))
    return {"Session": np.array(P.SESSION_NAMES)[sess], "News": news,
            "Volatility (last hour)": np.where(v <= vol_cuts[0], "quiet", np.where(v <= vol_cuts[1], "normal", "busy")),
            "1h trend": np.where(tr > 0, "1h trend up", np.where(tr < 0, "1h trend down", "no 1h trend yet")),
            "Shock in the last 30 min": np.where(col["shock_last_30min"] != 0, "yes", "no")}


# =============================================================================================== frozen models
def fit_frozen(b: dict, gbm_trees: int = 90) -> dict:
    """Every model of every level-map target trained on ALL rows of ``b`` (the discovery period) and frozen."""
    r, d = b["rows"], b["dec"]
    XL, lnames = level_matrix(b)
    XD, dnames = decision_matrix(b)
    y = _targets(b)
    mk = _masks(b, y)
    lsim = [lnames.index(c) for c in SIM_LEVEL if c in lnames]
    dsim = [dnames.index(c) for c in SIM_DEC if c in dnames]
    dslot = P.ny_minutes(d["t"]) // 30
    out = {"names": {"level": lnames, "decision": dnames}}
    for tgt in LEVEL_TARGETS + DECISION_TARGETS:
        lev = tgt in LEVEL_TARGETS
        msk = mk[tgt]
        X = (XL if lev else XD)[msk]
        yy = y[tgt][msk]
        kind = "real" if tgt.startswith("land") else "binary"
        sub = slice(None) if len(yy) <= GBM_ROWS else np.unique(np.linspace(0, len(yy) - 1, GBM_ROWS).astype(np.int64))
        mo = {"kind": kind, "X": X, "y": yy, "slot": (r["group"] if lev else dslot)[msk],
              "sim": lsim if lev else dsim, "exact": lev}
        if kind == "binary":
            if tgt in ("reach2h", "reach"):
                mo["baseline"] = G.Logistic(l2=1.0).fit(X[:, :BASE_COLS], yy)
            elif tgt == "react":
                mo["p_all"] = (yy.sum() + 1) / (len(yy) + 2)
            mo["logistic"] = G.Logistic(l2=1.0).fit(X, yy)
            mo["boosting"] = G.GBM(loss="logloss", n_trees=gbm_trees, seed=8).fit(X[sub], yy[sub])
        else:
            mo["logistic"] = G.Ridge(l2=10.0).fit(X, yy)
            mo["boosting"] = G.GBM(loss="l2", n_trees=gbm_trees, seed=8).fit(X[sub], yy[sub])
        out[tgt] = mo
    return out


def predict_frozen(fm: dict, b: dict) -> dict:
    """Predictions of every frozen model for every row of ``b`` (rows of any period, built with live knowledge)."""
    r, d = b["rows"], b["dec"]
    XL, _ = level_matrix(b)
    XD, _ = decision_matrix(b)
    dslot = P.ny_minutes(d["t"]) // 30
    preds = {}
    for tgt in LEVEL_TARGETS + DECISION_TARGETS:
        mo = fm[tgt]
        lev = tgt in LEVEL_TARGETS
        X = XL if lev else XD
        pm = {}
        if mo["kind"] == "binary":
            if "baseline" in mo:
                pm["baseline"] = mo["baseline"].predict(X[:, :BASE_COLS])
            elif "p_all" in mo:
                pm["baseline"] = np.full(len(X), mo["p_all"])
            else:
                pm["baseline"] = d["gambler"].astype(float)
        else:
            pm["baseline"] = np.zeros(len(X))
        pm["logistic"] = mo["logistic"].predict(X)
        pm["boosting"] = mo["boosting"].predict(X)
        sc = mo["sim"]
        pm["similar"] = F._similar(mo["X"][:, sc], mo["y"], mo["slot"], X[:, sc], r["group"] if lev else dslot,
                                   mo["kind"], exact=mo["exact"])
        preds[tgt] = pm
    return preds


def bands_from_analysis(path, chosen: dict) -> dict:
    """Landing bands (q10, q25, q75, q90 of the residual) from the discovery walk-forward's OUT-OF-SAMPLE predictions
    of the chosen landing models (saved in the analysis' levelmap.npz)."""
    out = {}
    with np.load(path) as z:
        for t in ("land2h", "land"):
            pr, yy = z[f"p_{t}_{chosen[t]}"].astype(float), z[f"dec_{t}"].astype(float)
            ok = np.isfinite(pr)
            out[t] = np.quantile((yy - pr)[ok], [0.1, 0.25, 0.75, 0.9]) if ok.sum() > 300 else np.zeros(4)
    return out


def frozen_period(fm: dict, b: dict, chosen: dict, bands: dict, path) -> dict:
    """Predict a period with frozen models, score it, save its level map (holdout / new days)."""
    preds = predict_frozen(fm, b)
    res = score_all(b, preds, chosen)
    save(path, b, preds, {t: preds[t][chosen[t]][:, None] + bands[t][None, :] for t in ("land2h", "land")})
    return {"scores": res, "preds": preds}


def score_all(b: dict, preds: dict, chosen: dict) -> dict:
    """Scores of every model of every target on the rows of ``b`` (all of them: the frozen models never saw them)."""
    y = _targets(b)
    r, d = b["rows"], b["dec"]
    out = {}
    for tgt in LEVEL_TARGETS + DECISION_TARGETS:
        lev = tgt in LEVEL_TARGETS
        kind = "real" if tgt.startswith("land") else "binary"
        days = (d["day"][r["dec"]] if lev else d["day"]).astype("datetime64[D]")
        msk = np.isfinite(y[tgt])
        pm = preds[tgt]
        res = {"official_model": chosen[tgt], "models": {}, "by_month": []}
        for k, p in pm.items():
            if kind == "real" and k == "baseline":
                continue
            res["models"][k] = (F.score_binary(p[msk], y[tgt][msk], pm["baseline"][msk], days[msk]) if kind == "binary"
                                else F.score_real(p[msk], y[tgt][msk], days[msk]))
        res["official"] = res["models"].get(chosen[tgt])
        months = days.astype("datetime64[M]")
        for mo in np.unique(months[msk]):
            mm = msk & (months == mo)
            p = pm[chosen[tgt]]
            s = (F.score_binary(p[mm], y[tgt][mm], pm["baseline"][mm], days[mm]) if kind == "binary"
                 else F.score_real(p[mm], y[tgt][mm], days[mm]))
            if s:
                res["by_month"].append({"month": str(mo), "n": s["n"], "skill": s["skill"], "accuracy": s.get("accuracy")})
        out[tgt] = res
    out["turn"] = turn_scores(b, preds, chosen, y)
    return out


# =============================================================================================== day view
def day_levels(lm: dict, lo_ns: int, hi_ns: int, chosen: dict) -> list[dict]:
    """The level map of every decision moment between lo_ns and hi_ns (from a saved levelmap npz)."""
    if lm is None or not len(lm.get("dec_t", [])):
        return []
    dt = lm["dec_t"]
    sel = np.flatnonzero((dt >= lo_ns) & (dt < hi_ns))
    if not len(sel):
        return []
    words = lm["label_words"]
    out = []
    rd = lm["row_dec"]
    b = {"rows": {"dec": rd, "side": lm["row_side"], "dist": lm["row_dist"].astype(float)}}
    pr = lm[f"p_reach_{chosen.get('reach', 'logistic')}"].astype(float)
    pc = lm[f"p_react_{chosen.get('react', 'logistic')}"].astype(float)
    for q in sel:
        rows = np.flatnonzero(rd == q)
        if not len(rows):
            continue
        sub = {"rows": {k: v[rows] for k, v in b["rows"].items()}}
        tp = turn_predict(sub, pr[rows], pc[rows])
        px, a15 = float(lm["dec_px"][q]), float(lm["dec_a15"][q])
        levels = []
        for n, z in enumerate(rows):
            def g(t, z=z):
                return F._f(lm[f"p_{t}_{chosen.get(t, 'logistic')}"][z])
            levels.append({"label": str(words[int(lm["row_label"][z])]), "price": float(lm["row_price"][z]),
                           "side": int(lm["row_side"][z]), "dist": float(lm["row_dist"][z]),
                           "kind": GROUPS[int(lm["row_group"][z])], "stack": int(lm["row_stack"][z]),
                           "p_reach2h": g("reach2h"), "p_reach": g("reach"), "p_react": g("react"),
                           "base_reach": F._f(lm["p_reach_baseline"][z]), "base_react": F._f(lm["p_react_baseline"][z]),
                           "turn_pick": any(tp["pick"].get(k) == n for k in tp["pick"]),
                           "reached2h": bool(lm["row_reach2h"][z]), "reached": bool(lm["row_reach"][z]),
                           "reacted": F._f(lm["row_react"][z]), "touch_ns": int(lm["row_touch_ns"][z]) or None})
        levels.sort(key=lambda x: -x["price"])
        land = {}
        for t in ("land2h", "land"):
            p = F._f(lm[f"p_{t}_{chosen.get(t, 'logistic')}"][q])
            qq = lm.get(f"q_{t}")
            band = [F._f(x) for x in qq[q]] if qq is not None and len(qq) > q else [None] * 4
            land[t] = {"median": None if p is None else px + p * a15,           # band values: absolute (forecast.bands)
                       "band80": [None if x is None else px + x * a15 for x in (band[0], band[3])],
                       "band50": [None if x is None else px + x * a15 for x in (band[1], band[2])],
                       "actual": px + float(lm[f"dec_{t}"][q]) * a15}
        out.append({"t": int(dt[q]), "px": px, "atr15": a15, "levels": levels,
                    "p_up_first": F._f(lm[f"p_first_{chosen.get('first', 'logistic')}"][q]),
                    "p_up_first_random_walk": F._f(lm["dec_gambler"][q]), "up_first": F._f(lm["dec_first"][q]),
                    "turn_prob": {("above" if k[1] > 0 else "below"): float(v) for k, v in tp["prob"].items()},
                    "land": land})
    return out
