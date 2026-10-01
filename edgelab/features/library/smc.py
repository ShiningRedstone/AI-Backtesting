"""Order blocks, breaker blocks and daily narrow-range (ADR-61: completes the agreed 30-family catalog).

Both features are fully mechanical (no discretionary judgement) and causal: every output at bar t is
a function of bars <= t. Zone outputs at bar t describe the state known at the CLOSE OF BAR t-1, so the
reaction bar t itself can test the zone without the zone depending on bar t.
"""
from __future__ import annotations

import numpy as np

from edgelab.features.library._util import NAN, safe_div
from edgelab.features.library.session_levels import _daily
from edgelab.features.spec import FeatureDef, FeatureSpec, Param, register

ZONE_KINDS = ("range", "body")


def _first_true(mask: np.ndarray) -> int:
    """Index of the first True in ``mask`` or -1."""
    return int(np.argmax(mask)) if mask.size and mask.any() else -1


def _events(flag: np.ndarray, cand: np.ndarray, lookback: int) -> tuple[np.ndarray, np.ndarray]:
    """For every event bar j (flag), the last bar i in [j-lookback, j-1] with cand[i]. -> (j, i) arrays."""
    n = len(flag)
    last = np.maximum.accumulate(np.where(cand, np.arange(n), -1))
    j = np.flatnonzero(flag)
    j = j[j >= 1]
    i = last[j - 1]
    ok = (i >= 0) & (i >= j - lookback)
    return j[ok], i[ok]


def _fill(n: int, zones: list, prefix: dict, out: dict) -> None:
    """Write zone outputs for bars t in [start, end]; the most recently FORMED zone wins, and when it ends
    the next most recent still-valid zone becomes current. ``zones`` = (formed_at, start, end, top, bottom)."""
    filled = np.zeros(n, bool)
    for formed, start, end, top, bottom in reversed(zones):
        if start > end or start >= n:
            continue
        sl = slice(start, min(end, n - 1) + 1)
        free = ~filled[sl]
        if not free.any():
            continue
        idx = np.arange(sl.start, sl.stop)[free]
        out[prefix["top"]][idx] = top
        out[prefix["bottom"]][idx] = bottom
        out[prefix["age"]][idx] = idx - formed
        filled[idx] = True


def _order_block(inp, p):
    b = inp.bars
    n = len(b)
    o, h, l, c = (np.asarray(x, float) for x in (b.open, b.high, b.low, b.close))
    atr = inp.dep(FeatureSpec.make("atr", {"period": p["atr_period"]}))["atr"]
    sw = inp.dep(FeatureSpec.make("swings", {"left": p["left"], "right": p["right"]}))
    with np.errstate(invalid="ignore"):
        body_atr = safe_div(np.abs(c - o), atr)
        disp = body_atr >= p["disp"]
        up = disp & (c > o)
        dn = disp & (c < o)
        if p["require_bos"]:
            up &= sw["bos_up"] == 1.0
            dn &= sw["bos_down"] == 1.0
    age_cap = p["max_age_bars"]
    body_zone = p["zone"] == "body"
    names = [f"{k}_{s}" for k in ("ob", "brk") for s in ("bull", "bear")]
    out = {}
    for nm in names:
        for part in ("top", "bottom", "age"):
            out[f"{nm}_{part}"] = np.full(n, NAN)
        out[f"{nm}_new"] = np.zeros(n)
    zones = {nm: [] for nm in names}

    for side, flag in (("bull", up), ("bear", dn)):
        bull = side == "bull"
        opposite = (c < o) if bull else (c > o)            # the last opposite candle before the displacement
        js, iis = _events(flag, opposite, p["lookback"])
        for j, i in zip(js.tolist(), iis.tolist()):
            if body_zone:
                bot, top = (c[i], o[i]) if bull else (o[i], c[i])
            else:
                bot, top = l[i], h[i]
            if not (top > bot):
                continue
            stop_at = min(n - 1, j + age_cap)
            seg = slice(j + 1, stop_at + 1)
            # invalidation: a CLOSE through the far edge (bull: <= bottom; bear: >= top)
            brk = _first_true((c[seg] <= bot) if bull else (c[seg] >= top))
            s_brk = j + 1 + brk if brk >= 0 else None
            touch = _first_true((l[seg] <= top) if bull else (h[seg] >= bot))
            s_touch = j + 1 + touch if touch >= 0 else None
            end = stop_at
            if s_brk is not None:
                end = min(end, s_brk)
            if p["first_touch_only"] and s_touch is not None:
                end = min(end, s_touch)
            out[f"ob_{side}_new"][j] = 1.0
            zones[f"ob_{side}"].append((j, j + 1, end, top, bot))
            # breaker: the failed block flips polarity (bull OB broken down -> bearish breaker) from the break bar
            if s_brk is not None:
                fs = "bear" if bull else "bull"
                out[f"brk_{fs}_new"][s_brk] = 1.0
                seg2 = slice(s_brk + 1, min(n - 1, s_brk + age_cap) + 1)
                # invalidated by a CLOSE back through the block the other way; consumed on first touch if asked
                inv = _first_true((c[seg2] >= top) if bull else (c[seg2] <= bot))
                tch = _first_true((h[seg2] >= bot) if bull else (l[seg2] <= top))
                end2 = min(n - 1, s_brk + age_cap)
                if inv >= 0:
                    end2 = min(end2, s_brk + 1 + inv)
                if p["first_touch_only"] and tch >= 0:
                    end2 = min(end2, s_brk + 1 + tch)
                zones[f"brk_{fs}"].append((s_brk, s_brk + 1, end2, top, bot))
    for nm in names:
        zs = sorted(zones[nm], key=lambda z: z[0])
        _fill(n, zs, {"top": f"{nm}_top", "bottom": f"{nm}_bottom", "age": f"{nm}_age"}, out)
    return out


