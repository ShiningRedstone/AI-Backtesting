"""Volume features. All declare requires=("volume",): the engine refuses them on datasets
without volume and records the dataset's volume_type (exchange vs TICK volume) with
every result, because CFD tick volume is an activity proxy, not traded contracts."""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.features.library._util import NAN, group_cum, prior, rolling_window, safe_div
from edgelab.features.spec import FeatureDef, Param, register


def _volume_stats(inp, p):
    n = p["period"]
    v = inp.bars.volume
    mean_prior = prior(v, n, np.mean)
    std_prior = prior(v, n, lambda w, axis: np.std(w, axis=axis, ddof=1)) if n > 1 else np.full(len(v), NAN)
    pct = np.full(len(v), NAN)
    if len(v) >= n:
        w = rolling_window(v, n)
        pct[n - 1:] = (w <= w[:, -1:]).mean(axis=1)
    return {"rel_volume": safe_div(v, mean_prior),
            "volume_z": safe_div(v - mean_prior, std_prior),
            "volume_pct": pct}


register(FeatureDef(
    feature_id="volume_stats", version=1, category="volume",
    params=(Param("period", 20, int, "lookback n", min=2),),
    outputs=(("rel_volume", "V_t / mean(V_{t-n} .. V_{t-1})  (prior bars only)"),
             ("volume_z", "(V_t - mean_prior) / std_prior (ddof=1)"),
             ("volume_pct", "share of V_{t-n+1}..V_t that are <= V_t (percentile rank incl. current)")),
    compute=_volume_stats, requires=("volume",),
    summary="Relative volume, z-score and percentile rank versus recent bars.",
    calculation="Rolling statistics over the n bars before t (percentile includes t).",
    edge_cases="Zero mean/std -> NaN. Windows span session breaks, where volume regimes differ "
               "(use rvol_tod for intraday-seasonality-aware comparison). Tick volume on CFDs.",
    warmup="n bars"))


def _rvol_tod(inp, p):
    k = p["days"]
    b, cal = inp.bars, inp.calendar
    loc = cal.local(b.ts)
    slot = (np.asarray(loc.hour * 60 + loc.minute) - cal.open_min) % 1440   # minutes since session open
    s = pd.Series(b.volume)
    prev = s.groupby(slot).shift(1)
    base = prev.groupby(slot).rolling(k, min_periods=k).mean().reset_index(level=0, drop=True).sort_index()
    base = base.to_numpy(float)
    return {"rvol_tod": safe_div(b.volume, base), "tod_mean_volume": base}


register(FeatureDef(
    feature_id="rvol_tod", version=1, category="volume",
    params=(Param("days", 20, int, "number of prior occurrences of the same time slot", min=1),),
    outputs=(("rvol_tod", "V_t / mean volume at the same session-relative time over the previous k occurrences"),
             ("tod_mean_volume", "that baseline mean")),
    compute=_rvol_tod, requires=("volume",),
    summary="Time-of-day relative volume (accounts for the intraday volume U-shape).",
    calculation="slot = minutes since the calendar session open (exchange-local, DST-safe). "
                "baseline_t = mean of V at the same slot over its previous k occurrences; rvol = V_t / baseline_t.",
    edge_cases="Occurrences, not calendar days: a missing bar at a slot means the average reaches "
               "further back. NaN until k prior occurrences exist.",
    warmup="k sessions"))


def _vwap(inp, p):
    b = inp.bars
    anchor = p["anchor"]
    if anchor == "trading_day":
        grp = inp.calendar.trading_dates_of(b).astype(np.int64)
        active = np.ones(len(b), bool)
    else:
        active, grp, _ = inp.session(anchor).membership_of(b)
    tp = (b.high + b.low + b.close) / 3.0
    v = np.where(active, b.volume, NAN)
    pv = v * tp
    cum_v = group_cum(v, grp, "cumsum")
    cum_pv = group_cum(pv, grp, "cumsum")
    cum_pv2 = group_cum(v * tp * tp, grp, "cumsum")
    vwap = safe_div(cum_pv, cum_v)
    var = safe_div(cum_pv2, cum_v) - vwap * vwap
    std = np.sqrt(np.clip(var, 0.0, None))
    vwap[~active] = NAN
    std[~active] = NAN
    dist = b.close - vwap
    return {"vwap": vwap, "vwap_std": std, "dist": dist, "dist_std": safe_div(dist, std)}


register(FeatureDef(
    feature_id="vwap", version=1, category="volume",
    params=(Param("anchor", "trading_day", str,
                  "'trading_day' (calendar trading date, e.g. from 18:00 ET) or a session name "
                  "(e.g. NY_RTH: resets at the session start, NaN outside)"),),
    outputs=(("vwap", "sum(TP*V)/sum(V) since anchor, including bar t"),
             ("vwap_std", "volume-weighted std of TP since anchor"),
             ("dist", "C_t - VWAP_t"), ("dist_std", "(C_t - VWAP_t) / vwap_std")),
    compute=_vwap, requires=("volume",), session_params=("anchor",),
    summary="Anchored volume-weighted average price with standard deviation.",
    calculation="TP = (H+L+C)/3; VWAP_t = cumsum(TP*V)/cumsum(V) within the anchor period; "
                "std = sqrt(cumsum(V*TP^2)/cumsum(V) - VWAP^2).",
    edge_cases="Bar-level approximation (TP, not trades). NaN until volume > 0 in the period. "
               "With CFD TICK volume this is a tick-weighted average - recorded in result metadata.",
    warmup="1 bar of the anchor period"))
