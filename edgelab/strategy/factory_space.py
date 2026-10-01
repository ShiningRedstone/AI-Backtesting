"""Strategy-factory variation space (ADR-60): the 30-family universe, shared dimensions, allocation.

Everything here is DATA + small deterministic builders that emit Strategy-DSL fragments. Nothing
here evaluates performance. Numeric domains (point distances, thresholds, lookbacks) are research
DESIGN choices, declared before any evaluation; they are not market data and imply no result.

A candidate is a ``choice`` (one value per applicable dimension). ``factory.build_definition``
turns it into a DSL document; ``factory.validate_candidate`` decides whether it is admissible.
Values that the existing DSL/engine cannot execute (trailing stops, breakeven, equity-based sizing,
volume-dependent levels on a feed whose volume is not exchange volume, exit-based re-entry rules)
are DECLARED with ``status: not_executable`` and a reason, and are rejected by name if requested -
they are never approximated.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

VARIATION_SPACE_VERSION = "edgelab-dt-space/5"
ALLOCATION_VERSION = "edgelab-dt-allocation/1"
TARGET_TOTAL = 10_000
NY, LDN, TYO = "America/New_York", "Europe/London", "Asia/Tokyo"
MF = ("mon", "tue", "wed", "thu", "fri")
SU_TH = ("sun", "mon", "tue", "wed", "thu")
TZ_CODE = {NY: "NY", LDN: "LDN", TYO: "TYO"}
TIMEFRAMES = ("1m", "5m", "15m", "30m", "60m")          # all derivable from the canonical 1m data
MTF_PAIRS = {"1m": ("5m", "15m"), "5m": ("15m", "30m", "60m"), "15m": ("60m",), "30m": ("60m",), "60m": ()}


# =============================================================================== DSL fragments
def F(fid: str, out: str, tf: str | None = None, lag: int = 0, **params) -> dict:
    d: dict = {"feature": fid, "params": dict(params), "output": out}
    if tf:
        d["timeframe"] = tf
    if lag:
        d["lag"] = lag
    return d


def BAR(field_: str = "close", lag: int = 0) -> dict:
    return {"bar": field_, "lag": lag} if lag else {"bar": field_}


CLOSE = BAR("close")
ATR = F("atr", "atr", period=14)


def cmp(a, op, b) -> dict:
    return {"left": a, "op": op, "right": b}


def gt(a, b, s: int) -> dict:
    """a > b for longs (s=+1); a < b for shorts (s=-1)."""
    return cmp(a, ">" if s > 0 else "<", b)


def ge(a, b, s: int) -> dict:
    return cmp(a, ">=" if s > 0 else "<=", b)


def xup(a, b, s: int) -> dict:
    """a crosses above b (long) / below b (short)."""
    return cmp(a, "crosses_above" if s > 0 else "crosses_below", b)


def ar(op: str, a, b) -> dict:
    return {"arith": op, "args": [a, b]}


def off(level, k: float, s: int, unit=ATR) -> Any:
    """level + s*k*unit, emitted WITHOUT no-op arithmetic when k == 0 (cosmetic forms never differ)."""
    if k == 0:
        return level
    return ar("add" if s > 0 else "sub", level, ar("mul", k, unit))


def ALL(*conds) -> dict | None:
    items = [c for c in conds if c is not None]
    if not items:
        return None
    return items[0] if len(items) == 1 else {"all": items}


def lagged(node: Any, k: int) -> Any:
    """Every bar/feature operand shifted k bars further back (entry delay). Constants unchanged."""
    if k == 0 or not isinstance(node, (Mapping, list)):
        return node
    if isinstance(node, list):
        return [lagged(x, k) for x in node]
    if "bar" in node or "feature" in node:
        return {**node, "lag": node.get("lag", 0) + k}
    return {kk: lagged(v, k) for kk, v in node.items()}


def hi_lo(s: int, hi, lo):
    return hi if s > 0 else lo


class Reject(Exception):
    """A candidate is inadmissible. ``code`` is machine-readable; ``stage`` names the validation stage."""

    def __init__(self, stage: str, code: str, detail: str = ""):
        self.stage, self.code, self.detail = stage, code, detail
        super().__init__(f"{stage}:{code}: {detail}")

    def to_dict(self) -> dict:
        return {"stage": self.stage, "code": self.code, "detail": self.detail}


# =============================================================================== sessions
@dataclass(frozen=True)
class Window:
    tz: str
    weekdays: tuple
    entry_start: str
    entry_end: str
    flat: str                       # strategy flat time (local wall clock of tz)


SESSION_PRESETS: dict[str, Window] = {
    "asia":              Window(TYO, MF, "09:00", "12:00", "15:00"),
    "london_morning":    Window(LDN, MF, "08:00", "10:00", "11:00"),
    "london":            Window(LDN, MF, "08:00", "14:00", "16:00"),
    "london_ny_overlap": Window(NY, MF, "08:00", "11:00", "12:00"),
    "ny_first30":        Window(NY, MF, "09:30", "10:00", "11:00"),
    "ny_open":           Window(NY, MF, "09:30", "10:30", "12:00"),
    "ny_morning":        Window(NY, MF, "09:30", "11:30", "13:00"),
    "ny_1000_1400":      Window(NY, MF, "10:00", "14:00", "15:00"),
    "ny_afternoon":      Window(NY, MF, "12:00", "14:30", "15:30"),
    "ny_last90":         Window(NY, MF, "14:30", "15:30", "16:00"),
    "ny_rth":            Window(NY, MF, "09:30", "15:30", "16:00"),
}
NY_SESSIONS = ("ny_first30", "ny_open", "ny_morning", "ny_1000_1400", "ny_afternoon", "ny_last90", "ny_rth")
ALL_SESSIONS = tuple(SESSION_PRESETS)
# Reference windows whose levels (prev_high / prev_low of the last COMPLETED instance) families trade.
REFERENCE_WINDOWS: dict[str, tuple] = {         # tz, weekdays, start, end
    "asia_tokyo":     (TYO, MF, "09:00", "15:00"),
    "london_am":      (LDN, MF, "08:00", "11:00"),
    "ny_am":          (NY, MF, "09:30", "12:00"),
    "prior_ny_rth":   (NY, MF, "09:30", "16:00"),
    "overnight_full": (NY, SU_TH, "18:00", "09:30"),
    "premarket":      (NY, MF, "04:00", "09:30"),
}


def hm(m: int) -> str:
    m %= 1440
    return f"{m // 60:02d}:{m % 60:02d}"


def mins(t: str) -> int:
    h, m = t.split(":")
    return int(h) * 60 + int(m)


def session_name(prefix: str, tz: str, weekdays: tuple, start: str, end: str) -> str:
    wd = "MF" if tuple(weekdays) == MF else "".join(d[:2].upper() for d in weekdays)
    return f"{prefix}_{TZ_CODE[tz]}_{start.replace(':', '')}_{end.replace(':', '')}_{wd}"


def session_def(tz: str, weekdays: tuple, start: str, end: str) -> dict:
    d = {"timezone": tz, "start": start, "end": end}
    if tuple(weekdays) != MF:
        d["weekdays"] = list(weekdays)
    return d


# =============================================================================== shared dimensions
# weights are sampling weights (declared, frozen with the allocation version)
DIRECTIONS = {"both": 0.5, "long": 0.25, "short": 0.25}
WEEKDAY_SETS = {"all": (0.55, None), "mon": (0.05, ["mon"]), "tue": (0.05, ["tue"]), "wed": (0.05, ["wed"]),
                "thu": (0.05, ["thu"]), "fri": (0.05, ["fri"]), "mon_thu": (0.07, ["mon", "tue", "wed", "thu"]),
                "tue_fri": (0.07, ["tue", "wed", "thu", "fri"]), "tue_thu": (0.06, ["tue", "wed", "thu"])}
FLAT_RULES = {"session_flat": (0.7, 0), "early_flat_30": (0.3, 30)}
TIME_EXITS = {"window": 0.35, 30: 0.15, 60: 0.2, 120: 0.2, 240: 0.1}   # minutes in trade (max_hold_bars)
COOLDOWNS = {"none": (0.5, None), "15min": (0.15, 15), "30min": (0.15, 30), "60min": (0.1, 60),
             "one_per_window": (0.1, "window")}
CONFIRMS = ("none", "candle", "close_strength", "momentum", "trend")
ENTRY_DELAYS = {0: 0.75, 1: 0.25}
STOP_POINTS = {1: (5, 8, 12), 5: (10, 15, 25), 15: (15, 25, 40), 30: (25, 40, 60), 60: (30, 50, 80)}
TARGET_POINTS = {1: (8, 15, 25), 5: (15, 30, 50), 15: (25, 50, 80), 30: (40, 75, 120), 60: (50, 100, 160)}
STOP_ATR = (1.0, 1.5, 2.0, 3.0)
TARGET_ATR = (1.0, 1.5, 2.0, 3.0, 4.0)
RR = (1.0, 1.5, 2.0, 3.0)
BUFFERS_ATR = (0.0, 0.25, 0.5)
ORDER_EXPIRY = (1, 3, 6)
APPROACH_ATR = (0.5, 1.0)
# Sizing variants for the $50,000 research account (whole MNQ contracts, ADR-63). Every risk-based variant carries a hard
# cap of RISK_QUANTITY_CAP micros so a small stop cannot request a quantity the 50K prop accounts cannot hold. The cap is a
# strategy design parameter (part of its identity); the prop layer still checks the real provider limit and reports any
# violation explicitly. NOTE: 40 is the figure stated for the 50K accounts in the task; it is UNVERIFIED until the prop rule
# profiles are verified against the providers' official documentation.
RISK_QUANTITY_CAP = 40
SIZING = {"fixed_1": 0.22, "fixed_5": 0.12, "fixed_10": 0.08,
          "risk_125": 0.12, "risk_250": 0.14, "risk_500": 0.10,
          "eq_025": 0.07, "eq_05": 0.09, "eq_10": 0.06}
FIXED_QUANTITY = {"fixed_1": 1, "fixed_5": 5, "fixed_10": 10}
RISK_USD = {"risk_125": 125.0, "risk_250": 250.0, "risk_500": 500.0}             # 0.25% / 0.5% / 1% of $50,000
EQUITY_RISK = {"eq_025": 0.25, "eq_05": 0.5, "eq_10": 1.0}                       # percent of realised equity at the signal
EXECUTION_CONTRACT = "MNQ"         # the traded contract (whole contracts); its spec lives ONLY in configs/instruments.yaml
MAX_TRADES = {0: 0.6, 1: 0.2, 2: 0.12, 3: 0.08}                       # 0 = no per-strategy cap
REENTRY = {"none": 0.7, "block_after_stop": 0.12, "block_after_target": 0.06, "cooldown_after_exit": 0.12}
REENTRY_MINUTES = (15, 30, 60)
NO_PROGRESS = {"none": 0.8, "yes": 0.2}
NO_PROGRESS_BARS = (3, 6, 12)
NO_PROGRESS_R, NO_PROGRESS_ATR = (0.25, 0.5), (0.5, 1.0)
REGIMES = ("none", "atr_high", "atr_low", "adx_trend", "adx_range", "bb_compressed", "ma_alignment",
           "prior_day_direction", "overnight_gap", "above_day_open", "prior_day_nr")
MTF_FILTERS = ("htf_ema_trend", "htf_ema_slope", "htf_rsi", "htf_structure", "htf_vol_regime",
               "htf_range_position", "htf_breakout")

NOT_EXECUTABLE = {   # declared variation values that are NOT executed (refused by name, never approximated)
    "trailing": {"fixed_ticks": "EQUIVALENT_REPRESENTATION"},      # ticks = points x tick size: offered once, as points
    "sizing": {"tiered_scaling": "ENGINE_UNSUPPORTED_SCALING", "drawdown_scaled": "EXCLUDED_ARBITRARY_TIER_TABLE"},
    "target": {"vwap": "DATA_UNSUPPORTED_VOLUME", "trailing_target": "ENGINE_UNSUPPORTED_SCALING"},
    "stop": {"fixed_ticks": "EQUIVALENT_REPRESENTATION"},
    "regime": {"vwap_distance": "DATA_UNSUPPORTED_VOLUME", "prior_day_range_expansion": "FEATURE_NOT_AVAILABLE",
               "event_news": "DATA_UNSUPPORTED_EVENTS"},
    "time_exit": {},
    "frequency": {"one_direction_per_session": "ENGINE_UNSUPPORTED_PATH_STATE"},
}
NOT_EXECUTABLE_REASONS = {
    "ENGINE_UNSUPPORTED_SCALING": "one fill in, one fill out (no pyramiding, scaling or partial exits; the target is fixed)",
    "EXCLUDED_ARBITRARY_TIER_TABLE": "equity-tier / drawdown-scaling tables would be arbitrary combinatorial additions; "
                                     "dynamic risk is covered by equity_risk (pct of equity at the signal)",
    "DATA_UNSUPPORTED_VOLUME": "the canonical Dukascopy feed's volume is provider-defined decimal volume, NOT CME "
                               "exchange volume; volume-weighted levels are refused by the feature engine",
    "EQUIVALENT_REPRESENTATION": "compiles identically to another value (e.g. ticks = points x tick size); offered once",
    "FEATURE_NOT_AVAILABLE": "no registered causal feature computes it",
    "DATA_UNSUPPORTED_EVENTS": "no supported event dataset exists; event information is never fabricated",
    "ENGINE_UNSUPPORTED_PATH_STATE": "needs per-session direction state the signal layer does not have",
}


# =============================================================================== stops / targets
def stop_operand(kind: str, prm: Mapping, s: int) -> dict:
    """(DSL stop block) for side s. Price stops sit on the losing side of the entry."""
    if kind == "points":
        return {"type": "points", "points": float(prm["points"])}
    if kind == "atr":
        return {"type": "atr", "multiple": float(prm["multiple"]), "period": 14}
    b = prm.get("buffer", 0.0)
    lvl = {
        "swing": lambda: hi_lo(s, F("swings", "swing_low", left=prm["left"], right=prm["right"]),
                               F("swings", "swing_high", left=prm["left"], right=prm["right"])),
        "recent_extreme": lambda: hi_lo(s, F("donchian", "lower", period=prm["n"]), F("donchian", "upper", period=prm["n"])),
        "prev_bar": lambda: hi_lo(s, BAR("low", 1), BAR("high", 1)),
        "day_structure": lambda: hi_lo(s, F("daily_levels", "day_low"), F("daily_levels", "day_high")),
        "ma": lambda: F("ema", "ema", period=prm["period"]),
        "channel": lambda: hi_lo(s, F("bollinger", "lower", period=20, k=2.0), F("bollinger", "upper", period=20, k=2.0)),
        "chandelier": lambda: hi_lo(s, F("donchian", "upper", period=prm["n"]), F("donchian", "lower", period=prm["n"])),
    }[kind]()
    if kind == "chandelier":
        return off(lvl, prm["multiple"], -s)
    if kind == "ma":
        return off(lvl, prm["multiple"], -s)
    return off(lvl, b, -s)


def target_operand(kind: str, prm: Mapping, s: int) -> dict | None:
    return {
        "prev_day_level": lambda: hi_lo(s, F("daily_levels", "prev_day_high"), F("daily_levels", "prev_day_low")),
        "swing": lambda: hi_lo(s, F("swings", "swing_high", left=3, right=1), F("swings", "swing_low", left=3, right=1)),
        "session_level": lambda: hi_lo(s, F("daily_levels", "day_high"), F("daily_levels", "day_low")),
        "ma": lambda: F("sma", "sma", period=prm["period"]),
        "band_mid": lambda: F("bollinger", "mid", period=20, k=2.0),
        "band_far": lambda: hi_lo(s, F("bollinger", "upper", period=20, k=2.0), F("bollinger", "lower", period=20, k=2.0)),
        "range_opposite": lambda: hi_lo(s, F("donchian", "upper", period=prm["n"]), F("donchian", "lower", period=prm["n"])),
    }[kind]()


STOP_DOMAINS: dict[str, Callable[[int], dict]] = {    # kind -> tf_minutes -> {param: values}
    "points": lambda tf: {"points": STOP_POINTS[tf]},
    "atr": lambda tf: {"multiple": STOP_ATR},
    "swing": lambda tf: {"left": (3, 5), "right": (1, 2), "buffer": BUFFERS_ATR},
    "recent_extreme": lambda tf: {"n": (3, 5, 10), "buffer": BUFFERS_ATR},
    "prev_bar": lambda tf: {"buffer": BUFFERS_ATR},
    "day_structure": lambda tf: {"buffer": BUFFERS_ATR},
    "ma": lambda tf: {"period": (20, 50), "multiple": (0.5, 1.0)},
    "channel": lambda tf: {"buffer": BUFFERS_ATR},
    "chandelier": lambda tf: {"n": (10, 22), "multiple": (2.0, 3.0)},
    "zone": lambda tf: {"buffer": BUFFERS_ATR},
}
TARGET_DOMAINS: dict[str, Callable[[int], dict]] = {
    "none": lambda tf: {},
    "points": lambda tf: {"points": TARGET_POINTS[tf]},
    "rr": lambda tf: {"multiple": RR},
    "atr": lambda tf: {"multiple": TARGET_ATR},
    "prev_day_level": lambda tf: {},
    "swing": lambda tf: {},
    "session_level": lambda tf: {},
    "ma": lambda tf: {"period": (20, 50)},
    "band_mid": lambda tf: {},
    "band_far": lambda tf: {},
    "range_opposite": lambda tf: {"n": (20, 50)},
}
TREND_STOPS = ("points", "atr", "swing", "recent_extreme", "prev_bar", "ma", "chandelier")
REVERSION_STOPS = ("points", "atr", "swing", "recent_extreme", "prev_bar", "channel", "day_structure")
BREAKOUT_STOPS = ("points", "atr", "swing", "recent_extreme", "prev_bar", "day_structure", "chandelier")
TREND_TARGETS = ("none", "points", "rr", "atr", "prev_day_level", "swing", "session_level")
REVERSION_TARGETS = ("none", "points", "rr", "atr", "ma", "band_mid", "range_opposite")
BREAKOUT_TARGETS = ("none", "points", "rr", "atr", "prev_day_level", "swing", "session_level", "band_far")


# =============================================================================== trailing (ADR-61, real engine support)
TRAIL_KINDS = {"none": 0.35, "fixed_points": 0.12, "atr": 0.12, "breakeven": 0.08, "prev_bar": 0.07, "swing": 0.08,
               "ma": 0.06, "channel": 0.06, "chandelier": 0.06}
LEVEL_TRAILS = ("prev_bar", "swing", "ma", "channel", "chandelier")      # stop follows a strategy level
TRAIL_POINTS = {1: (3, 5, 8), 5: (6, 10, 15), 15: (10, 15, 25), 30: (15, 25, 40), 60: (20, 30, 50)}
TRAIL_ATR = (1.0, 1.5, 2.0, 3.0)
TRAIL_ACTIVATION = {"immediate": 0.45, "r": 0.25, "atr": 0.15, "points": 0.15}
TRAIL_ACT_R, TRAIL_ACT_ATR = (0.5, 1.0, 1.5), (1.0, 2.0)
BE_TRIGGER_R, BE_TRIGGER_ATR = (0.5, 1.0, 1.5), (1.0, 2.0)
BE_OFFSET = (0.0, 2.0)
TRAIL_EVERY = {1: 0.7, 3: 0.3}
TRAIL_STEP = {"none": 0.8, "step": 0.2}
TRAIL_STEP_POINTS = {1: 1.0, 5: 2.0, 15: 3.0, 30: 5.0, 60: 8.0}
TRAIL_BUFFERS = (0.0, 0.25)


def trailing_domain(kind: str, tfm: int) -> dict:
    """Kind-specific parameter domains ({param: values})."""
    return {"fixed_points": lambda: {"points": TRAIL_POINTS[tfm]},
            "atr": lambda: {"multiple": TRAIL_ATR},
            "breakeven": lambda: {},
            "prev_bar": lambda: {"buffer": TRAIL_BUFFERS},
            "swing": lambda: {"left": (3, 5), "buffer": BUFFERS_ATR},
            "ma": lambda: {"period": (20, 50), "buffer": TRAIL_BUFFERS},
            "channel": lambda: {"channel": ("bollinger", "donchian"), "n": (10, 20)},
            "chandelier": lambda: {"n": (10, 22), "multiple": (2.0, 3.0)}}[kind]()


def trail_level(tr: Mapping, s: int) -> dict:
    """The per-bar candidate stop of a LEVEL trail for side s (long below the market, short above)."""
    k = tr["type"]
    if k == "prev_bar":
        return off(BAR(hi_lo(s, "low", "high")), tr["buffer"], -s)
    if k == "swing":
        return off(F("swings", hi_lo(s, "swing_low", "swing_high"), left=tr["left"], right=1), tr["buffer"], -s)
    if k == "ma":
        return off(F("ema", "ema", period=tr["period"]), tr["buffer"], -s)
    if k == "channel":
        if tr["channel"] == "bollinger":
            return F("bollinger", hi_lo(s, "lower", "upper"), period=tr["n"] * 2, k=2.0)
        return F("donchian", hi_lo(s, "lower", "upper"), period=tr["n"])
    # chandelier: the highest high of the previous n bars (short: lowest low) less/plus multiple x ATR
    return off(F("donchian", hi_lo(s, "upper", "lower"), period=tr["n"]), tr["multiple"], -s)


def trailing_block(tr: Mapping, sides: tuple) -> dict | None:
    """Sampled trailing choice -> DSL ``exit.trailing`` (None for 'none')."""
    k = tr["type"]
    if k == "none":
        return None
    uses_atr = False
    out: dict = {}
    be = tr.get("breakeven")                                  # breakeven-only kind, or breakeven before the trail
    if k == "breakeven":
        out["mode"] = "breakeven"
        be = {"trigger": tr["trigger"], "offset": tr["offset"]}
    else:
        out["mode"] = "distance" if k in ("fixed_points", "atr") else "level"
        if k == "fixed_points":
            out["distance"] = {"type": "points", "points": float(tr["points"])}
        elif k == "atr":
            out["distance"] = {"type": "atr", "multiple": float(tr["multiple"])}
            uses_atr = True
        else:
            out["level"] = {side: trail_level(tr, 1 if side == "long" else -1) for side in sides}
        act = tr["activation"]
        if act["type"] != "immediate":
            out["activation"] = {"type": act["type"], "value": float(act["value"])}
            uses_atr |= act["type"] == "atr"
        up = {}
        if tr["every_bars"] != 1:
            up["every_bars"] = int(tr["every_bars"])
        if tr["only_new_extreme"]:
            up["only_new_extreme"] = True
        if tr["min_step"]:
            up["min_step_points"] = float(tr["min_step"])
        if up:
            out["update"] = up
    if be:
        trg = be["trigger"]
        out["breakeven"] = {"trigger": {"type": trg["type"], "value": float(trg["value"])}}
        if be["offset"]:
            out["breakeven"]["offset_points"] = float(be["offset"])
        uses_atr |= trg["type"] == "atr"
    if uses_atr:
        out["atr_period"] = 14
    return out


# =============================================================================== regime / MTF / confirm
def regime_cond(key: str, s: int) -> dict | None:
    return {
        "none": lambda: None,
        "atr_high": lambda: cmp(F("atr_regime", "atr_rank"), ">=", 0.5),
        "atr_low": lambda: cmp(F("atr_regime", "atr_rank"), "<", 0.5),
        "adx_trend": lambda: cmp(F("adx", "adx", period=14), ">=", 25),
        "adx_range": lambda: cmp(F("adx", "adx", period=14), "<", 20),
        "bb_compressed": lambda: cmp(F("bollinger", "bandwidth_rank", period=20, k=2.0), "<=", 0.3),
        "ma_alignment": lambda: gt(F("ema", "ema", period=20), F("ema", "ema", period=50), s),
        "prior_day_direction": lambda: gt(F("daily_levels", "prev_day_close"), F("daily_levels", "prev_day_open"), s),
        "overnight_gap": lambda: gt(F("daily_levels", "gap"), 0, s),
        "above_day_open": lambda: gt(CLOSE, F("daily_levels", "day_open"), s),
        "prior_day_nr": lambda: cmp(F("daily_nr", "prev_is_nr", n=7), "==", 1),      # prior-day range CONTRACTION
    }[key]()


REGIME_DIRECTIONAL = {"ma_alignment", "prior_day_direction", "overnight_gap", "above_day_open"}


def mtf_cond(key: str, htf: str, s: int) -> dict:
    return {
        "htf_ema_trend": lambda: gt(CLOSE, F("ema", "ema", htf, period=50), s),
        "htf_ema_slope": lambda: gt(F("ema", "slope", htf, period=20), 0, s),
        "htf_rsi": lambda: gt(F("rsi", "rsi", htf, period=14), 50, s),
        "htf_structure": lambda: cmp(F("swings", "trend", htf, left=3, right=1), "==", s),
        "htf_vol_regime": lambda: cmp(F("atr_regime", "atr_rank", htf), ">=", 0.5),
        "htf_range_position": lambda: gt(CLOSE, F("donchian", "mid", htf, period=20), s),
        "htf_breakout": lambda: gt(CLOSE, F("donchian", hi_lo(s, "upper", "lower"), htf, period=20), s),
    }[key]()


def confirm_cond(key: str, s: int) -> dict | None:
    return {
        "none": lambda: None,
        "candle": lambda: cmp(F("candle", "direction"), "==", s),
        "close_strength": lambda: (cmp(F("candle", "close_pos"), ">=", 0.7) if s > 0
                                   else cmp(F("candle", "close_pos"), "<=", 0.3)),
        "momentum": lambda: gt(F("roc", "momentum", period=5), 0, s),
        "trend": lambda: gt(CLOSE, F("ema", "ema", period=50), s),
    }[key]()


# =============================================================================== families
@dataclass(frozen=True)
class Family:
    num: int
    fid: str
    name: str
    group: str                      # technical | price_action | smc | ict
    hypothesis: str
    params: Mapping[str, tuple]
    entry: Callable                 # (p, s, ctx) -> condition
    reversal: Callable | None = None           # (p, s, ctx) -> exit condition for side s
    stop_entry: Callable | None = None         # (p, s, ctx) -> (condition, level) for a resting STOP order
    limit_entry: Callable | None = None        # (p, s, ctx) -> (condition, level) for a resting LIMIT order
    constraint: Callable | None = None         # p -> None | reason
    timeframes: tuple = TIMEFRAMES
    sessions: tuple = ALL_SESSIONS
    window: Callable | None = None             # p -> Window (session-anchored families)
    references: Callable | None = None         # p -> {name: (tz, wd, start, end)} extra local sessions
    mtf: bool = True
    regimes: tuple = REGIMES
    confirms: tuple = CONFIRMS
    stops: tuple = TREND_STOPS
    targets: tuple = TREND_TARGETS
    reversal_exit: bool = True
    inactive: Callable | None = None           # p -> params that do not affect this variant (pinned)
    session_rule: Callable | None = None       # (p, session_key) -> None | reason code
    session_domain: Callable | None = None     # p -> sessions to sample from (subset of ``sessions``)
    zone_stop: Callable | None = None          # (p, s) -> the setup's invalidation level (stop kind "zone")
    scores: Mapping[str, int] = field(default_factory=dict)
    rationale: str = ""


def _ma(kind: str, period: int, **kw) -> dict:
    return F(kind, kind, period=period, **kw)


def _approach(level, p, s):
    """A resting stop order is placed when the close is within approach*ATR of the level, not beyond it."""
    return ALL(gt(level, CLOSE, s), ge(CLOSE, off(level, p["approach"], -s), s))


# ---------- 1-8 technical: trend / momentum
def f_ema_cross(p, s, ctx):
    return xup(_ma("ema", p["fast"]), _ma("ema", p["slow"]), s)


def f_sma_cross(p, s, ctx):
    return xup(_ma("sma", p["fast"]), _ma("sma", p["slow"]), s)


def f_price_ma(p, s, ctx):
    ma = _ma(p["ma"], p["period"])
    if p["mode"] == "cross":
        return xup(CLOSE, ma, s)
    return ALL(gt(CLOSE, ma, s), gt(BAR("close", 1), F(p["ma"], p["ma"], lag=1, period=p["period"]), s),
               gt(F(p["ma"], p["ma"], lag=2, period=p["period"]), BAR("close", 2), s))      # two closes beyond


def f_ma_slope(p, s, ctx):
    slope = F("ema", "slope", period=p["period"], slope_bars=p["slope_bars"])
    return xup(slope, ar("mul", s * p["threshold_atr"], ATR), s)


def f_adx(p, s, ctx):
    adx = F("adx", "adx", period=p["period"])
    pdi, mdi = F("adx", "plus_di", period=p["period"]), F("adx", "minus_di", period=p["period"])
    a, b = (pdi, mdi) if s > 0 else (mdi, pdi)
    if p["mode"] == "di_cross":
        return ALL(cmp(adx, ">=", p["threshold"]), cmp(a, "crosses_above", b))
    return ALL(cmp(adx, "crosses_above", p["threshold"]), cmp(a, ">", b))


def _macd_setting(p) -> tuple[int, int, int]:
    f_, sl, sg = (int(x) for x in p["setting"].split("-"))
    return f_, sl, sg


def f_macd(p, s, ctx):
    f_, sl, sg = _macd_setting(p)
    m = F("macd", "macd", fast=f_, slow=sl, signal=sg)
    g = F("macd", "signal", fast=f_, slow=sl, signal=sg)
    if p["mode"] == "zero_cross":
        return xup(m, 0, s)
    c = xup(m, g, s)
    return ALL(c, gt(m, 0, s)) if p["mode"] == "signal_cross_zero_confirm" else c


def f_roc(p, s, ctx):
    if p["unit"] == "pct":                                    # rate of change as a percentage of price
        return xup(F("roc", "roc", period=p["period"]), s * p["pct"] / 100.0, s)
    return xup(F("roc", "momentum", period=p["period"]), ar("mul", s * p["atr_k"], ATR), s)


def f_rsi_trend(p, s, ctx):
    lvl = p["level"] if s > 0 else 100 - p["level"]
    return xup(F("rsi", "rsi", period=p["period"]), lvl, s)


# ---------- 9-14 technical: mean reversion
def f_rsi_mr(p, s, ctx):
    lvl = p["level"] if s > 0 else 100 - p["level"]
    rsi = F("rsi", "rsi", period=p["period"])
    return xup(rsi, lvl, s) if p["mode"] == "exit_extreme" else gt(lvl, rsi, s)


def f_stoch(p, s, ctx):
    k = F("stoch", "k", k_period=p["k_period"], d_period=3)
    d = F("stoch", "d", k_period=p["k_period"], d_period=3)
    lvl = p["level"] if s > 0 else 100 - p["level"]
    if p["mode"] == "kd_cross_in_zone":
        return ALL(xup(k, d, s), gt(lvl, d, s))
    return xup(k, lvl, s)


def _bb(p, out):
    return F("bollinger", out, period=p["period"], k=p["k"])


def f_bb_mr(p, s, ctx):
    band = hi_lo(s, _bb(p, "lower"), _bb(p, "upper"))
    if p["mode"] == "reentry_cross":
        return xup(CLOSE, band, s)
    return ALL(gt(band, BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, band, s))      # wick outside, close inside


def f_bb_limit(p, s, ctx):
    band = hi_lo(s, _bb(p, "lower"), _bb(p, "upper"))
    return ALL(gt(CLOSE, band, s), gt(F("rsi", "rsi", period=14), 50, -s)), band


def f_zscore(p, s, ctx):
    z = F("bollinger", "zscore", period=p["period"], k=2.0)
    base = gt(-s * p["z"], z, s)
    return ALL(base, gt(z, F("bollinger", "zscore", lag=1, period=p["period"], k=2.0), s)) \
        if p["mode"] == "extreme_turn" else base


def _atr_band(p, s):
    return off(_ma(p["basis"], p["period"]), p["k"], -s)


def f_atr_channel(p, s, ctx):
    band = _atr_band(p, s)
    return xup(CLOSE, band, s) if p["mode"] == "reentry" else gt(band, CLOSE, s)


def f_atr_channel_limit(p, s, ctx):
    return gt(CLOSE, _atr_band(p, s), s), _atr_band(p, s)


def f_ma_dist(p, s, ctx):
    ma = _ma(p["ma"], p["period"])
    pct = ar("div", F(p["ma"], "dist", period=p["period"]), ma)
    base = gt(-s * p["pct"] / 100.0, pct, s)
    if p["mode"] == "turn":
        prev = ar("div", F(p["ma"], "dist", lag=1, period=p["period"]), F(p["ma"], p["ma"], lag=1, period=p["period"]))
        return ALL(base, gt(pct, prev, s))
    return base


# ---------- 15-27 price action
def f_range_rev(p, s, ctx):
    n = p["n"]
    edge = hi_lo(s, F("donchian", "lower", period=n), F("donchian", "upper", period=n))
    return ALL(ge(off(edge, p["buffer"], s), BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, edge, s),
               cmp(F("donchian", "width", period=n), "<=", ar("mul", p["max_width_atr"], ATR)))


def f_range_rev_limit(p, s, ctx):
    n = p["n"]
    edge = hi_lo(s, F("donchian", "lower", period=n), F("donchian", "upper", period=n))
    return ALL(gt(CLOSE, off(edge, p["buffer"], s), s),
               cmp(F("donchian", "width", period=n), "<=", ar("mul", p["max_width_atr"], ATR))), off(edge, p["buffer"], s)


def _donch(p, s):
    return hi_lo(s, F("donchian", "upper", period=p["n"]), F("donchian", "lower", period=p["n"]))


def f_donchian(p, s, ctx):
    return xup(CLOSE, off(_donch(p, s), p["buffer"], s), s)


def f_donchian_stop(p, s, ctx):
    lvl = off(_donch(p, s), p["buffer"], s)
    return _approach(lvl, p, s), lvl


def _ref_level(name: str, s: int) -> dict:
    return F("session", hi_lo(s, "prev_high", "prev_low"), session=name)


def _orb_ref(p) -> str:
    tz, st = (NY, "09:30") if p["market"] == "ny" else (LDN, "08:00")
    return session_name("FXR", tz, MF, st, hm(mins(st) + p["or_minutes"]))


def f_orb(p, s, ctx):
    return xup(CLOSE, off(_ref_level(_orb_ref(p), s), p["buffer"], s), s)


def f_orb_stop(p, s, ctx):
    lvl = off(_ref_level(_orb_ref(p), s), p["buffer"], s)
    return gt(lvl, CLOSE, s), lvl


def orb_window(p) -> Window:
    tz, st = (NY, "09:30") if p["market"] == "ny" else (LDN, "08:00")
    e0 = mins(st) + p["or_minutes"]
    e1 = e0 + p["entry_minutes"]
    flat = min(e1 + 60, mins("16:00") if tz == NY else mins("16:00"))
    return Window(tz, MF, hm(e0), hm(e1), hm(flat))


def orb_refs(p) -> dict:
    tz, st = (NY, "09:30") if p["market"] == "ny" else (LDN, "08:00")
    return {_orb_ref(p): (tz, MF, st, hm(mins(st) + p["or_minutes"]))}


def _pdl(s):
    return F("daily_levels", hi_lo(s, "prev_day_high", "prev_day_low"))


def f_pdhl(p, s, ctx):
    return xup(CLOSE, off(_pdl(s), p["buffer"], s), s)


def f_pdhl_stop(p, s, ctx):
    lvl = off(_pdl(s), p["buffer"], s)
    return _approach(lvl, p, s), lvl


def _ref_name(key: str) -> str:
    tz, wd, a, b = REFERENCE_WINDOWS[key]
    return session_name("FXR", tz, wd, a, b)


def f_session_hl(p, s, ctx):
    return xup(CLOSE, off(_ref_level(_ref_name(p["reference"]), s), p["buffer"], s), s)


def f_session_hl_stop(p, s, ctx):
    lvl = off(_ref_level(_ref_name(p["reference"]), s), p["buffer"], s)
    return _approach(lvl, p, s), lvl


def ref_refs(p) -> dict:
    return {_ref_name(p["reference"]): REFERENCE_WINDOWS[p["reference"]]}


def f_squeeze(p, s, ctx):
    bw = F("bollinger", "bandwidth_rank", lag=1, period=20, k=p["k"], rank_lookback=p["rank_lookback"])
    band = F("bollinger", hi_lo(s, "upper", "lower"), period=20, k=p["k"], rank_lookback=p["rank_lookback"])
    return ALL(cmp(bw, "<=", p["squeeze"]), xup(CLOSE, band, s))


def f_vcp(p, s, ctx):
    if p["contraction"] == "atr_rank":
        c = cmp(F("atr_regime", "atr_rank", lag=1, lookback=p["lookback"]), "<=", p["threshold"])
    else:
        c = cmp(F("range_stats", "consolidation_atr", lag=1, lookback=p["n"]), "<=", p["threshold"] * 20)
    return ALL(c, xup(CLOSE, _donch(p, s), s))


def f_nr(p, s, ctx):
    if p["scope"] == "day":          # the previous trading date was the narrowest of the last n: break its extreme
        return ALL(cmp(F("daily_nr", "prev_is_nr", n=p["n"]), "==", 1), xup(CLOSE, _pdl(s), s))
    return ALL(cmp(F("narrow_range", "is_nr", lag=1, n=p["n"]), "==", 1),
               gt(CLOSE, BAR(hi_lo(s, "high", "low"), 1), s))


def f_nr_stop(p, s, ctx):
    if p["scope"] == "day":
        return ALL(cmp(F("daily_nr", "prev_is_nr", n=p["n"]), "==", 1), gt(_pdl(s), CLOSE, s)), _pdl(s)
    return cmp(F("narrow_range", "is_nr", n=p["n"]), "==", 1), BAR(hi_lo(s, "high", "low"))


def f_range_exp(p, s, ctx):
    rx = F("range_stats", "range_expansion", lookback=p["lookback"])
    cp = F("candle", "close_pos")
    return ALL(cmp(rx, ">=", p["expansion"]),
               cmp(cp, ">=", p["close_pos"]) if s > 0 else cmp(cp, "<=", round(1 - p["close_pos"], 4)))


def f_ma_pullback(p, s, ctx):
    ma = _ma(p["ma"], p["period"])
    slope = F("ema", "slope", period=p["period"]) if p["ma"] == "ema" else \
        ar("sub", ma, F("sma", "sma", lag=5, period=p["period"]))
    touch = ge(off(ma, p["touch_atr"], s), BAR(hi_lo(s, "low", "high")), s)
    return ALL(gt(slope, 0, s), touch, gt(CLOSE, ma, s))


def f_ma_pullback_limit(p, s, ctx):
    ma = _ma(p["ma"], p["period"])
    slope = F("ema", "slope", period=p["period"]) if p["ma"] == "ema" else \
        ar("sub", ma, F("sma", "sma", lag=5, period=p["period"]))
    lvl = off(ma, p["touch_atr"], s)
    return ALL(gt(slope, 0, s), gt(CLOSE, lvl, s)), lvl


def _retest_level(p, s):
    if p["level"] == "donchian":
        return hi_lo(s, F("donchian", "upper", lag=p["k"], period=p["n"]), F("donchian", "lower", lag=p["k"], period=p["n"]))
    return _pdl(s)


def f_retest(p, s, ctx):
    lvl = _retest_level(p, s)
    broke = gt(BAR("close", p["k"]), lvl, s)
    return ALL(broke, ge(off(lvl, p["buffer"], s), BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, lvl, s))


def f_mom_pullback(p, s, ctx):
    k = p["k"]
    return ALL(cmp(F("range_stats", "body_atr", lag=k), ">=", p["impulse_atr"]),
               cmp(F("candle", "direction", lag=k), "==", s),
               gt(BAR("close", k), BAR(hi_lo(s, "low", "high"), 1), s),     # retraced beyond the impulse close
               gt(CLOSE, BAR(hi_lo(s, "high", "low"), 1), s))               # resumption


# ---------- 28-29 SMC
def _sw(p, out, lag=0):
    return F("swings", out, lag=lag, left=p["left"], right=p["right"])


def f_bos(p, s, ctx):
    c = cmp(_sw(p, hi_lo(s, "bos_up", "bos_down")), "==", 1)
    if p["displacement"]:
        return ALL(c, cmp(F("range_stats", "body_atr"), ">=", p["displacement"]))
    return c


def f_choch(p, s, ctx):
    c = ALL(cmp(_sw(p, hi_lo(s, "bos_up", "bos_down")), "==", 1), cmp(_sw(p, "trend", lag=1), "==", -s))
    if p["displacement"]:
        return ALL(c, cmp(F("range_stats", "body_atr"), ">=", p["displacement"]))
    return c


# ---------- 30 ICT / liquidity / FVG set
def f_ict(p, s, ctx):
    setup = p["setup"]
    if setup == "liquidity_sweep_reversal":
        return cmp(F("swings", hi_lo(s, "sweep_low", "sweep_high"), left=p["left"], right=1), "==", 1)
    if setup == "liquidity_raid_reversal":
        lvl = F("daily_levels", hi_lo(s, "prev_day_low", "prev_day_high"))
        return ALL(gt(lvl, BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, lvl, s))
    if setup == "fvg_reaction":
        g = "bull" if s > 0 else "bear"
        fp = {"min_size_atr": p["fvg_min_atr"], "max_age_bars": p["fvg_age"]}
        edge = F("fvg", f"{g}_top" if s > 0 else f"{g}_bottom", **fp)
        return ALL(cmp(F("fvg", f"{g}_active", **fp), ">=", 1), ge(edge, BAR(hi_lo(s, "low", "high")), s),
                   gt(CLOSE, edge, s))
    if setup == "killzone_momentum":
        return ALL(cmp(F("range_stats", "body_atr"), ">=", p["displacement"]), cmp(F("candle", "direction"), "==", s))
    if setup in ("order_block_reaction", "breaker_block"):
        k = "ob" if setup == "order_block_reaction" else "brk"
        g = "bull" if s > 0 else "bear"
        edge = _ob(p, f"{k}_{g}_{'top' if s > 0 else 'bottom'}")
        return ALL(ge(edge, BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, edge, s))     # touch the zone, close back out
    if setup == "ict_opening_range":
        # opening-range / initial-balance SWEEP and reclaim: probe beyond the completed OR extreme, close back inside
        lvl = _ref_level(_ict_or_ref(p), -s)
        return ALL(gt(lvl, BAR(hi_lo(s, "low", "high")), s), gt(CLOSE, lvl, s))
    # ict_ote: in a confirmed structure trend, price retraces into the optimal-trade-entry zone
    hi, lo = _sw({"left": p["left"], "right": 1}, "swing_high"), _sw({"left": p["left"], "right": 1}, "swing_low")
    lvl = _ote(p, s, hi, lo)
    return ALL(cmp(_sw({"left": p["left"], "right": 1}, "trend"), "==", s), gt(lvl, BAR(hi_lo(s, "low", "high")), s),
               gt(CLOSE, lvl, s))


def _ob(p, out):
    return F("order_block", out, left=3, right=1, disp=p["ob_disp"], require_bos=True, lookback=10, zone=p["ob_zone"],
             first_touch_only=p["ob_first_touch"], max_age_bars=100, atr_period=14)


def _ict_or_ref(p) -> str:
    return session_name("FXR", NY, MF, "09:30", hm(mins("09:30") + p["ib"]))


def ict_refs(p) -> dict:
    if p["setup"] != "ict_opening_range":
        return {}
    return {_ict_or_ref(p): (NY, MF, "09:30", hm(mins("09:30") + p["ib"]))}


def ict_zone_stop(p, s):
    """The setup's natural invalidation level (long: below it; short: above it)."""
    st = p["setup"]
    if st == "fvg_reaction":
        fp = {"min_size_atr": p["fvg_min_atr"], "max_age_bars": p["fvg_age"]}
        return F("fvg", "bull_bottom" if s > 0 else "bear_top", **fp)
    if st in ("order_block_reaction", "breaker_block"):
        k = "ob" if st == "order_block_reaction" else "brk"
        return _ob(p, f"{k}_bull_bottom" if s > 0 else f"{k}_bear_top")
    if st == "ict_ote":
        return _sw({"left": p["left"], "right": 1}, "swing_low" if s > 0 else "swing_high")
    return BAR(hi_lo(s, "low", "high"))              # sweep / raid / kill-zone / OR sweep: the signal candle's extreme


