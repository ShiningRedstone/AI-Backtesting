"""Classic oscillator / band / channel indicators (added for the strategy factory, ADR-60).

Every output at bar t uses bars <= t only (prefix-invariant, verified by the registry-wide
truncation test in tests/test_mtf_causality.py). Channel levels that a breakout compares
against (``donchian``) EXCLUDE bar t, so "close above the upper channel" is a real break.
"""
from __future__ import annotations

import numpy as np

from edgelab.features.library._util import NAN, prior, recursive_smooth, safe_div, trailing
from edgelab.features.library.price import _true_range
from edgelab.features.spec import FeatureDef, Param, register


def _ema_of(x: np.ndarray, n: int, first: int) -> np.ndarray:
    """SMA-seeded EMA of ``x`` whose first finite value is at index ``first``."""
    seed_i = first + n - 1
    seed = float(x[first:seed_i + 1].mean()) if len(x) > seed_i else NAN
    return recursive_smooth(x, 2.0 / (n + 1), seed_i, seed)


def _window_rank(x: np.ndarray, w: int) -> np.ndarray:
    """Fraction of the last w values (incl. t) that are <= x_t; NaN if any value in the window is NaN."""
    out = np.full(len(x), NAN)
    if len(x) >= w:
        win = np.lib.stride_tricks.sliding_window_view(x, w)
        r = (win <= win[:, -1:]).sum(axis=1) / w
        r[~np.isfinite(win).all(axis=1)] = NAN
        out[w - 1:] = r
    return out


# --------------------------------------------------------------------------- MACD
def _macd(inp, p):
    f, s, g = p["fast"], p["slow"], p["signal"]
    c = inp.bars.close
    macd = _ema_of(c, f, 0) - _ema_of(c, s, 0)
    first = max(f, s) - 1
    sig = _ema_of(np.where(np.isfinite(macd), macd, 0.0), g, first)
    sig[:first] = NAN
    return {"macd": macd, "signal": sig, "hist": macd - sig}


register(FeatureDef(
    feature_id="macd", version=1, category="momentum",
    params=(Param("fast", 12, int, "fast EMA length", min=1), Param("slow", 26, int, "slow EMA length", min=1),
            Param("signal", 9, int, "signal EMA length (of the MACD line)", min=1)),
    outputs=(("macd", "EMA_fast(C) - EMA_slow(C)"), ("signal", "EMA_signal of the MACD line"),
             ("hist", "macd - signal")),
    compute=_macd,
    summary="MACD line, signal line and histogram (SMA-seeded EMAs).",
    calculation="EMAs as in `ema` (SMA-seeded). MACD_t = EMA_fast - EMA_slow, defined from bar max(fast, slow)-1. "
                "Signal = EMA_signal of MACD, seeded with the mean of its first `signal` defined values.",
    edge_cases="NaN before bar max(fast, slow) + signal - 2 for signal/hist. fast >= slow is allowed but "
               "meaningless; strategy validation decides.",
    warmup="max(fast, slow) + signal - 2 bars"))


# --------------------------------------------------------------------------- ADX
def _adx(inp, p):
    n = p["period"]
    b = inp.bars
    up = b.high - np.r_[NAN, b.high[:-1]]
    dn = np.r_[NAN, b.low[:-1]] - b.low
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    mdm = np.where((dn > up) & (dn > 0), dn, 0.0)
    tr = _true_range(b)
    size = len(tr)
    out = {k: np.full(size, NAN) for k in ("adx", "plus_di", "minus_di", "dx")}
    if size <= n:
        return out
    sm = lambda x: recursive_smooth(x, 1.0 / n, n, float(x[1:n + 1].mean()))
    atr_w, pdm_s, mdm_s = sm(tr), sm(pdm), sm(mdm)
    pdi, mdi = 100.0 * safe_div(pdm_s, atr_w), 100.0 * safe_div(mdm_s, atr_w)
    tot = pdi + mdi
    dx = np.where(tot > 0, 100.0 * np.abs(pdi - mdi) / np.where(tot > 0, tot, 1.0), 0.0)
    dx[~np.isfinite(tot)] = NAN
    seed_i = 2 * n - 1
    adx = (recursive_smooth(np.where(np.isfinite(dx), dx, 0.0), 1.0 / n, seed_i, float(dx[n:seed_i + 1].mean()))
           if size > seed_i else np.full(size, NAN))
    return {"adx": adx, "plus_di": pdi, "minus_di": mdi, "dx": dx}


