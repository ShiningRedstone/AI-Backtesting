"""15-minute forecasts with LIVE knowledge only (ADR-106), scored walk-forward against baselines.

At the OPEN of every 15-minute candle the forecaster may use only what was known then: complete minutes before it,
complete higher-timeframe bars, breaks of structure and FVG zones already formed (and not yet filled), the day's
reference levels that were final, ES up to that minute, the economic calendar (times, impact and forecasts in
advance; an ACTUAL value only after its release) and shocks that had happened.

Targets: UP (the candle closes above its open), SIZE (log of its high-low against the median of that slot over the
previous 20 trading dates), BIAS (at 9:30 ... 15:30 every 30 minutes: does the session close above the current price)
and LEVELS (at 9:30, 10:00, 11:00 ... 15:00: is each reference level / open FVG edge traded before the session ends).

Models: the BASELINE (the opponent: the overall up-rate of the training data; 'usual size' of that slot for SIZE;
distance only for LEVELS), LOGISTIC (ridge for SIZE), GRADIENT BOOSTING (numpy, see gbm.py) and SIMILAR SITUATIONS (the 40
nearest past candles of the same time of day, compared on every input). Walk-forward: every month is predicted by
models trained only on the trading dates before it (logistic and similar situations retrained monthly, boosting every
3 months). The model shown by default is chosen on the first 70 % of the predicted months and its score is reported
on the last 30 % (never chosen on the months it is scored on).
"""
from __future__ import annotations

import math

import numpy as np

from edgelab.market import data as D
from edgelab.market import gbm as G
from edgelab.market import patterns as P

MIN_TRAIN_DAYS = 120
GBM_EVERY = 3
NEIGHBOURS = 40
BIAS_TIMES = tuple(range(570, 960, 30))                     # 9:30 ... 15:30
LEVEL_TIMES = (570, 600, 660, 720, 780, 840, 900)
MODELS = ("baseline", "logistic", "boosting", "similar")
LEVEL_KEYS = ("pdh", "pdl", "pwh", "pwl", "prth_hi", "prth_lo", "asia_hi", "asia_lo", "lon_hi", "lon_lo",
              "fvg15_above", "fvg15_below", "fvg60_above", "fvg60_below", "day_open", "rth_open")
LEVEL_WORDS = {"pdh": "Previous day high", "pdl": "Previous day low", "pwh": "Previous week high",
               "pwl": "Previous week low", "prth_hi": "Previous regular-session high",
               "prth_lo": "Previous regular-session low", "asia_hi": "Asia high", "asia_lo": "Asia low",
               "lon_hi": "London high", "lon_lo": "London low", "fvg15_above": "Nearest open 15m FVG above",
               "fvg15_below": "Nearest open 15m FVG below", "fvg60_above": "Nearest open 1h FVG above",
               "fvg60_below": "Nearest open 1h FVG below", "day_open": "18:00 session open",
               "rth_open": "9:30 open"}


SESSION_WORDS = ("Asia session", "London session", "NY pre-open", "NY morning", "NY lunch", "NY afternoon")


def input_word(name: str) -> str:
    """Plain words for an input name (shown as the 'why' of a prediction)."""
    fixed = {"slot_sin": "time of day", "slot_cos": "time of day", "rth_minutes": "time since 9:30",
             "size60": "size of the last hour", "move60": "move of the last hour", "move_day": "move since 18:00",
             "day_range_vs_prev": "today's range vs yesterday's", "from_day_high": "distance below today's high",
             "from_day_low": "distance above today's low", "took_pdh": "previous day high already taken",
             "took_pdl": "previous day low already taken", "bars_since_break15": "time since the last 15m break",
             "es_move15": "ES last 15 min", "es_move60": "ES last hour", "nq_minus_es60": "NQ vs ES last hour",
             "smt_last_hour": "SMT in the last hour", "sweeps_last_hour": "level sweeps in the last hour",
             "news_next_high_min": "time to the next red news", "news_since_high_min": "time since red news",
             "news_in_this_candle": "news inside this candle", "news_last_surprise_z": "last news surprise",
             "shock_last_30min": "shock in the last 30 min"}
    if name in fixed:
        return fixed[name]
    if name.startswith("session_"):
        return SESSION_WORDS[int(name.split("_")[1])]
    if name.startswith("weekday_"):
        return ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"][int(name.split("_")[1])]
    if name.startswith("size15_lag"):
        return f"size of the 15m candle {name[-1]} back"
    if name.startswith("move15_lag"):
        return f"move of the 15m candle {name[-1]} back"
    if name.startswith("to_"):
        return "distance to " + {"pdh": "previous day high", "pdl": "previous day low", "pwh": "previous week high",
                                 "pwl": "previous week low", "prth_hi": "previous session high", "prth_lo": "previous session low",
                                 "asia_hi": "Asia high", "asia_lo": "Asia low", "lon_hi": "London high",
                                 "lon_lo": "London low"}.get(name[3:], name[3:])
    if name.startswith("trend_"):
        return f"{name[6:]} structure"
    if name.startswith("fvg"):
        tf, what = name[3:].split("_")
        return {"above": f"open {tf} FVG above", "below": f"open {tf} FVG below", "inside": f"inside a {tf} FVG"}[what]
    return name


ONEHOT_PREFIX = ("session_", "weekday_")


