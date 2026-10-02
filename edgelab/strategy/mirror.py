"""Full mirror ("flip") of a strategy definition (ADR-87): take the OTHER side of every trade the strategy would take.

A long trade entered at reference E with stop S below and target T above becomes a short trade entered on the same signal
at E with its stop at T and its target at S (and vice versa for shorts). The mirror is written as an ordinary DSL document,
so it goes through the same validator, compiler, engine, costs, fills and prop audit as any strategy; nothing here
computes a result, and a stored result is never negated.

What is mirrored (exactly, on the definition's resolved logic: parameters substituted, disabled conditions removed):
  * entry: the long condition becomes the short condition and vice versa; direction long <-> short (both stays both);
    session, weekdays, cooldown and the per-day trade cap are unchanged;
  * entry order: market stays market; a buy STOP at P becomes a sell LIMIT at P (both trigger when price reaches P from
    below) and a sell STOP becomes a buy LIMIT, and the reverse for limit entries; the expiry is unchanged;
  * exits: the target becomes the stop and the stop becomes the target, each in its own kind (points from the fill, ATR
    from the reference, absolute price levels moved to the other side). A risk/reward target is resolved first: with a
    points or ATR stop the mirror's stop is R x the original distance and its target the original distance; with a price
    stop the mirror's stop is the original target level reference + R x (reference - stop), written as an arithmetic
    operand on the same reference (the signal close, or the order price for stop/limit entries);
  * signal exits swap sides; time stop and max hold are unchanged; ``reentry.block_day_after`` swaps stop <-> target.

What is refused by name (never approximated):
  * ``exit.trailing``: a trailing stop would become a trailing TARGET, which the engine does not support;
  * ``exit.no_progress``: the mirror would need an exit on missing ADVERSE excursion, which does not exist;
  * a strategy without a target: its mirror would have no protective stop (a stop is mandatory).

Known, disclosed differences (not refusals): the mirror pays its own spread, commission and slippage (directional BID/ASK:
the other side of the quote), its stop orders become limit orders (other fill rules), and risk-based sizing sizes the
mirror on ITS stop distance (the original target distance), so contract counts and R units differ from the original's.
"""
from __future__ import annotations

import copy
from typing import Any, Mapping

from edgelab.strategy import dsl

OTHER = {"long": "short", "short": "long"}
DIRECTION = {"long": "short", "short": "long", "both": "both"}
ORDER = {"market": "market", "stop": "limit", "limit": "stop"}
REENTRY = {"stop": "target", "target": "stop", "any": "any"}
MIRROR_NAME_SUFFIX = "_flipped"


class MirrorRefusal(ValueError):
    """A definition that cannot be mirrored exactly (``code`` + plain-English ``message``)."""

    def __init__(self, code: str, message: str):
        super().__init__(f"[{code}] {message}")
        self.code, self.message = code, message

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message}


REFUSALS = {
    "MIRROR_TRAILING": "uses a trailing or breakeven stop; its mirror would need a trailing target, which the engine "
                       "does not support",
    "MIRROR_NO_PROGRESS": "uses a no-progress exit; its mirror would need an exit on missing adverse movement, which does "
                          "not exist",
    "MIRROR_NO_TARGET": "has no profit target; its mirror would have no protective stop (a stop is required)",
}


def refusal_reason(defn: Mapping) -> MirrorRefusal | None:
    """Why ``defn`` cannot be mirrored exactly (None = it can). Reads the resolved logic only."""
    ex = dsl.resolve(defn).get("exit") or {}
    if ex.get("trailing") is not None:
        return MirrorRefusal("MIRROR_TRAILING", REFUSALS["MIRROR_TRAILING"])
    if ex.get("no_progress") is not None:
        return MirrorRefusal("MIRROR_NO_PROGRESS", REFUSALS["MIRROR_NO_PROGRESS"])
    if (ex.get("target") or {"type": "none"}).get("type", "none") == "none":
        return MirrorRefusal("MIRROR_NO_TARGET", REFUSALS["MIRROR_NO_TARGET"])
    return None


def _swap_sides(d: Mapping, keys=("long", "short")) -> dict:
    """The same block with its per-side entries moved to the other side (absent sides stay absent)."""
    out = {k: copy.deepcopy(v) for k, v in d.items() if k not in keys}
    for side in keys:
        if side in d:
            out[OTHER[side]] = copy.deepcopy(d[side])
    return out


def _as_stop(target: Mapping) -> dict:
    """An original points / ATR / price TARGET used as the mirror's STOP."""
    t = target["type"]
    if t == "price":
        return {"type": "price", **{k: v for k, v in _swap_sides(target).items() if k != "type"}}
    return copy.deepcopy(dict(target))                      # points from the fill / ATR from the reference: same kind


