"""Strategy pool 2 variation space (ADR-87): 25 new families + new variables for the 30 pool-1 families.

Pool 1 (``factory_space``, manifest FM_3B0B01CFC81AB15E) is NOT modified: this module imports it and builds a
separate space that the same generator (``factory.generate(..., S=factory_space_p2, exclude=<pool 1>)``) samples.

Frozen design (the user's choices, made before any evaluation):
  * 10,000 strategies: 6,000 from 25 NEW families (240 each) and 4,000 from the 30 EXISTING families (133 or 134 each,
    the extra one to the lowest family numbers), equal shares within each part.
  * every existing-family strategy uses at least one NEW variable value (``NEW_VALUES``), so it is a genuinely
    different design, not another draw of pool 1; any logic already in pool 1 is excluded by logic hash.
  * risk-based sizing only (fixed 125/250/500 USD and 0.25/0.5/1 % of equity, capped at 40 MNQ); the fixed-contract
    options of pool 1 are left out.
  * the same five timeframes as pool 1 (no new datasets); a 240m higher-timeframe filter is added.

Nothing here evaluates anything. Numeric domains are research DESIGN choices, not market data.
"""
from __future__ import annotations

import math
from dataclasses import replace
from datetime import datetime
from zoneinfo import ZoneInfo

from edgelab.strategy import factory_space as P1
from edgelab.strategy.factory_space import (  # noqa: F401  (re-exported: the generator reads them from this module)
    ALL, APPROACH_ATR, ATR, BAR, BE_OFFSET, BE_TRIGGER_ATR, BE_TRIGGER_R, BREAKOUT_STOPS, BREAKOUT_TARGETS, BUFFERS_ATR,
    CLOSE, CONFIRMS, COOLDOWNS, DIRECTIONS, EQUITY_RISK, EXECUTION_CONTRACT, F, FIXED_QUANTITY, FLAT_RULES, Family,
    LDN, LEVEL_TRAILS, MAX_TRADES, MF, MTF_FILTERS, NO_PROGRESS, NO_PROGRESS_ATR, NO_PROGRESS_BARS, NO_PROGRESS_R,
    NOT_EXECUTABLE, NOT_EXECUTABLE_REASONS, NY, ORDER_EXPIRY, REENTRY, REENTRY_MINUTES, REVERSION_STOPS,
    REVERSION_TARGETS, RISK_QUANTITY_CAP, RISK_USD, RR, STOP_ATR, STOP_POINTS, SU_TH, TARGET_ATR, TARGET_POINTS,
    TIMEFRAMES, TRAIL_ACT_ATR, TRAIL_ACT_R, TRAIL_ACTIVATION, TRAIL_ATR, TRAIL_BUFFERS, TRAIL_EVERY, TRAIL_KINDS,
    TRAIL_POINTS, TRAIL_STEP, TRAIL_STEP_POINTS, TREND_STOPS, TREND_TARGETS, TYO, TZ_CODE, WEEKDAY_SETS, Reject, Window,
    _orb_ref, _ref_level, ar, cmp, confirm_cond, ge, gt, hi_lo, hm, lagged, mins, mtf_cond, off, session_def,
    session_name, trailing_block, trailing_domain, xup)

FACTORY_VERSION = "edgelab-strategy-factory/5+pool2"
VARIATION_SPACE_VERSION = "edgelab-dt-space-pool2/1"
ALLOCATION_VERSION = "edgelab-dt-allocation-pool2/1"
DEFAULT_SEED = 20261002
TARGET_TOTAL = 10_000
NEW_FAMILY_TOTAL, EXISTING_FAMILY_TOTAL = 6_000, 4_000
POOL = {"pool": 2, "name": "Strategy pool 2", "excludes": "pool 1 (every logic hash of the pool-1 manifest)",
        "split": {"new_families": NEW_FAMILY_TOTAL, "existing_families_with_new_variables": EXISTING_FAMILY_TOTAL},
        "sizing": "risk-based only (risk_125/250/500, eq_025/05/10), capped at RISK_QUANTITY_CAP MNQ"}
REGENERATE = "python -m edgelab.cli factory generate-pool2 --seed {seed}"

# =============================================================================== reference windows
RTH_DEF = (NY, MF, "09:30", "16:00")
FH_DEF = (NY, MF, "09:30", "10:30")
MID_DEF = (NY, MF, "00:00", "16:00")
RTH, FH, MID = (session_name("FXR", *d) for d in (RTH_DEF, FH_DEF, MID_DEF))


def sess(out: str, name: str) -> dict:
    return F("session", out, session=name)


def dl(out: str) -> dict:
    return F("daily_levels", out)


# =============================================================================== shared dimensions (pool 2)
NEW_SESSIONS = {
    "ny_1000_1100": Window(NY, MF, "10:00", "11:00", "12:00"),     # ICT "silver bullet" hour
    "ny_lunch":     Window(NY, MF, "11:30", "13:30", "14:30"),
    "ny_last30":    Window(NY, MF, "15:30", "15:45", "16:00"),
    "london_close": Window(NY, MF, "10:00", "12:00", "13:00"),     # London close (16:00-17:00 London) in NY time
    "ny_evening":   Window(NY, SU_TH, "20:00", "23:00", "00:00"),  # evening of the NEXT trading date (Sunday = Monday)
}
SESSION_PRESETS = {**P1.SESSION_PRESETS, **NEW_SESSIONS}
ALL_SESSIONS = tuple(SESSION_PRESETS)
NY_SESSIONS = P1.NY_SESSIONS + ("ny_1000_1100", "ny_lunch", "ny_last30", "london_close")
REFERENCE_WINDOWS = {**P1.REFERENCE_WINDOWS,
                     "asia_ny": (NY, SU_TH, "20:00", "00:00"),            # Asian range in New York time
                     "ny_first_hour": FH_DEF}

NEW_REGIMES = ("sma200_trend", "above_prior_close", "open_inside_prior_range", "open_outside_prior_range",
               "gap_large", "gap_small", "wide_first_hour", "narrow_first_hour")
REGIMES = P1.REGIMES + NEW_REGIMES
REGIME_DIRECTIONAL = P1.REGIME_DIRECTIONAL | {"sma200_trend", "above_prior_close"}
RTH_REGIMES = ("open_inside_prior_range", "open_outside_prior_range", "gap_large", "gap_small")
FH_REGIMES = ("wide_first_hour", "narrow_first_hour")
GAP_LARGE, GAP_SMALL = 0.25, 0.1          # |RTH open - prior RTH close| as a fraction of the prior RTH range
FH_WIDE, FH_NARROW = 2.0, 1.0             # first-hour range in 60-minute ATR(14) units

MTF_PAIRS = {"1m": ("5m", "15m", "240m"), "5m": ("15m", "30m", "60m", "240m"), "15m": ("60m", "240m"),
             "30m": ("60m", "240m"), "60m": ("240m",)}
TIME_EXITS = {"window": 0.30, 15: 0.08, 30: 0.12, 60: 0.15, 90: 0.12, 120: 0.15, 240: 0.08}
ENTRY_DELAYS = {0: 0.6, 1: 0.2, 2: 0.2}
# pool-1 weights of the risk-based options, renormalised to sum to 1 (the fixed-contract options are left out)
_RISK_KEYS = ("risk_125", "risk_250", "risk_500", "eq_025", "eq_05", "eq_10")
_RW = sum(P1.SIZING[k] for k in _RISK_KEYS)
SIZING = {k: round(P1.SIZING[k] / _RW, 6) for k in _RISK_KEYS}
EXTRA_ORDER_TYPES = {"bar_stop": "stop", "bar_limit": "limit"}   # resting order placed at the signal bar