# =============================================================================================== context
class Context:
    """Everything the features need, built once from the minutes (causal pieces only)."""

    def __init__(self, m: D.Minute, es: D.Minute | None, news: list[dict]):
        self.m, self.es = m, es
        self.bars = D.all_timeframes(m, (1, 5, 15, 60, 240, D.DAY))
        self.lv = P.day_levels(m)
        self.typ = {tf: D.typical_by_slot(b, b.h - b.l) for tf, b in self.bars.items() if tf != D.DAY}
        self.atr = {tf: D.atr(b) for tf, b in self.bars.items()}
        self.breaks, self.zones = {}, {}
        for tf in (15, 60, 240, D.DAY):
            b, a = self.bars[tf], self.atr[tf]
            ev = P.Events()
            br = P.add_structure(ev, m, b, a) if len(b) > 10 else []
            if br:
                kn = np.array([b.known_ns[x["i"]] for x in br], np.int64)
                self.breaks[tf] = (kn, np.array([x["dir"] for x in br], np.int8))
            if tf in (15, 60, 240) and len(b) > 3:
                f = P.fvgs(b, a)
                if len(f["k"]):
                    kn = b.known_ns[f["k"]]
                    far = np.where(f["dir"] > 0, f["bottom"], f["top"])
                    j = D.first_touch(m, np.searchsorted(m.ts, kn), far, (-f["dir"]).astype(np.int8))
                    end = np.where(j >= 0, m.ts[np.maximum(j, 0)], np.iinfo(np.int64).max)
                    self.zones[tf] = {"known_ns": kn, "end_ns": end, "top": f["top"], "bottom": f["bottom"],
                                      "dir": f["dir"]}
        ev = P.Events()
        if len(self.bars[5]) > 10:
            P.add_level_sweeps(ev, m, self.bars[5], self.atr[5], self.lv)
        sw = ev.frame()
        self.sweeps = (sw["known_ns"].to_numpy(np.int64), sw["dir"].to_numpy(np.int8)) if len(sw) else None
        self.smt = None
        if es is not None and len(es) > 1000:
            from edgelab.market import nqes
            s = nqes.smt_events(m, es, {}).frame()
            if len(s):
                o = np.argsort(s["known_ns"].to_numpy(np.int64))
                self.smt = (s["known_ns"].to_numpy(np.int64)[o], s["dir"].to_numpy(np.int8)[o])
            self.es_bars = D.all_timeframes(es, (15, 60))
            self.es_atr = D.atr(self.es_bars[15])
        self.news = sorted(news, key=lambda e: e["ts"])
        self.news_ts = np.array([e["ts"] for e in self.news], np.int64)
        b5 = self.bars[5]
        self.ratio5 = (b5.h - b5.l) / np.where(self.typ[5] > 0, self.typ[5], np.nan)


def _last_complete(b: D.Bars, t: np.ndarray) -> np.ndarray:
    return np.searchsorted(b.known_ns, t, side="right") - 1


def _recent_sum(ev, t: np.ndarray, window_ns: int) -> np.ndarray:
    if ev is None or not len(ev[0]):
        return np.zeros(len(t))
    kn, d = ev
    o = np.argsort(kn, kind="stable")
    kn, d = kn[o], d[o]
    cs = np.r_[0, np.cumsum(d.astype(float))]
    a = np.searchsorted(kn, t - window_ns, side="left")
    b = np.searchsorted(kn, t, side="left")
    return cs[b] - cs[a]


