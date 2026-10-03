"""Every setting of My strategy (ADR-93): one schema, used for validation, the Settings page and the summary.

A setting is ``{key, group, label, type, default, help, source}`` plus ``min``/``max`` (numbers), ``options`` (choices)
or ``unavailable`` (a reason the setting cannot be switched on with the available data). ``resolve(overrides)`` gives
the complete, validated settings dict; ``settings_hash`` its identity (the strategy's logic hash in the protocol's trial
ledger: two settings dicts that resolve to the same values are the same strategy).
"""
from __future__ import annotations

import copy
import re
from typing import Any

from edgelab.core.identity import hash_obj

PARAMS_VERSION = 1
TF_CHOICES = ["1m", "2m", "3m", "4m", "5m", "15m", "30m", "1h", "4h", "1D"]

GROUPS = [
    ("models", "Models and direction"),
    ("session", "Trading times (New York)"),
    ("bias", "Step 1 - Bias"),
    ("draw", "Step 1 - Draw on liquidity"),
    ("eq", "Step 1 - Premium / discount (equilibrium)"),
    ("filters", "Day filters"),
    ("key", "Step 2 - Key levels"),
    ("leg", "Step 3 - Manipulation leg"),
    ("ifg", "Step 3 - Confirmation (inversion gap)"),
    ("entry", "Step 4 - Entry"),
    ("stop", "Step 4 - Stop loss"),
    ("target", "Step 4 - Take profit"),
    ("manage", "Step 4 - Breakeven and trailing"),
    ("day", "Step 4 - Daily rules"),
    ("risk", "Step 4 - Risk"),
]


def _p(key, label, typ, default, help, source="", **kw) -> dict:
    return {"key": key, "group": key.split(".")[0], "label": label, "type": typ, "default": default,
            "help": help, "source": source, **kw}


B, I, F, C, T = "bool", "int", "float", "choice", "time"
S_STRAT = "Updated strategy video"
S_JUDAS = "Judas swing video"
S_RECAP = "Trade recaps"
S_ASIA = "Asia session video"

