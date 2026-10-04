"""The Strategy autotuner's 10,000 settings combinations of My strategy (ADR-97). Pure, deterministic, no randomness.

Every combination = the BASE (your best settings so far, test 37) plus 0-4 REASONED CHANGES. A change is one option of
the catalogue below: a few settings changed together, a written reason (Blake's words where he states the rule, or what
the change tests), a theme (the settings it touches) and a priority:

* priority 1 = a rule or alternative Blake states directly (or the request: long/short, flip);
* priority 2 = a close reading of his words or a stated alternative he uses sometimes;
* priority 3 = a calibration of one of the app's judgement-call thresholds.

A combination never takes two options of the same theme (each setting has ONE theme), every change must still matter in
the combination (``inert``: a change to a switched-off rule is refused) and every combination must resolve (the settings
validation refuses contradictions, e.g. flip with breakeven). Combinations are unique by settings hash.

The design (``generate``), in this order:
1. the base itself;
2. every single change on its own;
3. pairs: every direction mode (long only, short only, forced long bias, flip ...) with every other single change, then
   the other cross-theme pairs, most Blake-central first (sum of priorities), until 3,600 pairs;
4. groups of 3, then 4 changes: a systematic covering design. Theme groups are visited in a fixed spread order (a
   constant stride through all theme subsets), and each theme contributes its least-used option so far (weighted by
   priority: priority-1 options appear about three times as often as priority-3 ones). Deterministic: the same code
   always yields the same 10,000 (``manifest_hash`` is a known answer in the tests).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from itertools import combinations

from edgelab.core.identity import hash_obj
from edgelab.mystrategy import params as P

AUTOTUNE_VERSION = 1
TOTAL = 10_000
N_PAIRS = 3_600
N_TRIPLES = 3_100

# Test 37 (2.52 trades / week, 52.0% win rate, +9.3 R on the discovery period): the user's best settings so far.
BASE: dict = {
    "session.entry_until": "15:00", "session.force_exit": "16:55", "bias.method": "both", "bias.min_score": 0.6,
    "bias.ath_rule": True, "eq.tolerance": 0.15, "ifg.tf_1m": False, "ifg.range_x_avg": 1.1, "manage.be": "off",
}
BASE_LABEL = "Test 37 (your best settings so far)"

BLAKE, READ, CAL, ASKED = "Blake", "Close reading of Blake", "Threshold calibration", "Your request"


@dataclass(frozen=True)
class Option:
    id: str
    theme: str
    label: str
    changes: dict
    reason: str
    priority: int
    source: str = BLAKE


THEMES: list[tuple[str, str]] = [
    ("direction", "Direction (long / short / flip)"), ("models", "Models"), ("session", "Trading times"),
    ("bias", "Bias decision"), ("bias_tf", "Bias timeframes and gap reading"), ("trend", "All-time-high rule"),
    ("draw", "Draw on liquidity"), ("eq", "Premium / discount"), ("filters", "Day filters, A+ score and SMT"),
    ("key_types", "Key-level types"), ("key_tf", "Key-level timeframes and age"), ("leg", "Manipulation leg"),
    ("ifg", "Inversion-gap confirmation"), ("displacement", "Displacement"), ("entry", "Entry order"),
    ("stop", "Stop loss"), ("target", "Take profit"), ("manage", "Breakeven and trailing"), ("day", "Daily rules"),
    ("risk", "Risk per trade"),
]
THEME_LABEL = dict(THEMES)


def _o(theme, id_, label, changes, reason, priority, source=BLAKE) -> Option:
    return Option(f"{theme}.{id_}", theme, label, dict(changes), reason, priority, source)


OPTIONS: list[Option] = [
    # ------------------------------------------------------------------------------------------------ direction
    _o("direction", "long_only", "Longs only", {"models.direction": "long_only"},
       "'The trend is your friend'; at all-time highs he mostly only longs. Do the long setups carry the edge alone?", 1),
    _o("direction", "short_only", "Shorts only", {"models.direction": "short_only"},
       "The counterpart of longs only: does the short side hold up on its own?", 1, ASKED),
    _o("direction", "bias_long", "Bias forced long every day", {"bias.override": "long"},
       "'Bullish until proven wrong' taken literally on an index in a long-run uptrend: every day is a long day.", 2, READ),
    _o("direction", "bias_short", "Bias forced short every day", {"bias.override": "short"},
       "The mirror of the forced long bias: switches every day to the short side.", 3, ASKED),
    _o("direction", "flip", "Flip every trade", {"models.flip": True},
       "Your request: trade the OPPOSITE side of every setup (its target becomes the stop, its stop the target). Tests "
       "whether the setups are reliably wrong.", 1, ASKED),
    _o("direction", "flip_long", "Flip the long setups only", {"models.flip": True, "models.direction": "long_only"},
       "Only the long setups, traded the other way (shorts where he would buy).", 2, ASKED),
    _o("direction", "flip_short", "Flip the short setups only", {"models.flip": True, "models.direction": "short_only"},
       "Only the short setups, traded the other way (longs where he would sell).", 2, ASKED),
    # ------------------------------------------------------------------------------------------------ models
    _o("models", "ny_only", "NY four-step only (no Judas)", {"models.judas": False},
       "The NY four-step is his main model; Judas swings happen only 1-3 times a week.", 1),
    _o("models", "judas_only", "Judas swing only", {"models.ny_4step": False},
       "His open-manipulation model on its own: the fake move at 9:30, then the reversal.", 1),
    _o("models", "judas_5", "Judas window 5 minutes", {"session.judas_minutes": 5},
       "'Usually in the first 15 minutes after the open, most trades done within 5 minutes.'", 2),
    _o("models", "judas_10", "Judas window 10 minutes", {"session.judas_minutes": 10},
       "Between his 'within 5 minutes' and 'first 15 minutes'.", 3, READ),
    _o("models", "judas_30", "Judas window 30 minutes", {"session.judas_minutes": 30},
       "A wider open window: does a later fake move still behave like a Judas swing?", 3, CAL),
    _o("models", "mid", "Rules read the mid price", {"models.price_series": "mid"},
       "Charting platforms show futures trades, not BID: the BID/ASK middle is closer to what he sees (fills stay "
       "BID/ASK).", 3, READ),
    # ------------------------------------------------------------------------------------------------ session
    _o("session", "until_1100", "Last entry 11:00", {"session.entry_until": "11:00"},
       "'9:30 to 11:00 a.m., the golden hour' - his stated window.", 1),
    _o("session", "until_1130", "Last entry 11:30", {"session.entry_until": "11:30"},
       "'Very rarely after 11, only if the morning was bad and liquidity was built.'", 2),
    _o("session", "until_1200", "Last entry 12:00", {"session.entry_until": "12:00"},
       "The end of the NY morning session.", 2, READ),
    _o("session", "until_1300", "Last entry 13:00", {"session.entry_until": "13:00"},
       "How much does the lunch hour add or cost?", 3, CAL),
    _o("session", "until_1400", "Last entry 14:00", {"session.entry_until": "14:00"},
       "The step before your 15:00 (test 25 -> 26 added trades).", 3, CAL),
    _o("session", "until_1600", "Last entry 16:00", {"session.entry_until": "16:00"},
       "The whole afternoon: is there anything left after 15:00?", 3, CAL),
    _o("session", "manip_0830", "Manipulation from 8:30", {"session.manip_from": "08:30"},
       "He watches the 8:30 open; 'if pre-market already manipulated into the level, levels inside that range are "
       "often just respected'.", 2),
    _o("session", "manip_0800", "Manipulation from 8:00", {"session.manip_from": "08:00"},
       "'If pre-market (e.g. 8 a.m.) already manipulated into the higher-timeframe level...'", 3),
    _o("session", "until_1100_manip_0830", "8:30 manipulation, entries until 11:00",
       {"session.manip_from": "08:30", "session.entry_until": "11:00"},
       "His golden hour, allowing the pre-market (8:30) manipulation he describes.", 2),
    # ------------------------------------------------------------------------------------------------ bias
    _o("bias", "fvg", "Bias from gaps only", {"bias.method": "fvg_respect"},
       "His question 1 exactly: which gaps are respected, which disrespected.", 1),
    _o("bias", "structure", "Bias from structure only", {"bias.method": "structure"},
       "Higher highs / higher lows (structure) on the higher timeframes as the bias.", 2, READ),
    _o("bias", "score_025", "Bias score 0.25", {"bias.min_score": 0.25},
       "A looser bias: more days qualify.", 3, CAL),
    _o("bias", "score_045", "Bias score 0.45", {"bias.min_score": 0.45},
       "Slightly looser than your 0.6.", 3, CAL),
    _o("bias", "score_08", "Bias score 0.8", {"bias.min_score": 0.8},
       "Slightly stricter than your 0.6.", 3, CAL),
    _o("bias", "score_10", "Bias score 1.0", {"bias.min_score": 1.0},
       "'Before you even think about executing, figure out where the market wants to go today' - a clear bias.", 2),
    _o("bias", "score_15", "Bias score 1.5", {"bias.min_score": 1.5},
       "Clear-bias days only: 'this step removes about 90% of bad trades'.", 2),
    _o("bias", "score_20", "Bias score 2.0", {"bias.min_score": 2.0},
       "Only days where every higher timeframe agrees.", 3, CAL),
    _o("bias", "prev_day", "Unclear day: keep yesterday's bias", {"bias.tie_break": "previous_day"},
       "'Bullish (or bearish) until proven wrong' - keep the higher-timeframe bias until a trade clearly fails.", 1),
    _o("bias", "daily_trend", "Unclear day: follow the daily trend", {"bias.tie_break": "daily_trend"},
       "'The trend is your friend.'", 2),
    _o("bias", "strict_prev", "Bias score 1.0, else yesterday's bias",
       {"bias.min_score": 1.0, "bias.tie_break": "previous_day"},
       "A clear bias when there is one, otherwise 'until proven wrong'.", 2),
    _o("bias", "fvg_prev", "Gap bias, else yesterday's bias", {"bias.method": "fvg_respect", "bias.tie_break": "previous_day"},
       "His gap question, with 'until proven wrong' on unclear days.", 2),
    # ------------------------------------------------------------------------------------------------ bias_tf
    _o("bias_tf", "tf15", "Add the 15m chart to the bias", {"bias.tf_15m": True},
       "'A 15-minute check around the open (perfect equal lows on the 15m can change the bias).'", 1),
    _o("bias_tf", "daily_x2", "Daily counts double", {"bias.weight_1d": 2.0},
       "The daily picture first ('daily, 4 hour and 1 hour').", 2, READ),
    _o("bias_tf", "h4_x2", "4h counts double", {"bias.weight_4h": 2.0},
       "The 4h holds most of his bias gaps in the recaps (4h bullish / bearish FVG).", 2, READ),
    _o("bias_tf", "h1_half", "1h counts half", {"bias.weight_1h": 0.5},
       "Less weight on the fastest bias chart.", 3, CAL),
    _o("bias_tf", "no_1h", "Bias without the 1h", {"bias.tf_1h": False},
       "Daily and 4h only: the slower picture.", 3, CAL),
    _o("bias_tf", "no_daily", "Bias without the daily", {"bias.tf_1d": False},
       "4h and 1h only: the intraday picture.", 3, CAL),
    _o("bias_tf", "untouched", "Untouched gaps count as respected", {"bias.respect_needs_touch": False},
       "A gap the trend never came back to also shows respect for that side.", 3, READ),
    _o("bias_tf", "events_2", "Last 2 gap reactions", {"bias.events_per_tf": 2},
       "Only the latest picture ('which gaps are being respected now').", 3, CAL),
    _o("bias_tf", "events_5", "Last 5 gap reactions", {"bias.events_per_tf": 5},
       "A longer memory of gap reactions.", 3, CAL),
    _o("bias_tf", "lookback_15", "Gaps of the last 15 candles", {"bias.fvg_lookback": 15},
       "Recent gaps only.", 3, CAL),
    _o("bias_tf", "lookback_60", "Gaps of the last 60 candles", {"bias.fvg_lookback": 60},
       "Older gaps count too.", 3, CAL),
    _o("bias_tf", "gap_min_5", "Bias ignores gaps under 5 points", {"bias.fvg_min_points": 5.0},
       "Tiny gaps say little about order flow.", 3, CAL),
    _o("bias_tf", "struct_3", "Structure swings of 3 candles", {"bias.structure_strength": 3},
       "More obvious swing points for structure.", 3, CAL),
    # ------------------------------------------------------------------------------------------------ trend
    _o("trend", "ath_off", "Allow shorts near the high", {"bias.ath_rule": False},
       "Your test 37 added 'I don't love shorting at all-time highs'; this removes it again.", 2),
    _o("trend", "ath_20", "High of the last 20 days", {"bias.ath_days": 20},
       "A shorter 'all-time high' (the last month).", 3, CAL),
    _o("trend", "ath_250", "High of the last 250 days", {"bias.ath_days": 250},
       "Closer to a real all-time high: the one-year high.", 2, READ),
    _o("trend", "ath_1pct", "Within 1% of the high", {"bias.ath_pct": 1.0},
       "A wider 'near the high' zone.", 3, CAL),
    _o("trend", "ath_2pct", "Within 2% of the high", {"bias.ath_pct": 2.0},
       "Shorts only well away from the highs.", 3, CAL),
    # ------------------------------------------------------------------------------------------------ draw
    _o("draw", "tf15", "15m swings as draws", {"draw.tf_15m": True},
       "Closer, 'obvious' swing highs and lows as the day's draw.", 3, READ),
    _o("draw", "no_nwog", "No new-week opening gap draws", {"draw.nwog": False},
       "He calls the NWOG 'king' in Asia, not in New York.", 3, READ),
    _o("draw", "no_unfilled", "No unfilled-gap draws", {"draw.unfilled_fvg": False},
       "Swing highs / lows only ('which swing high or low are we going to?').", 2),
    _o("draw", "no_prev_day", "No previous-day high/low draws", {"draw.prev_day": False},
       "Tests the previous-day levels as draws.", 3, CAL),
    _o("draw", "min_10", "Draw at least 10 points away", {"draw.min_points": 10.0},
       "Closer draws allowed.", 3, CAL),
    _o("draw", "min_40", "Draw at least 40 points away", {"draw.min_points": 40.0},
       "A draw worth trading toward: room for a 1:1.", 2, READ),
    _o("draw", "max_400", "Draw at most 400 points away", {"draw.max_points": 400.0},
       "Today's draw, not a far-away one.", 3, CAL),
    _o("draw", "strength_3", "More obvious draw swings", {"draw.swing_strength": 3},
       "'Don't overcomplicate it: the next obvious swing high.'", 2),
    # ------------------------------------------------------------------------------------------------ eq
    _o("eq", "tol_0", "Extreme at or below EQ exactly", {"eq.tolerance": 0.0},
       "His rule as stated: 'the low of the day should form at or below EQ (discount)'.", 1),
    _o("eq", "tol_008", "EQ tolerance 0.08", {"eq.tolerance": 0.08},
       "Between his strict rule and your 0.15.", 3, CAL),
    _o("eq", "tol_025", "EQ tolerance 0.25", {"eq.tolerance": 0.25},
       "Looser than your 0.15.", 3, CAL),
    _o("eq", "off", "No premium / discount rule", {"eq.enabled": False},
       "Does the premium / discount rule help at all?", 2, CAL),
    _o("eq", "prev_day", "EQ of the previous day's range", {"eq.range": "previous_day"},
       "The previous day's high-low as the dealing range.", 2, READ),
    _o("eq", "overnight", "EQ of the overnight range", {"eq.range": "overnight"},
       "The range since 18:00 (the true day open he watches).", 3, READ),
    _o("eq", "h4", "EQ of the 4h swing", {"eq.tf": "4h"},
       "'Draw the range of the higher-timeframe swing (a Gann box / 50% level).'", 2),
    _o("eq", "m30", "EQ of the 30m swing", {"eq.tf": "30m"},
       "A closer, intraday swing range.", 3, CAL),
    _o("eq", "level_045", "EQ at 45% of the range", {"eq.level": 0.45},
       "A deeper discount (premium for shorts).", 3, CAL),
    # ------------------------------------------------------------------------------------------------ filters
    _o("filters", "chop", "Skip choppy mornings", {"filters.chop": True},
       "'Choppy, wicky, barcode price action; no FVGs being created or gaps flipping back and forth' - skip.", 1),
    _o("filters", "chop_strict", "Skip choppy mornings (strict)",
       {"filters.chop": True, "filters.chop_min_gaps": 3, "filters.chop_max_flip": 0.5},
       "The same chop rule, demanding more clean gaps.", 2, CAL),
    _o("filters", "chop_loose", "Skip only very choppy mornings", {"filters.chop": True, "filters.chop_max_flip": 0.75},
       "Only the worst barcode mornings are skipped.", 3, CAL),
    _o("filters", "q1", "At least 1 extra confluence (SMT counts)", {"filters.min_quality": 1},
       "A setup with at least one of: sweep, several levels, displacement, discount, stacked target, open "
       "manipulation, SMT.", 2, READ),
    _o("filters", "q2", "A+ only: 2 extra confluences (SMT counts)", {"filters.min_quality": 2},
       "'A second trade only if it is A+.' A+ = at least two extra confluences.", 1),
    _o("filters", "q3", "3 extra confluences (SMT counts)", {"filters.min_quality": 3},
       "The strictest A+ reading.", 2, CAL),
    _o("filters", "q2_no_smt", "A+ by price action only", {"filters.min_quality": 2, "filters.smt_in_score": False},
       "Two extra confluences, SMT not counted.", 2, READ),
    _o("filters", "smt", "Require SMT divergence with ES", {"filters.smt": True},
       "'One index sweeps a high/low while the other does not' - on many of his recap trades (June 4, 5, 24, live).", 1),
    _o("filters", "smt_chop", "SMT required, choppy mornings skipped", {"filters.smt": True, "filters.chop": True},
       "SMT confluence on clean mornings only.", 2, READ),
    # ------------------------------------------------------------------------------------------------ key types
    _o("key_types", "no_cisd", "No CISD levels", {"key.cisd": False},
       "Your tests: trades involving a CISD won 39.8% vs 56.1% for the rest ('Try dropping CISD levels').", 1, ASKED),
    _o("key_types", "cisd_loose", "CISD without a gap", {"key.cisd_needs_fvg": False},
       "'It can be used on its own' - a CISD that did not come from a gap.", 3),
    _o("key_types", "cisd_any", "CISD: any return, not just the first", {"key.cisd_first_touch": False},
       "Price 'often returns' to a CISD more than once.", 3, CAL),
    _o("key_types", "cisd_3", "CISD from at most 3 candles", {"key.cisd_max_candles": 3},
       "A short, clean series of down-close candles.", 3, CAL),
    _o("key_types", "no_rb", "No rejection blocks", {"key.rejection_block": False},
       "Tests the rejection blocks as key levels.", 2, CAL),
    _o("key_types", "rb_ce", "Rejection block: reach the wick's 50%", {"key.rb_needs_ce": True},
       "'Mark the wick and its 50% (CE) - the CE gives bottom-tick entries.'", 2),
    _o("key_types", "rb_wick_06", "Rejection block: wick at least 60%", {"key.rb_wick_ratio": 0.6},
       "Only clear rejection wicks.", 3, CAL),
    _o("key_types", "bpr", "Add balanced price ranges", {"key.bpr": True},
       "BPR as a key level (his June 10 recap: '15m intermediate low + BPR').", 2),
    _o("key_types", "fvg_only", "Fair value gaps only", {"key.cisd": False, "key.rejection_block": False},
       "His first and main key level, alone.", 2, READ),
    _o("key_types", "no_sweep_rule", "Used gaps without the inside sweep", {"key.fvg_sweep_rule": False},
       "Tests his rule that a used gap needs its intermediate low swept first.", 3, CAL),
    _o("key_types", "keep_on_close", "A close through does not kill a level", {"key.invalidate_on_close": False},
       "Tests the invalidation rule.", 3, CAL),
    _o("key_types", "touch_1", "Count a 1-point near miss as a touch", {"key.tolerance_points": 1.0},
       "Price often reacts just before the level.", 3, CAL),
    _o("key_types", "two_levels", "Two key levels hit together", {"key.min_levels": 2},
       "'Strongest when it lines up with an FVG'; '2-3 good levels are enough'.", 1),
    # ------------------------------------------------------------------------------------------------ key tf
    _o("key_tf", "add_3m", "Add 3m key levels", {"key.tf_3m": True},
       "He lists 3m, 5m, 15m, 30m, 1h and 4h key levels.", 2),
    _o("key_tf", "no_5m", "Key levels 15m and higher", {"key.tf_5m": False},
       "Higher-timeframe key levels only.", 2, READ),
    _o("key_tf", "no_4h", "No 4h key levels", {"key.tf_4h": False},
       "Intraday key levels only.", 3, CAL),
    _o("key_tf", "judas_15m", "Judas: 15m+ key levels", {"key.judas_min_tf": "15m"},
       "A stricter reading of 'the 5-minute chart or higher'.", 3, CAL),
    _o("key_tf", "judas_3m", "Judas: 3m+ key levels", {"key.judas_min_tf": "3m"},
       "A looser reading for the Judas key level.", 3, CAL),
    _o("key_tf", "rb_5m", "Rejection blocks from 5m", {"key.rb_min_tf": "5m"},
       "Smaller-timeframe rejection wicks too.", 3, CAL),
    _o("key_tf", "rb_1h", "Rejection blocks 1h and higher", {"key.rb_min_tf": "1h"},
       "'1h rejection block' and '4h rejection block' in his recaps.", 2),
    _o("key_tf", "age_60", "Key gaps at most 60 candles old", {"key.fvg_max_age": 60},
       "Fresh gaps: 'an unmitigated FVG is valid on the first touch'.", 2, READ),
    _o("key_tf", "age_300", "Key gaps up to 300 candles old", {"key.fvg_max_age": 300},
       "Older, untouched gaps count too.", 3, CAL),
    _o("key_tf", "gap_min_4", "Key gaps at least 4 points", {"key.fvg_min_points": 4.0},
       "Only meaningful gaps as key levels.", 3, CAL),
    # ------------------------------------------------------------------------------------------------ leg
    _o("leg", "start_5m", "Leg from the 5m external swing", {"leg.start_tf": "5m"},
       "'Look at the whole manipulation leg from the external swing on the 5 minute.'", 1),
    _o("leg", "start_15m", "Leg from the 15m external swing", {"leg.start_tf": "15m"},
       "An even larger external swing.", 3, CAL),
    _o("leg", "sweep", "The leg must sweep liquidity", {"leg.require_sweep": True},
       "'Your stop should sit behind high-resistance liquidity (protected)' - the leg took a prior low.", 1),
    _o("leg", "sweep_15m", "The leg must sweep a 15m swing", {"leg.require_sweep": True, "leg.sweep_tf": "15m"},
       "A bigger liquidity grab: a 15m swing low (high for shorts).", 2, READ),
    _o("leg", "allow_equal", "Allow relative equal lows", {"leg.no_equal_extremes": False},
       "Tests his 'we cannot generate relative equal lows here' rule.", 3, CAL),
    _o("leg", "min_5", "Leg at least 5 points", {"leg.min_points": 5.0}, "Small manipulations count.", 3, CAL),
    _o("leg", "min_15", "Leg at least 15 points", {"leg.min_points": 15.0},
       "A real manipulation, not noise.", 2, CAL),
    _o("leg", "min_20", "Leg at least 20 points", {"leg.min_points": 20.0}, "Only clear manipulation legs.", 3, CAL),
    _o("leg", "max_60", "Leg at most 60 minutes", {"leg.max_minutes": 60},
       "A quick manipulation (the open's fake move).", 3, CAL),
    _o("leg", "max_90", "Leg at most 90 minutes", {"leg.max_minutes": 90}, "A shorter manipulation leg.", 3, CAL),
    _o("leg", "equal_2", "Equal lows within 2 points", {"leg.equal_points": 2.0}, "Stricter 'equal'.", 3, CAL),
    _o("leg", "equal_5", "Equal lows within 5 points", {"leg.equal_points": 5.0}, "Looser 'equal'.", 3, CAL),
    _o("leg", "judas_any_side", "Judas: extreme need not pass the open", {"leg.judas_open_side": False},
       "Tests the open-low-high-close shape rule.", 3, CAL),
    # ------------------------------------------------------------------------------------------------ ifg
    _o("ifg", "tf_1m", "Add 1m inversion gaps", {"ifg.tf_1m": True},
       "He confirms on 30s / 1m in many recaps (1m is the lowest timeframe here); your test 23 removed them.", 2),
    _o("ifg", "no_5m", "No 5m inversion gaps", {"ifg.tf_5m": False},
       "The 2-4m gaps only (more entries, worse highest-timeframe reading).", 3, CAL),
    _o("ifg", "nearest_gap", "Close through the nearest gap only", {"ifg.all_gaps": False},
       "Earlier entries: the first gap of the leg instead of all of them.", 3, CAL),
    _o("ifg", "wait_15", "Confirmation within 15 minutes", {"ifg.max_wait": 15},
       "'Most trades done within 5 minutes': a quick reversal after the extreme.", 2),
    _o("ifg", "wait_20", "Confirmation within 20 minutes", {"ifg.max_wait": 20}, "A quicker reversal.", 3, CAL),
    _o("ifg", "wait_45", "Confirmation within 45 minutes", {"ifg.max_wait": 45}, "More time to confirm.", 3, CAL),
    _o("ifg", "gap_min_1", "Inversion gaps at least 1 point", {"ifg.min_gap_points": 1.0},
       "Ignore the tiniest gaps.", 3, CAL),
    _o("ifg", "buffer_05", "Close at least 0.5 point through", {"ifg.close_buffer": 0.5},
       "'Only once the candle has CLOSED below/above the gap' - a clear close through.", 2),
    _o("ifg", "judas_highest", "Judas: highest-timeframe gap", {"ifg.rule_judas": "highest"},
       "His NY rule used for the Judas swing too.", 3, READ),
    # ------------------------------------------------------------------------------------------------ displacement
    _o("displacement", "off", "No displacement check", {"ifg.displacement": False},
       "Any close through the gap confirms.", 2, CAL),
    _o("displacement", "body_06", "Displacement body 60%", {"ifg.body_ratio": 0.6},
       "'A strong displacement candle closing through the gap raises the win rate.'", 2),
    _o("displacement", "body_07", "Displacement body 70%", {"ifg.body_ratio": 0.7},
       "Only very strong closes ('you can't fade trades like this').", 3),
    _o("displacement", "range_10", "Displacement range 1.0x average", {"ifg.range_x_avg": 1.0},
       "The app default (your test 14 raised it to 1.1).", 3, CAL),
    _o("displacement", "range_13", "Displacement range 1.3x average", {"ifg.range_x_avg": 1.3},
       "A bigger-than-usual candle.", 2, CAL),
    _o("displacement", "range_15", "Displacement range 1.5x average", {"ifg.range_x_avg": 1.5},
       "Clear displacement only.", 3, CAL),
    _o("displacement", "strong", "Strong displacement (body 60%, 1.3x)", {"ifg.body_ratio": 0.6, "ifg.range_x_avg": 1.3},
       "Both parts of 'strong displacement' at once.", 2),
    # ------------------------------------------------------------------------------------------------ entry
    _o("entry", "limit", "Limit order at the inverted gap", {"entry.type": "limit_gap"},
       "'If the close is too far away (bad risk-to-reward), a limit order at the IFG.'", 1),
    _o("entry", "limit_5", "Limit order, 5 minutes", {"entry.type": "limit_gap", "entry.limit_minutes": 5},
       "A small retrace right after the close, or no trade.", 2, READ),
    _o("entry", "limit_30", "Limit order, 30 minutes", {"entry.type": "limit_gap", "entry.limit_minutes": 30},
       "'Sometimes he waits for a small retrace for a better entry.'", 3),
    # ------------------------------------------------------------------------------------------------ stop
    _o("stop", "body", "Stop at the displacement candle's body", {"stop.mode": "confirm_body"},
       "One of his stop choices: 'at the displacement candle's body'.", 2),
    _o("stop", "gap_edge", "Stop at the gap's far edge", {"stop.mode": "gap_edge"},
       "One of his stop choices: 'at the FVG's far edge'.", 2),
    _o("stop", "candle", "Stop at the confirming candle", {"stop.mode": "confirm_candle"},
       "'At the IFG candle's body high' - the tightest stop (your test failed it with the old settings).", 3),
    _o("stop", "buf_0", "No stop buffer", {"stop.buffer": 0.0}, "Stop exactly at the swing.", 3, CAL),
    _o("stop", "buf_2", "Stop buffer 2 points", {"stop.buffer": 2.0}, "A little room beyond the swing.", 3, CAL),
    _o("stop", "max_50", "Stops at most 50 points", {"stop.max_points": 50.0},
       "'Huge stops for a small target' are a red flag; prop drawdown limits.", 2),
    _o("stop", "max_60", "Stops at most 60 points", {"stop.max_points": 60.0}, "A tighter stop limit.", 3, CAL),
    _o("stop", "max_100", "Stops up to 100 points", {"stop.max_points": 100.0}, "'100+ points' is his red flag.", 2),
    _o("stop", "max_124", "Stops up to 124 points", {"stop.max_points": 124.0},
       "'He faded a 124-point stop' - the largest he mentions.", 3),
    _o("stop", "min_8", "Stops at least 8 points", {"stop.min_points": 8.0},
       "Room against noise on tiny legs.", 3, CAL),
    # ------------------------------------------------------------------------------------------------ target
    _o("target", "fixed_1", "Fixed 1:1 target", {"target.mode": "fixed_r", "target.fixed_r": 1.0},
       "'A 1:1 risk-to-reward by default.'", 1),
    _o("target", "fixed_12", "Fixed 1.2R target", {"target.mode": "fixed_r", "target.fixed_r": 1.2},
       "A touch above 1:1 (several recaps ended at 1.2-1.3R).", 2),
    _o("target", "fixed_15", "Fixed 1.5R target", {"target.mode": "fixed_r", "target.fixed_r": 1.5},
       "Between his 1:1 default and 1:3 maximum.", 2),
    _o("target", "fixed_2", "Fixed 2R target", {"target.mode": "fixed_r", "target.fixed_r": 2.0},
       "A larger fixed target.", 3, CAL),
    _o("target", "min_12", "Liquidity target, at least 1.2R", {"target.min_r": 1.2},
       "Nearest liquidity, but never under 1.2R.", 2, CAL),
    _o("target", "min_15", "Liquidity target, at least 1.5R", {"target.min_r": 1.5},
       "Only setups with room to 1.5R.", 2, CAL),
    _o("target", "max_2", "Liquidity target, at most 2R", {"target.max_r": 2.0},
       "'Consistency rules mean no huge trades; take your piece of the pie.'", 2),
    _o("target", "max_15", "Liquidity target, at most 1.5R", {"target.max_r": 1.5},
       "Close to his default 1:1, a little more when liquidity is near.", 2, READ),
    _o("target", "next", "Next liquidity beyond 1R", {"target.below_min": "next"},
       "A real level beyond 1:1 instead of stretching the target to 1:1.", 2, READ),
    _o("target", "skip_below", "No trade when the level is under 1R", {"target.below_min": "skip"},
       "Tests 'he moves the target a bit higher to make 1:1'.", 2, CAL),
    _o("target", "no_draw", "Target not at the day's far draw", {"target.draw": False},
       "'Low-hanging fruit - the most obvious level, not the far higher-timeframe draw.'", 1),
    _o("target", "swings_1m", "Target: add 1m swings", {"target.swings_1m": True},
       "The closest lower-timeframe highs / lows.", 3, CAL),
    _o("target", "no_unfilled", "Target: no unfilled gaps", {"target.unfilled_fvg": False},
       "Swing and session levels only.", 3, CAL),
    _o("target", "no_session", "Target: no session levels", {"target.session_levels": False},
       "Swing and gap levels only.", 3, CAL),
    _o("target", "no_level_skip", "No trade without liquidity to target", {"target.no_level": "skip"},
       "'Is my take profit at low-resistance liquidity?' - none, no trade.", 2),
    _o("target", "strength_3", "Target swings of 3 candles", {"target.swing_strength": 3},
       "More obvious target swings.", 3, CAL),
    # ------------------------------------------------------------------------------------------------ manage
    _o("manage", "be_leg", "Breakeven at the leg's swing point", {"manage.be": "leg_swing"},
       "'Breakeven: mostly at the swing point of the manipulation leg.'", 1),
    _o("manage", "be_leg_075", "Breakeven at the swing point (if 0.75R+ away)",
       {"manage.be": "leg_swing", "manage.be_min_r": 0.75},
       "Only when that point is far enough not to choke the trade.", 3, CAL),
    _o("manage", "be_r_07", "Breakeven after 0.7R", {"manage.be": "r", "manage.be_r": 0.7},
       "'Or at the first internal high/low' - most of the way to a 1:1 target.", 3, READ),
    _o("manage", "trail_1m", "Trail behind 1m swings", {"manage.trail": "swing_1m"},
       "'Sometimes trails the stop to new swing lows once well in profit.'", 2),
    _o("manage", "trail_5m", "Trail behind 5m swings", {"manage.trail": "swing_5m"},
       "The same, on the 5-minute swings.", 2),
    _o("manage", "trail_1m_1r", "Trail behind 1m swings after 1R", {"manage.trail": "swing_1m", "manage.trail_start_r": 1.0},
       "A 'hard trail near targets'.", 3),
    # ------------------------------------------------------------------------------------------------ day
    _o("day", "one_trade", "One trade a day", {"day.max_trades": 1}, "'1 trade a day (at most 2).'", 1),
    _o("day", "stop_loss", "Done after a loss", {"day.stop_after_loss": True},
       "'One loss usually done; two losses and he is definitely done.'", 1),
    _o("day", "cooldown_30", "30 minutes before a second trade", {"day.cooldown": 30},
       "'No instant re-entry.'", 2),
    _o("day", "cooldown_15", "15 minutes before a second trade", {"day.cooldown": 15}, "A short pause first.", 3, CAL),
    _o("day", "three", "Up to 3 trades a day", {"day.max_trades": 3},
       "More chances on days without a win (his limit is 2).", 3, CAL),
    # ------------------------------------------------------------------------------------------------ risk
    _o("risk", "half", "Risk 0.5% per trade", {"risk.pct": 0.5},
       "Half his 1%: more room inside prop drawdown limits (R results unchanged, dollars and prop results change).",
       2, READ),
    _o("risk", "r075", "Risk 0.75% per trade", {"risk.pct": 0.75}, "A little under his 1%.", 3, CAL),
    _o("risk", "r15", "Risk 1.5% per trade", {"risk.pct": 1.5}, "Faster to the prop target, closer to its limits.", 3,
       CAL),
]
BY_ID = {o.id: o for o in OPTIONS}


def _differs(s: dict, key: str) -> bool:
    return s[key] != BASE_RESOLVED[key]


# A changed setting must still matter in the combination: e.g. a rejection-block setting while rejection blocks are off,
# or a limit-order time with market entries, does nothing (it would only fake a "new" combination).
INERT_UNLESS = {
    "bias.structure_strength": lambda s: s["bias.method"] in ("structure", "both") and s["bias.override"] == "auto",
    "bias.ath_days": lambda s: s["bias.ath_rule"] and s["models.direction"] != "long_only" and s["bias.override"] != "long",
    "bias.ath_pct": lambda s: s["bias.ath_rule"] and s["models.direction"] != "long_only" and s["bias.override"] != "long",
    "bias.ath_rule": lambda s: s["models.direction"] != "long_only" and s["bias.override"] != "long",
    "bias.tie_break": lambda s: s["bias.override"] == "auto",
    "bias.min_score": lambda s: s["bias.override"] == "auto",
    "bias.method": lambda s: s["bias.override"] == "auto",
    "bias.tf_15m": lambda s: s["bias.override"] == "auto",
    "bias.tf_1h": lambda s: s["bias.override"] == "auto",
    "bias.tf_1d": lambda s: s["bias.override"] == "auto",
    "bias.weight_1d": lambda s: s["bias.override"] == "auto" and s["bias.tf_1d"],
    "bias.weight_4h": lambda s: s["bias.override"] == "auto" and s["bias.tf_4h"],
    "bias.weight_1h": lambda s: s["bias.override"] == "auto" and s["bias.tf_1h"],
    "bias.respect_needs_touch": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    "bias.events_per_tf": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    "bias.fvg_lookback": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    "bias.fvg_min_points": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    "eq.tolerance": lambda s: s["eq.enabled"], "eq.range": lambda s: s["eq.enabled"],
    "eq.tf": lambda s: s["eq.enabled"] and s["eq.range"] == "swings", "eq.level": lambda s: s["eq.enabled"],
    "filters.chop_min_gaps": lambda s: s["filters.chop"], "filters.chop_max_flip": lambda s: s["filters.chop"],
    "filters.smt_in_score": lambda s: s["filters.min_quality"] > 0,
    "key.cisd_needs_fvg": lambda s: s["key.cisd"], "key.cisd_first_touch": lambda s: s["key.cisd"],
    "key.cisd_max_candles": lambda s: s["key.cisd"],
    "key.rb_needs_ce": lambda s: s["key.rejection_block"], "key.rb_wick_ratio": lambda s: s["key.rejection_block"],
    "key.rb_min_tf": lambda s: s["key.rejection_block"],
    "key.fvg_sweep_rule": lambda s: s["key.fvg"], "key.fvg_max_age": lambda s: s["key.fvg"],
    "key.fvg_min_points": lambda s: s["key.fvg"],
    "key.judas_min_tf": lambda s: s["models.judas"], "session.judas_minutes": lambda s: s["models.judas"],
    "leg.judas_open_side": lambda s: s["models.judas"], "ifg.rule_judas": lambda s: s["models.judas"],
    "leg.equal_points": lambda s: s["leg.no_equal_extremes"] or s["target.mode"] == "liquidity",
    "ifg.body_ratio": lambda s: s["ifg.displacement"], "ifg.range_x_avg": lambda s: s["ifg.displacement"],
    "entry.limit_minutes": lambda s: s["entry.type"] == "limit_gap",
    "target.min_r": lambda s: s["target.mode"] == "liquidity", "target.max_r": lambda s: s["target.mode"] == "liquidity",
    "target.below_min": lambda s: s["target.mode"] == "liquidity", "target.draw": lambda s: s["target.mode"] == "liquidity",
    "target.swings_1m": lambda s: s["target.mode"] == "liquidity",
    "target.unfilled_fvg": lambda s: s["target.mode"] == "liquidity",
    "target.session_levels": lambda s: s["target.mode"] == "liquidity",
    "target.no_level": lambda s: s["target.mode"] == "liquidity",
    "target.swing_strength": lambda s: s["target.mode"] == "liquidity",
    "manage.be_min_r": lambda s: s["manage.be"] == "leg_swing", "manage.be_r": lambda s: s["manage.be"] == "r",
    "manage.trail_start_r": lambda s: s["manage.trail"] != "off",
    "day.cooldown": lambda s: s["day.max_trades"] > 1,
    "stop.min_points": lambda s: True,
}
# combinations that contradict themselves (no trade could ever happen)
CONTRADICTS = [
    lambda s: s["models.direction"] == "long_only" and s["bias.override"] == "short",
    lambda s: s["models.direction"] == "short_only" and s["bias.override"] == "long",
]

BASE_RESOLVED = P.resolve(BASE)


@dataclass
class Combo:
    n: int
    stage: str
    options: tuple
    overrides: dict
    settings_hash: str
    changes: list = field(default_factory=list)


def _settings(opts) -> dict | None:
    """The overrides of the base + these options, or None when invalid, contradictory, inert or touching one setting
    twice."""
    ov = dict(BASE)
    seen: set = set()
    for o in opts:
        if seen & o.changes.keys():
            return None
        seen |= o.changes.keys()
        ov.update(o.changes)
    try:
        s = P.resolve(ov)
    except P.SettingsError:
        return None
    if any(c(s) for c in CONTRADICTS):
        return None
    for o in opts:
        if not any(_differs(s, k) for k in o.changes):            # the option repeats the base
            return None
        for k in o.changes:
            if _differs(s, k) and not INERT_UNLESS.get(k, lambda _s: True)(s):
                return None
    return P.changed(s)


def _stride(n: int) -> int:
    """A step through n items that visits each once and spreads consecutive visits (coprime, near n * 0.618)."""
    from math import gcd
    k = max(1, int(n * 0.6180339887))
    while gcd(k, n) != 1:
        k += 1
    return k


def generate() -> list[Combo]:
    themes = [t for t, _ in THEMES]
    by_theme = {t: [o for o in OPTIONS if o.theme == t] for t in themes}
    out: list[Combo] = []
    seen: set[str] = set()

    def add(stage: str, opts) -> bool:
        ov = _settings(opts)
        if ov is None:
            return False
        h = P.settings_hash(ov)
        if h in seen:
            return False
        seen.add(h)
        out.append(Combo(len(out) + 1, stage, tuple(o.id for o in opts), ov, h))
        return True

    add("base", ())
    for o in OPTIONS:                                            # 2. every single change
        add("single", (o,))
    n_pairs = 0
    for d in by_theme["direction"]:                              # 3a. every direction mode x every other change
        for o in OPTIONS:
            if o.theme != "direction" and add("pair", (d, o)):
                n_pairs += 1
    rest = [(a, b) for a, b in combinations(OPTIONS, 2)          # 3b. other pairs, most Blake-central first
            if a.theme != b.theme and "direction" not in (a.theme, b.theme)]
    order = {o.id: k for k, o in enumerate(OPTIONS)}
    rest.sort(key=lambda p: (p[0].priority + p[1].priority, max(p[0].priority, p[1].priority),
                             order[p[0].id], order[p[1].id]))
    for a, b in rest:
        if n_pairs >= N_PAIRS:
            break
        if add("pair", (a, b)):
            n_pairs += 1
    weight = {1: 3.0, 2: 2.0, 3: 1.0}                            # 4. covering design for 3 and 4 changes
    for k, quota in ((3, N_TRIPLES), (4, None)):
        target = len(out) + quota if quota is not None else TOTAL
        subsets = list(combinations(themes, k))
        step, used = _stride(len(subsets)), {o.id: 0 for o in OPTIONS}
        j = 0
        guard = 0
        while len(out) < target:
            sub = subsets[(j * step) % len(subsets)]
            j += 1
            ranked = [sorted(by_theme[t], key=lambda o: (used[o.id] / weight[o.priority], o.priority, order[o.id]))
                      for t in sub]
            picked = None
            for tries in range(4 ** k):                          # least-used first, then the next ones per theme
                idx, x = [], tries
                for r in ranked:
                    idx.append(x % min(4, len(r)))
                    x //= 4
                cand = tuple(r[i] for r, i in zip(ranked, idx))
                if add({3: "triple", 4: "quad"}[k], cand):
                    picked = cand
                    break
            if picked is None:
                guard += 1
                if guard > len(subsets) * 4:
                    raise RuntimeError("the covering design ran out of valid combinations")
                continue
            for o in picked:
                used[o.id] += 1
    for c in out:
        c.changes = [{"option": oid, "theme": BY_ID[oid].theme, "label": BY_ID[oid].label,
                      "changes": BY_ID[oid].changes, "reason": BY_ID[oid].reason, "priority": BY_ID[oid].priority,
                      "source": BY_ID[oid].source} for oid in c.options]
    return out


def describe(c: Combo) -> str:
    if not c.options:
        return BASE_LABEL
    return " + ".join(BY_ID[o].label for o in c.options)


def manifest(combos: list[Combo] | None = None) -> dict:
    combos = combos if combos is not None else generate()
    rows = [{"n": c.n, "stage": c.stage, "options": list(c.options), "label": describe(c), "overrides": c.overrides,
             "settings_hash": c.settings_hash, "changes": c.changes} for c in combos]
    h = hash_obj({"autotune_version": AUTOTUNE_VERSION, "params_version": P.PARAMS_VERSION, "base": BASE,
                  "rows": [[r["n"], r["settings_hash"], r["overrides"]] for r in rows]}, 16)
    return {"autotune_version": AUTOTUNE_VERSION, "manifest_hash": h, "total": len(rows), "base": BASE,
            "base_label": BASE_LABEL, "themes": [{"id": t, "label": lbl} for t, lbl in THEMES],
            "options": [{"id": o.id, "theme": o.theme, "label": o.label, "changes": o.changes, "reason": o.reason,
                         "priority": o.priority, "source": o.source} for o in OPTIONS],
            "counts": {s: sum(1 for r in rows if r["stage"] == s) for s in ("base", "single", "pair", "triple", "quad")},
            "rows": rows}