def features(cx: Context, t: np.ndarray) -> tuple[np.ndarray, list[str], dict]:
    """Feature matrix for decision times ``t`` (UTC ns; a decision at t sees minutes that OPENED before t)."""
    m = cx.m
    t = np.asarray(t, np.int64)
    n = len(t)
    cols: dict = {}
    i = np.searchsorted(m.ts, t, side="left") - 1                     # last minute that opened before t
    ok_i = i >= 0
    ic = np.maximum(i, 0)
    px = np.where(ok_i, m.c[ic], np.nan)
    ny = P.ny_minutes(t)
    sess = P.session_code(ny)
    tday = D.trading_day(t)
    b15 = cx.bars[15]
    k = _last_complete(b15, t)
    kc = np.maximum(k, 0)
    a15 = np.where(k >= 0, cx.atr[15][kc], np.nan)
    a15 = np.where(a15 > 0, a15, np.nan)
    cols["slot_sin"], cols["slot_cos"] = np.sin(2 * np.pi * ny / 1440), np.cos(2 * np.pi * ny / 1440)
    for s in range(len(P.SESSION_NAMES)):
        cols[f"session_{s}"] = (sess == s).astype(float)
    wd = (tday.astype("datetime64[D]").astype(np.int64) + 3) % 7
    for w in range(5):
        cols[f"weekday_{w}"] = (wd == w).astype(float)
    cols["rth_minutes"] = np.where((ny >= 570) & (ny < 975), (ny - 570) / 405, -0.1)
    for lag in range(1, 5):
        kk = k - lag + 1
        okk = (kk >= 0) & (b15.day[np.maximum(kk, 0)] == tday)
        r = (b15.h - b15.l)[np.maximum(kk, 0)] / cx.typ[15][np.maximum(kk, 0)]
        cols[f"size15_lag{lag}"] = np.where(okk & np.isfinite(r) & (r > 0), np.log(np.where(r > 0, r, 1)), 0.0)
        cols[f"move15_lag{lag}"] = np.where(okk, (b15.c - b15.o)[np.maximum(kk, 0)] / a15, 0.0)
    b60 = cx.bars[60]
    k60 = _last_complete(b60, t)
    r60 = (b60.h - b60.l)[np.maximum(k60, 0)] / cx.typ[60][np.maximum(k60, 0)]
    cols["size60"] = np.where((k60 >= 0) & np.isfinite(r60) & (r60 > 0), np.log(np.where(r60 > 0, r60, 1)), 0.0)
    j60 = np.searchsorted(m.ts, t - 60 * D.MIN_NS, side="left")
    cols["move60"] = np.where(ok_i, (px - m.o[np.minimum(j60, len(m) - 1)]) / a15, 0.0)
    lv = cx.lv
    di = np.clip(np.searchsorted(lv["days"], tday), 0, len(lv["days"]) - 1)
    have_day = lv["days"][di] == tday
    dstart = np.searchsorted(m.day, tday, side="left")
    day_open = np.where(have_day, m.o[np.minimum(dstart, len(m) - 1)], np.nan)
    cols["move_day"] = np.nan_to_num((px - day_open) / a15)
    # day high / low so far (minutes of today before t)
    hi_so, lo_so = np.full(n, np.nan), np.full(n, np.nan)
    for q in range(n):
        if ok_i[q] and dstart[q] <= i[q]:
            hi_so[q] = m.h[dstart[q]:i[q] + 1].max()
            lo_so[q] = m.l[dstart[q]:i[q] + 1].min()
    rth0 = np.searchsorted(m.ts, t, side="left")
    cols["day_range_vs_prev"] = np.nan_to_num((hi_so - lo_so) / (lv["pdh"][di] - lv["pdl"][di]))
    cols["from_day_high"] = np.nan_to_num((px - hi_so) / a15)
    cols["from_day_low"] = np.nan_to_num((px - lo_so) / a15)
    for key in ("pdh", "pdl", "pwh", "pwl", "prth_hi", "prth_lo"):
        cols["to_" + key] = np.clip(np.nan_to_num((lv[key][di] - px) / a15, nan=0.0), -12, 12)
    asia_final = (ny >= 120) & (ny < 1080)
    lon_final = (ny >= 420) & (ny < 1080)
    for key, fin in (("asia_hi", asia_final), ("asia_lo", asia_final), ("lon_hi", lon_final), ("lon_lo", lon_final)):
        cols["to_" + key] = np.where(fin, np.clip(np.nan_to_num((lv[key][di] - px) / a15), -12, 12), 0.0)
    cols["took_pdh"] = (hi_so > lv["pdh"][di]).astype(float)
    cols["took_pdl"] = (lo_so < lv["pdl"][di]).astype(float)
    for tf in (15, 60, 240, D.DAY):
        bk = cx.breaks.get(tf)
        if bk is None:
            cols[f"trend_{D.TF_LABEL[tf]}"] = np.zeros(n)
            continue
        j = np.searchsorted(bk[0], t, side="left") - 1
        cols[f"trend_{D.TF_LABEL[tf]}"] = np.where(j >= 0, bk[1][np.maximum(j, 0)], 0).astype(float)
        if tf == 15:
            since = (t - bk[0][np.maximum(j, 0)]) / (15 * D.MIN_NS)
            cols["bars_since_break15"] = np.where(j >= 0, np.minimum(since, 40) / 40, 1.0)
    for tf in (15, 60, 240):
        zn = cx.zones.get(tf)
        above = np.full(n, 12.0)
        below = np.full(n, 12.0)
        inside = np.zeros(n)
        if zn is not None:
            j = np.searchsorted(zn["known_ns"], t, side="left")
            for back in range(1, 61):
                jj = j - back
                okb = jj >= 0
                jc = np.maximum(jj, 0)
                act = okb & (zn["end_ns"][jc] >= t)            # filled by the minute opening at t: not known yet
                top, bot, dz = zn["top"][jc], zn["bottom"][jc], zn["dir"][jc]
                ins = act & (px <= top) & (px >= bot)
                inside = np.where((inside == 0) & ins, dz, inside)
                da = (bot - px) / a15                                   # zone above
                db = (px - top) / a15                                   # zone below
                above = np.where(act & (bot > px) & (da < above), da, above)
                below = np.where(act & (top < px) & (db < below), db, below)
        cols[f"fvg{D.TF_LABEL[tf]}_above"] = np.nan_to_num(np.minimum(above, 12), nan=12)
        cols[f"fvg{D.TF_LABEL[tf]}_below"] = np.nan_to_num(np.minimum(below, 12), nan=12)
        cols[f"fvg{D.TF_LABEL[tf]}_inside"] = inside.astype(float)
    if cx.es is not None and getattr(cx, "es_bars", None) is not None:
        es = cx.es
        je = np.searchsorted(es.ts, t, side="left") - 1
        okj = je >= 0
        je0 = np.searchsorted(es.ts, t - 15 * D.MIN_NS, side="left")
        je60 = np.searchsorted(es.ts, t - 60 * D.MIN_NS, side="left")
        ke = _last_complete(cx.es_bars[15], t)
        ea = np.where(ke >= 0, cx.es_atr[np.maximum(ke, 0)], np.nan)
        ea = np.where(ea > 0, ea, np.nan)
        ecl = es.c[np.maximum(je, 0)]
        e15 = np.where(okj, (ecl - es.o[np.minimum(je0, len(es) - 1)]) / ea, 0.0)
        e60 = np.where(okj, (ecl - es.o[np.minimum(je60, len(es) - 1)]) / ea, 0.0)
        cols["es_move15"] = np.nan_to_num(e15)
        cols["es_move60"] = np.nan_to_num(e60)
        cols["nq_minus_es60"] = np.nan_to_num(cols["move60"] - e60)
    else:
        cols["es_move15"] = cols["es_move60"] = cols["nq_minus_es60"] = np.zeros(n)
    cols["smt_last_hour"] = _recent_sum(cx.smt, t, 60 * D.MIN_NS)
    cols["sweeps_last_hour"] = _recent_sum(cx.sweeps, t, 60 * D.MIN_NS)
    nt = cx.news_ts
    imp = np.array([e["impact"] for e in cx.news], dtype=np.int8) if cx.news else np.zeros(0, np.int8)
    to_next = np.full(n, 240.0)
    since = np.full(n, 240.0)
    in_bar = np.zeros(n)
    last_z = np.zeros(n)
    if len(nt):
        hi_ts = nt[imp >= 3]
        j = np.searchsorted(hi_ts, t, side="left")
        nxt = np.where(j < len(hi_ts), (hi_ts[np.minimum(j, len(hi_ts) - 1)] - t) / D.MIN_NS, 240)
        to_next = np.minimum(np.where(j < len(hi_ts), nxt, 240), 240)
        jp = j - 1
        since = np.where(jp >= 0, np.minimum((t - hi_ts[np.maximum(jp, 0)]) / D.MIN_NS, 240), 240)
        a = np.searchsorted(nt, t, side="left")
        b = np.searchsorted(nt, t + 15 * D.MIN_NS, side="left")
        for q in range(n):
            if b[q] > a[q]:
                in_bar[q] = float(imp[a[q]:b[q]].max())
            r0 = np.searchsorted(nt, t[q] - 60 * D.MIN_NS, side="left")
            for e in cx.news[r0:a[q]][::-1]:                             # released BEFORE t: actual is known
                if e["impact"] >= 2 and e.get("surprise_z") is not None:
                    last_z[q] = max(-4.0, min(4.0, e["surprise_z"]))
                    break
    cols["news_next_high_min"] = to_next / 240
    cols["news_since_high_min"] = since / 240
    cols["news_in_this_candle"] = in_bar / 3
    cols["news_last_surprise_z"] = last_z
    b5 = cx.bars[5]
    k5 = _last_complete(b5, t)
    shock = np.zeros(n)
    for back in range(6):
        kk = k5 - back
        okk = kk >= 0
        rr = cx.ratio5[np.maximum(kk, 0)]
        mv = np.sign((b5.c - b5.o)[np.maximum(kk, 0)])
        shock = np.where((shock == 0) & okk & (rr >= 3), mv, shock)
    cols["shock_last_30min"] = shock
    names = list(cols)
    X = np.column_stack([np.nan_to_num(np.asarray(cols[c], float)) for c in names])
    info = {"px": px, "a15": a15, "ny": ny, "day": tday, "hi_so": hi_so, "lo_so": lo_so, "di": di,
            "day_open": day_open}
    return X, names, info