SCHEMA: list[dict] = [
    # ---------------------------------------------------------------- models
    _p("models.ny_4step", "NY AM four-step model", B, True,
       "Bias -> key level -> highest-timeframe inversion gap in the manipulation leg -> execution, 9:30-11:00.", S_STRAT),
    _p("models.judas", "Judas swing model", B, True,
       "The open's fake move into a 5m+ key level; confirmation on the timeframe with ONE gap in the leg "
       "instead of the highest timeframe.", S_JUDAS),
    _p("models.direction", "Allowed directions", C, "both", "Restrict to longs or shorts.", options=["both", "long_only", "short_only"]),
    _p("models.price_series", "Price series for the rules", C, "bid",
       "Candles the rules read (fills always use BID/ASK). 'mid' = average of BID and ASK.", options=["bid", "mid"]),
    # ---------------------------------------------------------------- session
    _p("session.manip_from", "Manipulation may start at", T, "09:30",
       "Earliest time the manipulation low/high may form (he wants the open to manipulate).", S_STRAT),
    _p("session.entry_from", "First entry time", T, "09:30", "No entry signal before this time.", S_STRAT),
    _p("session.entry_until", "Last entry time", T, "11:00",
       "No entry signal after this time ('9:30 to 11:00, the golden hour').", S_STRAT),
    _p("session.force_exit", "Close open trades at", T, "15:55",
       "A trade still open at this time is closed at the next 1-minute open.", ""),
    _p("session.judas_minutes", "Judas window (minutes after 9:30)", I, 15,
       "A manipulation extreme inside this many minutes after the open is treated as a Judas swing.", S_JUDAS, min=1, max=90),
    _p("session.skip_monday", "Skip Mondays", B, False, "No trades on Mondays.", ""),
    _p("session.skip_friday", "Skip Fridays", B, False, "No trades on Fridays.", ""),
    # ---------------------------------------------------------------- bias
    _p("bias.override", "Bias", C, "auto", "auto = decided by the rules below; long/short = forced every day.",
       options=["auto", "long", "short"]),
    _p("bias.method", "Bias method", C, "fvg_respect",
       "fvg_respect = which gaps are respected vs disrespected; structure = higher highs/lows of the swings; "
       "both = the sum of the two.", S_STRAT, options=["fvg_respect", "structure", "both"]),
    _p("bias.tf_1d", "Use the daily chart", B, True, "", S_STRAT),
    _p("bias.tf_4h", "Use the 4h chart", B, True, "", S_STRAT),
    _p("bias.tf_1h", "Use the 1h chart", B, True, "", S_STRAT),
    _p("bias.tf_15m", "Use the 15m chart", B, False, "He checks the 15m around the open.", S_STRAT),
    _p("bias.weight_1d", "Daily weight", F, 1.0, "", min=0, max=5),
    _p("bias.weight_4h", "4h weight", F, 1.0, "", min=0, max=5),
    _p("bias.weight_1h", "1h weight", F, 1.0, "", min=0, max=5),
    _p("bias.weight_15m", "15m weight", F, 0.5, "", min=0, max=5),
    _p("bias.fvg_lookback", "Gaps looked at (candles back per timeframe)", I, 30,
       "Only gaps created within this many candles of each timeframe count.", min=3, max=500),
    _p("bias.events_per_tf", "Most recent gap reactions counted", I, 3,
       "Per timeframe, only the latest N respected/disrespected gaps count (the current picture).", min=1, max=20),
    _p("bias.respect_needs_touch", "A gap must be touched to count as respected", B, True,
       "Off: an untouched gap in the trend direction also counts as respected.", S_STRAT),
    _p("bias.fvg_min_points", "Minimum gap size for the bias (points)", F, 2.0, "", min=0, max=500),
    _p("bias.structure_strength", "Swing strength for structure (candles each side)", I, 2, "", min=1, max=10),
    _p("bias.min_score", "Score needed for a bias", F, 1.0,
       "Each timeframe scores -1..+1 (times its weight); the sum must reach this for a bullish/bearish bias.", min=0, max=20),
    _p("bias.tie_break", "When the score is too small", C, "none",
       "none = no trade; previous_day = keep yesterday's bias ('until proven wrong'); daily_trend = direction of "
       "the daily close vs 20 days ago.", S_STRAT, options=["none", "previous_day", "daily_trend"]),
    _p("bias.ath_rule", "No shorts near the N-day high", B, False,
       "'I don't love shorting at all-time highs' - block shorts while price is within the distance below of the "
       "highest high of the last N days.", S_RECAP),
    _p("bias.ath_days", "N days for that high", I, 60, "", min=5, max=750),
    _p("bias.ath_pct", "Distance from that high (%)", F, 0.5, "", min=0, max=20),
    # ---------------------------------------------------------------- draw
    _p("draw.required", "A draw on liquidity must exist", B, True,
       "No obvious swing high (bullish) / low (bearish) to go to = no trade.", S_STRAT),
    _p("draw.tf_1d", "Daily swings as draws", B, True, ""),
    _p("draw.tf_4h", "4h swings as draws", B, True, ""),
    _p("draw.tf_1h", "1h swings as draws", B, True, ""),
    _p("draw.tf_15m", "15m swings as draws", B, False, ""),
    _p("draw.swing_strength", "Swing strength (candles each side)", I, 2, "", min=1, max=10),
    _p("draw.prev_day", "Previous day high / low", B, True, "", S_STRAT),
    _p("draw.unfilled_fvg", "Unfilled higher-timeframe gaps (1h / 4h)", B, True, "", S_STRAT),
    _p("draw.nwog", "New week opening gap", B, True, "", S_ASIA),
    _p("draw.min_points", "Minimum distance to the draw (points)", F, 20.0, "", min=0, max=5000),
    _p("draw.max_points", "Maximum distance to the draw (points)", F, 800.0, "", min=1, max=20000),
    # ---------------------------------------------------------------- eq
    _p("eq.enabled", "Longs from discount, shorts from premium", B, True,
       "The manipulation extreme must sit at or below equilibrium of the dealing range (longs; shorts mirrored).",
       S_STRAT),
    _p("eq.range", "Dealing range", C, "swings",
       "swings = last swing high and low of the chosen timeframe; previous_day = previous day high-low; "
       "overnight = 18:00 until the manipulation window.", options=["swings", "previous_day", "overnight"]),
    _p("eq.tf", "Timeframe of the swing range", C, "1h", "", options=["15m", "30m", "1h", "4h"]),
    _p("eq.swing_strength", "Swing strength (candles each side)", I, 2, "", min=1, max=10),
    _p("eq.level", "Equilibrium level (0.5 = middle)", F, 0.5, "", min=0.1, max=0.9),
    _p("eq.tolerance", "Tolerance (fraction of the range)", F, 0.0, "Allows the extreme to sit slightly beyond EQ.",
       min=0, max=0.5),
    # ---------------------------------------------------------------- filters
    _p("filters.chop", "Skip choppy mornings", B, False,
       "Count 5m/15m gaps created before the window; skip the day if too few were created or too many were "
       "immediately closed through ('barcode' price action).", S_ASIA),
    _p("filters.chop_minutes", "Look-back for the chop check (minutes)", I, 240, "", min=30, max=1440),
    _p("filters.chop_min_gaps", "Minimum gaps created", I, 2, "", min=0, max=50),
    _p("filters.chop_max_flip", "Maximum share of gaps closed through", F, 0.6, "", min=0, max=1),
    _p("filters.smt", "Require SMT divergence with ES", B, False,
       "Needs ES data, which is not imported.", S_STRAT, unavailable="needs ES data (not imported)"),
    _p("filters.min_quality", "Minimum confluence score", I, 0,
       "Score = number of extra confluences true (liquidity swept, several key levels, displacement, "
       "discount/premium, target at stacked liquidity, open manipulation).", min=0, max=6),
    # ---------------------------------------------------------------- key levels
    _p("key.tf_3m", "3m key levels", B, False, "", S_STRAT),
    _p("key.tf_5m", "5m key levels", B, True, "", S_STRAT),
    _p("key.tf_15m", "15m key levels", B, True, "", S_STRAT),
    _p("key.tf_30m", "30m key levels", B, True, "", S_STRAT),
    _p("key.tf_1h", "1h key levels", B, True, "", S_STRAT),
    _p("key.tf_4h", "4h key levels", B, True, "", S_STRAT),
    _p("key.judas_min_tf", "Judas: smallest key-level timeframe", C, "5m", "'5-minute time frame and above'.",
       S_JUDAS, options=["3m", "5m", "15m", "30m", "1h"]),
    _p("key.fvg", "Fair value gaps", B, True, "", S_STRAT),
    _p("key.fvg_min_points", "Minimum key-gap size (points)", F, 2.0, "", min=0, max=500),
    _p("key.fvg_max_age", "Maximum key-gap age (candles of its timeframe)", I, 120, "", min=3, max=2000),
    _p("key.fvg_sweep_rule", "Used gap needs its inside low/high swept", B, True,
       "If price already traded into the gap before the manipulation leg, the intermediate low/high inside it must "
       "be taken out first.", S_STRAT),
    _p("key.cisd", "Change in state of delivery (CISD)", B, True, "", S_STRAT),
    _p("key.cisd_needs_fvg", "CISD must come from a gap", B, True,
       "The candles before the CISD must have traded into a gap (or swept a low inside one).", S_STRAT),
    _p("key.cisd_max_candles", "CISD: most candles in the series", I, 6, "", min=1, max=20),
    _p("key.cisd_first_touch", "CISD: first return only", B, True, "", ""),
    _p("key.rejection_block", "Rejection blocks", B, True, "", S_STRAT),
    _p("key.rb_min_tf", "Rejection block: smallest timeframe", C, "15m", "", options=["5m", "15m", "30m", "1h", "4h"]),
    _p("key.rb_wick_ratio", "Rejection block: wick share of the candle", F, 0.4, "", min=0.05, max=0.95),
    _p("key.rb_needs_ce", "Rejection block: price must reach the wick's 50%", B, False, "", S_STRAT),
    _p("key.bpr", "Balanced price ranges (BPR)", B, False, "Overlap of a gap with an earlier inverted gap.", S_RECAP),
    _p("key.invalidate_on_close", "A close through the level kills it", B, True,
       "A candle of the level's timeframe closing beyond its far edge invalidates the level.", S_STRAT),
    _p("key.tolerance_points", "Touch tolerance (points)", F, 0.0, "Count a near miss as a touch.", min=0, max=50),
    _p("key.min_levels", "Key levels that must be hit together", I, 1, "", min=1, max=6),
    # ---------------------------------------------------------------- leg
    _p("leg.start_tf", "Leg starts at the swing high/low of", C, "1m",
       "1m = the nearest swing; 5m / 15m = the larger 'external' swing ('wait for more confirmation by looking at the whole "
       "manipulation leg from the external swing on the 5 minute').", S_STRAT, options=["1m", "5m", "15m"]),
    _p("leg.swing_strength", "Swing strength for the leg start (candles each side)", I, 2, "", min=1, max=10),
    _p("leg.min_points", "Minimum leg size (points)", F, 10.0, "", min=0, max=1000),
    _p("leg.max_minutes", "Longest leg (minutes)", I, 180, "", min=5, max=1440),
    _p("leg.judas_from_open", "Judas: leg starts at the 9:30 open", B, True, "", S_JUDAS),
    _p("leg.judas_open_side", "Judas: extreme beyond the 9:30 open price", B, True,
       "Open-low-high-close for longs (open-high-low-close for shorts).", S_JUDAS),
    _p("leg.require_sweep", "Leg must sweep a prior swing low/high", B, False,
       "Liquidity taken = protected (high-resistance) stop.", S_RECAP),
    _p("leg.no_equal_extremes", "Reject relative equal lows/highs", B, True,
       "'We cannot generate relative equal lows here' - an extreme that merely matches a prior swing without "
       "taking it is not valid.", S_RECAP),
    _p("leg.sweep_tf", "Timeframe of those prior swings", C, "5m", "", options=["1m", "5m", "15m"]),
    _p("leg.equal_points", "Equal = within (points)", F, 3.0, "", min=0, max=50),
    # ---------------------------------------------------------------- ifg
    _p("ifg.tf_1m", "1m inversion gaps", B, True, "", S_STRAT),
    _p("ifg.tf_2m", "2m inversion gaps", B, True, "", S_STRAT),
    _p("ifg.tf_3m", "3m inversion gaps", B, True, "", S_STRAT),
    _p("ifg.tf_4m", "4m inversion gaps", B, True, "", S_STRAT),
    _p("ifg.tf_5m", "5m inversion gaps", B, True, "", S_STRAT),
    _p("ifg.rule_ny", "NY model: which timeframe", C, "highest",
       "highest = highest timeframe with a gap in the leg (his rule); lowest = lowest such timeframe; "
       "single = timeframe with exactly one gap.", S_STRAT, options=["highest", "lowest", "single"]),
    _p("ifg.rule_judas", "Judas model: which timeframe", C, "single", "", S_JUDAS,
       options=["single", "lowest", "highest"]),
    _p("ifg.all_gaps", "Close through every gap of that timeframe", B, True,
       "On: the close must clear the outermost gap of the leg on that timeframe; off: the nearest one.", S_JUDAS),
    _p("ifg.min_gap_points", "Minimum inversion-gap size (points)", F, 0.5, "", min=0, max=200),
    _p("ifg.close_buffer", "Close beyond the gap by (points)", F, 0.0, "", min=0, max=50),
    _p("ifg.max_wait", "Confirmation must come within (minutes after the extreme)", I, 30, "", min=1, max=240),
    _p("ifg.displacement", "Require displacement on the confirming candle", B, True,
       "Body share and size of the candle that closes through the gap.", S_STRAT),
    _p("ifg.body_ratio", "Displacement: minimum body share", F, 0.5, "", min=0, max=1),
    _p("ifg.range_x_avg", "Displacement: candle range vs average of last 20", F, 1.0, "", min=0, max=10),
    # ---------------------------------------------------------------- entry
    _p("entry.type", "Entry order", C, "market",
       "market = next 1m open after the confirming close; limit_gap = limit at the inverted gap's edge.", S_STRAT,
       options=["market", "limit_gap"]),
    _p("entry.limit_minutes", "Limit order stays working (minutes)", I, 15, "", min=1, max=120),
    # ---------------------------------------------------------------- stop
    _p("stop.mode", "Stop loss at", C, "leg_extreme",
       "leg_extreme = swing low/high of the manipulation leg (his default); confirm_candle = the confirming "
       "candle's extreme; confirm_body = its body; gap_edge = the inverted gap's far edge.", S_STRAT,
       options=["leg_extreme", "confirm_candle", "confirm_body", "gap_edge"]),
    _p("stop.buffer", "Stop buffer (points)", F, 1.0, "", min=0, max=50),
    _p("stop.min_points", "Smallest stop (points)", F, 5.0, "Smaller stops are widened to this.", min=0.25, max=500),
    _p("stop.max_points", "Largest stop (points)", F, 80.0, "Larger stops: no trade (he faded a 124-point stop).",
       S_STRAT, min=1, max=2000),
    # ---------------------------------------------------------------- target
    _p("target.mode", "Take profit at", C, "liquidity",
       "liquidity = nearest obvious liquidity ('low-hanging fruit'); fixed_r = a fixed R multiple.", S_STRAT,
       options=["liquidity", "fixed_r"]),
    _p("target.fixed_r", "Fixed target (R)", F, 1.0, "", min=0.25, max=20),
    _p("target.swings_1m", "Target: 1m swings", B, False, ""),
    _p("target.swings_5m", "Target: 5m swings", B, True, ""),
    _p("target.swings_15m", "Target: 15m swings", B, True, ""),
    _p("target.swings_1h", "Target: 1h swings", B, True, ""),
    _p("target.swing_strength", "Target swing strength", I, 2, "", min=1, max=10),
    _p("target.session_levels", "Target: overnight / previous day high-low", B, True, ""),
    _p("target.unfilled_fvg", "Target: unfilled gaps (5m+)", B, True, ""),
    _p("target.draw", "Target: the day's draw", B, True, ""),
    _p("target.min_r", "Minimum target (R)", F, 1.0, "'I aim for 1:1 to 1:3'.", S_STRAT, min=0.25, max=20),
    _p("target.below_min", "When the nearest level is below the minimum", C, "extend",
       "extend = move the target out to the minimum ('move my take profit a little higher for 1:1'); next = use "
       "the next level beyond the minimum; skip = no trade.", S_STRAT, options=["extend", "next", "skip"]),
    _p("target.max_r", "Maximum target (R)", F, 3.0, "", S_STRAT, min=0.5, max=50),
    _p("target.above_max", "When the level is beyond the maximum", C, "cap", "", options=["cap", "skip"]),
    _p("target.no_level", "When no liquidity is found", C, "min_r", "", options=["min_r", "skip"]),
    # ---------------------------------------------------------------- manage
    _p("manage.be", "Breakeven", C, "leg_swing",
       "off; leg_swing = when price reaches the manipulation leg's swing point (his usual); r = after a profit of N R.",
       S_STRAT, options=["off", "leg_swing", "r"]),
    _p("manage.be_r", "Breakeven after (R)", F, 1.0, "", min=0.1, max=10),
    _p("manage.be_offset", "Breakeven offset (points beyond entry)", F, 0.5, "", min=0, max=20),
    _p("manage.be_min_r", "Leg-swing breakeven only if that point is at least (R)", F, 0.5,
       "Too-close swing points are ignored.", min=0, max=5),
    _p("manage.trail", "Trailing stop", C, "off",
       "off; swing_1m / swing_5m = trail behind new confirmed swing lows (longs) / highs (shorts).", S_RECAP,
       options=["off", "swing_1m", "swing_5m"]),
    _p("manage.trail_start_r", "Start trailing after (R)", F, 1.5, "", min=0.1, max=20),
    _p("manage.trail_buffer", "Trailing buffer (points)", F, 1.0, "", min=0, max=50),
    # ---------------------------------------------------------------- day
    _p("day.max_trades", "Trades per day", I, 2, "'Two losses and I'm done for the day'.", S_STRAT, min=1, max=10),
    _p("day.stop_after_win", "Stop after a win", B, True, "'One win, I'm done for the day'.", S_STRAT),
    _p("day.stop_after_loss", "Stop after a loss", B, False, "'One loss I'm probably also done' (probably).", S_STRAT),
    _p("day.cooldown", "Wait after an exit (minutes)", I, 0, "", min=0, max=240),
    # ---------------------------------------------------------------- risk
    _p("risk.mode", "Position size", C, "equity_pct",
       "equity_pct = % of the account at the signal; fixed_usd = a fixed dollar risk.", S_STRAT,
       options=["equity_pct", "fixed_usd"]),
    _p("risk.pct", "Risk per trade (%)", F, 1.0, "'Risking 1% a trade'.", S_ASIA, min=0.05, max=10),
    _p("risk.usd", "Risk per trade ($)", F, 500.0, "", min=10, max=100000),
    _p("risk.max_contracts", "Most MNQ contracts", I, 40, "", min=1, max=500),
]