def signal_bar_level(otype: str, s: int) -> dict:
    """bar_stop: beyond the signal bar's extreme (long: its high); bar_limit: 50 % of the signal bar's range."""
    if otype == "bar_stop":
        return BAR(hi_lo(s, "high", "low"))
    return ar("div", ar("add", BAR("high"), BAR("low")), 2.0)


NEW_STOPS = ("pct", "stdev")
NEW_TARGETS = ("prior_close", "pivot", "fib_ext", "midnight_open")
STOP_DOMAINS = {**P1.STOP_DOMAINS,
                "pct": lambda tf: {"pct": (0.1, 0.2, 0.3)},              # percent of price at the signal
                "stdev": lambda tf: {"multiple": (1.5, 2.0, 3.0)},       # standard deviations of the last 20 closes
                "range_mid": lambda tf: {"buffer": BUFFERS_ATR}}         # ORB: midpoint of the opening range
TARGET_DOMAINS = {**P1.TARGET_DOMAINS,
                  "prior_close": lambda tf: {},                          # prior New York regular-session close
                  "pivot": lambda tf: {},                                # classic floor pivot R1 / S1 (prior trading day)
                  "fib_ext": lambda tf: {"ext": (1.272, 1.618)},         # extension of the latest confirmed swing range
                  "midnight_open": lambda tf: {},                        # New York midnight open
                  "range_ext": lambda tf: {"multiple": (1.5, 2.0)}}      # ORB: opening-range extension
NEW_VALUES = {"session": set(NEW_SESSIONS), "regime": set(NEW_REGIMES), "stop": set(NEW_STOPS) | {"range_mid"},
              "target": set(NEW_TARGETS) | {"range_ext"}, "order": set(EXTRA_ORDER_TYPES), "entry_delay": {2},
              "time_exit": {15, 90}, "htf": {"240m"}, "reference": {"asia_ny", "ny_first_hour"}}

stop_operand = P1.stop_operand
target_operand = P1.target_operand


def stop_level_ext(fam, p, st, s):
    k = st["type"]
    if k == "pct":
        return ar("mul", CLOSE, round(1.0 - s * st["pct"] / 100.0, 6))
    if k == "stdev":
        return off(CLOSE, st["multiple"], -s, unit=F("bollinger", "std", period=20, k=2.0))
    if k == "range_mid":
        ref = _orb_ref(p)
        mid = ar("div", ar("add", _ref_level(ref, 1), _ref_level(ref, -1)), 2.0)
        return off(mid, st["buffer"], -s)
    return None


def _classic(s: int) -> dict:
    h, lo, c = dl("prev_day_high"), dl("prev_day_low"), dl("prev_day_close")
    pv = ar("div", ar("add", ar("add", h, lo), c), 3.0)
    return ar("sub", ar("mul", 2.0, pv), lo) if s > 0 else ar("sub", ar("mul", 2.0, pv), h)


def target_level_ext(fam, p, t, s):
    k = t["type"]
    if k == "prior_close":
        return sess("prev_close", RTH)
    if k == "pivot":
        return _classic(s)
    if k == "fib_ext":
        hi, lo = F("swings", "swing_high", left=3, right=1), F("swings", "swing_low", left=3, right=1)
        rng = ar("sub", hi, lo)
        return ar("add", lo, ar("mul", t["ext"], rng)) if s > 0 else ar("sub", hi, ar("mul", t["ext"], rng))
    if k == "midnight_open":
        return sess("session_open", MID)
    if k == "range_ext":
        ref = _orb_ref(p)
        hi, lo = _ref_level(ref, 1), _ref_level(ref, -1)
        rng = ar("sub", hi, lo)
        return ar("add", lo, ar("mul", t["multiple"], rng)) if s > 0 else ar("sub", hi, ar("mul", t["multiple"], rng))
    return None


def choice_refs(ch) -> dict:
    """Reference windows a sampled filter / target reads (added to the definition and checked for stability)."""
    out = {}
    if ch["regime"] in RTH_REGIMES:
        out[RTH] = RTH_DEF
    if ch["regime"] in FH_REGIMES:
        out[FH] = FH_DEF
    tk = ch["target"]["type"]
    if tk == "prior_close":
        out[RTH] = RTH_DEF
    if tk == "midnight_open":
        out[MID] = MID_DEF
    return out


def regime_cond_for(ch, s: int):
    key = ch["regime"]
    if key not in NEW_REGIMES:
        return P1.regime_cond(key, s)
    if key == "sma200_trend":
        return gt(CLOSE, F("sma", "sma", period=200), s)
    if key == "above_prior_close":
        return gt(CLOSE, dl("prev_day_close"), s)
    o, ph, pl, pc = sess("session_open", RTH), sess("prev_high", RTH), sess("prev_low", RTH), sess("prev_close", RTH)
    rng, gap = ar("sub", ph, pl), ar("sub", o, pc)
    if key == "open_inside_prior_range":
        return ALL(cmp(o, ">", pl), cmp(o, "<", ph))
    if key == "open_outside_prior_range":
        return {"any": [cmp(o, ">", ph), cmp(o, "<", pl)]}
    if key == "gap_large":
        return {"any": [cmp(gap, ">=", ar("mul", GAP_LARGE, rng)), cmp(gap, "<=", ar("mul", -GAP_LARGE, rng))]}
    if key == "gap_small":
        return ALL(cmp(gap, "<", ar("mul", GAP_SMALL, rng)), cmp(gap, ">", ar("mul", -GAP_SMALL, rng)))
    fh = ar("sub", sess("prev_high", FH), sess("prev_low", FH))
    atr60 = F("atr", "atr", "60m", period=14) if ch["timeframe"] != "60m" else F("atr", "atr", period=14)
    if key == "wide_first_hour":
        return cmp(fh, ">=", ar("mul", FH_WIDE, atr60))
    return cmp(fh, "<=", ar("mul", FH_NARROW, atr60))


# =============================================================================== window helpers
_PROBES = (datetime(2024, 1, 10), datetime(2024, 3, 13), datetime(2024, 7, 10), datetime(2024, 10, 30))