# =============================================================================================== datasets
def candle_rows(cx: Context) -> dict:
    """One row per complete 15-minute candle with history: decision time = its open."""
    b = cx.bars[15]
    typ = cx.typ[15]
    ok = (b.n >= 12) & np.isfinite(typ) & (typ > 0)
    ok &= np.r_[np.zeros(1, bool), np.isfinite(cx.atr[15][:-1])] if len(b) else ok
    k = np.flatnonzero(ok)
    up = np.sign(b.c[k] - b.o[k])
    size = np.log((b.h[k] - b.l[k]) / typ[k])
    good = np.isfinite(size)
    k, up, size = k[good], up[good], size[good]
    return {"k": k, "t": b.ts[k], "up": up, "size": size, "day": b.day[k], "slot": b.slot[k]}


def bias_rows(cx: Context) -> dict:
    """Decision points 9:30 ... 15:30 every 30 min: does the trading date close above the price at that moment?"""
    m = cx.m
    days = np.unique(m.day)
    ends = np.searchsorted(m.day, days, side="right") - 1
    t_list, y, dd = [], [], []
    for d, e in zip(days, ends):
        base = np.datetime64(d, "D")
        for mm in BIAS_TIMES:
            tt = _ny_ts(base, mm)
            if tt is None:
                continue
            i = np.searchsorted(m.ts, tt, side="left")
            if i >= len(m) or m.ts[i] != tt or m.day[i] != d or i >= e:
                continue
            y.append(np.sign(m.c[e] - m.o[i]))
            t_list.append(tt)
            dd.append(d)
    return {"t": np.array(t_list, np.int64), "up": np.array(y), "day": np.array(dd, "datetime64[D]")}


def _ny_ts(day: np.datetime64, ny_minute: int) -> int | None:
    import pandas as pd
    try:
        ts = pd.Timestamp(str(day)) + pd.Timedelta(minutes=ny_minute)
        return int(ts.tz_localize(D.NY).tz_convert("UTC").value)
    except Exception:                                                  # noqa: BLE001 - DST gap: no such minute
        return None


