"""Additional classic indicators for strategy pool 2 (ADR-86).

Supertrend, Parabolic SAR, Ichimoku, CCI, RSI divergence, prior-week levels, Hull MA, KAMA and Heikin-Ashi.
Every output at bar t uses bars <= t only (prefix-invariant; covered by the registry-wide truncation test in
tests/test_mtf_causality.py and by tests/test_features_pool2.py with non-default parameters). Path-dependent
indicators (Supertrend, SAR, KAMA, Heikin-Ashi) start at the first bar of the data they are given, exactly like the
EMAs: the same bars always give the same values.
"""
from __future__ import annotations

import math

import numpy as np
import pandas as pd

from edgelab.features.library._util import NAN, last_completed, prior, recursive_smooth, take_or_nan, trailing
from edgelab.features.library.price import _rsi, _true_range
from edgelab.features.spec import FeatureDef, Param, register


def _wilder_atr(b, n: int) -> np.ndarray:
    """Wilder ATR exactly as the ``atr`` feature computes it."""
    tr = _true_range(b)
    seed = float(tr[:n].mean()) if len(tr) >= n else NAN
    return recursive_smooth(tr, 1.0 / n, n - 1, seed)


def _flips(direction: np.ndarray) -> np.ndarray:
    """+1 on the bar the direction turns up, -1 on the bar it turns down, 0 otherwise (NaN while undefined)."""
    out = np.where(np.isfinite(direction), 0.0, NAN)
    prev = np.r_[NAN, direction[:-1]]
    ch = np.isfinite(direction) & np.isfinite(prev) & (direction != prev)
    out[ch] = direction[ch]
    return out


# --------------------------------------------------------------------------- Supertrend
def _supertrend(inp, p):
    n, m = p["period"], p["multiplier"]
    b = inp.bars
    size = len(b.close)
    atr = _wilder_atr(b, n)
    hl2 = (b.high + b.low) / 2.0
    up_basic, dn_basic = hl2 - m * atr, hl2 + m * atr
    line, dirn = np.full(size, NAN), np.full(size, NAN)
    c = b.close.tolist()
    ub, db = up_basic.tolist(), dn_basic.tolist()
    fu = fd = d = None                                   # final lower band, final upper band, direction
    for t in range(size):
        if not (math.isfinite(ub[t]) and math.isfinite(db[t])):
            continue
        if fu is None:
            fu, fd, d = ub[t], db[t], 1.0
        else:
            fu = max(ub[t], fu) if c[t - 1] > fu else ub[t]          # lower band only rises while price holds above it
            fd = min(db[t], fd) if c[t - 1] < fd else db[t]          # upper band only falls while price holds below it
            if d > 0 and c[t] < fu:
                d = -1.0
            elif d < 0 and c[t] > fd:
                d = 1.0
        dirn[t] = d
        line[t] = fu if d > 0 else fd
    return {"supertrend": line, "direction": dirn, "flip": _flips(dirn)}


register(FeatureDef(
    feature_id="supertrend", version=1, category="trend",
    params=(Param("period", 10, int, "ATR length (Wilder)", min=1),
            Param("multiplier", 3.0, float, "band distance in ATRs", min=0)),
    outputs=(("supertrend", "the active band: lower band in an up direction, upper band in a down direction"),
             ("direction", "+1 up / -1 down after the close of bar t"),
             ("flip", "+1 on the bar the direction turns up, -1 when it turns down, else 0")),
    compute=_supertrend,
    summary="Supertrend: ATR bands around the bar midpoint that ratchet with the trend and flip on a close through.",
    calculation="hl2 = (H+L)/2; basic bands hl2 -/+ multiplier*ATR. Final lower band = max(basic, previous final) "
                "while the previous close stayed above the previous final lower band (else reset to basic); the "
                "upper band mirrored. Direction turns down when the close is below the final lower band and up when "
                "it is above the final upper band. The first defined bar starts in the up direction.",
    edge_cases="NaN before bar period-1 (ATR warm-up). Starts with direction +1 at the first defined bar.",
    warmup="period - 1 bars (plus a few bars until the first flip is meaningful)"))