def _ote(p, s, hi, lo):
    rng = ar("sub", hi, lo)
    return ar("sub", hi, ar("mul", p["ote"], rng)) if s > 0 else ar("add", lo, ar("mul", p["ote"], rng))


KILLZONES = ("london_morning", "ny_first30", "ny_open")


def ict_sessions(p):
    if p["setup"] == "killzone_momentum":
        return KILLZONES
    if p["setup"] == "ict_opening_range":               # only after the opening range / initial balance completed
        return ("ny_1000_1400", "ny_afternoon", "ny_last90") if p["ib"] == 30 else ("ny_afternoon", "ny_last90")
    return ALL_SESSIONS


def ict_session_rule(p, session_key):
    if p["setup"] == "killzone_momentum" and session_key not in KILLZONES:
        return "KILLZONE_REQUIRES_KILLZONE_SESSION"
    return None


# ---------- reversal exits (loss of signal)
def r_ma_cross(kind):
    return lambda p, s, ctx: gt(_ma(kind, p["slow"]), _ma(kind, p["fast"]), s)


def r_price_ma(p, s, ctx):
    return gt(_ma(p["ma"], p["period"]), CLOSE, s)


def r_macd(p, s, ctx):
    f_, sl, sg = _macd_setting(p)
    return gt(F("macd", "signal", fast=f_, slow=sl, signal=sg), F("macd", "macd", fast=f_, slow=sl, signal=sg), s)


