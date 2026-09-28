"""Fair Value Gap (three-candle imbalance) - explicit definition.

Formation at bar t (known at the close of t), t >= 2:
  bullish:  L_t > H_{t-2}  -> zone [bottom, top] = [H_{t-2}, L_t]
  bearish:  H_t < L_{t-2}  -> zone [bottom, top] = [H_t, L_{t-2}]
  size = top - bottom; kept only if size >= max(min_size_points, min_size_atr * ATR_t).

Fill (evaluated on bars s > t only):
  bullish partial:  L_s < top;  full (gap closed):  L_s <= bottom  (wick rule)
                                                     C_s <= bottom  (close rule)
  bearish mirrored: partial H_s > bottom; full H_s >= top (wick) / C_s >= top (close).
  fill fraction = deepest penetration so far / size, clipped to [0, 1].
A gap is ACTIVE from its formation bar until (exclusive) the bar that fully fills it,
or until ``max_age_bars`` have elapsed (0 = no expiry).

Per-bar outputs describe the MOST RECENTLY FORMED gap that is still active, plus counts.
Every value at bar s depends only on bars <= s.
"""
from __future__ import annotations

import numpy as np

from edgelab.features.library._util import NAN
from edgelab.features.spec import FeatureDef, FeatureSpec, Param, register


def _first_cross(src, starts, levels, le: bool, n: int) -> np.ndarray:
    """First index s > start with src[s] <= level (le) / >= level; n if never."""
    m = len(starts)
    res = np.full(m, n, dtype=np.int64)
    pending = np.arange(m)
    probe = 32
    for d in range(1, probe + 1):                       # vectorized across all gaps
        if not len(pending):
            break
        s = starts[pending] + d
        valid = s < n
        hit = np.zeros(len(pending), bool)
        sv = s[valid]
        lv = levels[pending[valid]]
        hit[valid] = (src[sv] <= lv) if le else (src[sv] >= lv)
        res[pending[hit]] = s[hit]
        pending = pending[~hit & valid]
    for i in pending:                                   # long-lived gaps: chunked search
        k0, size = int(starts[i]) + probe + 1, 256
        lev = levels[i]
        while k0 < n:
            k1 = min(n, k0 + size)
            seg = src[k0:k1]
            mm = (seg <= lev) if le else (seg >= lev)
            if mm.any():
                res[i] = k0 + int(np.argmax(mm))
                break
            k0, size = k1, size * 4
    return res


