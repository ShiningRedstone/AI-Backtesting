"""Every setting of the Fair price strategy (ADR-114): one schema for validation, the Settings page and the summary.

The same shape as My strategy's schema (``mystrategy/params.py``): ``{key, group, label, type, default, help, source}``
plus ``min``/``max`` or ``options``. ``resolve(overrides)`` gives the complete validated settings; ``settings_hash`` is
the strategy's identity in the protocol's trial ledger. Defaults are the values stated in the video; settings whose value
the video leaves open were decided with the user (sessions, Asia open time, news, eval / funded rules, sizes).
"""
from __future__ import annotations

import copy
from typing import Any

from edgelab.core.identity import hash_obj
from edgelab.mystrategy.params import SettingsError, _check, minutes  # noqa: F401  (SettingsError re-exported)

PARAMS_VERSION = 1

GROUPS = [
    ("models", "Direction and prices"),
    ("session", "Sessions (New York time)"),
    ("fair", "Fair price"),
    ("news", "Scheduled 8:30 news"),
    ("cont", "Opening-candle continuation"),
    ("entry", "Reversion entries (1-minute candles)"),
    ("eval", "Evaluation-account rules"),
    ("funded", "Funded-account rules"),
    ("day", "Session limits"),
    ("risk", "Position size"),
]


def _p(key, label, typ, default, help, source="", **kw) -> dict:
    return {"key": key, "group": key.split(".")[0], "label": label, "type": typ, "default": default,
            "help": help, "source": source, **kw}


B, I, F, C, T = "bool", "int", "float", "choice", "time"
S_V = "Video"
S_U = "Your answer"