def r_rsi_mid(p, s, ctx):
    return gt(F("rsi", "rsi", period=p["period"]), 50, s)             # reverted past the midline


def r_rsi_trend_lost(p, s, ctx):
    return gt(50, F("rsi", "rsi", period=p["period"]), s)             # momentum back below the midline


def r_mean(p, s, ctx):
    return gt(CLOSE, F("bollinger", "mid", period=p["period"], k=p.get("k", 2.0)), s)


def r_basis(p, s, ctx):
    return gt(CLOSE, _ma(p["basis"], p["period"]), s)


def r_ma_dist(p, s, ctx):
    return gt(CLOSE, _ma(p["ma"], p["period"]), s)


def r_structure(p, s, ctx):
    return cmp(_sw(p, "trend"), "==", -s)


def _sc(c, d, n, h, x, u):
    return {"mechanical_clarity": c, "variation_dimensions": d, "intraday_nq_suitability": n,
            "hypothesis_diversity": h, "session_exit_usefulness": x, "structural_distinctness": u}


BRK = ("points", "atr", "swing", "recent_extreme", "prev_bar", "day_structure", "chandelier")
FAMILIES: tuple[Family, ...] = (
    Family(1, "ema_crossover", "EMA Crossover", "technical",
           "A fast EMA crossing a slower EMA marks a short-term momentum shift.",
           {"fast": (5, 8, 9, 12, 20), "slow": (21, 26, 34, 50, 100)}, f_ema_cross, r_ma_cross("ema"),
           constraint=lambda p: None if p["slow"] >= 2 * p["fast"] else "FAST_SLOW_TOO_CLOSE",
           confirms=("none", "candle", "close_strength", "momentum"), scores=_sc(3, 3, 3, 2, 3, 2)),
    Family(2, "sma_crossover", "SMA Crossover", "technical",
           "A fast SMA crossing a slower SMA marks a short-term trend change.",
           {"fast": (5, 10, 20), "slow": (30, 50, 100, 200)}, f_sma_cross, r_ma_cross("sma"),
           constraint=lambda p: None if p["slow"] >= 2 * p["fast"] else "FAST_SLOW_TOO_CLOSE",
           confirms=("none", "candle", "close_strength", "momentum"), scores=_sc(3, 3, 2, 2, 3, 1)),
    Family(3, "price_vs_ma", "Price vs Moving Average", "technical",
           "Price closing through its moving average signals a change in directional pressure.",
           {"ma": ("ema", "sma"), "period": (20, 50, 100, 200), "mode": ("cross", "two_close_hold")},
           f_price_ma, r_price_ma, confirms=("none", "candle", "close_strength", "momentum"),
           scores=_sc(3, 3, 3, 2, 3, 2)),
    Family(4, "ma_slope", "Moving-Average Slope", "technical",
           "A moving average whose slope exceeds a volatility-scaled threshold marks directional drift.",
           {"period": (20, 50, 100), "slope_bars": (3, 5, 10), "threshold_atr": (0.02, 0.05, 0.1)},
           f_ma_slope, lambda p, s, c: gt(0, F("ema", "slope", period=p["period"], slope_bars=p["slope_bars"]), s),
           confirms=("none", "candle", "close_strength", "momentum"), scores=_sc(3, 2, 2, 2, 2, 2)),
    Family(5, "adx_trend", "ADX Trend", "technical",
           "Directional movement with sufficient ADX trend strength tends to persist intraday.",
           {"period": (10, 14, 20), "threshold": (20, 25, 30), "mode": ("di_cross", "adx_rise")}, f_adx,
           lambda p, s, c: gt(F("adx", "minus_di" if s > 0 else "plus_di", period=p["period"]),
                              F("adx", "plus_di" if s > 0 else "minus_di", period=p["period"]), 1),
           regimes=tuple(r for r in REGIMES if r not in ("adx_trend", "adx_range")), scores=_sc(3, 3, 2, 2, 2, 3)),
    Family(6, "macd_crossover", "MACD Crossover", "technical",
           "MACD crossing its signal line (optionally confirmed by the zero line) marks a momentum change.",
           {"setting": ("12-26-9", "8-17-9", "5-35-5", "6-13-5"),                  # fast-slow-signal
            "mode": ("signal_cross", "signal_cross_zero_confirm", "zero_cross")}, f_macd, r_macd,
           confirms=("none", "candle", "close_strength", "trend"), scores=_sc(3, 3, 2, 2, 2, 2)),
    Family(7, "roc_momentum", "Rate-of-Change Momentum", "technical",
           "Momentum above a threshold (percentage of price or ATR-scaled points) tends to continue over the next bars.",
           {"period": (5, 10, 20, 30), "unit": ("atr", "pct"), "atr_k": (1.0, 1.5, 2.0, 3.0),
            "pct": (0.05, 0.1, 0.2, 0.3)}, f_roc,
           lambda p, s, c: gt(0, F("roc", "momentum", period=p["period"]), s),
           inactive=lambda p: {"pct"} if p["unit"] == "atr" else {"atr_k"},
           confirms=("none", "candle", "close_strength", "trend"), scores=_sc(3, 2, 3, 2, 2, 2)),
    Family(8, "rsi_trend", "RSI Trend Continuation", "technical",
           "RSI crossing into its trend zone confirms directional momentum for continuation.",
           {"period": (7, 14, 21), "level": (55, 60, 65)}, f_rsi_trend, r_rsi_trend_lost,
           confirms=("none", "candle", "close_strength", "trend"), scores=_sc(3, 2, 2, 2, 2, 2)),
    Family(9, "rsi_mean_reversion", "RSI Mean Reversion", "technical",
           "After a short-term RSI extreme, price tends to revert toward its mean.",
           {"period": (2, 3, 5, 7, 14), "level": (10, 15, 20, 25, 30), "mode": ("in_extreme", "exit_extreme")},
           f_rsi_mr, r_rsi_mid, stops=REVERSION_STOPS, targets=REVERSION_TARGETS,
           confirms=("none", "candle", "close_strength"), scores=_sc(3, 3, 3, 3, 3, 2)),
    Family(10, "stochastic_reversion", "Stochastic Reversion", "technical",
           "Stochastic turns from an extreme zone mark exhaustion of the short-term move.",
           {"k_period": (5, 9, 14), "level": (10, 20, 30), "mode": ("kd_cross_in_zone", "zone_exit")}, f_stoch,
           lambda p, s, c: gt(F("stoch", "k", k_period=p["k_period"], d_period=3), 50, s),
           stops=REVERSION_STOPS, targets=REVERSION_TARGETS, confirms=("none", "candle", "close_strength"),
           scores=_sc(3, 2, 2, 2, 2, 2)),
    Family(11, "bollinger_mean_reversion", "Bollinger Mean Reversion", "technical",
           "Extensions beyond a Bollinger band that are rejected revert toward the mean.",
           {"period": (20, 30, 50), "k": (1.5, 2.0, 2.5), "mode": ("wick_reject", "reentry_cross")}, f_bb_mr, r_mean,
           limit_entry=f_bb_limit, stops=REVERSION_STOPS, targets=REVERSION_TARGETS,
           regimes=tuple(r for r in REGIMES if r != "bb_compressed"),
           confirms=("none", "candle", "close_strength"), scores=_sc(3, 3, 3, 2, 3, 2)),
    Family(12, "zscore_reversion", "Z-Score Reversion", "technical",
           "Price far from its rolling mean in standardized units tends to revert once the stretch turns.",
           {"period": (20, 50, 100), "z": (1.5, 2.0, 2.5, 3.0), "mode": ("extreme_level", "extreme_turn")},
           f_zscore, r_mean, stops=REVERSION_STOPS, targets=REVERSION_TARGETS,
           confirms=("none", "candle", "close_strength"), scores=_sc(3, 2, 2, 2, 2, 2)),
    Family(13, "atr_channel_reversion", "ATR Channel Reversion", "technical",
           "Deviations beyond an ATR channel around a mean are stretched and tend to revert.",
           {"basis": ("ema", "sma"), "period": (20, 50), "k": (1.5, 2.0, 2.5, 3.0), "mode": ("beyond", "reentry")},
           f_atr_channel, r_basis, limit_entry=f_atr_channel_limit, stops=REVERSION_STOPS, targets=REVERSION_TARGETS,
           confirms=("none", "candle", "close_strength"), scores=_sc(3, 3, 2, 2, 2, 2)),
    Family(14, "ma_distance_reversion", "Moving-Average Distance Reversion", "technical",
           "A large percentage distance from a moving average tends to shrink.",
           {"ma": ("ema", "sma"), "period": (20, 50, 100), "pct": (0.2, 0.3, 0.5, 0.75, 1.0), "mode": ("level", "turn")},
           f_ma_dist, r_ma_dist, stops=REVERSION_STOPS, targets=REVERSION_TARGETS,
           confirms=("none", "candle", "close_strength"), scores=_sc(3, 2, 2, 2, 2, 1)),
    Family(15, "range_reversion", "Range Reversion", "price_action",
           "In a bounded intraday range, probes of the range edge that close back inside revert.",
           {"n": (12, 24, 48), "buffer": (0.0, 0.25), "max_width_atr": (4.0, 6.0, 8.0)}, f_range_rev,
           lambda p, s, c: gt(CLOSE, F("donchian", "mid", period=p["n"]), s), limit_entry=f_range_rev_limit,
           stops=REVERSION_STOPS, targets=REVERSION_TARGETS, confirms=("none", "candle", "close_strength"),
           scores=_sc(2, 3, 3, 2, 3, 2)),
    Family(16, "donchian_breakout", "Donchian Breakout", "price_action",
           "A close beyond the prior N-bar high/low starts a directional move.",
           {"n": (10, 20, 30, 55), "buffer": (0.0, 0.25), "approach": APPROACH_ATR}, f_donchian,
           lambda p, s, c: gt(F("donchian", "mid", period=p["n"]), CLOSE, s), stop_entry=f_donchian_stop,
           stops=BRK, targets=BREAKOUT_TARGETS, scores=_sc(3, 3, 3, 2, 3, 2)),
    Family(17, "opening_range_breakout", "Opening Range Breakout", "price_action",
           "A break of the opening range continues in the break direction.",
           {"market": ("ny", "london"), "or_minutes": (5, 15, 30, 60), "entry_minutes": (60, 120, 240),
            "buffer": (0.0, 0.25)}, f_orb, lambda p, s, c: gt(F("session", "prev_low" if s > 0 else "prev_high",
                                                                 session=_orb_ref(p)), CLOSE, s),
           stop_entry=f_orb_stop, sessions=(), window=orb_window, references=orb_refs,
           stops=BRK + ("range_side",), targets=BREAKOUT_TARGETS, scores=_sc(3, 3, 3, 3, 3, 3)),
    Family(18, "prev_day_breakout", "Previous Day High/Low Breakout", "price_action",
           "A break of the previous trading day's extreme continues.",
           {"buffer": (0.0, 0.25, 0.5), "approach": APPROACH_ATR}, f_pdhl,
           lambda p, s, c: gt(_pdl(s), CLOSE, s), stop_entry=f_pdhl_stop, stops=BRK, targets=BREAKOUT_TARGETS,
           scores=_sc(3, 2, 3, 2, 3, 2)),
    Family(19, "session_hl_breakout", "Session High/Low Breakout", "price_action",
           "A break of a completed reference session's high/low continues in a later session.",
           {"reference": ("asia_tokyo", "london_am", "ny_am", "prior_ny_rth"), "buffer": (0.0, 0.25),
            "approach": APPROACH_ATR}, f_session_hl,
           lambda p, s, c: gt(_ref_level(_ref_name(p["reference"]), s), CLOSE, s), stop_entry=f_session_hl_stop,
           references=ref_refs, stops=BRK, targets=BREAKOUT_TARGETS, scores=_sc(3, 3, 3, 3, 3, 2)),
    Family(20, "overnight_range_breakout", "Overnight Range Breakout", "price_action",
           "A break of the overnight range during the New York session continues.",
           {"reference": ("overnight_full", "premarket"), "buffer": (0.0, 0.25), "approach": APPROACH_ATR},
           f_session_hl, lambda p, s, c: gt(_ref_level(_ref_name(p["reference"]), s), CLOSE, s),
           stop_entry=f_session_hl_stop, references=ref_refs, sessions=("ny_first30", "ny_open", "ny_morning", "ny_rth"),
           stops=BRK, targets=BREAKOUT_TARGETS, scores=_sc(3, 3, 3, 2, 3, 2)),
    Family(21, "bollinger_squeeze_breakout", "Bollinger Squeeze Breakout", "price_action",
           "Volatility expansion out of a Bollinger compression starts a directional move.",
           {"k": (1.5, 2.0), "rank_lookback": (50, 100, 200), "squeeze": (0.1, 0.2, 0.3)}, f_squeeze,
           lambda p, s, c: gt(F("bollinger", "mid", period=20, k=p["k"], rank_lookback=p["rank_lookback"]), CLOSE, s),
           regimes=tuple(r for r in REGIMES if r not in ("bb_compressed", "atr_low")), stops=BRK,
           targets=BREAKOUT_TARGETS, scores=_sc(3, 2, 2, 2, 2, 2)),
    Family(22, "volatility_contraction_breakout", "Volatility Contraction Breakout", "price_action",
           "A breakout after a low-volatility regime expands in the break direction.",
           {"contraction": ("atr_rank", "consolidation"), "lookback": (50, 100, 200), "n": (10, 20),
            "threshold": (0.1, 0.2, 0.3)}, f_vcp, lambda p, s, c: gt(F("donchian", "mid", period=p["n"]), CLOSE, s),
           inactive=lambda p: {"lookback"} if p["contraction"] == "consolidation" else set(), regimes=tuple(r for r in REGIMES if r not in ("atr_low", "atr_high", "bb_compressed")),
           stops=BRK, targets=BREAKOUT_TARGETS, scores=_sc(2, 2, 2, 2, 2, 2)),
    Family(23, "nr_breakout", "NR7 Breakout", "price_action",
           "A break of a narrow-range bar or of a narrow-range trading date releases stored energy in the break direction.",
           {"scope": ("bar", "day"), "n": (4, 7)}, f_nr, None, stop_entry=f_nr_stop, reversal_exit=False, stops=BRK,
           targets=BREAKOUT_TARGETS, scores=_sc(3, 2, 2, 2, 2, 2)),
    Family(24, "range_expansion_breakout", "Range Expansion Breakout", "price_action",
           "A bar with a significant range expansion closing near its extreme continues.",
           {"lookback": (10, 20), "expansion": (1.5, 2.0, 2.5), "close_pos": (0.7, 0.8)}, f_range_exp, None,
           reversal_exit=False, stops=BRK, targets=BREAKOUT_TARGETS, scores=_sc(3, 2, 3, 2, 2, 2)),
    Family(25, "ma_pullback", "Moving-Average Pullback", "price_action",
           "In a trend, a retracement to the moving average that holds resumes the trend.",
           {"ma": ("ema", "sma"), "period": (20, 50), "touch_atr": (0.0, 0.25, 0.5)}, f_ma_pullback, r_price_ma,
           limit_entry=f_ma_pullback_limit, confirms=("none", "candle", "close_strength", "momentum"),
           scores=_sc(3, 3, 3, 2, 3, 2)),
    Family(26, "breakout_retest", "Breakout Retest Continuation", "price_action",
           "After a level breaks, a retest that holds confirms continuation.",
           {"level": ("donchian", "prev_day"), "n": (10, 20), "k": (2, 3, 5), "buffer": (0.0, 0.25)}, f_retest, None,
           inactive=lambda p: {"n"} if p["level"] == "prev_day" else set(), reversal_exit=False, stops=BRK, targets=BREAKOUT_TARGETS, scores=_sc(2, 3, 3, 2, 2, 3)),
    Family(27, "momentum_pullback", "Momentum Pullback", "price_action",
           "After an impulse bar, a shallow retracement followed by resumption continues the impulse.",
           {"k": (2, 3, 4), "impulse_atr": (1.5, 2.0, 2.5)}, f_mom_pullback, None, reversal_exit=False,
           confirms=("none", "close_strength", "trend"), scores=_sc(2, 2, 3, 2, 2, 3)),
    Family(28, "bos_continuation", "Break of Structure (BOS)", "smc",
           "A close through the latest confirmed swing continues in the break direction.",
           {"left": (2, 3, 5), "right": (1, 2, 3), "displacement": (0.0, 1.0, 1.5)}, f_bos, r_structure,
           stops=("points", "atr", "swing", "recent_extreme", "prev_bar", "chandelier"),
           targets=TREND_TARGETS, scores=_sc(3, 3, 3, 3, 3, 3)),
    Family(29, "choch_reversal", "Change of Character (CHOCH)", "smc",
           "A structural break against the prior structure direction marks a potential reversal.",
           {"left": (2, 3, 5), "right": (1, 2, 3), "displacement": (0.0, 1.0, 1.5)}, f_choch, r_structure,
           stops=("points", "atr", "swing", "recent_extreme", "prev_bar", "chandelier"),
           targets=TREND_TARGETS + ("ma",), scores=_sc(3, 3, 3, 3, 3, 3)),
    Family(30, "ict_liquidity_fvg", "Liquidity Sweep / FVG / ICT Price-Action Set", "ict",
           "Liquidity taken beyond a known level, fair value gaps and kill-zone displacement mark "
           "institutional order flow (eight mechanical setups under one family).",
           {"setup": ("liquidity_sweep_reversal", "liquidity_raid_reversal", "fvg_reaction", "killzone_momentum",
                      "ict_ote", "order_block_reaction", "breaker_block", "ict_opening_range"),
            "left": (3, 5), "fvg_min_atr": (0.0, 0.25, 0.5), "fvg_age": (0, 20, 50), "displacement": (1.0, 1.5, 2.0),
            "ote": (0.62, 0.705, 0.79), "ob_disp": (1.0, 1.5, 2.0), "ob_zone": ("range", "body"),
            "ob_first_touch": (True, False), "ib": (30, 60)}, f_ict, None, reversal_exit=False,
           inactive=lambda p: ICT_ALL_PARAMS - ICT_PARAMS[p["setup"]],
           session_rule=ict_session_rule, session_domain=ict_sessions, references=ict_refs, zone_stop=ict_zone_stop,
           stops=("points", "atr", "swing", "recent_extreme", "prev_bar", "day_structure", "zone"),
           targets=TREND_TARGETS, confirms=("none", "candle", "close_strength"), scores=_sc(2, 3, 3, 3, 3, 3)),
)
FAMILY_BY_ID = {f.fid: f for f in FAMILIES}
# Parameters that only matter for some ICT setups: others are pinned to one value so they never
# create logically identical "different" candidates.
ICT_PARAMS = {"liquidity_sweep_reversal": {"left"}, "liquidity_raid_reversal": set(),
              "fvg_reaction": {"fvg_min_atr", "fvg_age"}, "killzone_momentum": {"displacement"},
              "ict_ote": {"left", "ote"}, "order_block_reaction": {"ob_disp", "ob_zone", "ob_first_touch"},
              "breaker_block": {"ob_disp", "ob_zone", "ob_first_touch"}, "ict_opening_range": {"ib"}}