# --------------------------------------------------------------------------- Parabolic SAR
def _psar(inp, p):
    step, mx = p["step"], p["max_step"]
    b = inp.bars
    size = len(b.close)
    sar_out, dirn = np.full(size, NAN), np.full(size, NAN)
    if size < 2:
        return {"sar": sar_out, "direction": dirn, "flip": _flips(dirn)}
    h, lo = b.high.tolist(), b.low.tolist()
    d = 1.0 if b.close[1] >= b.close[0] else -1.0
    ep = max(h[0], h[1]) if d > 0 else min(lo[0], lo[1])
    sar = min(lo[0], lo[1]) if d > 0 else max(h[0], h[1])        # the stop that applies to bar 2
    af = step
    dirn[1] = d
    sar_out[1] = sar
    for t in range(2, size):
        if d > 0 and lo[t] <= sar:                              # stopped: reverse to down
            d, sar, ep, af = -1.0, ep, lo[t], step
        elif d < 0 and h[t] >= sar:                             # stopped: reverse to up
            d, sar, ep, af = 1.0, ep, h[t], step
        else:
            if d > 0 and h[t] > ep:
                ep, af = h[t], min(af + step, mx)
            elif d < 0 and lo[t] < ep:
                ep, af = lo[t], min(af + step, mx)
        nxt = sar + af * (ep - sar)                             # the stop for bar t+1, known at the close of bar t
        nxt = min(nxt, lo[t], lo[t - 1]) if d > 0 else max(nxt, h[t], h[t - 1])
        sar = nxt
        dirn[t] = d
        sar_out[t] = sar
    return {"sar": sar_out, "direction": dirn, "flip": _flips(dirn)}


register(FeatureDef(
    feature_id="psar", version=1, category="trend",
    params=(Param("step", 0.02, float, "acceleration step", min=0), Param("max_step", 0.2, float, "maximum acceleration", min=0)),
    outputs=(("sar", "the SAR level that applies to the NEXT bar, computed at the close of bar t"),
             ("direction", "+1 long phase / -1 short phase after bar t"),
             ("flip", "+1 on the bar the phase turns long, -1 when it turns short, else 0")),
    compute=_psar,
    summary="Wilder Parabolic SAR (stop-and-reverse).",
    calculation="Initial phase from close_1 vs close_0, SAR = extreme of the first two bars. Each bar: if its range "
                "touches the SAR the phase reverses (SAR = previous extreme point, AF = step); otherwise a new extreme "
                "raises AF by step up to max_step. Next SAR = SAR + AF*(EP - SAR), never inside the last two bars' range.",
    edge_cases="NaN for bar 0. Starts at the first bar of the data given.",
    warmup="2 bars"))


# --------------------------------------------------------------------------- Ichimoku
def _ichimoku(inp, p):
    t9, k26, s52, disp = p["tenkan"], p["kijun"], p["senkou_b"], p["displacement"]
    b = inp.bars
    mid = lambda w: (trailing(b.high, w, np.max) + trailing(b.low, w, np.min)) / 2.0
    tenkan, kijun = mid(t9), mid(k26)
    a_raw, b_raw = (tenkan + kijun) / 2.0, mid(s52)
    shift = lambda x: np.r_[np.full(min(disp, len(x)), NAN), x[:len(x) - disp]] if disp else x.copy()
    ca, cb = shift(a_raw), shift(b_raw)
    return {"tenkan": tenkan, "kijun": kijun, "cloud_a": ca, "cloud_b": cb,
            "cloud_top": np.fmax(ca, cb), "cloud_bottom": np.fmin(ca, cb),
            "span_a_lead": a_raw, "span_b_lead": b_raw}


register(FeatureDef(
    feature_id="ichimoku", version=1, category="trend",
    params=(Param("tenkan", 9, int, "conversion line length", min=1), Param("kijun", 26, int, "base line length", min=1),
            Param("senkou_b", 52, int, "leading span B length", min=1),
            Param("displacement", 26, int, "bars the cloud is plotted ahead", min=0)),
    outputs=(("tenkan", "(highest high + lowest low) / 2 over the last `tenkan` bars incl. t"),
             ("kijun", "the same over the last `kijun` bars"),
             ("cloud_a", "leading span A as it stands UNDER bar t (computed `displacement` bars ago)"),
             ("cloud_b", "leading span B as it stands under bar t"),
             ("cloud_top", "max(cloud_a, cloud_b)"), ("cloud_bottom", "min(cloud_a, cloud_b)"),
             ("span_a_lead", "span A computed now (plotted `displacement` bars ahead)"),
             ("span_b_lead", "span B computed now (plotted `displacement` bars ahead)")),
    compute=_ichimoku,
    summary="Ichimoku Kinko Hyo: conversion/base lines and the cloud at the current bar.",
    calculation="Tenkan/Kijun/Span B = midpoint of the high-low range over their lengths, INCLUDING bar t. "
                "Span A = (Tenkan + Kijun)/2. The cloud under bar t = spans computed `displacement` bars earlier "
                "(the cloud plotted ahead is only ever read at the bar it is plotted under). The lagging span is "
                "the close compared with the close `kijun` bars ago (use a lagged close; no extra output).",
    edge_cases="NaN until each window is full; the cloud needs senkou_b + displacement - 1 bars.",
    warmup="senkou_b + displacement - 1 bars"))