register(FeatureDef(
    feature_id="adx", version=1, category="trend",
    params=(Param("period", 14, int, "Wilder length n", min=1),),
    outputs=(("adx", "Average Directional Index 0..100 (trend strength)"), ("plus_di", "+DI 0..100"),
             ("minus_di", "-DI 0..100"), ("dx", "directional index of the bar")),
    compute=_adx,
    summary="Wilder ADX with +DI / -DI.",
    calculation="+DM = H_t - H_{t-1} if it exceeds L_{t-1} - L_t and is > 0 (else 0); -DM mirrored. TR, +DM, -DM "
                "Wilder-smoothed (seed = mean of bars 1..n at bar n). DI = 100 * smoothed DM / smoothed TR; "
                "DX = 100 |+DI - -DI| / (+DI + -DI) (0 if both 0); ADX = Wilder average of DX seeded at bar 2n-1.",
    edge_cases="NaN before bar n (DI) and 2n-1 (ADX). Gaps enter through TR and DM of the previous AVAILABLE bar.",
    warmup="2n - 1 bars"))


# --------------------------------------------------------------------------- Stochastic
def _stoch(inp, p):
    k, d = p["k_period"], p["d_period"]
    b = inp.bars
    hh, ll = trailing(b.high, k, np.max), trailing(b.low, k, np.min)
    pk = 100.0 * safe_div(b.close - ll, hh - ll)
    return {"k": pk, "d": trailing(pk, d, np.mean)}


register(FeatureDef(
    feature_id="stoch", version=1, category="momentum",
    params=(Param("k_period", 14, int, "%K lookback", min=1), Param("d_period", 3, int, "%D smoothing", min=1)),
    outputs=(("k", "%K = 100 (C - LL_k) / (HH_k - LL_k)"), ("d", "SMA_d of %K")),
    compute=_stoch,
    summary="Stochastic oscillator %K / %D.",
    calculation="HH/LL over the last k bars incl. t; %D = mean of the last d %K values.",
    edge_cases="HH == LL gives NaN (not 50). %D is NaN while any %K in its window is NaN.",
    warmup="k + d - 2 bars"))


# --------------------------------------------------------------------------- Bollinger / z-score
def _bollinger(inp, p):
    n, k, rl = p["period"], p["k"], p["rank_lookback"]
    c = inp.bars.close
    mid = trailing(c, n, np.mean)
    std = trailing(c, n, np.std)
    upper, lower = mid + k * std, mid - k * std
    bw = safe_div(upper - lower, mid)
    return {"mid": mid, "upper": upper, "lower": lower, "std": std, "zscore": safe_div(c - mid, std),
            "bandwidth": bw, "bandwidth_rank": _window_rank(bw, rl)}


