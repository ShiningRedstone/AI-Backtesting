"""Price-action, trend and momentum features."""
from __future__ import annotations

import numpy as np

from edgelab.features.library._util import (NAN, recursive_smooth, safe_div, shift, trailing)
from edgelab.features.spec import FeatureDef, Param, register


# --------------------------------------------------------------------------- candle
def _candle(inp, p):
    b = inp.bars
    o, h, l, c = b.open, b.high, b.low, b.close
    rng = h - l
    return {
        "body": c - o,
        "abs_body": np.abs(c - o),
        "range": rng,
        "upper_wick": h - np.maximum(o, c),
        "lower_wick": np.minimum(o, c) - l,
        "body_frac": safe_div(np.abs(c - o), rng),
        "close_pos": safe_div(c - l, rng),
        "direction": np.sign(c - o).astype(float),
        "gap": o - shift(c, 1),
    }


register(FeatureDef(
    feature_id="candle", version=1, category="price_action", params=(),
    outputs=(("body", "C - O (signed, points)"), ("abs_body", "|C - O|"), ("range", "H - L"),
             ("upper_wick", "H - max(O, C)"), ("lower_wick", "min(O, C) - L"),
             ("body_frac", "|C - O| / (H - L); NaN when H == L"),
             ("close_pos", "(C - L) / (H - L): 0 = closed at low, 1 = at high; NaN when H == L"),
             ("direction", "sign(C - O): +1 / 0 / -1"),
             ("gap", "O_t - C_{t-1} (previous bar in the dataset, including across sessions)")),
    compute=_candle,
    summary="Single-bar anatomy: body, range, wicks, gap.",
    calculation="Per bar from O, H, L, C; gap uses the previous bar's close.",
    edge_cases="Zero-range bars give NaN ratios. gap is NaN on the first bar and spans "
               "session breaks and missing bars (it is 'vs previous available bar').",
    warmup="0 bars (gap: 1)"))


# --------------------------------------------------------------------------- ATR
def _true_range(b):
    prev_c = shift(b.close, 1)
    tr = np.maximum(b.high - b.low, np.maximum(np.abs(b.high - prev_c), np.abs(b.low - prev_c)))
    if len(tr):
        tr[0] = b.high[0] - b.low[0]
    return tr


def _atr(inp, p):
    n = p["period"]
    tr = _true_range(inp.bars)
    seed = float(tr[:n].mean()) if len(tr) >= n else NAN
    atr = recursive_smooth(tr, 1.0 / n, n - 1, seed)
    return {"atr": atr, "tr": tr, "atr_pct": safe_div(atr, inp.bars.close)}


register(FeatureDef(
    feature_id="atr", version=1, category="volatility",
    params=(Param("period", 14, int, "smoothing length n", min=1),),
    outputs=(("atr", "Wilder ATR in points"), ("tr", "true range of the bar"),
             ("atr_pct", "atr / close")),
    compute=_atr,
    summary="Average True Range, Wilder smoothing.",
    calculation="TR_t = max(H_t - L_t, |H_t - C_{t-1}|, |L_t - C_{t-1}|), TR_0 = H_0 - L_0. "
                "ATR_{n-1} = mean(TR_0..TR_{n-1}); ATR_t = ATR_{t-1} + (TR_t - ATR_{t-1}) / n.",
    edge_cases="Session gaps and missing bars enter TR through C_{t-1} (the previous AVAILABLE bar). "
               "NaN before bar n-1.",
    warmup="n - 1 bars"))


# --------------------------------------------------------------------------- EMA / SMA
def _ema(inp, p):
    n, k = p["period"], p["slope_bars"]
    c = inp.bars.close
    seed = float(c[:n].mean()) if len(c) >= n else NAN
    ema = recursive_smooth(c, 2.0 / (n + 1), n - 1, seed)
    return {"ema": ema, "slope": (ema - shift(ema, k)) / k, "dist": c - ema}