_OB_OUT = []
for _k, _d in (("ob", "order block"), ("brk", "breaker block")):
    for _s in ("bull", "bear"):
        _OB_OUT += [(f"{_k}_{_s}_top", f"top of the most recent valid {_s} {_d} zone as of the close of bar t-1"),
                    (f"{_k}_{_s}_bottom", f"bottom of that {_s} {_d} zone"),
                    (f"{_k}_{_s}_age", f"bars since that {_s} {_d} formed"),
                    (f"{_k}_{_s}_new", f"1 on the bar a {_s} {_d} forms (known at its close)")]

register(FeatureDef(
    feature_id="order_block", version=1, category="structure",
    params=(Param("left", 3, int, "swing pivot bars on the left (structure break)", min=1),
            Param("right", 1, int, "swing pivot bars on the right = confirmation delay", min=0),
            Param("disp", 1.5, float, "displacement: candle body >= disp x ATR", min=0.0),
            Param("require_bos", True, bool, "the displacement must also close through the prior confirmed swing"),
            Param("lookback", 10, int, "max bars back to find the last opposite candle", min=1),
            Param("zone", "range", str, "zone = candle range (high/low) or body (open/close)", choices=ZONE_KINDS),
            Param("first_touch_only", True, bool, "a zone is consumed by the first bar that touches it"),
            Param("max_age_bars", 100, int, "a zone expires after this many bars", min=1),
            Param("atr_period", 14, int, "ATR length for the displacement test", min=1)),
    outputs=tuple(_OB_OUT), compute=_order_block,
    depends=lambda p: [FeatureSpec.make("atr", {"period": p["atr_period"]}),
                       FeatureSpec.make("swings", {"left": p["left"], "right": p["right"]})],
    summary="Order blocks (last opposite candle before a displacement that breaks structure) and breaker blocks "
            "(failed order blocks that flip polarity).",
    calculation="Bull displacement at j: bullish candle with |C-O| >= disp x ATR_j and (optionally) bos_up_j = 1. The "
                "order block is the last BEARISH candle i in [j-lookback, j-1]; zone = its range or body. The zone "
                "is valid for bars t in [j+1, end], end = the first bar that closes <= bottom (invalidation), the first "
                "bar touching it (low <= top) when first_touch_only, or j + max_age_bars. Bear mirrored. A bull block "
                "invalidated by a close <= bottom at s becomes a BEARISH breaker valid from s+1 until a close >= top, "
                "the first touch (high >= bottom) when first_touch_only, or s + max_age_bars; a failed bear block "
                "becomes a bull breaker. The most recently formed valid zone is reported.",
    edge_cases="Outputs at bar t use zone state as of the close of bar t-1 (formed at <= t-1, invalidated by bars "
               "<= t-1), so the reaction bar t can test the zone causally. NaN when no valid zone. Zones of "
               "different sessions are not separated: use max_age_bars.",
    warmup="atr_period + left + right bars"))


# --------------------------------------------------------------------------- daily narrow range
def _daily_nr(inp, p):
    n = p["n"]
    b = inp.bars
    lv = _daily(inp, {})
    td = inp.calendar.trading_dates(b.ts).astype(np.int64)
    size = len(b)
    rng = lv["prev_day_high"] - lv["prev_day_low"]
    _, first = np.unique(td, return_index=True)
    first = np.sort(first)
    day_rng = rng[first]                       # range of the trading date BEFORE each trading date
    m = len(day_rng)
    is_nr = np.full(m, NAN)
    for q in range(n - 1, m):
        w = day_rng[q - n + 1:q + 1]
        if np.isfinite(w).all():
            is_nr[q] = float(w[-1] < w[:-1].min())
    day_of_bar = np.searchsorted(first, np.arange(size), side="right") - 1
    return {"prev_is_nr": is_nr[day_of_bar], "prev_range": rng}


register(FeatureDef(
    feature_id="daily_nr", version=1, category="structure",
    params=(Param("n", 7, int, "NR-n over completed trading dates", min=2),),
    outputs=(("prev_is_nr", "1 if the last COMPLETED trading date's range is strictly below the ranges of the "
                            "n-1 completed dates before it, else 0"),
             ("prev_range", "high - low of the last completed trading date")),
    compute=_daily_nr,
    summary="Session-level NR7: was the previous trading date the narrowest of the last n?",
    calculation="Per trading date D (exchange calendar), R_D = range of the previous completed date (daily_levels "
                "prev_day_high - prev_day_low, completed = scheduled close <= bar close). prev_is_nr_D = "
                "R_D < min(R_{D-1} .. R_{D-n+2}); constant across the bars of D.",
    edge_cases="NaN until n completed dates exist. Holidays/early closes follow the calendar (unverified for the "
               "Dukascopy feed). The bar loop is over trading dates only.",
    warmup="n completed trading dates"))