SCHEMA: list[dict] = [
    # ---------------------------------------------------------------- models
    _p("models.direction", "Allowed directions", C, "both", "Restrict to longs or shorts.",
       options=["both", "long_only", "short_only"]),
    _p("models.price_series", "Price series for the rules", C, "bid",
       "Candles the rules read (fills always use BID/ASK). 'mid' = average of BID and ASK.", options=["bid", "mid"]),
    # ---------------------------------------------------------------- sessions
    _p("session.ny_am", "Trade the New York open", B, True, "First 90 minutes from the New York open.", S_V),
    _p("session.ny_am_open", "New York open", T, "09:30", "Its opening price is the session's fair price.", S_V),
    _p("session.ny_pm", "Trade the New York afternoon", B, True, "The 2 p.m. session.", S_U),
    _p("session.ny_pm_open", "New York afternoon open", T, "14:00", "", S_V),
    _p("session.asia", "Trade the Asia open", B, True, "", S_U),
    _p("session.asia_open", "Asia open", T, "20:00", "Tokyo cash open (your answer).", S_U),
    _p("session.london", "Trade the London open", B, True, "", S_U),
    _p("session.london_open", "London open", T, "03:00", "", S_U),
    _p("session.window_minutes", "Entry window (minutes after each open)", I, 90,
       "He trades the first 90 minutes of every session; no new entries after that.", S_V, min=15, max=240),
    _p("session.exit_after_minutes", "Close open trades this long after the window (minutes)", I, 120,
       "A trade still open this many minutes after its session's entry window ended is closed at the next minute's "
       "open (0 = at the window end).", S_U, min=0, max=480),
    _p("session.force_exit", "Close every open trade at", T, "15:55",
       "No position is held into the daily close (the data's trading day ends 16:15 New York).", "", ),
    _p("session.skip_monday", "Skip Mondays", B, False, "No trades on Mondays.", ""),
    _p("session.skip_friday", "Skip Fridays", B, False, "No trades on Fridays.", ""),
    # ---------------------------------------------------------------- fair price
    _p("fair.pm_target", "Afternoon fair price", C, "ny_am_open",
       "ny_am_open = the 9:30 opening price (his 2 p.m. rule); session_open = the afternoon's own opening price.",
       S_U, options=["ny_am_open", "session_open"]),
    _p("fair.adapt", "Move the fair price to a consolidation", B, False,
       "Optional mechanical version of his discretionary rule. On: (1) when the minutes before an open moved less than "
       "the range below, the middle of that range is fair instead of the opening price; (2) after the losing-streak "
       "stop, the session continues once with the middle of the last minutes' range as the new fair price (only when "
       "that range is narrow enough).", S_U),
    _p("fair.cons_minutes", "Consolidation: minutes looked at", I, 30, "", S_U, min=5, max=240),
    _p("fair.cons_max_points", "Consolidation: largest range (points)", F, 40.0, "", S_U, min=2, max=500),
    # ---------------------------------------------------------------- news
    _p("news.enabled", "Trade reversions to the pre-news price", B, True,
       "On days with a high-impact USD release at the news time (Market simulator news calendar), the price just "
       "before the release is fair; reversions to it are traded from the release until the end time below, through the "
       "New York open. Days the calendar does not cover trade the normal rules.", S_V),
    _p("news.time", "News time", T, "08:30", "Scheduled red-folder releases.", S_V),
    _p("news.until", "News reversions until", T, "11:00", "", S_V),
    _p("news.max_surprise_z", "Largest surprise (0 = no limit)", F, 0.0,
       "Skip the news reversion when a release at that time differs from its forecast by more than this many standard "
       "deviations of its earlier surprises (his idea: only outcomes close to the forecast are already priced in).",
       S_V, min=0, max=20),
    # ---------------------------------------------------------------- continuation
    _p("cont.eval", "Continuation trades in evaluations", B, True,
       "At every session open: a trade in the colour of the opening candle once it breaks structure.", S_V),
    _p("cont.funded", "Continuation trades in funded accounts", B, False, "He mostly keeps these for evaluations.", S_U),
    _p("cont.max_minutes", "Continuation: only in the first minutes", I, 5,
       "The break must happen on one of the first N candles of the session.", S_V, min=1, max=30),
    _p("cont.structure", "Continuation: break of", C, "swing",
       "swing = the most recent 1m swing high / low before the candle (a wick beyond the candles on both sides); "
       "prev3 = the high / low of the last 3 candles; prev1 = the previous candle.", S_U,
       options=["swing", "prev3", "prev1"]),
    _p("cont.bias_hours", "Higher-timeframe bias look-back (hours, 0 = off)", I, 12,
       "The continuation must agree with the reversal of the move over these hours before the open "
       "(price fell -> long bias).", S_U, min=0, max=24),
    _p("cont.target_points", "Continuation take profit (points)", F, 38.0, "", S_V, min=2, max=500),
    _p("cont.stop_points", "Continuation stop loss (points)", F, 25.0, "", S_V, min=2, max=500),
    _p("cont.big_candle_points", "Opening candle size that doubles stop and target (0 = off)", F, 25.0,
       "When the opening candle is bigger than this, stop and target are doubled; the dollar risk stays the same, so "
       "the contracts halve.", S_V, min=0, max=500),
    # ---------------------------------------------------------------- reversion entries
    _p("entry.swing_strength", "Swing: candles each side", I, 1,
       "A swing low is a wick lower than this many candles before and after it.", S_V, min=1, max=5),
    _p("entry.bos_lookback", "Break of structure: swings looked at (minutes back)", I, 60, "", "", min=5, max=600),
    _p("entry.disp_opposite", "Displacement must engulf an opposite-colour candle", B, True,
       "A displacement must close beyond a candle of the other colour.", S_V),
    _p("entry.min_distance_share", "Points to fair price needed (share of the target)", F, 0.8,
       "A trade is only taken when the distance to the fair price is at least this share of its take profit.", S_V,
       min=0.1, max=2.0),
    _p("entry.min_gap_points", "Smallest distance from fair price for any reversion (points)", F, 5.0,
       "Below this distance there is nothing to revert.", "", min=0, max=200),
    # ---------------------------------------------------------------- eval
    _p("eval.displacement", "Evaluations: displacement entries", B, True,
       "Body bigger than the previous candle's body and a close beyond its wick, towards the fair price.", S_V),
    _p("eval.bos", "Evaluations: break-of-structure entries", B, True,
       "A close beyond the most recent 1m swing, towards the fair price.", S_V),
    _p("eval.target_points", "Evaluations: take profit (points)", F, 38.0, "Static; never more even with more room.",
       S_V, min=2, max=500),
    _p("eval.stop_points", "Evaluations: stop loss (points)", F, 25.0, "", S_V, min=2, max=500),
    _p("eval.risk_usd", "Evaluations: risk per trade ($)", F, 500.0,
       "25 points on one NQ (10 MNQ) on a 50K account.", S_U, min=10, max=100000),
    # ---------------------------------------------------------------- funded
    _p("funded.displacement", "Funded: displacement entries", B, False, "He keeps funded accounts for the stronger "
       "break-of-structure entries.", S_V),
    _p("funded.bos", "Funded: break-of-structure entries", B, True, "", S_V),
    _p("funded.win_usd", "Funded: dollar win per trade", F, 1500.0,
       "The take profit decides the size: contracts = this / (target points x $2 per MNQ point), rounded down.", S_U,
       min=50, max=100000),
    _p("funded.stop_points", "Funded: stop loss (points)", F, 25.0, "Static; he never uses less than 25 in New York.",
       S_V, min=2, max=500),
    _p("funded.target_step", "Funded: take profit steps (points)", F, 25.0,
       "Take profit = the largest step that fits the room to the fair price (see the share above).", S_V, min=1,
       max=200),
    _p("funded.min_target", "Funded: smallest take profit (points)", F, 25.0, "", S_V, min=1, max=1000),
    _p("funded.max_target", "Funded: largest take profit (points)", F, 200.0, "", S_V, min=1, max=2000),
    # ---------------------------------------------------------------- session limits
    _p("day.max_losses_in_row", "Stop a session after this many losses in a row", I, 3,
       "Three reversion losses in a row: the market is not reverting, stop for that session.", S_V, min=1, max=20),
    _p("day.max_trades_per_session", "Most trades per session (0 = no limit)", I, 0, "", "", min=0, max=50),
    _p("day.cooldown", "Wait after an exit (minutes)", I, 0, "", "", min=0, max=120),
    # ---------------------------------------------------------------- risk
    _p("risk.max_contracts", "Most MNQ contracts", I, 40, "", "", min=1, max=500),
]

