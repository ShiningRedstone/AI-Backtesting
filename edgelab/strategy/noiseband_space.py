"""The noise band set (ADR-116): 10,000 frozen versions of the user's noise band breakout, as ordinary DSL strategies.

The user's fixed rules (never varied):
  * NQ only (the protocol's own NQ Dukascopy datasets) and trading only from 9:30 New York: the entry window always
    starts at 9:30;
  * a trade only when a candle CLOSES outside the noise area (feature ``noise_area``): above the upper band = long,
    below the lower band = short (with the break).

Everything else is a variation (the user's words: "close time, timeframe, SL and TP etc."; trades per day 1 to 3):
timeframe, the band's lookback / width / gap rule, "any close outside" vs "a fresh break", the last entry time, the
close time (signal exit) or the session end (the engine's 16:00 flatten), direction, stop, target, trailing stop and
trades per day. Sizing is fixed: $500 risk per trade in MNQ, at most 40 contracts.

``generate(seed)`` draws combinations uniformly from the space with a seeded generator, refuses the invalid ones
(the close time must leave time after the last entry) and keeps the first ``TARGET_TOTAL`` with distinct logic
(deduplicated by the compiler's logic hash). Pure: nothing is saved or evaluated here. Numeric domains are research
DESIGN choices, not market data.
"""
from __future__ import annotations

import random
from typing import Mapping

from edgelab.core.identity import hash_obj

SPACE_VERSION = "edgelab-noise-band-space/1"
DEFAULT_SEED = 20261027
TARGET_TOTAL = 10_000
FAMILY = {"id": "noise_band_breakout", "name": "Noise band breakout", "category": "breakout",
          "hypothesis": "A candle close outside the intraday noise area (Zarattini / Aziz / Barbon 2024) after the "
                        "9:30 New York open starts a move that continues in the break direction."}

TIMEFRAMES = ("1m", "5m", "15m", "30m")           # every one starts a bar at 9:30 New York (bars anchored at 18:00)
TF_MIN = {"1m": 1, "5m": 5, "15m": 15, "30m": 30}
LOOKBACK = (7, 10, 14, 20)
MULT = (0.75, 1.0, 1.25, 1.5)
GAP = (True, False)
TRIGGER = ("close_outside", "fresh_break")        # any close outside / a close outside after a close inside
ENTRY_UNTIL = ("10:00", "10:30", "11:00", "12:00", "14:00", "15:30")
CLOSE_TIME = ("11:00", "12:00", "13:00", "14:00", "15:00", "session_end")
DIRECTION = ("both", "long", "short")
STOPS = ("atr_1.0", "atr_1.5", "atr_2.0", "pts_10", "pts_20", "pts_40", "band", "opposite_band")
TARGETS = ("none", "rr_1.0", "rr_1.5", "rr_2.0", "rr_3.0", "pts_20", "pts_40", "pts_80")
TRAILING = ("none", "band", "atr_2.0")
MAX_TRADES = (1, 2, 3)
ATR_PERIOD = 14
SIZING = {"mode": "risk", "risk_usd": 500, "max_quantity": 40, "contract": "MNQ"}

DIMENSIONS = {"timeframe": TIMEFRAMES, "lookback": LOOKBACK, "mult": MULT, "gap": GAP, "trigger": TRIGGER,
              "entry_until": ENTRY_UNTIL, "close_time": CLOSE_TIME, "direction": DIRECTION, "stop": STOPS,
              "target": TARGETS, "trailing": TRAILING, "max_trades": MAX_TRADES}


def design() -> dict:
    """The frozen design (its hash is recorded in the protocol): what is fixed and what varies."""
    return {"space_version": SPACE_VERSION, "target_total": TARGET_TOTAL, "family": FAMILY,
            "dimensions": {k: list(v) for k, v in DIMENSIONS.items()}, "atr_period": ATR_PERIOD, "sizing": SIZING,
            "fixed": ["NQ only (the protocol's NQ datasets)", "entry window starts at 09:30 New York",
                      "entry only on a candle close outside the noise area, with the break"],
            "rule": "uniform seeded draws; invalid (close time not after the last entry) and duplicate logic refused"}


def design_hash() -> str:
    return hash_obj(design(), 16)