# --------------------------------------------------------------------------- CCI
def _cci(inp, p):
    n = p["period"]
    b = inp.bars
    tp = (b.high + b.low + b.close) / 3.0
    out = np.full(len(tp), NAN)
    if len(tp) >= n:
        win = np.lib.stride_tricks.sliding_window_view(tp, n)
        sma = win.mean(axis=1)
        md = np.abs(win - sma[:, None]).mean(axis=1)
        with np.errstate(divide="ignore", invalid="ignore"):
            v = (tp[n - 1:] - sma) / (0.015 * md)
        v[md == 0] = 0.0
        out[n - 1:] = v
    return {"cci": out}


register(FeatureDef(
    feature_id="cci", version=1, category="momentum",
    params=(Param("period", 20, int, "window n", min=1),),
    outputs=(("cci", "Commodity Channel Index (typically -300..300)"),),
    compute=_cci,
    summary="Lambert's Commodity Channel Index.",
    calculation="TP = (H+L+C)/3; CCI = (TP - SMA_n(TP)) / (0.015 * mean absolute deviation of the last n TP "
                "from their SMA), window incl. bar t.",
    edge_cases="NaN before bar n-1; 0 when the deviation is 0 (flat window).",
    warmup="n - 1 bars"))


# --------------------------------------------------------------------------- RSI divergence
def _divergence(inp, p):
    left, right, gap = p["left"], p["right"], p["max_gap"]
    b = inp.bars
    rsi = _rsi(inp, {"period": p["rsi_period"]})["rsi"]
    size = len(b.close)
    bull, bear = np.zeros(size), np.zeros(size)
    h, lo, r = b.high, b.low, rsi
    piv_lo, piv_hi = np.zeros(size, bool), np.zeros(size, bool)
    if size > left + right:
        q = np.arange(left, size - right)                # pivot q is CONFIRMED (and used) only at bar q + right
        before_lo = prior(lo, left, np.min)[q]
        before_hi = prior(h, left, np.max)[q]
        after_lo = trailing(lo, right, np.min)[q + right]
        after_hi = trailing(h, right, np.max)[q + right]
        piv_lo[q] = (lo[q] < before_lo) & (lo[q] <= after_lo) & np.isfinite(r[q])
        piv_hi[q] = (h[q] > before_hi) & (h[q] >= after_hi) & np.isfinite(r[q])
    last = None
    for q in np.flatnonzero(piv_lo).tolist():
        if last is not None and q - last <= gap and lo[q] < lo[last] and r[q] > r[last]:
            bull[q + right] = 1.0                        # lower low in price, higher low in RSI
        last = q
    last = None
    for q in np.flatnonzero(piv_hi).tolist():
        if last is not None and q - last <= gap and h[q] > h[last] and r[q] < r[last]:
            bear[q + right] = 1.0                        # higher high in price, lower high in RSI
        last = q
    return {"bull": bull, "bear": bear, "rsi": rsi}


register(FeatureDef(
    feature_id="divergence", version=1, category="momentum",
    params=(Param("rsi_period", 14, int, "RSI length", min=1), Param("left", 3, int, "bars left of a pivot", min=1),
            Param("right", 2, int, "bars right of a pivot (confirmation delay)", min=1),
            Param("max_gap", 50, int, "most bars between the two pivots", min=1)),
    outputs=(("bull", "1 on the bar a regular bullish divergence is CONFIRMED (price lower low, RSI higher low)"),
             ("bear", "1 on the bar a regular bearish divergence is confirmed (price higher high, RSI lower high)"),
             ("rsi", "the RSI used")),
    compute=_divergence,
    summary="Regular RSI divergence between the last two confirmed price pivots.",
    calculation="A pivot low at q: L_q below the `left` previous lows and <= the `right` following lows; it is known "
                "only at bar q + right. When a new pivot low is confirmed and the previous pivot low is at most "
                "max_gap bars earlier, a lower price low with a higher RSI is a bullish divergence (event at the "
                "confirmation bar). Pivot highs mirrored for bearish divergence.",
    edge_cases="Only confirmed pivots are used; the event is late by `right` bars by design (no lookahead).",
    warmup="rsi_period + left + right bars"))


