"""Basic market structure with explicit, causal definitions.

A raw "pivot at bar j" label needs R FUTURE bars and is therefore NOT causal; it is
never exposed. What is exposed is the most recent swing CONFIRMED by bar t, i.e. whose
pivot bar j satisfies j + R <= t.
"""
from __future__ import annotations

import numpy as np

from edgelab.features.library._util import (NAN, ffill, prior, rolling_window, safe_div, shift,
                                            trailing)
from edgelab.features.spec import FeatureDef, FeatureSpec, Param, register


def _confirmed_pivots(x: np.ndarray, left: int, right: int, high: bool):
    """Return (level_t, pivot_index_t): latest confirmed pivot as known at each bar t."""
    n = len(x)
    level = np.full(n, NAN)
    pidx = np.full(n, NAN)
    if n < left + right + 1:
        return level, pidx
    j = np.arange(left, n - right)
    lw = rolling_window(x, left)[: n - left]          # row j - left covers x[j-left : j]
    rw = rolling_window(x, right)[1:] if right else None   # row j covers x[j+1 : j+1+right]
    if high:
        left_ext = lw.max(axis=1)[j - left]
        right_ext = rw.max(axis=1)[j] if right else np.full(len(j), -np.inf)
        is_piv = (x[j] > left_ext) & (x[j] >= right_ext)
    else:
        left_ext = lw.min(axis=1)[j - left]
        right_ext = rw.min(axis=1)[j] if right else np.full(len(j), np.inf)
        is_piv = (x[j] < left_ext) & (x[j] <= right_ext)
    piv = j[is_piv]
    conf = piv + right
    level[conf] = x[piv]
    pidx[conf] = piv
    return ffill(level), ffill(pidx)


def _swings(inp, p):
    L, R = p["left"], p["right"]
    b = inp.bars
    n = len(b)
    sh, sh_i = _confirmed_pivots(b.high, L, R, high=True)
    sl, sl_i = _confirmed_pivots(b.low, L, R, high=False)
    t = np.arange(n, dtype=float)
    sh_prev, sl_prev = shift(sh, 1), shift(sl, 1)          # levels known BEFORE bar t
    c, c_prev = b.close, shift(b.close, 1)
    with np.errstate(invalid="ignore"):
        bos_up = (c > sh_prev) & (c_prev <= sh_prev)
        bos_dn = (c < sl_prev) & (c_prev >= sl_prev)
        sweep_h = (b.high > sh_prev) & (c < sh_prev)
        sweep_l = (b.low < sl_prev) & (c > sl_prev)
    trend = np.where(bos_up, 1.0, np.where(bos_dn, -1.0, NAN))
    trend = ffill(trend)
    trend[np.isnan(trend)] = 0.0
    return {"swing_high": sh, "swing_high_age": t - sh_i, "swing_low": sl, "swing_low_age": t - sl_i,
            "bos_up": bos_up.astype(float), "bos_down": bos_dn.astype(float),
            "sweep_high": sweep_h.astype(float), "sweep_low": sweep_l.astype(float), "trend": trend}


register(FeatureDef(
    feature_id="swings", version=1, category="structure",
    params=(Param("left", 3, int, "bars required on the left", min=1),
            Param("right", 3, int, "bars required on the right = confirmation delay", min=0)),
    outputs=(("swing_high", "price of the latest CONFIRMED swing high as of bar t"),
             ("swing_high_age", "bars since that swing's pivot bar (>= right)"),
             ("swing_low", "latest confirmed swing low"), ("swing_low_age", "bars since its pivot bar"),
             ("bos_up", "1 on the bar whose close first crosses above the prior confirmed swing high"),
             ("bos_down", "1 on the bar whose close first crosses below the prior confirmed swing low"),
             ("sweep_high", "1 if H_t > prior swing high but C_t < it (wick through, close back)"),
             ("sweep_low", "1 if L_t < prior swing low but C_t > it"),
             ("trend", "+1 after the latest bos_up, -1 after the latest bos_down, 0 before any")),
    compute=_swings,
    summary="Fractal swings (confirmed only), break of structure, liquidity sweeps, structure trend.",
    calculation="Pivot high at j: H_j > max(H_{j-L}..H_{j-1}) and H_j >= max(H_{j+1}..H_{j+R}); confirmed "
                "at bar j+R (lows mirrored). Levels are forward-filled from confirmation. BOS/sweep compare "
                "bar t with the level known at t-1: bos_up_t = C_t > S_{t-1} and C_{t-1} <= S_{t-1}.",
    edge_cases="Equal highs: the left-most bar of a flat top is the pivot (strict left, non-strict right). "
               "A new swing replaces the level even if the old one was never broken. Swings span session breaks.",
    warmup="left + right bars"))


def _range_stats(inp, p):
    b = inp.bars
    n = p["lookback"]
    atr = inp.dep(FeatureSpec.make("atr", {"period": p["atr_period"]}))["atr"]
    rng = b.high - b.low
    hh = trailing(b.high, n, np.max)
    ll = trailing(b.low, n, np.min)
    return {"body_atr": safe_div(np.abs(b.close - b.open), atr),
            "range_atr": safe_div(rng, atr),
            "range_expansion": safe_div(rng, prior(rng, n, np.mean)),
            "consolidation_atr": safe_div(hh - ll, atr)}


register(FeatureDef(
    feature_id="range_stats", version=1, category="structure",
    params=(Param("atr_period", 14, int, "ATR length for normalisation", min=1),
            Param("lookback", 20, int, "window n", min=2)),
    outputs=(("body_atr", "|C - O| / ATR_t  (displacement strength)"),
             ("range_atr", "(H - L) / ATR_t"),
             ("range_expansion", "(H - L) / mean range of the previous n bars"),
             ("consolidation_atr", "(max H - min L over the last n bars incl. t) / ATR_t; small = tight range")),
    compute=_range_stats,
    depends=lambda p: [FeatureSpec.make("atr", {"period": p["atr_period"]})],
    summary="Displacement, range expansion and consolidation, normalised by ATR.",
    calculation="Ratios of the bar body/range and the n-bar range to ATR_t (ATR includes bar t, known at its close).",
    edge_cases="NaN during ATR warm-up. Thresholds (e.g. 'displacement if body_atr > 1.5') are strategy "
               "parameters, not part of the feature.",
    warmup="max(atr_period - 1, n) bars"))