def _side(b, atr, p, bull: bool) -> dict:
    n = len(b)
    H, L, C = b.high, b.low, b.close
    pre = "bull_" if bull else "bear_"
    out = {pre + k: np.full(n, NAN) for k in ("new_size", "top", "bottom", "size", "size_atr",
                                              "age", "fill", "dist")}
    out[pre + "new"] = np.zeros(n)
    out[pre + "active"] = np.zeros(n)
    if n < 3:
        return out
    t = np.arange(2, n)
    if bull:
        bottom, top = H[t - 2], L[t]
    else:
        bottom, top = H[t], L[t - 2]
    size = top - bottom
    a = atr[t]
    need = np.maximum(p["min_size_points"], p["min_size_atr"] * np.where(np.isfinite(a), a, np.inf)) \
        if p["min_size_atr"] > 0 else np.full(len(t), p["min_size_points"])
    with np.errstate(invalid="ignore"):
        keep = (size > 0) & (size >= need)
    ft, fb, ftop, fsz, fatr = t[keep], bottom[keep], top[keep], size[keep], a[keep]
    m = len(ft)
    if m == 0:
        return out
    # ---- when does each gap stop being active? --------------------------------------
    if bull:
        src = L if p["fill_rule"] == "wick" else C
        fill_idx = _first_cross(src, ft, fb, le=True, n=n)
    else:
        src = H if p["fill_rule"] == "wick" else C
        fill_idx = _first_cross(src, ft, ftop, le=False, n=n)
    end = fill_idx.copy()
    if p["max_age_bars"] > 0:
        end = np.minimum(end, ft + p["max_age_bars"])
    # ---- counts & events ------------------------------------------------------------
    diff = np.zeros(n + 1)
    np.add.at(diff, ft, 1.0)
    np.add.at(diff, end, -1.0)
    out[pre + "active"] = np.cumsum(diff)[:n]
    out[pre + "new"][ft] = 1.0
    out[pre + "new_size"][ft] = fsz
    # ---- most recent active gap per bar: later formations overwrite earlier ones ----
    sel = np.full(n, -1, dtype=np.int64)
    for i in range(m):
        sel[ft[i]:end[i]] = i
    has = sel >= 0
    si = sel[has]
    s_idx = np.flatnonzero(has)
    out[pre + "top"][has] = ftop[si]
    out[pre + "bottom"][has] = fb[si]
    out[pre + "size"][has] = fsz[si]
    out[pre + "size_atr"][has] = fsz[si] / fatr[si] if len(si) else fsz[si]
    out[pre + "age"][has] = s_idx - ft[si]
    out[pre + "dist"][has] = (C[has] - ftop[si]) if bull else (fb[si] - C[has])
    # ---- fill fraction: deepest penetration since formation (runs of constant sel) --
    pen_src = L if bull else H
    fill = out[pre + "fill"]
    change = np.flatnonzero(np.diff(sel)) + 1
    starts = np.r_[0, change]
    stops = np.r_[change, n]
    ext_upto = np.full(m, -1, dtype=np.int64)          # last index folded into ext_val
    ext_val = np.where(np.full(m, bull), np.inf, -np.inf)
    for a_, b_ in zip(starts, stops):
        i = sel[a_]
        if i < 0:
            continue
        f0 = ft[i] + 1                                   # first bar that can penetrate
        lo = max(f0, ext_upto[i] + 1)
        if lo < a_:                                      # fold bars since last visit
            seg = pen_src[lo:a_]
            ext_val[i] = min(ext_val[i], seg.min()) if bull else max(ext_val[i], seg.max())
        vals = pen_src[a_:b_].astype(float)
        if a_ < f0:                                      # formation bar itself cannot fill
            vals = vals.copy()
            vals[: f0 - a_] = np.inf if bull else -np.inf
        run = (np.minimum.accumulate(np.minimum(vals, ext_val[i])) if bull
               else np.maximum.accumulate(np.maximum(vals, ext_val[i])))
        pen = (ftop[i] - run) if bull else (run - fb[i])
        with np.errstate(invalid="ignore"):
            fill[a_:b_] = np.clip(pen / fsz[i], 0.0, 1.0)
        fill[a_:b_][~np.isfinite(pen)] = 0.0
        ext_val[i] = run[-1]
        ext_upto[i] = b_ - 1
    return out


def _fvg(inp, p):
    atr = inp.dep(FeatureSpec.make("atr", {"period": p["atr_period"]}))["atr"]
    out = _side(inp.bars, atr, p, bull=True)
    out.update(_side(inp.bars, atr, p, bull=False))
    return out


_OUT = []
for side, word in (("bull", "bullish"), ("bear", "bearish")):
    _OUT += [(f"{side}_new", f"1 on the bar a {word} gap forms (its third candle)"),
             (f"{side}_new_size", "size of the gap formed on this bar (points)"),
             (f"{side}_active", f"number of active {word} gaps"),
             (f"{side}_top", "top of the most recent active gap"),
             (f"{side}_bottom", "bottom of the most recent active gap"),
             (f"{side}_size", "its size (points)"), (f"{side}_size_atr", "its size / ATR at formation"),
             (f"{side}_age", "bars since it formed"),
             (f"{side}_fill", "deepest penetration so far / size, 0..1 (1 = reached the far edge)"),
             (f"{side}_dist", "bull: C - top; bear: bottom - C (>0: price outside the gap on the "
                              "side it was left; <=0: inside/through)")]

register(FeatureDef(
    feature_id="fvg", version=1, category="structure",
    params=(Param("atr_period", 14, int, "ATR length (size filter, size_atr)", min=1),
            Param("min_size_points", 0.0, float, "minimum gap size in points", min=0),
            Param("min_size_atr", 0.0, float, "minimum gap size as a multiple of ATR_t", min=0),
            Param("fill_rule", "wick", str, "what fully fills a gap", choices=("wick", "close")),
            Param("max_age_bars", 0, int, "deactivate after this many bars (0 = never)", min=0)),
    outputs=tuple(_OUT),
    compute=_fvg,
    depends=lambda p: [FeatureSpec.make("atr", {"period": p["atr_period"]})],
    summary="Three-candle fair value gaps with partial/full fill tracking.",
    calculation=__doc__.split("Formation", 1)[1].split("Per-bar outputs")[0].strip()
    .replace("\n", " "),
    edge_cases="Gaps are detected on consecutive AVAILABLE bars (a missing bar can create or hide a gap; "
               "session-break gaps count). Zero-size gaps are ignored. When the newest gap fills, the "
               "next most recent still-active gap becomes current.",
    warmup="2 bars (atr_period - 1 if a minimum ATR size is set)"))
