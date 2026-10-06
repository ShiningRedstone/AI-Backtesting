"""Cross-timeframe effects and context of every pattern (ADR-106).

EFFECTS (what happened after the pattern, measured from its ACT time = the open of its entry minute, or the minute it
became known when it has no entry):
* next_dir[Y]  for every timeframe Y: the direction of the first Y-bar that opens at or after the act time, signed by
               the pattern's direction (+1 = that bar went the pattern's way, -1 against, 0 flat / missing);
* next_rng[Y]  that bar's high-low divided by the median high-low of the SAME slot over the previous 20 trading dates
               (a scale, so 'big' means big for that time of day; not a prediction);
* m15_rest     the rest of the 15-minute candle the pattern happened in: did it close the pattern's way from the act price;
* m15_4        the next four 15-minute candles together, in 15m ATRs, signed by direction;
* m15_bos      a 15-minute break of structure in the pattern's direction within the next eight 15-minute candles.

CONTEXT (known strictly before the act time; signed relative to the pattern's direction):
* trend[Y]     for Y in 15m / 1h / 4h / 1D: direction of the last break of structure on Y (+1 aligned, -1 against, 0 none);
* zone[Y]      for Y in 15m / 1h / 4h / 1D (higher than the pattern's own timeframe): price inside an UNFILLED FVG of Y
               (+1 one pointing the pattern's way, -1 the opposite way, 0 none);
* pd           premium / discount in the previous trading date's range (+1 the 'right' half for the direction: buys in
               discount, sells in premium; -1 the other half);
* es           ES's last 15 minutes moved the same way (+1) or the opposite way (-1), 0 unknown;
* news         a high-impact USD event within 30 minutes before (+1 'after news') / after (-1 'before news'), 0 none;
* vol          the last hour's range against its usual size for that time: -1 quiet (< 0.7), 0 normal, +1 wild (> 1.4).
"""
from __future__ import annotations

import numpy as np

from edgelab.market import data as D

CTX_TF = (15, 60, 240, D.DAY)
CONTEXTS = ("trend_15m", "trend_1h", "trend_4h", "trend_1D", "zone_15m", "zone_1h", "zone_4h", "zone_1D", "pd", "es",
            "news", "vol")
CTX_WORDS = {
    "trend": {1: "with the {tf} trend", -1: "against the {tf} trend", 0: "no {tf} trend yet"},
    "zone": {1: "inside a {tf} FVG pointing the same way", -1: "inside a {tf} FVG pointing the other way",
             0: "not inside a {tf} FVG"},
    "pd": {1: "buy in discount / sell in premium", -1: "buy in premium / sell in discount", 0: "no previous day"},
    "es": {1: "ES moved the same way (15 min)", -1: "ES moved the other way (15 min)", 0: "ES unknown / flat"},
    "news": {1: "within 30 min after high-impact news", -1: "within 30 min before high-impact news", 0: "no news near"},
    "vol": {1: "wild hour", -1: "quiet hour", 0: "normal hour"},
}


def ctx_word(name: str, value: int) -> str:
    if "_" in name:
        base, tf = name.split("_", 1)
        return CTX_WORDS[base][value].format(tf=tf)
    return CTX_WORDS[name][value]


class Frame:
    """Per-timeframe helpers shared by all patterns: bars, ATR, slot-typical range, structure breaks, FVG zones."""

    def __init__(self, bars: dict, breaks: dict, zones: dict):
        self.bars = bars
        self.typ = {tf: D.typical_by_slot(b, b.h - b.l) for tf, b in bars.items() if tf != D.DAY}
        self.atr = {tf: D.atr(b) for tf, b in bars.items()}
        self.breaks = breaks          # tf -> (known_ns sorted, dir)
        self.zones = zones            # tf -> dict(known_ns, end_ns, top, bottom, dir) sorted by known_ns


def effects(m: D.Minute, fr: Frame, act_i: np.ndarray, act_ns: np.ndarray, d: np.ndarray) -> dict:
    out = {}
    d = np.sign(np.asarray(d)).astype(np.int8)
    for tf, b in fr.bars.items():
        if tf == D.DAY or not len(b):
            continue
        k = np.searchsorted(b.ts, act_ns, side="left")
        ok = k < len(b)
        kk = np.minimum(k, len(b) - 1)
        nd = np.where(ok, np.sign(b.c[kk] - b.o[kk]), 0) * d
        typ = fr.typ[tf][kk]
        rr = np.where(ok & (typ > 0), (b.h[kk] - b.l[kk]) / np.where(typ > 0, typ, 1), np.nan)
        out[f"next_dir_{tf}"] = nd.astype(np.int8)
        out[f"next_rng_{tf}"] = rr.astype(np.float32)
    b = fr.bars[15]
    a15 = fr.atr[15]
    if len(b):
        cont = np.searchsorted(b.ts, act_ns, side="right") - 1          # the 15m candle the act time falls in
        cont = np.clip(cont, 0, len(b) - 1)
        px = np.where((act_i >= 0) & (act_i < len(m)), m.o[np.clip(act_i, 0, len(m) - 1)], np.nan)
        out["m15_rest"] = np.nan_to_num(np.sign(b.c[cont] - px) * d).astype(np.int8)
        k4 = np.minimum(cont + 4, len(b) - 1)
        prev_atr = a15[np.maximum(cont - 1, 0)]
        out["m15_4"] = ((b.c[k4] - px) * d / np.where(prev_atr > 0, prev_atr, np.nan)).astype(np.float32)
        bk_ns, bk_dir = fr.breaks.get(15, (np.zeros(0, np.int64), np.zeros(0, np.int8)))
        if len(bk_ns):
            horizon = act_ns + 8 * 15 * D.MIN_NS
            j = np.searchsorted(bk_ns, act_ns, side="right")             # breaks strictly AFTER the act time
            hit = np.zeros(len(act_ns), np.int8)
            for step in range(6):                                       # up to 6 breaks inside the window
                jj = np.minimum(j + step, len(bk_ns) - 1)
                inside = (j + step < len(bk_ns)) & (bk_ns[jj] <= horizon)
                hit |= (inside & (bk_dir[jj] == d)).astype(np.int8)
            out["m15_bos"] = hit
    return out