def _min(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def _hm(m: int) -> str:
    return f"{m // 60:02d}:{m % 60:02d}"


def valid(v: Mapping) -> bool:
    if v["close_time"] == "session_end":
        return True
    return _min(v["close_time"]) - TF_MIN[v["timeframe"]] > _min(v["entry_until"])


def _nb(v: Mapping, out: str) -> dict:
    return {"feature": "noise_area", "params": {"lookback": v["lookback"], "mult": v["mult"], "gap": v["gap"]},
            "output": out}


def _entry(v: Mapping, side: str) -> dict:
    band = _nb(v, "upper" if side == "long" else "lower")
    if v["trigger"] == "close_outside":
        op = ">" if side == "long" else "<"
        label = "a candle closes above the upper noise band" if side == "long" else \
            "a candle closes below the lower noise band"
    else:
        op = "crosses_above" if side == "long" else "crosses_below"
        label = "a candle closes above the upper noise band after closing inside it" if side == "long" else \
            "a candle closes below the lower noise band after closing inside it"
    return {"label": label, "left": {"bar": "close"}, "op": op, "right": band}


def _stop(v: Mapping) -> dict:
    kind, _, x = v["stop"].partition("_")
    if kind == "atr":
        return {"type": "atr", "multiple": float(x), "period": ATR_PERIOD}
    if kind == "pts":
        return {"type": "points", "points": float(x)}
    if v["stop"] == "band":                      # the band the candle closed through
        return {"type": "price", "long": _nb(v, "upper"), "short": _nb(v, "lower")}
    return {"type": "price", "long": _nb(v, "lower"), "short": _nb(v, "upper")}       # the opposite band


def _target(v: Mapping) -> dict:
    kind, _, x = v["target"].partition("_")
    if kind == "none":
        return {"type": "none"}
    if kind == "rr":
        return {"type": "risk_reward", "multiple": float(x)}
    return {"type": "points", "points": float(x)}


def _trailing(v: Mapping) -> dict | None:
    if v["trailing"] == "none":
        return None
    if v["trailing"] == "band":                  # the stop follows the band the price broke out of
        return {"mode": "level", "level": {"long": _nb(v, "upper"), "short": _nb(v, "lower")}}
    return {"mode": "distance", "distance": {"type": "atr", "multiple": 2.0}, "atr_period": ATR_PERIOD}


def _sides(v: Mapping, keep: tuple) -> dict:
    return {s: x for s, x in zip(("long", "short"), keep) if v["direction"] in ("both", s)}


def build_definition(v: Mapping) -> dict:
    """The DSL definition of one variation (deterministic)."""
    tf = v["timeframe"]
    entry_name = f"NB_ENTRY_0930_{v['entry_until'].replace(':', '')}"
    sessions = {entry_name: {"timezone": "America/New_York", "start": "09:30", "end": v["entry_until"]}}
    entry = {"direction": v["direction"], "session": entry_name, "max_trades_per_day": int(v["max_trades"])}
    for side in ("long", "short"):
        if v["direction"] in ("both", side):
            entry[side] = _entry(v, side)
    stop = _stop(v)
    if stop["type"] == "price":
        stop = {"type": "price", **_sides(v, (stop["long"], stop["short"]))}
    exit_ = {"stop": stop, "target": _target(v)}
    tr = _trailing(v)
    if tr is not None:
        if tr["mode"] == "level":
            tr = {**tr, "level": _sides(v, (tr["level"]["long"], tr["level"]["short"]))}
        exit_["trailing"] = tr
    if v["close_time"] != "session_end":         # close at the close time: flag the first bar opening at or after
        hold_end = _hm(_min(v["close_time"]) - TF_MIN[tf])   # close - 1 bar, exit at the next bar's open = close time
        hold = f"NB_HOLD_0930_{hold_end.replace(':', '')}"
        sessions[hold] = {"timezone": "America/New_York", "start": "09:30", "end": hold_end}
        out = {"label": f"close the trade at {v['close_time']} New York",
               "left": {"feature": "session", "params": {"session": hold}, "output": "in_session"}, "op": "==", "right": 0}
        exit_["signal"] = _sides(v, (out, out))
    name = (f"noise_band_{tf}_lb{v['lookback']}_m{v['mult']}_{'gap' if v['gap'] else 'open'}_{v['trigger']}_"
            f"to{v['entry_until'].replace(':', '')}_x{v['close_time'].replace(':', '')}_{v['direction']}_"
            f"{v['stop']}_{v['target']}_tr{v['trailing']}_n{v['max_trades']}")
    return {"dsl_version": 1, "name": name,
            "description": describe(v), "family": dict(FAMILY), "timeframe": tf, "sessions": sessions,
            "entry": entry, "exit": exit_, "sizing": dict(SIZING)}


def describe(v: Mapping) -> str:
    side = {"both": "long above the upper band, short below the lower band", "long": "long above the upper band only",
            "short": "short below the lower band only"}[v["direction"]]
    trig = "any close outside the band" if v["trigger"] == "close_outside" else "a fresh close outside the band"
    close = "the 16:00 session end" if v["close_time"] == "session_end" else f"{v['close_time']} New York"
    return (f"Noise band ({v['lookback']} days, x{v['mult']}, {'with' if v['gap'] else 'without'} the previous close) on "
            f"{v['timeframe']} candles: {side}, on {trig}, entries 09:30-{v['entry_until']}, closed at {close}; "
            f"stop {v['stop']}, target {v['target']}, trailing {v['trailing']}, up to {v['max_trades']} trade(s) a day.")


def draw(rng: random.Random) -> dict:
    return {k: vals[rng.randrange(len(vals))] for k, vals in DIMENSIONS.items()}


def generate(sessions: Mapping, config_hash: str | None, seed: int = DEFAULT_SEED, total: int = TARGET_TOTAL,
             timeframes: tuple = TIMEFRAMES, max_attempts: int = 400_000) -> dict:
    """``total`` distinct variations: [{variation, definition, identity}] in draw order, plus the counts."""
    from edgelab.strategy.compiler import compile_definition
    rng = random.Random(seed)
    seen_v: set[str] = set()
    seen_logic: set[str] = set()
    rows, invalid, dup, attempts = [], 0, 0, 0
    while len(rows) < total:
        attempts += 1
        if attempts > max_attempts:
            raise RuntimeError(f"only {len(rows)} distinct noise band versions after {max_attempts} draws")
        v = draw(rng)
        if v["timeframe"] not in timeframes or not valid(v):
            invalid += 1
            continue
        key = hash_obj(v, 16)
        if key in seen_v:
            dup += 1
            continue
        seen_v.add(key)
        d = build_definition(v)
        ident = compile_definition(d, sessions, config_hash).identity
        if ident.logic_hash in seen_logic:
            dup += 1
            continue
        seen_logic.add(ident.logic_hash)
        rows.append({"variation": v, "definition": d, "identity": ident.to_dict(), "seq": len(rows) + 1})
    return {"rows": rows, "seed": seed, "attempts": attempts, "invalid": invalid, "duplicates": dup,
            "design_hash": design_hash(), "timeframes": list(timeframes)}