register(FeatureDef(
    feature_id="ema", version=1, category="trend",
    params=(Param("period", 20, int, "length n; alpha = 2 / (n + 1)", min=1),
            Param("slope_bars", 5, int, "k for slope", min=1)),
    outputs=(("ema", "exponential moving average of close"),
             ("slope", "(EMA_t - EMA_{t-k}) / k, points per bar"),
             ("dist", "C_t - EMA_t")),
    compute=_ema,
    summary="Exponential moving average of close, SMA-seeded.",
    calculation="EMA_{n-1} = mean(C_0..C_{n-1}); EMA_t = EMA_{t-1} + alpha (C_t - EMA_{t-1}), "
                "alpha = 2/(n+1).",
    edge_cases="NaN before bar n-1; slope NaN before n-1+k. Seeding differs from platforms that "
               "seed with the first close (values converge after ~3n bars).",
    warmup="n - 1 bars (slope: n - 1 + k)"))


def _sma(inp, p):
    n = p["period"]
    c = inp.bars.close
    sma = trailing(c, n, np.mean)
    return {"sma": sma, "dist": c - sma}


register(FeatureDef(
    feature_id="sma", version=1, category="trend",
    params=(Param("period", 20, int, "window n", min=1),),
    outputs=(("sma", "mean of the last n closes, including C_t"), ("dist", "C_t - SMA_t")),
    compute=_sma,
    summary="Simple moving average of close.",
    calculation="SMA_t = mean(C_{t-n+1} .. C_t), computed per window (no running-sum drift).",
    edge_cases="NaN before bar n-1. Windows span session breaks and missing bars.",
    warmup="n - 1 bars"))


# --------------------------------------------------------------------------- RSI
def _rsi(inp, p):
    n = p["period"]
    c = inp.bars.close
    delta = c - shift(c, 1)
    gain = np.where(delta > 0, delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)
    gain[0] = loss[0] = NAN
    ok = len(c) > n
    ag = recursive_smooth(gain, 1.0 / n, n, float(gain[1:n + 1].mean()) if ok else NAN)
    al = recursive_smooth(loss, 1.0 / n, n, float(loss[1:n + 1].mean()) if ok else NAN)
    with np.errstate(divide="ignore", invalid="ignore"):
        rsi = 100.0 - 100.0 / (1.0 + ag / al)
    rsi = np.where((al == 0) & (ag > 0), 100.0, rsi)
    rsi = np.where((al == 0) & (ag == 0), 50.0, rsi)
    rsi[~np.isfinite(ag)] = NAN
    return {"rsi": rsi}


register(FeatureDef(
    feature_id="rsi", version=1, category="momentum",
    params=(Param("period", 14, int, "Wilder length n", min=1),),
    outputs=(("rsi", "0..100"),),
    compute=_rsi,
    summary="Relative Strength Index, Wilder smoothing.",
    calculation="d_t = C_t - C_{t-1}; gain = max(d,0), loss = max(-d,0). avg_n = mean over bars "
                "1..n; then avg_t = avg_{t-1} + (x_t - avg_{t-1})/n. RSI = 100 - 100/(1 + avgGain/avgLoss).",
    edge_cases="avgLoss = 0: RSI = 100 if avgGain > 0, else 50 (flat market). NaN before bar n.",
    warmup="n bars"))


# --------------------------------------------------------------------------- ROC
def _roc(inp, p):
    n = p["period"]
    c = inp.bars.close
    past = shift(c, n)
    return {"roc": safe_div(c, past) - 1.0, "momentum": c - past}


register(FeatureDef(
    feature_id="roc", version=1, category="momentum",
    params=(Param("period", 10, int, "lookback n", min=1),),
    outputs=(("roc", "C_t / C_{t-n} - 1 (fraction)"), ("momentum", "C_t - C_{t-n} (points)")),
    compute=_roc,
    summary="Rate of change and raw momentum over n bars.",
    calculation="roc = C_t / C_{t-n} - 1; momentum = C_t - C_{t-n}.",
    edge_cases="n counts AVAILABLE bars (missing bars shorten the real time span). NaN before bar n.",
    warmup="n bars"))
