"""Every setting of the Premarket breakout strategy (ADR-115), in the shape of My strategy's schema.

The user gave the core rules (premarket high / low, a 5-minute close beyond it, stop at that candle's wick, 1 : 8 target)
and left every detail open; the defaults below are the choices made for them and each one is a setting.
"""
from __future__ import annotations

import copy

from edgelab.core.identity import hash_obj
from edgelab.mystrategy.params import SettingsError, _check, minutes  # noqa: F401  (SettingsError re-exported)

PARAMS_VERSION = 1

GROUPS = [
    ("models", "Direction and prices"),
    ("pm", "Premarket range (New York time)"),
    ("entry", "Breakout entry"),
    ("stop", "Stop loss"),
    ("target", "Take profit"),
    ("day", "Daily rules"),
    ("risk", "Position size"),
]


def _p(key, label, typ, default, help, source="", **kw) -> dict:
    return {"key": key, "group": key.split(".")[0], "label": label, "type": typ, "default": default,
            "help": help, "source": source, **kw}


B, I, F, C, T = "bool", "int", "float", "choice", "time"
S_U = "Your rule"
S_D = "Chosen default"

SCHEMA: list[dict] = [
    _p("models.direction", "Allowed directions", C, "both",
       "both = a close above the high goes long, a close below the low goes short.", S_D,
       options=["both", "long_only", "short_only"]),
    _p("models.price_series", "Price series for the rules", C, "bid",
       "Candles the rules read (fills always use BID/ASK). 'mid' = average of BID and ASK.", S_D, options=["bid", "mid"]),
    # ---------------------------------------------------------------- premarket
    _p("pm.start", "Premarket starts", T, "04:00", "US premarket hours: 4:00 to the 9:30 open.", S_D),
    _p("pm.end", "Premarket ends", T, "09:30", "The range uses every 1-minute bar from the start up to (not including) this time.",
       S_D),
    _p("pm.min_minutes", "Fewest premarket minutes with data", I, 60,
       "Days with fewer 1-minute bars in the premarket window are skipped (no reliable high / low).", S_D, min=1, max=600),
    _p("pm.min_range_points", "Smallest premarket range (points, 0 = any)", F, 0.0, "", S_D, min=0, max=2000),
    _p("pm.max_range_points", "Largest premarket range (points, 0 = any)", F, 0.0, "", S_D, min=0, max=5000),
    # ---------------------------------------------------------------- entry
    _p("entry.tf", "Breakout candle", C, "5m", "The candle that must close beyond the premarket high / low.", S_U,
       options=["1m", "3m", "5m", "15m"]),
    _p("entry.from", "Breakouts from", T, "09:30", "First candle that may count (its start time).", S_D),
    _p("entry.until", "Breakouts until", T, "12:00", "No breakout candle starting at or after this time.", S_D),
    _p("entry.buffer_points", "Close beyond the level by (points)", F, 0.0, "0 = any close above the high / below the low.",
       S_D, min=0, max=200),
    _p("entry.first_break_only", "Only the first close beyond each level", B, True,
       "On: once a candle has closed beyond the high (or low), later closes beyond it that day are not new signals.", S_D),
    # ---------------------------------------------------------------- stop
    _p("stop.buffer_points", "Stop beyond the wick by (points)", F, 0.0,
       "The stop sits at the breakout candle's low (longs) / high (shorts), plus this buffer.", S_U, min=0, max=50),
    _p("stop.min_points", "Smallest stop (points, 0 = any)", F, 0.0, "Smaller stops: no trade.", S_D, min=0, max=500),
    _p("stop.max_points", "Largest stop (points, 0 = any)", F, 0.0, "Larger stops: no trade.", S_D, min=0, max=2000),
    # ---------------------------------------------------------------- target
    _p("target.rr", "Take profit (reward : risk)", F, 8.0, "Target = entry + this many times the stop distance.", S_U,
       min=0.25, max=50),
    _p("target.force_exit", "Close open trades at", T, "15:55", "A trade still open is closed at the next minute's open.",
       S_D),
    # ---------------------------------------------------------------- day
    _p("day.max_trades", "Trades per day", I, 1, "1 = the first breakout of the day only.", S_D, min=1, max=6),
    _p("day.skip_monday", "Skip Mondays", B, False, "", S_D),
    _p("day.skip_friday", "Skip Fridays", B, False, "", S_D),
    # ---------------------------------------------------------------- risk
    _p("risk.mode", "Position size", C, "fixed_usd",
       "fixed_usd = a fixed dollar risk; equity_pct = % of the account at the signal.", S_D,
       options=["fixed_usd", "equity_pct"]),
    _p("risk.usd", "Risk per trade ($)", F, 500.0, "", S_D, min=10, max=100000),
    _p("risk.pct", "Risk per trade (%)", F, 1.0, "", S_D, min=0.05, max=10),
    _p("risk.max_contracts", "Most MNQ contracts", I, 40, "", S_D, min=1, max=500),
]

BY_KEY = {p["key"]: p for p in SCHEMA}


def defaults() -> dict:
    return {p["key"]: copy.deepcopy(p["default"]) for p in SCHEMA}


def resolve(overrides: dict | None = None) -> dict:
    s = defaults()
    issues = []
    for k, v in (overrides or {}).items():
        p = BY_KEY.get(k)
        if p is None:
            issues.append(f"{k}: unknown setting")
            continue
        val, err = _check(p, v)
        if err:
            issues.append(f"{p['label']} ({k}): {err}")
        else:
            s[k] = val
    if not issues:
        if minutes(s["pm.end"]) <= minutes(s["pm.start"]):
            issues.append("The premarket must end after it starts")
        if minutes(s["entry.from"]) < minutes(s["pm.end"]):
            issues.append("Breakouts may only start once the premarket range is complete")
        if minutes(s["entry.until"]) <= minutes(s["entry.from"]):
            issues.append("Breakouts until must be after Breakouts from")
        if minutes(s["target.force_exit"]) <= minutes(s["entry.from"]):
            issues.append("Close open trades at must be after Breakouts from")
        if s["pm.max_range_points"] and s["pm.max_range_points"] < s["pm.min_range_points"]:
            issues.append("Largest premarket range must be at least the smallest")
        if s["stop.max_points"] and s["stop.max_points"] < s["stop.min_points"]:
            issues.append("Largest stop must be at least the smallest stop")
    if issues:
        raise SettingsError(issues)
    return s


def changed(settings: dict) -> dict:
    d = defaults()
    return {k: v for k, v in settings.items() if k in d and d[k] != v}


def smt_used(s: dict) -> bool:          # interface of My strategy's params; this strategy reads no ES
    return False


def identity(s: dict) -> dict:
    return dict(s)


def settings_hash(settings: dict) -> str:
    return hash_obj({"pm_breakout_params": PARAMS_VERSION, "settings": identity(resolve(settings))}, 16)


def schema_payload() -> dict:
    return {"version": PARAMS_VERSION, "groups": [{"id": g, "label": lbl} for g, lbl in GROUPS], "settings": SCHEMA}
