"""Trend and session statistics (ADR-106). Conditional, not averages: what FOLLOWS what (day type after day type, the
next 15-minute candle after k candles one way), where and when the day's extremes form, and how each session behaves.
Distributions are reported as quantiles; every rate carries its sample size and a 95 % interval."""
from __future__ import annotations

import math

import numpy as np

from edgelab.market import data as D
from edgelab.market.patterns import SESSION_NAMES, session_code

DAY_TYPES = ("trend up", "trend down", "range", "normal")


def ci(k: int, n: int) -> list | None:
    """Wilson 95 % interval of k / n."""
    if n <= 0:
        return None
    z = 1.96
    p = k / n
    den = 1 + z * z / n
    c = (p + z * z / (2 * n)) / den
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return [c - h, c + h]


def rate(k: int, n: int) -> dict:
    return {"k": int(k), "n": int(n), "p": (k / n) if n else None, "ci": ci(k, n)}


def quantiles(x, qs=(0.1, 0.25, 0.5, 0.75, 0.9)) -> dict | None:
    x = np.asarray(x, float)
    x = x[np.isfinite(x)]
    if not len(x):
        return None
    return {f"q{int(q * 100)}": float(np.quantile(x, q)) for q in qs} | {"n": int(len(x))}


def day_types(facts: dict) -> np.ndarray:
    """Per day: 0 trend up, 1 trend down, 2 range, 3 normal, -1 unknown. Trend = the regular session closes in its top
    (bottom) 20 % AND its range is at least the median of the previous 20 sessions; range = closes in the middle
    30-70 % AND its range is below that median."""
    hi, lo, op, cl = (np.asarray(facts[k], float) for k in ("rth_hi", "rth_lo", "rth_open", "rth_close"))
    rng = hi - lo
    pos = (cl - lo) / np.where(rng > 0, rng, np.nan)
    med = np.full(len(rng), np.nan)
    hist: list[float] = []
    for i in range(len(rng)):
        if len(hist) >= 10:
            med[i] = float(np.median(hist[-20:]))
        if np.isfinite(rng[i]):
            hist.append(float(rng[i]))
    t = np.full(len(rng), -1, dtype=np.int8)
    ok = np.isfinite(pos) & np.isfinite(med)
    t[ok] = 3
    t[ok & (pos >= 0.8) & (rng >= med)] = 0
    t[ok & (pos <= 0.2) & (rng >= med)] = 1
    t[ok & (pos >= 0.3) & (pos <= 0.7) & (rng < med)] = 2
    return t


