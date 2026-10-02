"""Human-readable PRESENTATION of factory strategies (ADR-70): display name, short mechanical explanation, key parameters.

Everything here is derived deterministically from a manifest row's frozen ``variation`` (the factory's sampled choice), its
``definition`` and the family catalog (``factory_space.FAMILIES``). Nothing is stored and nothing changes: strategy_id,
logic_hash and the definition are untouched; the canonical machine identity stays available next to the display name.
Wording is mechanical only (what the rules do), never evaluative; it states only what the variation records.
"""
from __future__ import annotations

import re
from typing import Any, Mapping

from edgelab.strategy import factory_space as S
from edgelab.strategy import factory_space_p2 as P2

_FAMILIES = {f.fid: f for f in S.FAMILIES} | {f.fid: f for f in P2.NEW_FAMILIES}     # pool 2 (ADR-86): 25 new families

SESSION_TEXT = {
    "asia": "the Asia session", "london": "the London session", "london_morning": "the London morning",
    "london_ny_overlap": "the London/New York overlap", "ny_1000_1400": "New York 10:00-14:00",
    "ny_afternoon": "the New York afternoon", "ny_first30": "the first 30 minutes of the New York open",
    "ny_last90": "the last 90 minutes of the New York session", "ny_morning": "the New York morning",
    "ny_open": "the New York open", "ny_rth": "New York regular hours",
    "ny_1000_1100": "New York 10:00-11:00", "ny_lunch": "the New York lunch hours", "ny_last30": "the last 30 minutes "
    "of the New York session", "london_close": "the London close (New York 10:00-12:00)",
    "ny_evening": "the New York evening (20:00-23:00)",
}
STOP_TEXT = {
    "points": "a fixed-points stop", "atr": "an ATR-based stop", "swing": "a stop at the last swing",
    "recent_extreme": "a stop beyond the recent extreme", "prev_bar": "a stop beyond the previous bar",
    "ma": "a moving-average stop", "chandelier": "a chandelier (ATR from the extreme) stop", "channel": "a channel stop",
    "day_structure": "a stop at the day's structure level", "range_side": "a stop at the far side of the range",
    "zone": "a stop at the setup's invalidation level",
    "pct": "a percent-of-price stop", "stdev": "a standard-deviation stop", "range_mid": "a stop at the middle of the range",
}
TARGET_TEXT = {
    "none": None, "points": "a fixed-points target", "rr": "a risk-multiple target", "atr": "an ATR-based target",
    "prev_day_level": "the previous day's level as target", "swing": "the opposite swing as target",
    "session_level": "the session level as target", "band_far": "the far band as target", "band_mid": "the middle band as target",
    "ma": "the moving average as target", "range_opposite": "the opposite side of the range as target",
    "prior_close": "the prior New York close as target", "pivot": "the R1/S1 pivot as target",
    "fib_ext": "a Fibonacci extension as target", "midnight_open": "the New York midnight open as target",
    "range_ext": "an opening-range extension as target",
}
TRAIL_TEXT = {
    "none": None, "atr": "an ATR trailing stop", "breakeven": "a move to breakeven", "chandelier": "a chandelier trailing stop",
    "channel": "a channel trailing stop", "fixed_points": "a fixed-points trailing stop", "ma": "a moving-average trailing stop",
    "prev_bar": "a previous-bar trailing stop", "swing": "a swing trailing stop",
}
REGIME_TEXT = {
    "none": None, "atr_high": "a high-ATR regime", "atr_low": "a low-ATR regime", "adx_trend": "ADX showing a trend",
    "adx_range": "ADX showing a range", "bb_compressed": "compressed Bollinger bands",
    "ma_alignment": "aligned moving averages", "above_day_open": "price on the day-open side",
    "overnight_gap": "an overnight gap", "prior_day_direction": "agreement with the prior day's direction",
    "prior_day_nr": "a narrow-range prior day",
    "sma200_trend": "the 200-bar SMA trend side", "above_prior_close": "price on the prior-close side",
    "open_inside_prior_range": "an open inside the prior regular session's range",
    "open_outside_prior_range": "an open outside the prior regular session's range",
    "gap_large": "a large opening gap", "gap_small": "a small opening gap",
    "wide_first_hour": "a wide first hour", "narrow_first_hour": "a narrow first hour",
}
CONFIRM_TEXT = {"none": None, "candle": "a candle confirmation", "close_strength": "a close-strength confirmation",
                "momentum": "a momentum confirmation", "trend": "a trend confirmation"}