def level_rows(cx: Context) -> dict:
    """At LEVEL_TIMES: every reference level strictly above / below the price; reached before the session ends?"""
    m = cx.m
    days = np.unique(m.day)
    ends = np.searchsorted(m.day, days, side="right") - 1
    pts = []
    for d, e in zip(days, ends):
        for mm in LEVEL_TIMES:
            tt = _ny_ts(np.datetime64(d, "D"), mm)
            if tt is None:
                continue
            i = np.searchsorted(m.ts, tt, side="left")
            if i < len(m) and m.ts[i] == tt and m.day[i] == d and i < e:
                pts.append((tt, i, e))
    if not pts:
        return {"t": np.zeros(0, np.int64)}
    t = np.array([p[0] for p in pts], np.int64)
    X, names, info = features(cx, t)
    rows = {"t": [], "level": [], "price": [], "dist": [], "reached": [], "x": [], "day": [], "minutes_left": []}
    lv = cx.lv
    for q, (tt, i, e) in enumerate(pts):
        px = info["px"][q]
        a15 = info["a15"][q]
        if not (np.isfinite(px) and np.isfinite(a15)):
            continue
        di = info["di"][q]
        cand = {}
        for key in ("pdh", "pdl", "pwh", "pwl", "prth_hi", "prth_lo", "asia_hi", "asia_lo", "lon_hi", "lon_lo"):
            cand[key] = lv[key][di]
        cand["day_open"] = info["day_open"][q]
        r0 = np.searchsorted(m.day, m.day[i], side="left")
        rth = np.flatnonzero((m.ny_min[r0:i] == 570))
        cand["rth_open"] = m.o[r0 + rth[0]] if len(rth) else np.nan
        for tf in (15, 60):
            zn = cx.zones.get(tf)
            ab, be = np.nan, np.nan
            if zn is not None:
                j = np.searchsorted(zn["known_ns"], tt, side="left")
                lo_j = max(0, j - 60)
                act = zn["end_ns"][lo_j:j] >= tt
                tops, bots = zn["top"][lo_j:j][act], zn["bottom"][lo_j:j][act]
                above = bots[bots > px]
                below = tops[tops < px]
                ab = above.min() if len(above) else np.nan
                be = below.max() if len(below) else np.nan
            cand[f"fvg{tf}_above"], cand[f"fvg{tf}_below"] = ab, be
        for key, lvl in cand.items():
            if not np.isfinite(lvl) or lvl == px:
                continue
            side = 1 if lvl > px else -1
            seg_h, seg_l = m.h[i:e + 1], m.l[i:e + 1]
            reached = bool((seg_h >= lvl).any()) if side > 0 else bool((seg_l <= lvl).any())
            rows["t"].append(tt)
            rows["level"].append(key)
            rows["price"].append(float(lvl))
            rows["dist"].append(float((lvl - px) / a15))
            rows["reached"].append(reached)
            rows["x"].append(X[q])
            rows["day"].append(m.day[i])
            rows["minutes_left"].append(float((m.ts[e] - tt) / D.MIN_NS))
    out = {k: np.array(v) for k, v in rows.items()}
    out["names"] = names
    return out


def level_matrix(lr: dict) -> tuple[np.ndarray, list[str]]:
    d = lr["dist"]
    base = [np.abs(d), np.sign(d), np.log1p(np.abs(d)), lr["minutes_left"] / 405,
            np.abs(d) / np.sqrt(np.maximum(lr["minutes_left"], 1) / 15)]
    names = ["abs_dist", "side", "log_dist", "time_left", "dist_per_sqrt_time"]
    for key in LEVEL_KEYS:
        base.append((lr["level"] == key).astype(float))
        names.append("is_" + key)
    X = np.column_stack(base + [lr["x"]]) if len(lr["x"]) else np.zeros((0, len(names)))
    return X, names + list(lr.get("names", []))


# =============================================================================================== walk-forward
def _months(days: np.ndarray) -> np.ndarray:
    return days.astype("datetime64[M]")


def walk_forward(X: np.ndarray, y: np.ndarray, days: np.ndarray, slot: np.ndarray | None, kind: str,
                 progress=None, label: str = "", base_X: np.ndarray | None = None,
                 onehot: np.ndarray | None = None, sim_cols: list | None = None, sim_exact: bool = False,
                 base_p: np.ndarray | None = None, gbm_every: int = GBM_EVERY, gbm_rows: int | None = None,
                 fit_mask: np.ndarray | None = None) -> dict:
    """Predictions for every row of every month after MIN_TRAIN_DAYS of history. kind: 'binary' (y in {0, 1}) or
    'real'. Returns per-model predictions (NaN where not predicted) and, for 'binary', logistic contributions.
    ``base_p``: a fixed baseline probability per row (the level map's random-walk 'which side first', ADR-108);
    ``gbm_rows``: boosting trained on at most this many training rows, evenly spaced over the training period (the
    level map has many rows per day; every other model still uses all rows); ``fit_mask``: only these rows are LEARNED
    from (their outcome is known), every row is predicted (a reaction is forecast for every level before anyone knows
    whether it will be touched)."""
    n = len(y)
    months = _months(days)
    um = np.unique(months)
    preds = {mname: np.full(n, np.nan) for mname in MODELS}
    contrib_idx = np.full((n, 3), -1, np.int16)
    contrib_val = np.zeros((n, 3), np.float32)
    gbm = None
    first_pred = None
    for mi, mo in enumerate(um):
        test = months == mo
        train = days < np.datetime64(mo, "D")
        if fit_mask is not None:
            train &= fit_mask
        if len(np.unique(days[train])) < MIN_TRAIN_DAYS or test.sum() == 0:
            continue
        if first_pred is None:
            first_pred = mi
        if progress:
            progress(f"{label}: predicting {str(mo)} (trained on {int(len(np.unique(days[train])))} earlier trading dates)")
        Xtr, ytr, Xte = X[train], y[train], X[test]
        # baseline: slot up-rate (binary) / usual size (real) / distance-only (levels: base_X)
        if kind == "binary":
            if base_p is not None:
                preds["baseline"][test] = base_p[test]
            elif base_X is not None:
                preds["baseline"][test] = G.Logistic(l2=1.0).fit(base_X[train], ytr).predict(base_X[test])
            else:
                # the training period's overall up-rate: the strongest simple 'usual'. (A per-time-slot rate was tried
                # and is a WEAK opponent: on random data its slot noise scores worse than a constant, so any model
                # 'beat' it without knowing anything - ADR-107.)
                preds["baseline"][test] = (ytr.sum() + 1) / (len(ytr) + 2)
            lg = G.Logistic(l2=1.0).fit(Xtr, ytr)
            preds["logistic"][test] = lg.predict(Xte)
            c = lg.contributions(Xte)
            if onehot is not None:                       # an inactive category ('not Monday') is never shown as a reason
                c[:, onehot] *= (Xte[:, onehot] != 0)
            top = np.argsort(-np.abs(c), axis=1)[:, :3]
            contrib_idx[test] = top
            contrib_val[test] = np.take_along_axis(c, top, axis=1)
        else:
            preds["baseline"][test] = 0.0
            preds["logistic"][test] = G.Ridge(l2=10.0).fit(Xtr, ytr).predict(Xte)
        if gbm is None or (mi - first_pred) % gbm_every == 0:
            sub = slice(None) if not gbm_rows or len(ytr) <= gbm_rows else \
                np.unique(np.linspace(0, len(ytr) - 1, gbm_rows).astype(np.int64))
            gbm = G.GBM(loss="logloss" if kind == "binary" else "l2", n_trees=90, seed=mi).fit(Xtr[sub], ytr[sub])
        preds["boosting"][test] = gbm.predict(Xte)
        Str, Ste = (Xtr, Xte) if sim_cols is None else (Xtr[:, sim_cols], Xte[:, sim_cols])
        if slot is not None:
            preds["similar"][test] = _similar(Str, ytr, slot[train], Ste, slot[test], kind, exact=sim_exact)
        else:
            preds["similar"][test] = _similar(Str, ytr, None, Ste, None, kind)
    out = {"pred": preds}
    if kind == "binary":
        out["contrib_idx"], out["contrib_val"] = contrib_idx, contrib_val
    return out