BY_KEY = {p["key"]: p for p in SCHEMA}
_TIME = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


class SettingsError(ValueError):
    def __init__(self, issues: list[str]):
        super().__init__("; ".join(issues))
        self.issues = issues


def defaults() -> dict:
    return {p["key"]: copy.deepcopy(p["default"]) for p in SCHEMA}


def _check(p: dict, v: Any) -> tuple[Any, str | None]:
    t = p["type"]
    if t == B:
        if isinstance(v, bool):
            return v, None
        return v, "must be true or false"
    if t in (I, F):
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            return v, "must be a number"
        if t == I:
            if float(v) != int(v):
                return v, "must be a whole number"
            v = int(v)
        else:
            v = float(v)
        if "min" in p and v < p["min"]:
            return v, f"must be at least {p['min']}"
        if "max" in p and v > p["max"]:
            return v, f"must be at most {p['max']}"
        return v, None
    if t == C:
        return v, None if v in p["options"] else f"must be one of {', '.join(p['options'])}"
    if t == T:
        return v, None if isinstance(v, str) and _TIME.match(v) else "must be a time HH:MM"
    return v, "unknown type"


def minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def resolve(overrides: dict | None = None) -> dict:
    """Defaults + overrides, every value checked. Unknown keys are refused (a typo must not silently do nothing)."""
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
        elif p.get("unavailable") and val not in (False, p["default"]):
            issues.append(f"{p['label']} ({k}): {p['unavailable']}")
        else:
            s[k] = val
    if not issues:
        if minutes(s["session.entry_until"]) <= minutes(s["session.entry_from"]):
            issues.append("Last entry time must be after the first entry time")
        if minutes(s["session.force_exit"]) <= minutes(s["session.entry_until"]):
            issues.append("Close open trades at must be after the last entry time")
        if minutes(s["session.manip_from"]) > minutes(s["session.entry_until"]):
            issues.append("Manipulation may start at must be before the last entry time")
        if not (s["models.ny_4step"] or s["models.judas"]):
            issues.append("Switch on at least one model")
        if not any(s[f"ifg.tf_{t}"] for t in ("1m", "2m", "3m", "4m", "5m")):
            issues.append("Switch on at least one inversion-gap timeframe")
        if not any(s[f"key.tf_{t}"] for t in ("3m", "5m", "15m", "30m", "1h", "4h")):
            issues.append("Switch on at least one key-level timeframe")
        if not (s["key.fvg"] or s["key.cisd"] or s["key.rejection_block"] or s["key.bpr"]):
            issues.append("Switch on at least one key-level type")
        if s["target.max_r"] < s["target.min_r"]:
            issues.append("Maximum target must be at least the minimum target")
        if s["draw.max_points"] <= s["draw.min_points"]:
            issues.append("Maximum draw distance must be larger than the minimum")
        if s["stop.max_points"] < s["stop.min_points"]:
            issues.append("Largest stop must be at least the smallest stop")
    if issues:
        raise SettingsError(issues)
    return s


def changed(settings: dict) -> dict:
    """Only the values that differ from the defaults (compact display / upload)."""
    d = defaults()
    return {k: v for k, v in settings.items() if d.get(k) != v}


def settings_hash(settings: dict) -> str:
    return hash_obj({"my_strategy_params": PARAMS_VERSION, "settings": resolve(settings)}, 16)


def schema_payload() -> dict:
    return {"version": PARAMS_VERSION, "groups": [{"id": g, "label": lbl} for g, lbl in GROUPS],
            "settings": SCHEMA}