ICT_ALL_PARAMS = {"left", "fvg_min_atr", "fvg_age", "displacement", "ote", "ob_disp", "ob_zone", "ob_first_touch", "ib"}

# =============================================================================== allocation (frozen)
GROUPS = ("technical", "price_action", "smc", "ict")
GROUP_SHARE = {"technical": 4200, "price_action": 4000, "smc": 800, "ict": 1000}
MIN_PER_FAMILY = 200
ALLOCATION_METHOD = {
    "version": ALLOCATION_VERSION,
    "results_used": False,
    "rule": "1) fixed group totals (GROUP_SHARE); 2) every family gets MIN_PER_FAMILY; 3) the rest of its group "
            "is split in proportion to the family's predeclared structural score (sum of six 1-3 criteria); "
            "4) integer rounding by largest remainder, ties broken by family number. Frozen before any evaluation.",
    "group_rationale": {
        "technical": "14 indicator families (trend, momentum, oscillator mean reversion); broad timeframe/MTF/exit "
                     "applicability -> the largest group",
        "price_action": "13 families of level-based breakouts, ranges and pullbacks; session-anchored levels make the "
                        "session/exit dimensions especially meaningful for intraday NQ",
        "smc": "2 structure families (BOS, CHOCH) with rich structural stop/target variation -> 400 each",
        "ict": "one family id bundling five mechanically distinct setups at the user's request -> 1000 (~200 per setup)"},
    "criteria": ["mechanical_clarity", "variation_dimensions", "intraday_nq_suitability", "hypothesis_diversity",
                 "session_exit_usefulness", "structural_distinctness"],
    "criteria_note": "structural judgements of the definition space made before testing; they are NOT performance "
                     "estimates and are never updated from results",
}