def _similar(Xtr, ytr, str_, Xte, ste, kind, exact: bool = False) -> np.ndarray:
    mu = Xtr.mean(0)
    sd = Xtr.std(0)
    sd[~(sd > 0)] = 1
    Ztr, Zte = (Xtr - mu) / sd, (Xte - mu) / sd
    out = np.full(len(Xte), np.nan)
    groups = [None] if ste is None else np.unique(ste)
    for s in groups:
        te = np.arange(len(Xte)) if s is None else np.flatnonzero(ste == s)
        tr = np.arange(len(Xtr)) if s is None else np.flatnonzero(np.abs(str_.astype(int) - int(s)) <= (0 if exact else 1))
        if len(tr) < NEIGHBOURS:
            continue
        if len(tr) > 20000:
            tr = tr[-20000:]
        ntr = (Ztr[tr] ** 2).sum(1)[None, :]
        for c0 in range(0, len(te), 256):                                # bounded memory: 256 rows at a time
            tc = te[c0:c0 + 256]
            d2 = (Zte[tc] ** 2).sum(1)[:, None] - 2 * Zte[tc] @ Ztr[tr].T + ntr
            nn = np.argpartition(d2, NEIGHBOURS - 1, axis=1)[:, :NEIGHBOURS]
            yy = ytr[tr][nn]
            if kind == "binary":
                out[tc] = (yy.sum(1) + 10 * ytr.mean()) / (NEIGHBOURS + 10)  # shrunk toward the overall rate
            else:
                out[tc] = np.median(yy, axis=1)
    return out


# =============================================================================================== scoring
def skill_ci(loss_model: np.ndarray, loss_base: np.ndarray, days: np.ndarray, reps: int = 2000) -> list | None:
    """95 % interval of the skill (1 - model loss / baseline loss) by resampling whole TRADING DATES (candles of one
    day are not independent). A skill counts only when the interval's lower end is above 0."""
    if days is None or len(loss_model) < 50:
        return None
    ud, inv = np.unique(days, return_inverse=True)
    lm = np.bincount(inv, weights=loss_model, minlength=len(ud))
    lb = np.bincount(inv, weights=loss_base, minlength=len(ud))
    rng = np.random.default_rng(106)
    idx = rng.integers(0, len(ud), size=(reps, len(ud)))
    sm, sb = lm[idx].sum(1), lb[idx].sum(1)
    sk = 1 - sm / np.where(sb > 0, sb, np.nan)
    return [float(np.nanquantile(sk, 0.025)), float(np.nanquantile(sk, 0.975))]


def score_binary(p: np.ndarray, y: np.ndarray, base: np.ndarray, days: np.ndarray | None = None) -> dict | None:
    ok = np.isfinite(p) & np.isfinite(base)
    if ok.sum() < 50:
        return None
    dd = days[ok] if days is not None else None
    p, y, base = np.clip(p[ok], 1e-4, 1 - 1e-4), y[ok], np.clip(base[ok], 1e-4, 1 - 1e-4)
    brier = float(np.mean((p - y) ** 2))
    brier0 = float(np.mean((base - y) ** 2))
    ll = float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))
    acc = float(np.mean((p > 0.5) == (y > 0.5)))
    conf = np.abs(p - 0.5) >= 0.1
    calib = []
    for lo in np.arange(0, 1, 0.1):
        b = (p >= lo) & (p < lo + 0.1)
        if b.sum() >= 20:
            calib.append({"from": float(lo), "n": int(b.sum()), "said": float(p[b].mean()), "happened": float(y[b].mean())})
    ci = skill_ci((p - y) ** 2, (base - y) ** 2, dd)
    return {"n": int(ok.sum()), "brier": brier, "brier_baseline": brier0, "skill": 1 - brier / brier0 if brier0 else None,
            "skill_ci": ci, "real": bool(ci and ci[0] > 0),
            "logloss": ll, "accuracy": acc, "confident_n": int(conf.sum()),
            "confident_accuracy": float(np.mean((p[conf] > 0.5) == (y[conf] > 0.5))) if conf.any() else None,
            "spread": float(np.std(p)), "calibration": calib}