BY_KEY = {p["key"]: p for p in SCHEMA}
SESSIONS = (("ny_am", "New York open"), ("ny_pm", "New York afternoon"), ("asia", "Asia"), ("london", "London"))


def defaults() -> dict:
    return {p["key"]: copy.deepcopy(p["default"]) for p in SCHEMA}


def resolve(overrides: dict | None = None) -> dict:
    """Defaults + overrides, every value checked. Unknown keys are refused."""
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
        if not any(s[f"session.{k}"] for k, _ in SESSIONS):
            issues.append("Switch on at least one session")
        if not (s["eval.displacement"] or s["eval.bos"] or s["cont.eval"]):
            issues.append("Evaluations need at least one kind of entry")
        if not (s["funded.displacement"] or s["funded.bos"] or s["cont.funded"]):
            issues.append("Funded accounts need at least one kind of entry")
        if s["funded.max_target"] < s["funded.min_target"]:
            issues.append("Funded: the largest take profit must be at least the smallest")
        if minutes(s["news.until"]) <= minutes(s["news.time"]):
            issues.append("News reversions must end after the news time")
    if issues:
        raise SettingsError(issues)
    return s


def changed(settings: dict) -> dict:
    d = defaults()
    return {k: v for k, v in settings.items() if k in d and d[k] != v}


def smt_used(s: dict) -> bool:          # interface of My strategy's params; Fair price never reads ES
    return False


def identity(s: dict) -> dict:
    return dict(s)


def settings_hash(settings: dict) -> str:
    return hash_obj({"fair_price_params": PARAMS_VERSION, "settings": identity(resolve(settings))}, 16)


def schema_payload() -> dict:
    return {"version": PARAMS_VERSION, "groups": [{"id": g, "label": lbl} for g, lbl in GROUPS], "settings": SCHEMA}


def news_used(s: dict) -> bool:
    return bool(s["news.enabled"] and s["session.ny_am"])