def _as_target(stop: Mapping) -> dict:
    """An original points / ATR / price STOP used as the mirror's TARGET."""
    if stop["type"] == "price":
        return {"type": "price", **{k: v for k, v in _swap_sides(stop).items() if k != "type"}}
    return copy.deepcopy(dict(stop))


def _reference(entry: Mapping, side: str) -> Any:
    """The operand the engine uses as the entry reference of ``side``: the signal close (market) or the order price."""
    order = entry.get("order") or {"type": "market"}
    if order.get("type", "market") == "market":
        return {"bar": "close"}
    return copy.deepcopy(order[f"{side}_price"])


def _risk_reward(stop: Mapping, multiple: float, entry: Mapping) -> tuple[dict, dict]:
    """(mirror stop, mirror target) for an original ``target: risk_reward`` (the target level resolved explicitly)."""
    r = float(multiple)
    t = stop["type"]
    if t == "points":
        return {"type": "points", "points": r * float(stop["points"])}, {"type": "points", "points": float(stop["points"])}
    if t == "atr":
        base = {k: copy.deepcopy(v) for k, v in stop.items() if k != "multiple"}
        return {**base, "multiple": r * float(stop["multiple"])}, {**base, "multiple": float(stop["multiple"])}
    # price stop: original target = ref + R * (ref - S) for both sides (long: S below, short: S above)
    new_stop = {"type": "price"}
    for side in ("long", "short"):
        if side in stop:
            ref = _reference(entry, side)
            new_stop[OTHER[side]] = {"arith": "add", "args": [
                ref, {"arith": "mul", "args": [r, {"arith": "sub", "args": [copy.deepcopy(ref), copy.deepcopy(stop[side])]}]}]}
    return new_stop, _as_target(stop)


def mirror_definition(defn: Mapping, *, parent_strategy_id: str | None = None) -> dict:
    """The full mirror of ``defn`` as a NEW DSL document (raises MirrorRefusal when it cannot be exact).

    The input is resolved first (parameters substituted, disabled conditions removed), so the mirror carries no
    parameters. Names and texts mark it as a mirror; they are not part of the logic identity."""
    why = refusal_reason(defn)
    if why is not None:
        raise why
    r = dsl.resolve(defn)
    entry, ex = r["entry"], r["exit"]
    out = {k: copy.deepcopy(v) for k, v in r.items() if k not in ("entry", "exit", "name", "description", "family")}
    # ---- entry
    ne = {k: copy.deepcopy(v) for k, v in entry.items() if k not in ("direction", "long", "short", "order", "reentry")}
    ne["direction"] = DIRECTION[entry["direction"]]
    for side in ("long", "short"):
        if entry.get(side) is not None:
            ne[OTHER[side]] = copy.deepcopy(entry[side])
    order = entry.get("order") or {"type": "market"}
    otype = order.get("type", "market")
    no = {"type": ORDER[otype]}
    if otype != "market":
        for side in ("long", "short"):
            if f"{side}_price" in order:
                no[f"{OTHER[side]}_price"] = copy.deepcopy(order[f"{side}_price"])
        if "expiry_bars" in order:
            no["expiry_bars"] = order["expiry_bars"]
    ne["order"] = no
    if entry.get("reentry") is not None:
        ree = dict(entry["reentry"])
        if ree.get("block_day_after") is not None:
            ree["block_day_after"] = REENTRY[ree["block_day_after"]]
        ne["reentry"] = ree
    # ---- exits
    stop, tgt = ex["stop"], ex["target"]
    if tgt["type"] == "risk_reward":
        new_stop, new_tgt = _risk_reward(stop, tgt["multiple"], entry)
    else:
        new_stop, new_tgt = _as_stop(tgt), _as_target(stop)
    nx = {k: copy.deepcopy(v) for k, v in ex.items() if k not in ("stop", "target", "signal")}
    nx["stop"], nx["target"] = new_stop, new_tgt
    if ex.get("signal") is not None:
        nx["signal"] = _swap_sides(ex["signal"])
    # ---- texts (not part of the logic identity)
    name = str(defn.get("name") or "strategy")
    fam = dict(defn.get("family") or {})
    if fam.get("hypothesis"):
        fam["hypothesis"] = f"Inverse of: {fam['hypothesis']}"
    of = f" of {parent_strategy_id}" if parent_strategy_id else ""
    out.update({"name": name if name.endswith(MIRROR_NAME_SUFFIX) else name + MIRROR_NAME_SUFFIX,
                "description": f"Full mirror{of}: every trade on the other side (stop and target swapped). "
                               + str(defn.get("description") or ""),
                "entry": ne, "exit": nx})
    if fam:
        out["family"] = fam
    return out