register(FeatureDef(
    feature_id="bollinger", version=1, category="volatility",
    params=(Param("period", 20, int, "window n", min=2), Param("k", 2.0, float, "band width in std devs", min=0.0),
            Param("rank_lookback", 100, int, "window for bandwidth_rank", min=2)),
    outputs=(("mid", "SMA_n of close"), ("upper", "mid + k * std"), ("lower", "mid - k * std"),
             ("std", "population std (ddof=0) of the last n closes"), ("zscore", "(C - mid) / std"),
             ("bandwidth", "(upper - lower) / mid"),
             ("bandwidth_rank", "fraction of the last rank_lookback bandwidths (incl. t) <= bandwidth_t; "
                                "low = compressed volatility")),
    compute=_bollinger,
    summary="Bollinger bands, rolling z-score and bandwidth rank (squeeze).",
    calculation="Windows of the last n closes incl. C_t; std with ddof=0. bandwidth_rank is a trailing percentile rank.",
    edge_cases="std == 0 gives NaN zscore. bandwidth_rank is NaN while any bandwidth in its window is NaN.",
    warmup="n - 1 bars (bandwidth_rank: n + rank_lookback - 2)"))


# --------------------------------------------------------------------------- Donchian
def _donchian(inp, p):
    n = p["period"]
    b = inp.bars
    up, lo = prior(b.high, n, np.max), prior(b.low, n, np.min)
    return {"upper": up, "lower": lo, "mid": 0.5 * (up + lo), "width": up - lo}


register(FeatureDef(
    feature_id="donchian", version=1, category="structure",
    params=(Param("period", 20, int, "channel length n (bars before t)", min=1),),
    outputs=(("upper", "max(H_{t-n} .. H_{t-1}) - EXCLUDES bar t"), ("lower", "min(L_{t-n} .. L_{t-1})"),
             ("mid", "(upper + lower) / 2"), ("width", "upper - lower")),
    compute=_donchian,
    summary="Donchian channel of the n bars BEFORE t (a close beyond it is a breakout).",
    calculation="Rolling max/min of the previous n highs/lows, excluding the current bar.",
    edge_cases="NaN before bar n. Windows span session breaks and missing bars.",
    warmup="n bars"))


# --------------------------------------------------------------------------- narrow range (NR-n)
def _narrow_range(inp, p):
    n = p["n"]
    b = inp.bars
    rng = b.high - b.low
    prev_min = prior(rng, n - 1, np.min)
    with np.errstate(invalid="ignore"):
        is_nr = np.where(np.isfinite(prev_min), (rng < prev_min).astype(float), NAN)
    return {"is_nr": is_nr, "range": rng}


register(FeatureDef(
    feature_id="narrow_range", version=1, category="volatility",
    params=(Param("n", 7, int, "NR-n: bar range strictly below the previous n-1 ranges", min=2),),
    outputs=(("is_nr", "1 if H_t - L_t < min range of the previous n-1 bars, else 0"), ("range", "H_t - L_t")),
    compute=_narrow_range,
    summary="Narrow-range bar (NR4 / NR7 ...).",
    calculation="is_nr_t = (H_t - L_t) < min(H_s - L_s, s = t-n+1 .. t-1). Strict: ties are not narrow.",
    edge_cases="NaN before bar n-1. On a higher timeframe it is the NR of completed HTF bars.",
    warmup="n - 1 bars"))


# --------------------------------------------------------------------------- ATR regime
def _atr_regime(inp, p):
    n, lb = p["atr_period"], p["lookback"]
    tr = _true_range(inp.bars)
    seed = float(tr[:n].mean()) if len(tr) >= n else NAN
    atr = recursive_smooth(tr, 1.0 / n, n - 1, seed)
    return {"atr_rank": _window_rank(atr, lb), "atr": atr}


register(FeatureDef(
    feature_id="atr_regime", version=1, category="volatility",
    params=(Param("atr_period", 14, int, "ATR length", min=1), Param("lookback", 100, int, "rank window", min=2)),
    outputs=(("atr_rank", "fraction of the last `lookback` ATR values (incl. t) <= ATR_t (0..1)"),
             ("atr", "Wilder ATR (same as `atr`)")),
    compute=_atr_regime,
    summary="Volatility regime: trailing percentile rank of ATR.",
    calculation="ATR as in `atr`; rank over the trailing window including t.",
    edge_cases="NaN until atr_period + lookback - 2 bars.",
    warmup="atr_period + lookback - 2 bars"))