# --------------------------------------------------------------------------- prior-week levels
def _weekly(inp, p):
    b, cal = inp.bars, inp.calendar
    n = len(b.close)
    keys = ("week_open", "week_high", "week_low", "prev_week_open", "prev_week_high", "prev_week_low", "prev_week_close")
    out = {k: np.full(n, NAN) for k in keys}
    if n == 0:
        return out
    td = cal.trading_dates(b.ts).astype("datetime64[D]").astype(np.int64)
    wk = td - ((td + 3) % 7)                             # Monday (days since epoch) of the trading date's week
    last_wd = max(cal.trading_weekdays)                  # the week ends at the close of its last scheduled weekday
    df = pd.DataFrame({"i": wk, "o": b.open, "h": b.high, "l": b.low, "c": b.close})
    g = df.groupby("i", sort=False)
    out["week_open"] = g["o"].transform("first").to_numpy(float)
    out["week_high"] = g["h"].cummax().to_numpy(float)
    out["week_low"] = g["l"].cummin().to_numpy(float)
    agg = g.agg(o=("o", "first"), h=("h", "max"), l=("l", "min"), c=("c", "last")).sort_index()
    ends = np.array([cal.session_bounds(pd.Timestamp(np.datetime64(int(m) + last_wd, "D")).date())[1]
                     .tz_convert("UTC").as_unit("ns").value for m in agg.index], dtype=np.int64)
    j = last_completed(ends, inp.ts_close_ns)
    for src, dst in (("o", "prev_week_open"), ("h", "prev_week_high"), ("l", "prev_week_low"), ("c", "prev_week_close")):
        out[dst] = take_or_nan(agg[src].to_numpy(float), j)
    return out


register(FeatureDef(
    feature_id="weekly_levels", version=1, category="session",
    params=(),
    outputs=(("week_open", "open of the current trading week"), ("week_high", "running high of the week incl. bar t"),
             ("week_low", "running low of the week incl. bar t"),
             ("prev_week_open", "previous completed trading week: open"),
             ("prev_week_high", "previous completed trading week: high"),
             ("prev_week_low", "previous completed trading week: low"),
             ("prev_week_close", "previous completed trading week: close")),
    compute=_weekly,
    summary="Trading-week levels (weeks of calendar trading dates, Monday-based).",
    calculation="Week = the Monday-based calendar week of each bar's TRADING DATE (dataset calendar). A week is "
                "complete at the scheduled session close of its last scheduled trading weekday; previous = latest "
                "completed week at the bar's close.",
    edge_cases="A holiday on the last weekday does not end the week early: its levels appear only after that "
               "weekday's scheduled close (conservative, never early).",
    warmup="one completed week for prev_*"))


# --------------------------------------------------------------------------- Hull MA
def _wma(x: np.ndarray, n: int) -> np.ndarray:
    out = np.full(len(x), NAN)
    if n >= 1 and len(x) >= n:
        w = np.arange(1, n + 1, dtype=float)
        win = np.lib.stride_tricks.sliding_window_view(x, n)
        out[n - 1:] = (win * w).sum(axis=1) / w.sum()
    return out