def _inside_ny(w: Window, a: str, b: str) -> bool:
    """Is the entry window [entry_start, entry_end) inside [a, b) New York wall clock on every DST probe date?"""
    dur = (mins(w.entry_end) - mins(w.entry_start)) % 1440
    for d in _PROBES:
        t = datetime(d.year, d.month, d.day, mins(w.entry_start) // 60, mins(w.entry_start) % 60, tzinfo=ZoneInfo(w.tz))
        ny = t.astimezone(ZoneInfo(NY))
        x0 = ny.hour * 60 + ny.minute
        if not (mins(a) <= x0 and x0 + dur <= mins(b)):
            return False
    return True


def _approach(level, p, s):
    return ALL(gt(level, CLOSE, s), ge(CLOSE, off(level, p["approach"], -s), s))


# =============================================================================== 25 new families
def _pv(p) -> tuple:
    if p["basis"] == "rth":
        return sess("prev_high", RTH), sess("prev_low", RTH), sess("prev_close", RTH)
    return dl("prev_day_high"), dl("prev_day_low"), dl("prev_day_close")


def _pivot_levels(p) -> tuple:
    """(support, resistance, pivot) for the family's kind / level / basis."""
    h, lo, c = _pv(p)
    rng = ar("sub", h, lo)
    pv = ar("div", ar("add", ar("add", h, lo), c), 3.0)
    if p["kind"] == "classic":
        if p["level"] == 1:
            return ar("sub", ar("mul", 2.0, pv), h), ar("sub", ar("mul", 2.0, pv), lo), pv
        return ar("sub", pv, rng), ar("add", pv, rng), pv
    k = 0.275 if p["level"] == 1 else 0.55                       # Camarilla S3/R3 = C -/+ 1.1/4 range; S4/R4 = 1.1/2
    return ar("sub", c, ar("mul", k, rng)), ar("add", c, ar("mul", k, rng)), c


def _pv_level(p, s):
    sup, res, _ = _pivot_levels(p)
    return hi_lo(s, sup, res) if p["mode"] == "fade" else hi_lo(s, res, sup)


def f_pivot(p, s, ctx):
    lvl = _pv_level(p, s)
    if p["mode"] == "fade":
        return ALL(ge(off(lvl, p["buffer"], s), BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, lvl, s))
    return xup(CLOSE, off(lvl, p["buffer"], s), s)


def f_pivot_stop(p, s, ctx):
    lvl = off(_pv_level(p, s), p["buffer"], s)
    return gt(lvl, CLOSE, s), lvl


def f_pivot_limit(p, s, ctx):
    lvl = _pv_level(p, s)
    return gt(CLOSE, lvl, s), lvl


def r_pivot(p, s, ctx):
    if p["mode"] == "fade":
        return gt(CLOSE, _pivot_levels(p)[2], s)                 # reverted to the central pivot
    return gt(_pv_level(p, s), CLOSE, s)                         # back through the broken level


def _rth_ref(p) -> dict:
    return {RTH: RTH_DEF}


def _vb_anchor(p):
    return sess("session_open", RTH) if p["anchor"] == "rth_open" else dl("day_open")


def _vb_ranges(p) -> list:
    h, lo, c = dl("prev_day_high"), dl("prev_day_low"), dl("prev_day_close")
    if p["range"] == "prev_range":
        return [ar("sub", h, lo)]
    return [ar("sub", h, c), ar("sub", c, lo)]                   # Dual Thrust (1 day): max(H - C, C - L)


def f_vol_breakout(p, s, ctx):
    a = _vb_anchor(p)
    lv = [ar("add" if s > 0 else "sub", a, ar("mul", p["k"], r)) for r in _vb_ranges(p)]
    if len(lv) == 1:
        return xup(CLOSE, lv[0], s)
    # crossing ABOVE max(level1, level2) (long) exactly, without a max operator: now beyond both, before not beyond one
    before = {"any": [ge(lagged(x, 1), BAR("close", 1), s) for x in lv]}
    return ALL(*[gt(CLOSE, x, s) for x in lv], before)


def f_vol_breakout_stop(p, s, ctx):
    a = _vb_anchor(p)
    lvl = ar("add" if s > 0 else "sub", a, ar("mul", p["k"], _vb_ranges(p)[0]))
    return gt(lvl, CLOSE, s), lvl


def vb_refs(p) -> dict:
    return _rth_ref(p) if p["anchor"] == "rth_open" else {}


def f_gap(p, s, ctx):
    o, pc = sess("session_open", RTH), sess("prev_close", RTH)
    rng = ar("sub", sess("prev_high", RTH), sess("prev_low", RTH))
    gap = ar("sub", o, pc)
    want = gt(ar("mul", -s * p["min_gap"], rng), gap, s) if p["mode"] == "fade" else \
        gt(gap, ar("mul", s * p["min_gap"], rng), s)
    return want if p["trigger"] == "immediate" else ALL(want, xup(CLOSE, o, s))


def _inside(k: int) -> dict:
    return ALL(cmp(BAR("high", k + 1), ">", BAR("high", k)), cmp(BAR("low", k), ">", BAR("low", k + 1)))


def f_inside_bar(p, s, ctx):
    if p["mode"] == "break":
        ins = [_inside(1)] + ([_inside(2)] if p["count"] == 2 else [])
        return ALL(*ins, gt(CLOSE, off(BAR(hi_lo(s, "high", "low"), 1), p["buffer"], s), s))
    ins = [_inside(2)] + ([_inside(3)] if p["count"] == 2 else [])        # hikkake: false break of an inside bar
    return ALL(*ins, gt(BAR(hi_lo(s, "low", "high"), 2), BAR(hi_lo(s, "low", "high"), 1), s),
               gt(CLOSE, off(BAR(hi_lo(s, "high", "low"), 2), p["buffer"], s), s))


def f_inside_bar_stop(p, s, ctx):
    ins = [_inside(0)] + ([_inside(1)] if p["count"] == 2 else [])
    return ALL(*ins), off(BAR(hi_lo(s, "high", "low")), p["buffer"], s)


def f_candle(p, s, ctx):
    pat = p["pattern"]
    lo_f = hi_lo(s, "low", "high")
    if pat == "engulfing":
        core = ALL(cmp(F("candle", "direction"), "==", s), cmp(F("candle", "direction", lag=1), "==", -s),
                   ge(CLOSE, BAR("open", 1), s), ge(BAR("close", 1), BAR("open"), s),
                   cmp(F("candle", "abs_body"), ">=", ar("mul", p["strength"], F("candle", "abs_body", lag=1))))
        k = 0
    elif pat == "pin_bar":
        core = cmp(F("candle", hi_lo(s, "lower_wick", "upper_wick")), ">=", ar("mul", p["wick"], F("candle", "range")))
        k = 0
    else:                                                 # three-bar reversal: middle bar is the extreme
        core = ALL(gt(CLOSE, BAR(hi_lo(s, "high", "low"), 1), s), gt(BAR(lo_f, 2), BAR(lo_f, 1), s),
                   gt(BAR(lo_f), BAR(lo_f, 1), s))
        k = 1
    if p["location"] == "n_bar_extreme":
        core = ALL(core, ge(F("donchian", hi_lo(s, "lower", "upper"), lag=k, period=p["n"]), BAR(lo_f, k), s))
    return core


def f_ibs(p, s, ctx):
    thr = p["threshold"] if s > 0 else round(1 - p["threshold"], 4)
    op = "<=" if s > 0 else ">="
    c = cmp(F("candle", "close_pos"), op, thr)
    return ALL(c, cmp(F("candle", "close_pos", lag=1), op, thr)) if p["bars"] == 2 else c


def r_beyond_prev_bar(p, s, ctx):
    return gt(CLOSE, BAR(hi_lo(s, "high", "low"), 1), s)


def f_double_sevens(p, s, ctx):
    return xup(CLOSE, F("donchian", hi_lo(s, "lower", "upper"), period=p["n"]), -s)   # close through the n-bar extreme


def r_double_sevens(p, s, ctx):
    return gt(CLOSE, F("donchian", hi_lo(s, "upper", "lower"), period=p["exit_n"]), s)


def _ts_level(p, s, lag=0):
    return F("donchian", hi_lo(s, "lower", "upper"), lag=lag, period=p["n"])


def _ts_age(p, s, lag=0):
    """The prior n-bar extreme is at least 4 bars old: it is not inside the last 3 bars."""
    return gt(F("donchian", hi_lo(s, "lower", "upper"), lag=lag, period=3), _ts_level(p, s, lag), s)


def f_turtle_soup(p, s, ctx):
    if p["variant"] == "same_bar":
        lvl = _ts_level(p, s)
        core = ALL(gt(lvl, BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, lvl, s))
        age = _ts_age(p, s)
    else:                                                 # "plus one": the previous bar closed beyond, this one is back
        lvl = _ts_level(p, s, 1)
        core = ALL(gt(lvl, BAR("close", 1), s), gt(CLOSE, lvl, s))
        age = _ts_age(p, s, 1)
    return ALL(core, age) if p["min_age"] else core


def f_turtle_soup_stop(p, s, ctx):
    lvl = _ts_level(p, s)
    cond = gt(lvl, CLOSE, s)                              # a new extreme closed beyond the old one: stop order back at it
    return (ALL(cond, _ts_age(p, s)) if p["min_age"] else cond), lvl


def f_keltner_break(p, s, ctx):
    return xup(CLOSE, off(F(p["basis"], p["basis"], period=p["period"]), p["k"], s), s)


def f_keltner_break_stop(p, s, ctx):
    lvl = off(F(p["basis"], p["basis"], period=p["period"]), p["k"], s)
    return _approach(lvl, p, s), lvl


def r_basis(p, s, ctx):
    return gt(F(p["basis"], p["basis"], period=p["period"]), CLOSE, s)


def _squeeze(p, lag: int) -> dict:
    ema = F("ema", "ema", lag=lag, period=20)
    atr = F("atr", "atr", lag=lag, period=14)
    up = F("bollinger", "upper", lag=lag, period=20, k=p["bb_k"])
    lo = F("bollinger", "lower", lag=lag, period=20, k=p["bb_k"])
    return ALL(cmp(up, "<", ar("add", ema, ar("mul", p["kc_k"], atr))),
               cmp(lo, ">", ar("sub", ema, ar("mul", p["kc_k"], atr))))


def f_kc_squeeze(p, s, ctx):
    if p["release"] == "bb_break":
        return ALL(_squeeze(p, 1), xup(CLOSE, F("bollinger", hi_lo(s, "upper", "lower"), period=20, k=p["bb_k"]), s))
    return ALL(_squeeze(p, 1), {"not": _squeeze(p, 0)}, gt(F("roc", "momentum", period=12), 0, s))


def _ribbon(p):
    return [F("ema", "ema", period=int(x)) for x in p["set"].split("-")]


def f_ribbon(p, s, ctx):
    f_, m, sl = _ribbon(p)
    if p["mode"] == "align_cross":
        return ALL(xup(f_, m, s), gt(m, sl, s))
    return ALL(gt(f_, m, s), gt(m, sl, s), ge(f_, BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, f_, s))


def r_ribbon(p, s, ctx):
    f_, m, _ = _ribbon(p)
    return gt(m, f_, s)


def f_macd_hist(p, s, ctx):
    f_, sl, sg = P1._macd_setting(p)
    h = [F("macd", "hist", lag=k, fast=f_, slow=sl, signal=sg) for k in (0, 1, 2)]
    turn = ALL(gt(h[0], h[1], s), gt(h[2], h[1], s))
    return ALL(turn, gt(0, h[1], s)) if p["zone"] == "beyond_zero" else turn


def _fib_level(p, s):
    hi, lo = F("swings", "swing_high", left=p["left"], right=1), F("swings", "swing_low", left=p["left"], right=1)
    rng = ar("sub", hi, lo)
    return ar("sub", hi, ar("mul", p["level"], rng)) if s > 0 else ar("add", lo, ar("mul", p["level"], rng))


def _fib_trend(p, s):
    if p["trend"] == "structure":
        return cmp(F("swings", "trend", left=p["left"], right=1), "==", s)
    return gt(F("ema", "slope", period=50), 0, s)


def f_fib(p, s, ctx):
    lvl = _fib_level(p, s)
    return ALL(_fib_trend(p, s), gt(lvl, BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, lvl, s))


def f_fib_limit(p, s, ctx):
    lvl = _fib_level(p, s)
    return ALL(_fib_trend(p, s), gt(CLOSE, lvl, s)), lvl


def fib_zone(p, s):
    return F("swings", "swing_low" if s > 0 else "swing_high", left=p["left"], right=1)


def _power(p, s, lag=0):
    return ar("sub", BAR(hi_lo(s, "low", "high"), lag), F("ema", "ema", lag=lag, period=p["period"]))


def f_elder(p, s, ctx):
    slope = gt(F("ema", "slope", period=p["period"]), 0, s)
    if p["mode"] == "power_turn":
        return ALL(slope, gt(0, _power(p, s), s), gt(_power(p, s), _power(p, s, 1), s))
    return ALL(slope, xup(_power(p, s), 0, s))


def _im_ref(p) -> str:
    return session_name("FXR", NY, MF, "09:30", hm(mins("09:30") + p["first"]))


def f_intraday_mom(p, s, ctx):
    fw = _im_ref(p)
    base = sess("prev_open", fw) if p["measure"] == "first_window" else sess("prev_close", RTH)
    ret = ar("sub", sess("prev_close", fw), base)
    if p["threshold"] == 0:
        return gt(ret, 0, s)
    return gt(ret, ar("mul", s * p["threshold"], ar("sub", dl("prev_day_high"), dl("prev_day_low"))), s)


def im_window(p) -> Window:
    e0 = mins(p["entry"])
    return Window(NY, MF, hm(e0), hm(e0 + 15), "16:00")


def im_refs(p) -> dict:
    return {_im_ref(p): (NY, MF, "09:30", hm(mins("09:30") + p["first"])), RTH: RTH_DEF}


def f_ict_time(p, s, ctx):
    st = p["setup"]
    if st == "silver_bullet":
        g = "bull" if s > 0 else "bear"
        fp = {"min_size_atr": p["fvg_min_atr"], "max_age_bars": 20}
        edge = F("fvg", f"{g}_top" if s > 0 else f"{g}_bottom", **fp)
        return ALL(cmp(F("fvg", f"{g}_active", **fp), ">=", 1), ge(edge, BAR(hi_lo(s, "low", "high")), s),
                   gt(CLOSE, edge, s))
    if st == "judas_swing":                               # a false move away from the midnight open, then back through it
        mo = sess("session_open", MID)
        return ALL(gt(off(mo, p["k"], -s), sess(hi_lo(s, "session_low", "session_high"), MID), s), xup(CLOSE, mo, s))
    ro = sess("session_open", RTH)                        # London-close reversal of the New York morning move
    return ALL(gt(off(ro, p["k"], -s), CLOSE, s), gt(CLOSE, BAR(hi_lo(s, "high", "low"), 1), s))


def ict_time_window(p) -> Window:
    st = p["setup"]
    if st == "silver_bullet":
        return Window(NY, MF, "10:00", "11:00", "12:00") if p["sb_window"] == "am" else \
            Window(NY, MF, "14:00", "15:00", "16:00")
    if st == "judas_swing":
        return Window(NY, MF, "02:00", "05:00", "08:00") if p["js_window"] == "london" else \
            Window(NY, MF, "09:30", "11:00", "12:00")
    return Window(NY, MF, "10:00", "12:00", "13:00")


def ict_time_refs(p) -> dict:
    return {"judas_swing": {MID: MID_DEF}, "london_close_reversal": {RTH: RTH_DEF}}.get(p["setup"], {})


def ict_time_zone(p, s):
    st = p["setup"]
    if st == "silver_bullet":
        fp = {"min_size_atr": p["fvg_min_atr"], "max_age_bars": 20}
        return F("fvg", "bull_bottom" if s > 0 else "bear_top", **fp)
    if st == "judas_swing":
        return sess("session_low" if s > 0 else "session_high", MID)
    return BAR(hi_lo(s, "low", "high"))


ICT_TIME_PARAMS = {"silver_bullet": {"sb_window", "fvg_min_atr"}, "judas_swing": {"js_window", "k"},
                   "london_close_reversal": {"k"}}
ICT_TIME_ALL = {"sb_window", "fvg_min_atr", "js_window", "k"}


def f_streak(p, s, ctx):
    d = -1 if p["mode"] == "fade" else 1
    return ALL(*[gt(BAR("close", i), BAR("close", i + 1), d * s) for i in range(p["n"])])


def r_streak(p, s, ctx):
    return gt(CLOSE, BAR("close", 1), s) if p["mode"] == "fade" else gt(BAR("close", 1), CLOSE, s)


def _st(p, out):
    return F("supertrend", out, period=p["period"], multiplier=p["multiplier"])


def f_supertrend(p, s, ctx):
    if p["mode"] == "flip":
        return cmp(_st(p, "flip"), "==", s)
    line = _st(p, "supertrend")
    return ALL(cmp(_st(p, "direction"), "==", s), ge(off(line, p["touch_atr"], s), BAR(hi_lo(s, "low", "high")), s),
               gt(CLOSE, line, s))


def _sar(p, out):
    return F("psar", out, step=p["step"], max_step=p["max_step"])


def _ich(p, out):
    a, b, c = (int(x) for x in p["setting"].split("-"))
    return F("ichimoku", out, tenkan=a, kijun=b, senkou_b=c, displacement=b)


def f_ichimoku(p, s, ctx):
    cloud = _ich(p, hi_lo(s, "cloud_top", "cloud_bottom"))
    filt = gt(CLOSE, cloud, s) if p["cloud_filter"] else None
    if p["mode"] == "tk_cross":
        return ALL(xup(_ich(p, "tenkan"), _ich(p, "kijun"), s), filt)
    if p["mode"] == "cloud_break":
        return xup(CLOSE, cloud, s)
    kj = _ich(p, "kijun")
    return ALL(gt(CLOSE, kj, s), ge(kj, BAR(hi_lo(s, "low", "high")), s), filt)


def f_cci(p, s, ctx):
    c = F("cci", "cci", period=p["period"])
    return xup(c, s * p["level"], s) if p["mode"] == "trend_break" else xup(c, -s * p["level"], s)


def r_cci(p, s, ctx):
    c = F("cci", "cci", period=p["period"])
    return gt(0, c, s) if p["mode"] == "trend_break" else gt(c, 0, s)


def f_divergence(p, s, ctx):
    return cmp(F("divergence", "bull" if s > 0 else "bear", rsi_period=p["rsi_period"], left=p["left"],
                 right=p["right"], max_gap=p["max_gap"]), "==", 1)


def _pw(s, far=True):
    return F("weekly_levels", hi_lo(s, "prev_week_high", "prev_week_low") if far else
             hi_lo(s, "prev_week_low", "prev_week_high"))


def f_prev_week(p, s, ctx):
    if p["mode"] == "break":
        return xup(CLOSE, off(_pw(s), p["buffer"], s), s)
    lvl = _pw(s, far=False)
    return ALL(gt(off(lvl, p["buffer"], -s), BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, lvl, s))


def f_prev_week_stop(p, s, ctx):
    lvl = off(_pw(s), p["buffer"], s)
    return _approach(lvl, p, s), lvl


def r_prev_week(p, s, ctx):
    return gt(_pw(s) if p["mode"] == "break" else _pw(s, far=False), CLOSE, s)


def _ama(p, out):
    return F(p["ma"], out if out != "line" else p["ma"], period=p["period"])


def f_adaptive_ma(p, s, ctx):
    return xup(CLOSE, _ama(p, "line"), s) if p["mode"] == "price_cross" else xup(_ama(p, "slope"), 0, s)


def r_adaptive_ma(p, s, ctx):
    return gt(_ama(p, "line"), CLOSE, s) if p["mode"] == "price_cross" else gt(0, _ama(p, "slope"), s)


def f_heikin(p, s, ctx):
    ha = lambda out, lag=0: F("heikin_ashi", out, lag=lag)
    if p["mode"] == "flip":
        return ALL(cmp(ha("direction"), "==", s), cmp(ha("direction", 1), "==", -s))
    if p["mode"] == "streak":
        return cmp(ha("streak"), "==", s * p["n"])
    w = "no_lower_wick" if s > 0 else "no_upper_wick"
    return ALL(cmp(ha(w), "==", 1), cmp(ha(w, 1), "==", 0))


def _dir_lost(feature_out):
    return lambda p, s, c: cmp(feature_out(p), "==", -s)


# --------------------------------------------------------------------------- stop / target lists (pool 2)
def _plus(base: tuple, extra: tuple) -> tuple:
    return tuple(base) + tuple(x for x in extra if x not in base)


T_STOPS, R_STOPS, B_STOPS = (_plus(x, NEW_STOPS) for x in (TREND_STOPS, REVERSION_STOPS, BREAKOUT_STOPS))
T_TGTS, R_TGTS, B_TGTS = (_plus(x, NEW_TARGETS) for x in (TREND_TARGETS, REVERSION_TARGETS, BREAKOUT_TARGETS))
COMMON_STOPS = _plus(("points", "atr", "swing", "recent_extreme", "prev_bar"), NEW_STOPS)
COMMON_TGTS = _plus(("none", "points", "rr", "atr", "swing", "session_level", "ma"), NEW_TARGETS)
ZONE_STOPS = _plus(("points", "atr", "swing", "recent_extreme", "prev_bar", "zone"), NEW_STOPS)
NY_EARLY = ("ny_first30", "ny_open", "ny_morning", "ny_1000_1100")


def _new(num, fid, name, group, hyp, params, entry, reversal=None, **kw) -> Family:
    kw.setdefault("sessions", ALL_SESSIONS)
    kw.setdefault("regimes", REGIMES)
    if reversal is None:
        kw.setdefault("reversal_exit", False)
    return Family(num, fid, name, group, hyp, params, entry, reversal, **kw)


NEW_FAMILIES: tuple[Family, ...] = (
    _new(31, "pivot_points", "Pivot Points", "price_action",
         "Floor-trader pivot levels from the prior session act as support/resistance: price either rejects them or "
         "continues after breaking them.",
         {"kind": ("classic", "camarilla"), "level": (1, 2), "mode": ("fade", "break"), "basis": ("trading_day", "rth"),
          "buffer": (0.0, 0.25)}, f_pivot, r_pivot, stop_entry=f_pivot_stop, limit_entry=f_pivot_limit,
         references=lambda p: _rth_ref(p) if p["basis"] == "rth" else {},
         stops=_plus(("points", "atr", "swing", "recent_extreme", "prev_bar", "day_structure"), NEW_STOPS),
         targets=_plus(("none", "points", "rr", "atr", "prev_day_level", "session_level"), NEW_TARGETS)),
    _new(32, "volatility_breakout", "Volatility Breakout (Larry Williams / Dual Thrust)", "price_action",
         "A move from the session open larger than a fraction of the prior day's range starts a directional day.",
         {"anchor": ("rth_open", "day_open"), "range": ("prev_range", "dual_thrust"), "k": (0.3, 0.5, 0.7, 1.0)},
         f_vol_breakout, lambda p, s, c: gt(_vb_anchor(p), CLOSE, s), stop_entry=f_vol_breakout_stop,
         references=vb_refs, session_domain=lambda p: NY_SESSIONS if p["anchor"] == "rth_open" else ALL_SESSIONS,
         stops=B_STOPS, targets=B_TGTS),
    _new(33, "opening_gap", "Opening Gap (fade / gap-and-go)", "price_action",
         "A gap between the prior regular-session close and today's open either fills (fade) or extends (go).",
         {"mode": ("fade", "go"), "min_gap": (0.1, 0.2, 0.3), "trigger": ("immediate", "through_open")}, f_gap,
         references=_rth_ref, sessions=NY_EARLY, stops=_plus(REVERSION_STOPS, NEW_STOPS),
         targets=_plus(("none", "points", "rr", "atr", "session_level", "prev_day_level"), NEW_TARGETS)),
    _new(34, "inside_bar", "Inside Bar Breakout / Hikkake", "price_action",
         "A bar inside the previous bar stores energy; the break of its range (or a failed break) sets the direction.",
         {"count": (1, 2), "mode": ("break", "fakeout"), "buffer": (0.0, 0.25)}, f_inside_bar,
         stop_entry=f_inside_bar_stop, stops=B_STOPS, targets=B_TGTS),
    _new(35, "candlestick_reversal", "Candlestick Reversal (engulfing / pin bar / three-bar)", "price_action",
         "Classic reversal candles, optionally at an n-bar extreme, mark exhaustion of the prior move.",
         {"pattern": ("engulfing", "pin_bar", "three_bar"), "strength": (1.0, 1.5), "wick": (0.6, 0.7),
          "location": ("any", "n_bar_extreme"), "n": (10, 20)}, f_candle,
         inactive=lambda p: ({"wick"} if p["pattern"] == "engulfing" else {"strength"} if p["pattern"] == "pin_bar"
                             else {"strength", "wick"}) | ({"n"} if p["location"] == "any" else set()),
         confirms=("none", "momentum", "trend"), stops=R_STOPS, targets=COMMON_TGTS),
    _new(36, "ibs_reversion", "Internal Bar Strength Reversion", "price_action",
         "A close near the bar's low (high) tends to be followed by a rebound (pullback).",
         {"threshold": (0.1, 0.15, 0.2, 0.25), "bars": (1, 2)}, f_ibs, r_beyond_prev_bar, reversal_exit=True,
         confirms=("none",), stops=R_STOPS, targets=R_TGTS),
    _new(37, "double_sevens", "Double Sevens (Connors)", "price_action",
         "A close through the n-bar low (high) reverts; exit at the opposite n-bar extreme.",
         {"n": (5, 7, 10), "exit_n": (5, 7, 10)}, f_double_sevens, r_double_sevens, reversal_exit=True,
         confirms=("none",), stops=R_STOPS, targets=R_TGTS),
    _new(38, "turtle_soup", "Turtle Soup (failed breakout)", "price_action",
         "A new n-bar extreme that fails to hold (Raschke) traps breakout traders and reverses.",
         {"n": (10, 20, 55), "variant": ("same_bar", "plus_one"), "min_age": (True, False)}, f_turtle_soup,
         stop_entry=f_turtle_soup_stop, confirms=("none", "candle", "close_strength"), stops=R_STOPS, targets=R_TGTS),
    _new(39, "keltner_breakout", "Keltner Channel Breakout", "technical",
         "A close beyond an ATR channel around a moving average starts a trend leg.",
         {"basis": ("ema", "sma"), "period": (20, 50), "k": (1.5, 2.0, 2.5), "approach": APPROACH_ATR},
         f_keltner_break, r_basis, reversal_exit=True, stop_entry=f_keltner_break_stop, stops=B_STOPS, targets=B_TGTS),
    _new(40, "keltner_squeeze", "Bollinger-inside-Keltner Squeeze", "technical",
         "When the Bollinger bands sit inside the Keltner channel volatility is compressed; the release starts a move.",
         {"kc_k": (1.0, 1.5, 2.0), "bb_k": (1.5, 2.0), "release": ("bb_break", "momentum")}, f_kc_squeeze,
         lambda p, s, c: gt(F("ema", "ema", period=20), CLOSE, s), reversal_exit=True,
         regimes=tuple(r for r in REGIMES if r not in ("bb_compressed", "atr_low")), stops=B_STOPS, targets=B_TGTS),
    _new(41, "ma_ribbon", "Moving-Average Ribbon", "technical",
         "Three aligned EMAs define a trend; a fresh alignment or a pullback to the fast EMA continues it.",
         {"set": ("8-21-55", "5-13-34", "10-20-50", "20-50-100"), "mode": ("align_cross", "pullback")}, f_ribbon,
         r_ribbon, reversal_exit=True, confirms=("none", "candle", "close_strength", "momentum"), stops=T_STOPS,
         targets=T_TGTS),
    _new(42, "macd_histogram", "MACD Histogram Turn", "technical",
         "A turn of the MACD histogram (beyond zero or anywhere) marks a momentum shift before the line cross.",
         {"setting": ("12-26-9", "8-17-9", "5-35-5"), "zone": ("beyond_zero", "any")}, f_macd_hist, P1.r_macd,
         reversal_exit=True, confirms=("none", "candle", "close_strength", "trend"), stops=T_STOPS, targets=T_TGTS),
    _new(43, "fib_pullback", "Fibonacci Pullback", "price_action",
         "In a trend, a retracement to a Fibonacci level of the last swing that holds resumes the trend.",
         {"left": (3, 5), "level": (0.382, 0.5, 0.618), "trend": ("structure", "ema50")}, f_fib,
         lambda p, s, c: cmp(F("swings", "trend", left=p["left"], right=1), "==", -s), reversal_exit=True,
         limit_entry=f_fib_limit, zone_stop=fib_zone, confirms=("none", "candle", "close_strength"), stops=ZONE_STOPS,
         targets=T_TGTS),
    _new(44, "elder_ray", "Elder Ray (bull/bear power)", "technical",
         "With a rising EMA, bear power below zero that turns up (or recovers above zero) marks a buyable dip.",
         {"period": (13, 21, 34), "mode": ("power_turn", "power_cross")}, f_elder,
         lambda p, s, c: gt(0, F("ema", "slope", period=p["period"]), s), reversal_exit=True, stops=T_STOPS,
         targets=T_TGTS),
    _new(45, "intraday_momentum", "Intraday Momentum (first window predicts the close)", "price_action",
         "The return of the first 30/60 minutes (optionally with the overnight move) predicts the direction of the last "
         "part of the regular session (Gao, Han, Li & Zhou 2018).",
         {"first": (30, 60), "measure": ("first_window", "overnight_plus_first"), "threshold": (0.0, 0.1, 0.2),
          "entry": ("15:00", "15:30")}, f_intraday_mom, timeframes=("1m", "5m", "15m"), sessions=(),
         window=im_window, references=im_refs, stops=_plus(("points", "atr", "prev_bar", "day_structure"), NEW_STOPS),
         targets=_plus(("none", "points", "rr", "atr"), NEW_TARGETS)),
    _new(46, "ict_time_models", "ICT Time Models (Silver Bullet / Judas Swing / London-close reversal)", "ict",
         "Time-of-day ICT models: a fair-value-gap reaction in the silver-bullet hour, a false move away from the "
         "midnight open that reverses, and the London-close reversal of the New York morning move.",
         {"setup": ("silver_bullet", "judas_swing", "london_close_reversal"), "sb_window": ("am", "pm"),
          "fvg_min_atr": (0.0, 0.25, 0.5), "js_window": ("london", "ny"), "k": (0.5, 1.0)}, f_ict_time,
         inactive=lambda p: ICT_TIME_ALL - ICT_TIME_PARAMS[p["setup"]], sessions=(), window=ict_time_window,
         references=ict_time_refs, zone_stop=ict_time_zone, confirms=("none", "candle", "close_strength"),
         stops=ZONE_STOPS, targets=T_TGTS),
    _new(47, "consecutive_closes", "Consecutive Closes (streak fade / continuation)", "price_action",
         "After n closes in a row in one direction, price either snaps back (fade) or keeps going (continue).",
         {"n": (3, 4, 5), "mode": ("fade", "continue")}, f_streak, r_streak, reversal_exit=True, confirms=("none",),
         stops=COMMON_STOPS, targets=COMMON_TGTS),
    _new(48, "supertrend", "Supertrend", "technical",
         "An ATR band that ratchets with the trend: a flip starts a trend, a pullback to the band continues it.",
         {"period": (7, 10, 14), "multiplier": (2.0, 3.0, 4.0), "mode": ("flip", "pullback"), "touch_atr": (0.25, 0.5)},
         f_supertrend, _dir_lost(lambda p: _st(p, "direction")), reversal_exit=True,
         inactive=lambda p: {"touch_atr"} if p["mode"] == "flip" else set(),
         zone_stop=lambda p, s: _st(p, "supertrend"), stops=_plus(T_STOPS, ("zone",)), targets=T_TGTS),
    _new(49, "parabolic_sar", "Parabolic SAR", "technical",
         "A stop-and-reverse flip of Wilder's parabolic SAR marks a trend change.",
         {"step": (0.01, 0.02, 0.03), "max_step": (0.1, 0.2, 0.3)},
         lambda p, s, c: cmp(_sar(p, "flip"), "==", s), _dir_lost(lambda p: _sar(p, "direction")), reversal_exit=True,
         zone_stop=lambda p, s: _sar(p, "sar"), stops=_plus(T_STOPS, ("zone",)), targets=T_TGTS),
    _new(50, "ichimoku", "Ichimoku (TK cross / cloud break / Kijun bounce)", "technical",
         "Ichimoku lines and cloud define trend and equilibrium; crosses, cloud breaks and Kijun bounces trade it.",
         {"setting": ("9-26-52", "7-22-44", "10-30-60"), "mode": ("tk_cross", "cloud_break", "kijun_bounce"),
          "cloud_filter": (True, False)}, f_ichimoku, lambda p, s, c: gt(_ich(p, "kijun"), CLOSE, s), reversal_exit=True,
         inactive=lambda p: {"cloud_filter"} if p["mode"] == "cloud_break" else set(),
         zone_stop=lambda p, s: _ich(p, "kijun"), stops=_plus(T_STOPS, ("zone",)), targets=T_TGTS),
    _new(51, "cci", "Commodity Channel Index", "technical",
         "CCI beyond +/-level marks momentum (trend break) or an extreme that reverts when it crosses back.",
         {"period": (14, 20, 30), "level": (100, 150, 200), "mode": ("trend_break", "reversion")}, f_cci, r_cci,
         reversal_exit=True, stops=COMMON_STOPS, targets=COMMON_TGTS),
    _new(52, "rsi_divergence", "RSI Divergence", "technical",
         "A lower price low with a higher RSI low (bullish) or the mirror (bearish) marks fading momentum.",
         {"rsi_period": (9, 14), "left": (3, 5), "right": (1, 2, 3), "max_gap": (30, 60)}, f_divergence,
         confirms=("none", "candle", "close_strength"), stops=R_STOPS, targets=COMMON_TGTS),
    _new(53, "prev_week_breakout", "Previous Week High/Low", "price_action",
         "The prior week's extreme is a key level: a break continues, a sweep that reclaims it reverses.",
         {"mode": ("break", "sweep_reclaim"), "buffer": (0.0, 0.25), "approach": APPROACH_ATR}, f_prev_week,
         r_prev_week, reversal_exit=True, stop_entry=f_prev_week_stop,
         inactive=lambda p: {"approach"} if p["mode"] == "sweep_reclaim" else set(), stops=B_STOPS, targets=B_TGTS),
    _new(54, "adaptive_ma", "Adaptive Moving Average (Hull / KAMA)", "technical",
         "Low-lag (Hull) and adaptive (Kaufman) averages react faster to trend changes than plain averages.",
         {"ma": ("hma", "kama"), "period": (10, 20, 50, 100), "mode": ("price_cross", "slope_turn")}, f_adaptive_ma,
         r_adaptive_ma, reversal_exit=True, confirms=("none", "candle", "close_strength", "momentum"), stops=T_STOPS,
         targets=T_TGTS),
    _new(55, "heikin_ashi", "Heikin-Ashi Trend", "technical",
         "Smoothed Heikin-Ashi candles: a colour flip, an n-candle run or a wickless candle marks trend strength.",
         {"mode": ("flip", "streak", "no_wick"), "n": (2, 3, 4)}, f_heikin,
         lambda p, s, c: cmp(F("heikin_ashi", "direction"), "==", -s), reversal_exit=True,
         inactive=lambda p: {"n"} if p["mode"] != "streak" else set(), stops=T_STOPS, targets=T_TGTS),
)

# Order types that would contradict a family variant (e.g. a buy STOP below the market for a fade) are refused.
ORDER_RULES = {
    "pivot_points": lambda p, o: {"fade": "stop", "break": "limit"}.get(p["mode"]) == o,
    "volatility_breakout": lambda p, o: o == "stop" and p["range"] == "dual_thrust",
    "inside_bar": lambda p, o: o == "stop" and p["mode"] == "fakeout",
    "prev_week_breakout": lambda p, o: o == "stop" and p["mode"] == "sweep_reclaim",
}


# =============================================================================== the 30 existing families, extended
def f_session_hl2(p, s, ctx):
    return xup(CLOSE, off(_ref_level(_ref_name2(p["reference"]), s), p["buffer"], s), s)


def f_session_hl_stop2(p, s, ctx):
    lvl = off(_ref_level(_ref_name2(p["reference"]), s), p["buffer"], s)
    return P1._approach(lvl, p, s), lvl


def _ref_name2(key: str) -> str:
    tz, wd, a, b = REFERENCE_WINDOWS[key]
    return session_name("FXR", tz, wd, a, b)


def ref_refs2(p) -> dict:
    return {_ref_name2(p["reference"]): REFERENCE_WINDOWS[p["reference"]]}


def _ict_sessions2(p):
    out = P1.ict_sessions(p)
    return ALL_SESSIONS if out is P1.ALL_SESSIONS else out


def _extend(fam: Family) -> Family:
    kw: dict = {"regimes": _plus(fam.regimes, NEW_REGIMES),
                "stops": _plus(fam.stops, NEW_STOPS + (("range_mid",) if fam.fid == "opening_range_breakout" else ())),
                "targets": _plus(fam.targets, NEW_TARGETS + (("range_ext",) if fam.fid == "opening_range_breakout"
                                                              else ()))}
    if fam.sessions == P1.ALL_SESSIONS:
        kw["sessions"] = ALL_SESSIONS
    elif fam.fid == "overnight_range_breakout":
        kw["sessions"] = _plus(fam.sessions, ("ny_1000_1100", "ny_lunch", "ny_last30", "london_close"))
    if fam.fid == "session_hl_breakout":
        kw.update(params={**fam.params, "reference": fam.params["reference"] + ("asia_ny", "ny_first_hour")},
                  entry=f_session_hl2, stop_entry=f_session_hl_stop2, references=ref_refs2,
                  reversal=lambda p, s, c: gt(_ref_level(_ref_name2(p["reference"]), s), CLOSE, s))
    if fam.fid == "ict_liquidity_fvg":
        kw["session_domain"] = _ict_sessions2
    return replace(fam, **kw)


EXISTING_FAMILIES = tuple(_extend(f) for f in P1.FAMILIES)
FAMILIES: tuple[Family, ...] = EXISTING_FAMILIES + NEW_FAMILIES
FAMILY_BY_ID = {f.fid: f for f in FAMILIES}
ICT_PARAMS = P1.ICT_PARAMS                    # the generator pins the ICT family spec with this (unchanged setups)
NEW_FAMILY_IDS = frozenset(f.fid for f in NEW_FAMILIES)


# =============================================================================== validation hook
def _entry_window(fam: Family, ch) -> Window:
    return fam.window(ch["family_params"]) if fam.window else SESSION_PRESETS[ch["session"]]


def uses_new_value(fam: Family, ch) -> list[str]:
    """Which NEW pool-2 variable values a choice uses (empty: it could have been drawn from pool 1's space)."""
    out = []
    if ch.get("session") in NEW_VALUES["session"]:
        out.append(f"session={ch['session']}")
    if ch["regime"] in NEW_VALUES["regime"]:
        out.append(f"regime={ch['regime']}")
    if ch["stop"]["type"] in NEW_VALUES["stop"]:
        out.append(f"stop={ch['stop']['type']}")
    if ch["target"]["type"] in NEW_VALUES["target"]:
        out.append(f"target={ch['target']['type']}")
    if ch["order"]["type"] in NEW_VALUES["order"]:
        out.append(f"order={ch['order']['type']}")
    if ch["entry_delay"] in NEW_VALUES["entry_delay"]:
        out.append(f"entry_delay={ch['entry_delay']}")
    if ch["time_exit"] in NEW_VALUES["time_exit"]:
        out.append(f"time_exit={ch['time_exit']}")
    if ch["mtf"] and ch["mtf"]["htf"] in NEW_VALUES["htf"]:
        out.append(f"htf={ch['mtf']['htf']}")
    if ch["family_params"].get("reference") in NEW_VALUES["reference"]:
        out.append(f"reference={ch['family_params']['reference']}")
    return out


def extra_checks(fam: Family, ch) -> None:
    rule = ORDER_RULES.get(fam.fid)
    otype = ch["order"]["type"]
    if rule and rule(ch["family_params"], otype):
        raise Reject("entry_exit", "ORDER_TYPE_NOT_APPLICABLE", f"{otype} entry contradicts {fam.fid} {ch['family_params']}")
    w = _entry_window(fam, ch)
    if ch["regime"] in RTH_REGIMES and not _inside_ny(w, "09:30", "16:00"):
        raise Reject("entry_exit", "FILTER_NEEDS_RTH_ENTRY", f"{ch['regime']} reads today's regular-session open")
    if ch["regime"] in FH_REGIMES and not _inside_ny(w, "10:30", "16:00"):
        raise Reject("entry_exit", "FILTER_NEEDS_COMPLETED_FIRST_HOUR", f"{ch['regime']} needs today's first hour")
    if ch["target"]["type"] == "midnight_open" and not _inside_ny(w, "00:00", "16:00"):
        raise Reject("stop_target", "TARGET_NEEDS_MIDNIGHT_SESSION", "the midnight open exists 00:00-16:00 New York")
    if fam.fid not in NEW_FAMILY_IDS and not uses_new_value(fam, ch):
        raise Reject("parameter_domain", "POOL2_NO_NEW_VARIABLE",
                     "an existing-family variant in pool 2 must use at least one new pool-2 variable value")


# =============================================================================== allocation (frozen)
GROUPS = P1.GROUPS
MIN_PER_FAMILY = EXISTING_FAMILY_TOTAL // len(EXISTING_FAMILIES)            # 133
ALLOCATION_METHOD = {
    "version": ALLOCATION_VERSION, "results_used": False,
    "rule": f"{NEW_FAMILY_TOTAL} strategies split equally over the {len(NEW_FAMILIES)} new families "
            f"({NEW_FAMILY_TOTAL // len(NEW_FAMILIES)} each); {EXISTING_FAMILY_TOTAL} split equally over the "
            f"{len(EXISTING_FAMILIES)} existing families (largest remainder; the extra strategies go to the lowest "
            "family numbers). Chosen by the user before any evaluation.",
    "group_rationale": {"note": "pool 2 uses equal shares per family (no structural scores)"},
    "criteria": [], "criteria_note": "no scores: equal shares",
}


def allocate() -> dict[str, int]:
    out: dict[str, int] = {}
    per_new, rem_new = divmod(NEW_FAMILY_TOTAL, len(NEW_FAMILIES))
    per_old, rem_old = divmod(EXISTING_FAMILY_TOTAL, len(EXISTING_FAMILIES))
    for i, f in enumerate(sorted(EXISTING_FAMILIES, key=lambda f: f.num)):
        out[f.fid] = per_old + (1 if i < rem_old else 0)
    for i, f in enumerate(sorted(NEW_FAMILIES, key=lambda f: f.num)):
        out[f.fid] = per_new + (1 if i < rem_new else 0)
    assert sum(out.values()) == TARGET_TOTAL
    return out


GROUP_SHARE = {g: sum(n for fid, n in allocate().items() if FAMILY_BY_ID[fid].group == g) for g in GROUPS}


def dimension_catalog() -> dict:
    d = P1.dimension_catalog()
    d["mtf"] = {**d["mtf"], "pairs": {k: list(v) for k, v in MTF_PAIRS.items()}}
    d["session"] = {k: {"timezone": w.tz, "entry": [w.entry_start, w.entry_end], "flat": w.flat}
                    for k, w in SESSION_PRESETS.items()}
    d["reference_windows"] = {k: list(v) for k, v in REFERENCE_WINDOWS.items()}
    d["entry"] = {**d["entry"], "delay_bars": list(ENTRY_DELAYS),
                  "order": ["market", "stop", "limit"] + list(EXTRA_ORDER_TYPES)}
    d["stop"] = {k: {p: list(v) for p, v in fn(5).items()} for k, fn in STOP_DOMAINS.items()} | \
        {"range_side": {"buffer": list(BUFFERS_ATR)}}
    d["target"] = {k: {p: list(v) for p, v in fn(5).items()} for k, fn in TARGET_DOMAINS.items()}
    d["time_exit_minutes"] = [str(k) for k in TIME_EXITS]
    d["regime"] = list(REGIMES)
    d["sizing"] = list(SIZING)
    d["pool2_new_values"] = {k: sorted(map(str, v)) for k, v in NEW_VALUES.items()}
    d["pool2_thresholds"] = {"gap_large": GAP_LARGE, "gap_small": GAP_SMALL, "wide_first_hour_atr60": FH_WIDE,
                             "narrow_first_hour_atr60": FH_NARROW}
    return d


def capability_matrix() -> dict:
    from edgelab.strategy import capabilities
    return {"pool1": capabilities.matrix(), "pool2_additions": [
        {"dimension": "new indicators", "status": "executable",
         "factory": "supertrend, psar, ichimoku, cci, divergence, weekly_levels, hma, kama, heikin_ashi",
         "evidence": ["edgelab.features.library.trend_extra"],
         "note": "registered causal features (registry-wide truncation test + tests/test_features_pool2.py)"},
        {"dimension": "resting order at the signal bar", "status": "executable", "factory": "bar_stop, bar_limit",
         "evidence": ["edgelab.engine.fills:find_entry"], "note": "ordinary stop / limit entries (DSL entry.order)"},
        {"dimension": "pool-2 filters / targets / stops", "status": "executable",
         "factory": ", ".join(NEW_REGIMES + NEW_TARGETS + NEW_STOPS + ("range_mid", "range_ext")),
         "evidence": ["edgelab.strategy.compiler:_Eval"], "note": "expressions over registered causal features"},
        {"dimension": "fixed-contract sizing in pool 2", "status": "not_executable", "factory": "NOT varied",
         "evidence": [], "note": "left out by the user's choice (risk-based sizing only)"}]}