def analyse(m: D.Minute, bars: dict, facts: dict, news_days: set | None = None) -> dict:
    out: dict = {}
    t = day_types(facts)
    known = t >= 0
    out["day_types"] = {DAY_TYPES[k]: rate(int((t == k).sum()), int(known.sum())) for k in range(4)}
    trans = {}
    for a in range(4):
        prev = np.flatnonzero(t[:-1] == a)
        nxt = t[prev + 1]
        nxt = nxt[nxt >= 0]
        trans[DAY_TYPES[a]] = {DAY_TYPES[b]: rate(int((nxt == b).sum()), len(nxt)) for b in range(4)}
    out["day_type_next"] = trans
    # where the regular session's high / low form (30-minute bins from 9:30) and which comes first
    th, tl = np.asarray(facts["t_hi"], float), np.asarray(facts["t_lo"], float)
    ok = np.isfinite(th) & np.isfinite(tl)
    bins = list(range(570, 961, 30))
    out["time_of_high"] = np.histogram(th[ok], bins=bins)[0].tolist()
    out["time_of_low"] = np.histogram(tl[ok], bins=bins)[0].tolist()
    out["time_bins"] = [f"{b // 60}:{b % 60:02d}" for b in bins[:-1]]
    up = (np.asarray(facts["rth_close"]) > np.asarray(facts["rth_open"])) & ok
    dn = (np.asarray(facts["rth_close"]) < np.asarray(facts["rth_open"])) & ok
    out["power_of_3"] = {"up_days_low_first": rate(int((tl[up] < th[up]).sum()), int(up.sum())),
                         "down_days_high_first": rate(int((th[dn] < tl[dn]).sum()), int(dn.sum())),
                         "high_or_low_in_first_hour": rate(int(((th[ok] < 630) | (tl[ok] < 630)).sum()), int(ok.sum()))}
    # session ranges and which session makes the trading date's high / low
    days = np.unique(m.day)
    di = np.searchsorted(days, m.day)
    sc = session_code(m.ny_min)
    sess = []
    day_hi = np.full(len(days), -np.inf)
    day_lo = np.full(len(days), np.inf)
    np.maximum.at(day_hi, di, m.h)
    np.minimum.at(day_lo, di, m.l)
    hi_i = np.full(len(days), -1)
    lo_i = np.full(len(days), -1)
    order = np.lexsort((-m.h, di))
    first = np.r_[True, di[order][1:] != di[order][:-1]]
    hi_i[di[order][first]] = order[first]
    order = np.lexsort((m.l, di))
    first = np.r_[True, di[order][1:] != di[order][:-1]]
    lo_i[di[order][first]] = order[first]
    for s, name in enumerate(SESSION_NAMES):
        msk = sc == s
        shi = np.full(len(days), -np.inf)
        slo = np.full(len(days), np.inf)
        np.maximum.at(shi, di[msk], m.h[msk])
        np.minimum.at(slo, di[msk], m.l[msk])
        have = np.isfinite(shi) & np.isfinite(slo)
        share = (shi - slo) / np.where(day_hi > day_lo, day_hi - day_lo, np.nan)
        sess.append({"session": name, "range_pts": quantiles((shi - slo)[have]), "share_of_day": quantiles(share[have]),
                     "makes_high": rate(int((sc[hi_i[hi_i >= 0]] == s).sum()), int((hi_i >= 0).sum())),
                     "makes_low": rate(int((sc[lo_i[lo_i >= 0]] == s).sum()), int((lo_i >= 0).sum()))})
    out["sessions"] = sess
    # 15-minute candles: what follows k candles in a row, autocorrelation, run lengths vs independence
    b = bars[15]
    sgn = np.sign(b.c - b.o).astype(int)
    same_day = np.r_[False, b.day[1:] == b.day[:-1]]
    follow = []
    for k in range(1, 6):
        for d in (1, -1):
            run = np.ones(len(sgn), bool)                 # candles i-k .. i-1 all went d, all in the same session as i
            run[:k] = False
            for j in range(1, k + 1):
                run[k:] &= (sgn[k - j:len(sgn) - j] == d) & same_day[k - j + 1:len(sgn) - j + 1]
            nxt = sgn[run]
            nz = nxt[nxt != 0]
            follow.append({"k": k, "after": "up" if d > 0 else "down",
                           "next_same": rate(int((nz == d).sum()), len(nz))})
    base_up = sgn[sgn != 0]
    out["m15_after_runs"] = follow
    out["m15_up_rate"] = rate(int((base_up > 0).sum()), len(base_up))
    r = (b.c - b.o)
    ac = []
    for lag in range(1, 9):
        ok2 = np.r_[np.zeros(lag, bool), b.day[lag:] == b.day[:-lag]]
        x, y = r[lag:][ok2[lag:]], r[:-lag][ok2[lag:]]
        if len(x) > 30:
            c = float(np.corrcoef(x, y)[0, 1])
            ac.append({"lag": lag, "corr": c, "band": 1.96 / math.sqrt(len(x)), "n": int(len(x))})
    out["m15_autocorr"] = ac
    # volatility by 15-minute slot of the day (NY clock): quantiles, so the shape is visible, not one number
    slot_ny = (b.slot * 15 + 18 * 60) % 1440
    prof = []
    rng15 = b.h - b.l
    for s in range(0, 1440, 15):
        msk = slot_ny == s
        if msk.sum() >= 20:
            q = quantiles(rng15[msk], (0.25, 0.5, 0.75, 0.9))
            prof.append({"t": f"{s // 60}:{s % 60:02d}", **q, "up": rate(int((sgn[msk] > 0).sum()),
                                                                           int((sgn[msk] != 0).sum()))})
    out["m15_profile"] = prof
    # weekdays: regular-session range and direction
    wd = (np.asarray(facts["day"]).astype("datetime64[D]").astype(np.int64) + 3) % 7      # 0 = Monday
    rr = np.asarray(facts["rth_hi"], float) - np.asarray(facts["rth_lo"], float)
    out["weekdays"] = [{"weekday": ["Mon", "Tue", "Wed", "Thu", "Fri"][w], "range": quantiles(rr[(wd == w) & ok]),
                        "up": rate(int(up[wd == w].sum()), int((up | dn)[wd == w].sum()))} for w in range(5)]
    if news_days is not None:
        nd = np.array([str(d) in news_days for d in facts["day"]])
        out["news_days"] = {"with_high_impact": {"range": quantiles(rr[nd & ok]), "types": {
                                DAY_TYPES[k]: rate(int((t[nd & known] == k).sum()), int((nd & known).sum())) for k in range(4)}},
                            "without": {"range": quantiles(rr[~nd & ok]), "types": {
                                DAY_TYPES[k]: rate(int((t[~nd & known] == k).sum()), int((~nd & known).sum())) for k in range(4)}}}
    return out