def _hma(inp, p):
    n = p["period"]
    c = inp.bars.close
    half, root = max(1, n // 2), max(1, int(math.isqrt(n)))
    raw = 2.0 * _wma(c, half) - _wma(c, n)
    first = n - 1
    hma = np.full(len(c), NAN)
    if len(c) > first:
        hma[first:] = _wma(raw[first:], root)
    return {"hma": hma, "slope": hma - np.r_[NAN, hma[:-1]]}


register(FeatureDef(
    feature_id="hma", version=1, category="trend",
    params=(Param("period", 20, int, "length n", min=2),),
    outputs=(("hma", "Hull moving average of close"), ("slope", "hma_t - hma_{t-1}")),
    compute=_hma,
    summary="Hull moving average (low-lag weighted average).",
    calculation="HMA = WMA_{floor(sqrt n)}( 2*WMA_{floor(n/2)}(C) - WMA_n(C) ); WMA weights 1..k (newest heaviest).",
    edge_cases="NaN before bar n + floor(sqrt n) - 2.",
    warmup="n + floor(sqrt(n)) - 2 bars"))


# --------------------------------------------------------------------------- KAMA
def _kama(inp, p):
    n, f, s = p["period"], p["fast"], p["slow"]
    c = inp.bars.close
    size = len(c)
    kama, er = np.full(size, NAN), np.full(size, NAN)
    if size <= n:
        return {"kama": kama, "slope": kama.copy(), "er": er}
    change = np.abs(c[n:] - c[:-n])
    vol = trailing(np.abs(np.r_[NAN, np.diff(c)]), n, np.sum)[n:]
    er[n:] = np.where(vol > 0, change / np.where(vol > 0, vol, 1.0), 0.0)
    fsc, ssc = 2.0 / (f + 1), 2.0 / (s + 1)
    sc = ((er * (fsc - ssc) + ssc) ** 2).tolist()
    cl = c.tolist()
    k = float(c[n - 1])
    kama[n - 1] = k
    for t in range(n, size):
        k = k + sc[t] * (cl[t] - k)
        kama[t] = k
    return {"kama": kama, "slope": kama - np.r_[NAN, kama[:-1]], "er": er}


register(FeatureDef(
    feature_id="kama", version=1, category="trend",
    params=(Param("period", 10, int, "efficiency-ratio length n", min=1), Param("fast", 2, int, "fast length", min=1),
            Param("slow", 30, int, "slow length", min=1)),
    outputs=(("kama", "Kaufman adaptive moving average of close"), ("slope", "kama_t - kama_{t-1}"),
             ("er", "efficiency ratio 0..1 (1 = straight-line move)")),
    compute=_kama,
    summary="Kaufman adaptive moving average and efficiency ratio.",
    calculation="ER = |C_t - C_{t-n}| / sum of |C_i - C_{i-1}| over the last n bars; SC = (ER*(2/(fast+1) - "
                "2/(slow+1)) + 2/(slow+1))^2; KAMA seeded with C_{n-1}, then KAMA_t = KAMA_{t-1} + SC*(C_t - KAMA_{t-1}).",
    edge_cases="NaN before bar n-1 (KAMA) / n (ER, slope). ER = 0 for a flat window.",
    warmup="n bars"))


# --------------------------------------------------------------------------- Heikin-Ashi
def _heikin_ashi(inp, p):
    b = inp.bars
    size = len(b.close)
    hc = (b.open + b.high + b.low + b.close) / 4.0
    ho = np.full(size, NAN)
    if size:
        ho[0] = (b.open[0] + b.close[0]) / 2.0
        if size > 1:
            ho[1:] = _ha_open_tail(float(ho[0]), hc)
    hh = np.fmax(b.high, np.fmax(ho, hc))
    hl = np.fmin(b.low, np.fmin(ho, hc))
    dirn = np.sign(hc - ho)
    streak = np.zeros(size)
    run = 0.0
    for t, d in enumerate(dirn.tolist()):
        if not d or not math.isfinite(d):
            run = 0.0
        elif run * d > 0:
            run += d
        else:
            run = d
        streak[t] = run
    return {"ha_open": ho, "ha_close": hc, "ha_high": hh, "ha_low": hl, "direction": dirn, "streak": streak,
            "no_lower_wick": ((dirn > 0) & (hl >= ho)).astype(float),
            "no_upper_wick": ((dirn < 0) & (hh <= ho)).astype(float)}


def _ha_open_tail(ho0: float, hc: np.ndarray) -> np.ndarray:
    out = np.empty(len(hc) - 1)
    prev = ho0
    for t, x in enumerate(hc[:-1].tolist()):
        prev = (prev + x) / 2.0
        out[t] = prev
    return out


register(FeatureDef(
    feature_id="heikin_ashi", version=1, category="trend",
    params=(),
    outputs=(("ha_open", "Heikin-Ashi open"), ("ha_close", "Heikin-Ashi close = (O+H+L+C)/4"),
             ("ha_high", "max(H, ha_open, ha_close)"), ("ha_low", "min(L, ha_open, ha_close)"),
             ("direction", "+1 bullish / -1 bearish / 0 HA candle"),
             ("streak", "signed count of consecutive same-direction HA candles (0 on a doji)"),
             ("no_lower_wick", "1 on a bullish HA candle without a lower wick"),
             ("no_upper_wick", "1 on a bearish HA candle without an upper wick")),
    compute=_heikin_ashi,
    summary="Heikin-Ashi candles, their direction and run length.",
    calculation="ha_close = (O+H+L+C)/4; ha_open_0 = (O_0+C_0)/2, ha_open_t = (ha_open_{t-1} + ha_close_{t-1})/2; "
                "ha_high/low = extremes of H/L and the HA open/close.",
    edge_cases="Starts at the first bar of the data given (recursive like an EMA).",
    warmup="0 bars (a few bars until the recursion forgets the seed)"))