def score_real(p: np.ndarray, y: np.ndarray, days: np.ndarray | None = None) -> dict | None:
    ok = np.isfinite(p)
    if ok.sum() < 50:
        return None
    dd = days[ok] if days is not None else None
    p, y = p[ok], y[ok]
    mae = float(np.mean(np.abs(p - y)))
    mae0 = float(np.mean(np.abs(0 - y)))
    ci = skill_ci(np.abs(p - y), np.abs(y), dd)
    return {"n": int(ok.sum()), "mae": mae, "mae_baseline": mae0, "skill": 1 - mae / mae0 if mae0 else None,
            "skill_ci": ci, "real": bool(ci and ci[0] > 0),
            "corr": float(np.corrcoef(p, y)[0, 1]) if np.std(p) > 0 else None}


def bands(pred: np.ndarray, y: np.ndarray, months: np.ndarray) -> tuple[np.ndarray, dict]:
    """Size bands from OUT-OF-SAMPLE residuals of EARLIER months only (q10, q25, q75, q90 per row)."""
    n = len(pred)
    q = np.full((n, 4), np.nan)
    um = np.unique(months[np.isfinite(pred)])
    hist = np.zeros(0)
    for mo in um:
        sel = (months == mo) & np.isfinite(pred)
        if len(hist) >= 300:
            qs = np.quantile(hist, [0.1, 0.25, 0.75, 0.9])
            q[sel] = pred[sel][:, None] + qs[None, :]
        hist = np.r_[hist, (y - pred)[sel]]
    ok = np.isfinite(q[:, 0])
    cover = {}
    if ok.any():
        cover = {"inside_50": float(np.mean((y[ok] >= q[ok, 1]) & (y[ok] <= q[ok, 2]))),
                 "inside_80": float(np.mean((y[ok] >= q[ok, 0]) & (y[ok] <= q[ok, 3]))), "n": int(ok.sum())}
    return q, cover


def evaluate(preds: dict, y: np.ndarray, months: np.ndarray, kind: str, days: np.ndarray | None = None) -> dict:
    """Scores per model on the whole predicted period, the model CHOSEN on the first 70 % of the predicted months and
    its score on the last 30 %."""
    have = np.isfinite(preds["logistic"])
    um = np.unique(months[have])
    out: dict = {"months": [str(x) for x in um]}
    if not len(um):
        return out
    cut = um[int(math.floor(0.7 * len(um)))] if len(um) > 1 else um[-1]
    early, late = have & (months < cut), have & (months >= cut)
    sc = {}
    for mname, p in preds.items():
        if mname == "baseline" and kind == "real":
            continue
        dv = (lambda msk: days[msk]) if days is not None else (lambda msk: None)
        if kind == "binary":
            sc[mname] = {"all": score_binary(p[have], y[have], preds["baseline"][have], dv(have)),
                         "early": score_binary(p[early], y[early], preds["baseline"][early], dv(early)),
                         "late": score_binary(p[late], y[late], preds["baseline"][late], dv(late))}
        else:
            sc[mname] = {"all": score_real(p[have], y[have], dv(have)), "early": score_real(p[early], y[early], dv(early)),
                         "late": score_real(p[late], y[late], dv(late))}
    cands = [k for k in sc if k != "baseline" and sc[k]["early"] and sc[k]["early"].get("skill") is not None]
    chosen = max(cands, key=lambda k: sc[k]["early"]["skill"]) if cands else None
    out.update(scores=sc, chosen=chosen, chosen_on=f"months before {cut}", reported_on=f"months from {cut}")
    return out


# =============================================================================================== the job
def run(cx: Context, out_dir, progress=None) -> dict:
    """Walk-forward predictions of UP / SIZE / BIAS / LEVELS; writes forecast.npz (per candle) and returns scores."""
    from pathlib import Path
    step = progress or (lambda s: None)
    out_dir = Path(out_dir)
    step("Forecast: building the live-knowledge inputs of every 15-minute candle")
    cr = candle_rows(cx)
    X, names, info = features(cx, cr["t"])
    days = cr["day"].astype("datetime64[D]")
    months = _months(days)
    res: dict = {"inputs": names, "candles": int(len(cr["t"]))}
    upm = cr["up"] != 0
    yb = (cr["up"] > 0).astype(float)
    onehot = np.array([n.startswith(ONEHOT_PREFIX) for n in names])
    wf_up = walk_forward(X[upm], yb[upm], days[upm], cr["slot"][upm], "binary", step, "Next candle up or down",
                         onehot=onehot)
    p_up = {k: np.full(len(yb), np.nan) for k in MODELS}
    for k in MODELS:
        p_up[k][upm] = wf_up["pred"][k]
    res["up"] = evaluate(wf_up["pred"], yb[upm], months[upm], "binary", days[upm])
    ci = np.full((len(yb), 3), -1, np.int16)
    cv = np.zeros((len(yb), 3), np.float32)
    ci[upm], cv[upm] = wf_up["contrib_idx"], wf_up["contrib_val"]
    wf_sz = walk_forward(X, cr["size"], days, cr["slot"], "real", step, "Candle size")
    res["size"] = evaluate(wf_sz["pred"], cr["size"], months, "real", days)
    chosen_sz = res["size"].get("chosen") or "logistic"
    q, cover = bands(wf_sz["pred"][chosen_sz], cr["size"], months)
    res["size"]["bands"] = cover
    step("Forecast: daily bias (does the session close above the current price?)")
    br = bias_rows(cx)
    if len(br["t"]):
        Xb, _, _ = features(cx, br["t"])
        bm = br["up"] != 0
        bd = br["day"].astype("datetime64[D]")
        slot_b = P.ny_minutes(br["t"]) // 30
        wf_b = walk_forward(Xb[bm], (br["up"][bm] > 0).astype(float), bd[bm], slot_b[bm], "binary", step, "Daily bias")
        res["bias"] = evaluate(wf_b["pred"], (br["up"][bm] > 0).astype(float), _months(bd[bm]), "binary", bd[bm])
        bias_pred = {k: v for k, v in wf_b["pred"].items()}
        bias_t = br["t"][bm]
    else:
        bias_pred, bias_t = {k: np.zeros(0) for k in MODELS}, np.zeros(0, np.int64)
    step("Forecast: which levels get reached before the session ends")
    lr = level_rows(cx)
    if len(lr.get("t", [])):
        XL, lnames = level_matrix(lr)
        base_cols = XL[:, :5]
        ld = lr["day"].astype("datetime64[D]")
        ltype = np.array([LEVEL_KEYS.index(k) for k in lr["level"]])
        keep = ["abs_dist", "side", "log_dist", "time_left", "dist_per_sqrt_time", "size60", "size15_lag1", "move_day"]
        sim_cols = [lnames.index(c) for c in keep if c in lnames]
        wf_l = walk_forward(XL, lr["reached"].astype(float), ld, ltype, "binary", step, "Levels", base_X=base_cols,
                            sim_cols=sim_cols, sim_exact=True)
        res["levels"] = evaluate(wf_l["pred"], lr["reached"].astype(float), _months(ld), "binary", ld)
        by = []
        for key in LEVEL_KEYS:
            s = lr["level"] == key
            if s.sum() >= 20:
                by.append({"level": key, "name": LEVEL_WORDS[key], "n": int(s.sum()),
                           "reached": float(lr["reached"][s].mean())})
        res["levels"]["by_level"] = by
        level_pred = wf_l["pred"]
    else:
        level_pred = {k: np.zeros(0) for k in MODELS}
    np.savez_compressed(
        out_dir / "forecast.npz", t=cr["t"], up=cr["up"], size=cr["size"], day=cr["day"].astype("datetime64[D]").astype(np.int64),
        **{f"p_up_{k}": v.astype(np.float32) for k, v in p_up.items()},
        **{f"size_{k}": v.astype(np.float32) for k, v in wf_sz["pred"].items()}, size_q=q.astype(np.float32),
        contrib_idx=ci, contrib_val=cv, bias_t=bias_t, **{f"bias_{k}": np.asarray(v, np.float32) for k, v in bias_pred.items()},
        level_t=lr.get("t", np.zeros(0, np.int64)), level_key=lr.get("level", np.zeros(0, str)).astype(str),
        level_price=lr.get("price", np.zeros(0)).astype(np.float64), level_reached=lr.get("reached", np.zeros(0, bool)),
        **{f"level_{k}": np.asarray(v, np.float32) for k, v in level_pred.items()})
    return res


