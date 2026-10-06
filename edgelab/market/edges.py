"""Edge scan (ADR-106): which pattern, in which CONDITIONS, did something chance does not explain?

A cell = pattern kind x timeframe x direction x condition, where a condition is: all cases, one session, one context
value (higher-timeframe trend, inside a higher-timeframe FVG, premium / discount, ES agreement, news, volatility), or a
PAIR of them (session x context, context x context). Outcomes (each signed by the pattern's direction):
  edge     1 ATR (of the pattern's timeframe) the pattern's way before 1 ATR against it;
  next15   the next complete 15-minute candle closes the pattern's way;
  rest15   the rest of the current 15-minute candle closes the pattern's way.
Overlapping events are not independent (dozens of 1-minute patterns can share the same next 15-minute candle), so
inside every cell only the FIRST event of each 15-minute window counts: sample sizes are independent 15-minute
windows, not raw pattern counts.
Every cell is compared with the BASE RATE of the same outcome at the same time of day (session) for the same
timeframe and direction, measured from random minutes - not with an overall average.

Find early, confirm late: cells are tested on the FIRST 70 % of the discovery period (two-sided z-test of the rate
against the base rate) with Benjamini-Hochberg false-discovery control at 5 % over ALL tested cells; a cell is an
EDGE CANDIDATE only if it then goes the same way on the LAST 30 % (one-sided p < 0.05), which the scan never saw.
The 1:1 outcome also shows the win rate needed to cover costs (spread + commission + slippage of one MNQ contract, in
points, against the pattern's typical ATR).
"""
from __future__ import annotations

import itertools
import math

import numpy as np

from edgelab.market.crosstf import CONTEXTS, ctx_word
from edgelab.market.patterns import SESSION_NAMES

FIND_SHARE = 0.70
FDR_Q = 0.05
MIN_FIND = 60
MIN_CONFIRM = 25
OUTCOMES = ("edge", "next15", "rest15")
PAIR_CTX = ("trend_1h", "trend_4h", "trend_1D", "zone_1h", "zone_4h", "pd", "es", "news", "vol")


def _p_two(z: float) -> float:
    return math.erfc(abs(z) / math.sqrt(2))


def _p_one(z: float) -> float:
    return 0.5 * math.erfc(z / math.sqrt(2))


def conditions(ctx: dict, session: np.ndarray) -> list[tuple[str, np.ndarray]]:
    n = len(session)
    out = [("all", np.ones(n, bool))]
    for s, name in enumerate(SESSION_NAMES):
        out.append((name, session == s))
    single = []
    for c in CONTEXTS:
        v = ctx.get(c)
        if v is None:
            continue
        for val in (1, -1):
            single.append((c, val, ctx_word(c, val), v == val))
    out += [(w, msk) for _, _, w, msk in single]
    for s, name in enumerate(SESSION_NAMES):
        sm = session == s
        for c, val, w, msk in single:
            if c in PAIR_CTX:
                out.append((f"{name} + {w}", sm & msk))
    pair = [x for x in single if x[0] in PAIR_CTX]
    for (c1, v1, w1, m1), (c2, v2, w2, m2) in itertools.combinations(pair, 2):
        if c1 != c2:
            out.append((f"{w1} + {w2}", m1 & m2))
    return out


def scan_group(key: dict, outcomes: dict, base: dict, ctx: dict, session: np.ndarray, find: np.ndarray,
               atr_pts: float | None, cost_pts: float | None, pvals: list, window: np.ndarray) -> list[dict]:
    """All cells of one (kind, timeframe, direction) group. ``outcomes[o]`` int8 arrays (+1 / -1 / 0),
    ``base[o]`` per-row base rate of a +1 (by session). Every p-value is appended to ``pvals`` (the false-discovery
    control counts them all); only cells with p <= FDR_Q are returned in full (no other cell can pass)."""
    cells = []
    conf = ~find
    for cname, msk in conditions(ctx, session):
        if msk.sum() < MIN_FIND:
            continue
        idx = np.flatnonzero(msk)
        _, first = np.unique(window[idx], return_index=True)     # one event per 15-minute window
        msk = np.zeros(len(msk), bool)
        msk[idx[first]] = True
        if msk.sum() < MIN_FIND:
            continue
        for o in OUTCOMES:
            y = outcomes.get(o)
            if y is None:
                continue
            mf = msk & find & (y != 0)
            nf = int(mf.sum())
            if nf < MIN_FIND:
                continue
            kf = int((y[mf] > 0).sum())
            p0 = float(np.mean(base[o][mf]))
            if not 0 < p0 < 1:
                continue
            z = (kf - nf * p0) / math.sqrt(nf * p0 * (1 - p0))
            mc = msk & conf & (y != 0)
            nc = int(mc.sum())
            kc = int((y[mc] > 0).sum())
            pc0 = float(np.mean(base[o][mc])) if nc else None
            pv = _p_two(z)
            pvals.append(pv)
            if pv > FDR_Q:
                continue
            cells.append({**key, "condition": cname, "outcome": o, "n_find": nf, "rate_find": kf / nf, "base_find": p0,
                          "z": z, "p": _p_two(z), "n_confirm": nc, "rate_confirm": kc / nc if nc else None,
                          "base_confirm": pc0, "kc": kc, "atr_pts": atr_pts,
                          "breakeven": (0.5 + cost_pts / (2 * atr_pts)) if (o == "edge" and atr_pts and cost_pts) else None})
    return cells


def finish(cells: list[dict], pvals: list) -> dict:
    """Benjamini-Hochberg over every tested cell (``pvals``), then the confirmation on the last 30 %."""
    m = len(pvals)
    out = {"cells_tested": m, "fdr_q": FDR_Q, "find_share": FIND_SHARE}
    if not m:
        out.update(passed_find=0, candidates=[], failed_confirm=[], confirmed=0, failed=0, p_cut=None)
        return out
    allp = np.sort(np.asarray(pvals))
    ok = allp <= FDR_Q * (np.arange(1, m + 1) / m)
    last = np.flatnonzero(ok)
    cut = float(allp[last[-1]]) if len(last) else 0.0          # the BH threshold: every p at or below it passes
    out["p_cut"] = cut
    passed = [c for c in cells if len(last) and c["p"] <= cut]
    cands, failed = [], []
    for c in sorted(passed, key=lambda c: c["p"]):
        sign = 1 if c["rate_find"] > c["base_find"] else -1
        nc, pc0 = c["n_confirm"], c["base_confirm"]
        if nc >= MIN_CONFIRM and pc0 and 0 < pc0 < 1:
            zc = (c["kc"] - nc * pc0) / math.sqrt(nc * pc0 * (1 - pc0)) * sign
            c["z_confirm"], c["p_confirm"] = zc, _p_one(zc)
            c["direction"] = "more often than usual" if sign > 0 else "less often than usual (the opposite is the edge)"
            (cands if c["p_confirm"] < 0.05 else failed).append(c)
        else:
            c["p_confirm"] = None
            failed.append(c)
    out.update(passed_find=len(passed), candidates=cands[:300], failed_confirm=failed[:300],
               confirmed=len(cands), failed=len(failed))
    return out