SIGNAL_EXIT_TEXT = {"none": None, "opposite_signal": "exits on the opposite signal", "reversal": "exits on the family's reversal signal"}
REENTRY_TEXT = {"none": None, "block_after_stop": "no re-entry after a stop-out that day",
                "block_after_target": "no re-entry after a target that day", "cooldown_after_exit": "a cooldown after each exit"}
MTF_TEXT = {"htf_ema_trend": "the higher-timeframe EMA trend", "htf_ema_slope": "the higher-timeframe EMA slope",
            "htf_rsi": "the higher-timeframe RSI", "htf_structure": "the higher-timeframe structure",
            "htf_vol_regime": "the higher-timeframe volatility regime"}
WEEKDAY_TEXT = {"all": None, "mon": "Mondays only", "tue": "Tuesdays only", "wed": "Wednesdays only", "thu": "Thursdays only",
                "fri": "Fridays only", "mon_thu": "Monday to Thursday", "tue_fri": "Tuesday to Friday", "tue_thu": "Tuesday to Thursday"}
ORDER_TEXT = {"market": "market orders", "stop": "resting stop orders", "limit": "resting limit orders",
              "bar_stop": "stop orders beyond the signal bar", "bar_limit": "limit orders at 50% of the signal bar"}


def humanize(key: Any) -> str:
    """'recent_extreme' -> 'Recent Extreme'; acronyms stay upper-case; never contains an underscore."""
    s = str(key).replace("_", " ").strip()
    out = []
    for w in s.split():
        out.append(w.upper() if w.lower() in ("ema", "sma", "rsi", "atr", "adx", "macd", "nr7", "vwap", "ict", "smc", "bos", "choch",
                                              "fvg", "ma", "htf", "rr", "ny") else w[:1].upper() + w[1:])
    return " ".join(out)


def _tf(tf: str) -> str:
    m = re.fullmatch(r"(\d+)m", str(tf))
    if not m:
        return str(tf)
    k = int(m.group(1))
    return "1-hour" if k == 60 else f"{k}-minute"


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "yes" if v else "no"
    if isinstance(v, float):
        return f"{v:g}"
    if isinstance(v, str):
        return humanize(v)
    return str(v)


def _name_params(fp: Mapping) -> Mapping:
    """Up to four parameters in the name: the setup first (ICT), then the others in key order."""
    items = sorted(fp.items())
    if len(items) <= 4:
        return dict(items)
    first = [(k, v) for k, v in items if k == "setup"]
    rest = [(k, v) for k, v in items if k != "setup"]
    return dict(first + rest[: 4 - len(first)])


def _params_text(fp: Mapping) -> str:
    return " / ".join(f"{humanize(k)} {_fmt(v)}" for k, v in sorted(fp.items()))


def display_name(row: Mapping) -> str:
    """'EMA Crossover — Fast 20 / Slow 100 · 5m · Long'. From the variation; falls back to the family and name."""
    defn = row.get("definition") or {}
    v = row.get("variation")
    fam = _FAMILIES.get(row.get("family_id") or (defn.get("family") or {}).get("id"))
    fam_name = fam.name if fam else humanize((defn.get("family") or {}).get("name") or row.get("family_id") or "Strategy")
    if not isinstance(v, Mapping):
        base = humanize(re.sub(r"_[0-9a-f]{6,}$", "", str(defn.get("name") or row.get("strategy_id") or "")))
        return f"{fam_name} — {base}" if base and base.lower() != fam_name.lower() else fam_name
    parts = [fam_name]
    fp = v.get("family_params") or {}
    if fp:
        parts.append(_params_text(_name_params(fp)))
    tail = [str(v.get("timeframe") or defn.get("timeframe") or ""), humanize(v.get("direction") or "")]
    if v.get("session"):
        tail.append(humanize(v["session"]))
    name = " — ".join(parts) + " · " + " · ".join(t for t in tail if t)
    return name.replace("_", " ")