def day_view(m: D.Minute, fc: dict, summary: dict, date: str, news: list[dict]) -> dict:
    """The 15-minute candles of one trading date with the predictions that were made live for them."""
    d = np.datetime64(date, "D")
    sel = m.day == d
    if not sel.any():
        raise KeyError(date)
    mm = D.Minute(m.ts[sel], m.o[sel], m.h[sel], m.l[sel], m.c[sel])
    b = D.resample(mm, 15)
    t = fc["t"]
    j = np.searchsorted(t, b.ts)
    j = np.minimum(j, len(t) - 1)
    have = (len(t) > 0) & (t[j] == b.ts) if len(t) else np.zeros(len(b), bool)
    up_chosen = (summary.get("up") or {}).get("chosen") or "logistic"
    sz_chosen = (summary.get("size") or {}).get("chosen") or "logistic"
    inputs = summary.get("inputs", [])
    candles = []
    for q in range(len(b)):
        row = {"t": int(b.ts[q]), "o": float(b.o[q]), "h": float(b.h[q]), "l": float(b.l[q]), "c": float(b.c[q])}
        if have[q]:
            r = j[q]
            row["p_up"] = {k: _f(fc[f"p_up_{k}"][r]) for k in MODELS}
            row["size"] = {k: _f(fc[f"size_{k}"][r]) for k in MODELS}
            row["size_q"] = [_f(x) for x in fc["size_q"][r]]
            row["actual_size"] = _f(fc["size"][r])
            row["why"] = [{"input": input_word(inputs[int(i)]) if 0 <= int(i) < len(inputs) else None, "push": _f(v)}
                          for i, v in zip(fc["contrib_idx"][r], fc["contrib_val"][r]) if int(i) >= 0 and _f(v)]
        candles.append(row)
    lo_ns, hi_ns = int(mm.ts[0]), int(mm.ts[-1]) + D.MIN_NS
    bias = []
    if len(fc["bias_t"]):
        s = (fc["bias_t"] >= lo_ns) & (fc["bias_t"] < hi_ns)
        for r in np.flatnonzero(s):
            bias.append({"t": int(fc["bias_t"][r]), **{k: _f(fc[f"bias_{k}"][r]) for k in MODELS}})
    levels = []
    if len(fc["level_t"]):
        s = (fc["level_t"] >= lo_ns) & (fc["level_t"] < hi_ns)
        for r in np.flatnonzero(s):
            levels.append({"t": int(fc["level_t"][r]), "level": str(fc["level_key"][r]),
                           "name": LEVEL_WORDS.get(str(fc["level_key"][r]), str(fc["level_key"][r])),
                           "price": float(fc["level_price"][r]), "reached": bool(fc["level_reached"][r]),
                           **{k: _f(fc[f"level_{k}"][r]) for k in MODELS}})
    ev = [{"t": e["ts"], "name": e["name"], "impact": e["impact"], "forecast": e.get("forecast"),
           "actual": e.get("actual"), "surprise_z": e.get("surprise_z")} for e in news if lo_ns <= e["ts"] < hi_ns]
    return {"date": date, "candles": candles, "bias": bias, "levels": levels, "news": ev,
            "chosen": {"up": up_chosen, "size": sz_chosen}}


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None