def allocate() -> dict[str, int]:
    out: dict[str, int] = {}
    for g in GROUPS:
        fams = [f for f in FAMILIES if f.group == g]
        rest = GROUP_SHARE[g] - MIN_PER_FAMILY * len(fams)
        if rest < 0:
            raise ValueError(f"group {g}: share below the minimum allocation")
        tot = sum(sum(f.scores.values()) for f in fams)
        raw = {f.fid: rest * sum(f.scores.values()) / tot for f in fams}
        base = {k: math.floor(v) for k, v in raw.items()}
        left = rest - sum(base.values())
        order = sorted(fams, key=lambda f: (-(raw[f.fid] - base[f.fid]), f.num))
        for f in order[:left]:
            base[f.fid] += 1
        for f in fams:
            out[f.fid] = MIN_PER_FAMILY + base[f.fid]
    assert sum(out.values()) == TARGET_TOTAL
    return out


def dimension_catalog() -> dict:
    """What can vary, the executable values, and the declared-but-not-executable values (for the UI)."""
    return {
        "timeframe": {"values": list(TIMEFRAMES), "note": "derived from the canonical 1m dataset"},
        "mtf": {"pairs": {k: list(v) for k, v in MTF_PAIRS.items()}, "filters": list(MTF_FILTERS),
                "causality": "feature timeframe = last COMPLETED higher-timeframe bar (feature engine)"},
        "session": {k: {"timezone": w.tz, "entry": [w.entry_start, w.entry_end], "flat": w.flat}
                    for k, w in SESSION_PRESETS.items()},
        "reference_windows": {k: list(v) for k, v in REFERENCE_WINDOWS.items()},
        "flat_rule": {k: v[1] for k, v in FLAT_RULES.items()},
        "weekdays": {k: v[1] for k, v in WEEKDAY_SETS.items()},
        "direction": list(DIRECTIONS),
        "entry": {"confirmation": list(CONFIRMS), "delay_bars": list(ENTRY_DELAYS),
                  "order": ["market", "stop", "limit"], "expiry_bars": list(ORDER_EXPIRY),
                  "approach_atr": list(APPROACH_ATR)},
        "stop": {k: {p: list(v) for p, v in fn(5).items()} for k, fn in STOP_DOMAINS.items()}
        | {"range_side": {"buffer": list(BUFFERS_ATR)}},
        "stop_points_by_tf": {f"{k}m": list(v) for k, v in STOP_POINTS.items()},
        "target": {k: {p: list(v) for p, v in fn(5).items()} for k, fn in TARGET_DOMAINS.items()},
        "target_points_by_tf": {f"{k}m": list(v) for k, v in TARGET_POINTS.items()},
        "time_exit_minutes": [str(k) for k in TIME_EXITS],
        "signal_exit": ["none", "opposite_signal", "reversal"],
        "trailing": {"kinds": list(TRAIL_KINDS), "level_kinds": list(LEVEL_TRAILS),
                     "activation": ["immediate", "r", "atr", "points"], "breakeven_trigger": ["r", "atr", "points"],
                     "update": {"every_bars": list(TRAIL_EVERY), "only_new_extreme": [False, True],
                                "min_step": ["none", "by timeframe"]},
                     "semantics": "ADR-61 / engine.fills.simulate_exit_trailing (decided at bar close, effective next "
                                  "bar, ratchet only, exit-side extreme)",
                     "declared_not_executable": NOT_EXECUTABLE["trailing"]},
        "regime": list(REGIMES),
        "sizing": list(SIZING),
        "cooldown": {k: v[1] for k, v in COOLDOWNS.items()},
        "not_executable": NOT_EXECUTABLE,
        "not_executable_reasons": NOT_EXECUTABLE_REASONS,
    }