def key_parameters(row: Mapping) -> dict:
    """The distinguishing choices of a variation, flat and readable (values exactly as recorded)."""
    v = row.get("variation") or {}
    defn = row.get("definition") or {}
    if not isinstance(v, Mapping):
        return {"timeframe": defn.get("timeframe"), "direction": (defn.get("entry") or {}).get("direction")}
    out = {"timeframe": v.get("timeframe"), "direction": v.get("direction"), "session": v.get("session") or "anchored window",
           "weekdays": v.get("weekdays"), "order": (v.get("order") or {}).get("type"), **{k: v_ for k, v_ in sorted((v.get("family_params") or {}).items())}}
    for k in ("stop", "target", "trailing"):
        d = v.get(k) or {}
        if isinstance(d, Mapping) and d.get("type") not in (None, "none"):
            out[k] = ", ".join(f"{kk} {_fmt(vv)}" for kk, vv in sorted(d.items()) if kk != "type" and not isinstance(vv, Mapping)) or d["type"]
            out[k] = f"{d['type']}" + (f" ({out[k]})" if out[k] != d["type"] else "")
    if v.get("mtf"):
        out["higher timeframe filter"] = f"{v['mtf']['filter']} @ {v['mtf']['htf']}"
    for k in ("regime", "confirm", "signal_exit", "reentry"):
        val = v.get(k)
        val = val.get("type") if isinstance(val, Mapping) else val
        if val not in (None, "none"):
            out[k] = val
    out["max hold"] = f"{(defn.get('exit') or {}).get('max_hold_bars')} bars"
    out["sizing"] = (defn.get("sizing") or {}).get("mode")
    return {k: v_ for k, v_ in out.items() if v_ not in (None, "")}


def explanation(row: Mapping) -> str:
    """1-3 mechanical sentences: entry idea, filters, timeframe, exits. No performance language."""
    defn = row.get("definition") or {}
    v = row.get("variation")
    fam = _FAMILIES.get(row.get("family_id") or (defn.get("family") or {}).get("id"))
    hyp = (fam.hypothesis if fam else (defn.get("family") or {}).get("hypothesis") or "").rstrip(".")
    if not isinstance(v, Mapping):
        tf = defn.get("timeframe")
        return (f"{hyp}. Runs on {_tf(tf)} bars." if hyp else f"Runs on {_tf(tf)} bars.")
    direction = {"long": "long only", "short": "short only", "both": "long and short"}.get(v.get("direction"), str(v.get("direction")))
    s1 = f"{hyp}: trades {direction} on {_tf(v.get('timeframe'))} bars"
    fp = v.get("family_params") or {}
    if fp:
        s1 += f" with {_params_text(fp).lower()}"
    s1 += f", using {ORDER_TEXT.get((v.get('order') or {}).get('type'), 'orders')}"
    if v.get("session"):
        s1 += f" during {SESSION_TEXT.get(v['session'], humanize(v['session']).lower())}"
    wd = WEEKDAY_TEXT.get(v.get("weekdays"))
    if wd:
        s1 += f" ({wd})"
    s1 += "."
    filters = []
    if v.get("mtf"):
        filters.append(f"{MTF_TEXT.get(v['mtf']['filter'], humanize(v['mtf']['filter']).lower())} on {v['mtf']['htf']} bars")
    reg = REGIME_TEXT.get(v.get("regime"))
    if reg:
        filters.append(reg)
    conf = CONFIRM_TEXT.get(v.get("confirm"))
    if conf:
        filters.append(conf)
    s2 = ("Entries also require " + (", ".join(filters[:-1]) + " and " + filters[-1] if len(filters) > 1 else filters[0]) + ".") if filters else ""
    exits = []
    st = (v.get("stop") or {}).get("type")
    exits.append(STOP_TEXT.get(st, f"a {humanize(st).lower()} stop") if st else "the configured stop")
    tg = TARGET_TEXT.get((v.get("target") or {}).get("type"))
    if tg:
        exits.append(tg)
    tr = TRAIL_TEXT.get((v.get("trailing") or {}).get("type"))
    if tr:
        exits.append(tr)
    se = SIGNAL_EXIT_TEXT.get(v.get("signal_exit"))
    if se:
        exits.append(se)
    npg = v.get("no_progress") or {}
    if isinstance(npg, Mapping) and npg.get("type") == "yes":
        exits.append(f"a no-progress exit after {npg.get('bars')} bars")
    mh = (defn.get("exit") or {}).get("max_hold_bars")
    exits.append(f"a maximum hold of {mh} bars" if mh else "a maximum hold")
    exits.append("and is flat before the New York trading date ends")
    re_ = REENTRY_TEXT.get((v.get("reentry") or {}).get("type") if isinstance(v.get("reentry"), Mapping) else v.get("reentry"))
    s3 = "Exits use " + ", ".join(exits) + (f"; {re_}." if re_ else ".")
    return " ".join(x for x in (s1, s2, s3) if x)


def present(row: Mapping) -> dict:
    """The presentation block attached to a strategy row (identity untouched and repeated for audit)."""
    return {"display_name": display_name(row), "explanation": explanation(row), "key_parameters": key_parameters(row),
            "strategy_id": row.get("strategy_id"), "logic_hash": row.get("logic_hash"),
            "definition_hash": row.get("definition_hash"), "machine_name": (row.get("definition") or {}).get("name")}


__all__ = ["display_name", "explanation", "key_parameters", "present", "humanize"]