def contexts(m: D.Minute, fr: Frame, own_tf: int, act_i: np.ndarray, act_ns: np.ndarray, d: np.ndarray,
             lv: dict, es: D.Minute | None, news_ns: np.ndarray) -> dict:
    d = np.sign(np.asarray(d)).astype(np.int8)
    n = len(act_ns)
    out = {}
    px = np.where((act_i >= 0) & (act_i < len(m)), m.o[np.clip(act_i, 0, len(m) - 1)], np.nan)
    for tf in CTX_TF:
        lab = D.TF_LABEL[tf]
        bk_ns, bk_dir = fr.breaks.get(tf, (np.zeros(0, np.int64), np.zeros(0, np.int8)))
        if len(bk_ns):
            j = np.searchsorted(bk_ns, act_ns, side="left") - 1          # the last break strictly BEFORE
            out[f"trend_{lab}"] = np.where(j >= 0, bk_dir[np.maximum(j, 0)] * d, 0).astype(np.int8)
        else:
            out[f"trend_{lab}"] = np.zeros(n, np.int8)
        z = np.zeros(n, np.int8)
        zn = fr.zones.get(tf)
        if tf > own_tf and zn is not None and len(zn["known_ns"]):
            j = np.searchsorted(zn["known_ns"], act_ns, side="left")     # zones known strictly before the act time
            for back in range(1, 41):                                    # the 40 most recent zones of that timeframe
                jj = j - back
                okb = jj >= 0
                jc = np.maximum(jj, 0)
                active = okb & (zn["end_ns"][jc] > act_ns) & (px <= zn["top"][jc]) & (px >= zn["bottom"][jc])
                z = np.where((z == 0) & active, (zn["dir"][jc] * d).astype(np.int8), z)
        out[f"zone_{lab}"] = z
    di = np.searchsorted(lv["days"], m.day[np.clip(act_i, 0, len(m) - 1)])
    di = np.clip(di, 0, len(lv["days"]) - 1)
    pdh, pdl = lv["pdh"][di], lv["pdl"][di]
    pos = (px - pdl) / np.where(pdh > pdl, pdh - pdl, np.nan)
    out["pd"] = np.where(np.isfinite(pos), np.where((pos < 0.5) == (d > 0), 1, -1), 0).astype(np.int8)
    if es is not None and len(es):
        j = np.searchsorted(es.ts, act_ns, side="left") - 1             # last ES minute that opened before the act time
        j0 = np.searchsorted(es.ts, act_ns - 15 * D.MIN_NS, side="left")
        ok = (j >= 0) & (j0 <= j) & (j < len(es))
        mv = np.where(ok, es.c[np.clip(j, 0, len(es) - 1)] - es.o[np.clip(j0, 0, len(es) - 1)], 0)
        out["es"] = (np.sign(mv) * d).astype(np.int8)
    else:
        out["es"] = np.zeros(n, np.int8)
    nw = np.zeros(n, np.int8)
    if len(news_ns):
        j = np.searchsorted(news_ns, act_ns, side="right") - 1
        after = (j >= 0) & (act_ns - news_ns[np.maximum(j, 0)] <= 30 * D.MIN_NS)
        k = np.searchsorted(news_ns, act_ns, side="left")
        before = (k < len(news_ns)) & (news_ns[np.minimum(k, len(news_ns) - 1)] - act_ns <= 30 * D.MIN_NS) & \
            (news_ns[np.minimum(k, len(news_ns) - 1)] > act_ns)
        nw = np.where(after, 1, np.where(before, -1, 0)).astype(np.int8)
    out["news"] = nw
    b = fr.bars[60]
    typ = fr.typ[60]
    j = np.searchsorted(b.known_ns, act_ns, side="right") - 1           # the last COMPLETE hour
    ok = j >= 0
    jc = np.maximum(j, 0)
    ratio = np.where(ok & (typ[jc] > 0), (b.h[jc] - b.l[jc]) / np.where(typ[jc] > 0, typ[jc], 1), np.nan)
    out["vol"] = np.where(ratio > 1.4, 1, np.where(ratio < 0.7, -1, 0)).astype(np.int8)
    return out
