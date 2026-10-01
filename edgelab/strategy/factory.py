"""Controlled day-trading strategy factory (ADR-60).

    30 families (factory_space.FAMILIES) x shared dimensions
      -> deterministic weighted sampling per family (frozen allocation, seeded)
      -> 12-stage validation (machine-readable rejections, never repaired)
      -> Strategy-DSL definition, compiled by the normal compiler, identity = logic hash
      -> global de-duplication by logic hash
      -> manifest (strategies / rejections / duplicates) with a content-derived manifest_id

GENERATION ONLY. This module never loads market data, never backtests, never touches a research
protocol, trial ledger or holdout. The manifest is the input of a FUTURE dedicated research protocol.

Determinism. Every random draw is ``u = sha256(seed | family | candidate_seq | dimension)`` mapped to
[0, 1) and applied to the declared (sorted) values and weights: independent of platform, numpy
version, dict order and family order. Same seed + factory/space/allocation versions + DSL/compiler
-> the same candidates, identities and manifest_id.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from datetime import datetime
from typing import Any, Iterable, Mapping
from zoneinfo import ZoneInfo

from edgelab.core.identity import hash_obj
from edgelab.data.schema import timeframe_minutes
from edgelab.strategy import capabilities
from edgelab.strategy import factory_space as S
from edgelab.strategy.compiler import COMPILER_VERSION, compile_definition
from edgelab.strategy.daytrading import (DAY_TRADING_POLICY, DST_PROBE_DATES, check_engine_config,
                                         flat_condition, validate_day_trading)
from edgelab.strategy.dsl import DSL_VERSION, StrategyValidationError, canonical_condition
from edgelab.strategy.factory_space import Reject

FACTORY_VERSION = "edgelab-strategy-factory/5"
DEFAULT_SEED = 20260930
MANIFEST_FORMAT = 1
MAX_ATTEMPTS_PER_ACCEPTED = 30
STAGES = ("schema", "parameter_domain", "causality", "mtf_causality", "day_trading", "session", "entry_exit",
          "stop_target", "trailing", "sizing", "identity", "dataset_timeframe")
TF_WEIGHTS = {"1m": 0.15, "5m": 0.35, "15m": 0.25, "30m": 0.15, "60m": 0.10}
MTF_PROBABILITY = 0.35


class FactoryError(RuntimeError):
    pass


# =============================================================================== deterministic draws
class Draw:
    def __init__(self, seed: int, fid: str, seq: int):
        self.key = f"{int(seed)}|{fid}|{int(seq)}"

    def u(self, dim: str) -> float:
        h = hashlib.sha256(f"{self.key}|{dim}".encode()).hexdigest()
        return int(h[:13], 16) / float(16 ** 13)

    def pick(self, dim: str, values: Iterable, weights: Mapping | None = None):
        vals = list(values)
        if not vals:
            raise FactoryError(f"empty domain for {dim}")
        w = [float(weights[v]) if weights else 1.0 for v in vals]
        x, acc = self.u(dim) * sum(w), 0.0
        for v, wi in zip(vals, w):
            acc += wi
            if x < acc:
                return v
        return vals[-1]


def family_spec(fam: S.Family) -> dict:
    """The declarative definition of a family (what a manifest row's family hash pins)."""
    return {"num": fam.num, "family_id": fam.fid, "name": fam.name, "group": fam.group, "hypothesis": fam.hypothesis,
            "parameters": {k: list(v) for k, v in sorted(fam.params.items())}, "timeframes": list(fam.timeframes),
            "sessions": list(fam.sessions), "anchored_window": bool(fam.window), "mtf": fam.mtf,
            "order_types": ["market"] + (["stop"] if fam.stop_entry else []) + (["limit"] if fam.limit_entry else []),
            "stops": list(fam.stops), "targets": list(fam.targets), "regimes": list(fam.regimes),
            "confirmations": list(fam.confirms), "reversal_exit": bool(fam.reversal_exit and fam.reversal),
            "ict_setup_params": {k: sorted(v) for k, v in sorted(S.ICT_PARAMS.items())} if fam.fid == "ict_liquidity_fvg" else None}


def family_hash(fam: S.Family) -> str:
    return hash_obj(family_spec(fam))


def catalog_hash() -> str:
    return hash_obj([[f.fid, family_hash(f)] for f in S.FAMILIES])


def _json_key(v) -> str:
    return json.dumps(v, sort_keys=True, default=str)


# =============================================================================== sampling
def sample_choice(fam: S.Family, seed: int, seq: int) -> dict:
    d = Draw(seed, fam.fid, seq)
    p = {k: d.pick(f"param.{k}", vals) for k, vals in sorted(fam.params.items())}
    if fam.inactive:
        for k in sorted(fam.inactive(p)):
            p[k] = fam.params[k][0]                 # pinned: never varied when it has no effect
    tf = d.pick("timeframe", fam.timeframes, TF_WEIGHTS)
    tfm = timeframe_minutes(tf)
    ch: dict = {"family_params": p, "timeframe": tf,
                "session": None if fam.window else d.pick(
                    "session", fam.session_domain(p) if fam.session_domain else fam.sessions),
                "flat_rule": d.pick("flat_rule", S.FLAT_RULES, {k: v[0] for k, v in S.FLAT_RULES.items()}),
                "direction": d.pick("direction", S.DIRECTIONS, S.DIRECTIONS),
                "weekdays": d.pick("weekdays", S.WEEKDAY_SETS, {k: v[0] for k, v in S.WEEKDAY_SETS.items()})}
    ch["mtf"] = None
    if fam.mtf and S.MTF_PAIRS.get(tf) and d.u("mtf.use") < MTF_PROBABILITY:
        ch["mtf"] = {"filter": d.pick("mtf.filter", S.MTF_FILTERS), "htf": d.pick("mtf.htf", S.MTF_PAIRS[tf])}
    regs = [r for r in fam.regimes if r != "none"]
    ch["regime"] = "none" if d.u("regime.use") < 0.5 or not regs else d.pick("regime", regs)
    confs = [c for c in fam.confirms if c != "none"]
    ch["confirm"] = "none" if d.u("confirm.use") < 0.6 or not confs else d.pick("confirm", confs)
    orders = ["market"] + (["stop"] if fam.stop_entry else []) + (["limit"] if fam.limit_entry else [])
    ch["order"] = {"type": "market" if len(orders) == 1 or d.u("order.use") < 0.6 else d.pick("order", orders[1:])}
    if ch["order"]["type"] != "market":
        ch["order"]["expiry_bars"] = d.pick("order.expiry", S.ORDER_EXPIRY)
    ch["entry_delay"] = d.pick("entry_delay", S.ENTRY_DELAYS, S.ENTRY_DELAYS) if ch["order"]["type"] == "market" else 0
    sk = d.pick("stop.kind", fam.stops)
    dom = {"buffer": S.BUFFERS_ATR} if sk == "range_side" else S.STOP_DOMAINS[sk](tfm)
    ch["stop"] = {"type": sk, **{k: d.pick(f"stop.{k}", v) for k, v in sorted(dom.items())}}
    tk = d.pick("target.kind", fam.targets)
    ch["target"] = {"type": tk, **{k: d.pick(f"target.{k}", v) for k, v in sorted(S.TARGET_DOMAINS[tk](tfm).items())}}
    ch["stop_bounds"] = "none"
    if sk not in ("points", "atr") and d.u("stop_bounds.use") < 0.3:
        ch["stop_bounds"] = "atr_0.5_3"
    ch["time_exit"] = d.pick("time_exit", S.TIME_EXITS, S.TIME_EXITS)
    sx = {"none": 0.5, "opposite_signal": 0.25, "reversal": 0.25} if fam.reversal_exit and fam.reversal else \
        {"none": 0.7, "opposite_signal": 0.3}
    ch["signal_exit"] = d.pick("signal_exit", sx, sx)
    ch["trailing"] = sample_trailing(d, tfm)
    ch["sizing"] = d.pick("sizing", S.SIZING, S.SIZING)
    ch["no_progress"] = sample_no_progress(d, tfm)
    ch["max_trades"] = d.pick("max_trades", S.MAX_TRADES, S.MAX_TRADES)
    rk = d.pick("reentry.kind", S.REENTRY, S.REENTRY)
    rm = d.pick("reentry.minutes", S.REENTRY_MINUTES)
    ch["reentry"] = {"type": rk, "minutes": rm if rk == "cooldown_after_exit" else None}
    ch["cooldown"] = d.pick("cooldown", S.COOLDOWNS, {k: v[0] for k, v in S.COOLDOWNS.items()})
    return ch


def sample_no_progress(d: Draw, tfm: int) -> dict:
    """No-progress exit (ADR-62). All draws are unconditional so the stream never depends on the outcome."""
    on = d.pick("no_progress.use", S.NO_PROGRESS, S.NO_PROGRESS) == "yes"
    bars = d.pick("no_progress.bars", S.NO_PROGRESS_BARS)
    kind = d.pick("no_progress.kind", ("r", "atr", "points"))
    value = d.pick("no_progress.value", {"r": S.NO_PROGRESS_R, "atr": S.NO_PROGRESS_ATR,
                                        "points": S.STOP_POINTS[tfm][:1]}[kind])
    return {"type": "none"} if not on else {"type": "yes", "bars": bars, "kind": kind, "value": value}


def sample_trailing(d: Draw, tfm: int) -> dict:
    """Trailing choice (ADR-61). Every draw is made unconditionally so the stream never depends on the kind."""
    kind = d.pick("trailing.kind", S.TRAIL_KINDS, S.TRAIL_KINDS)
    tr: dict = {"type": kind}
    if kind == "none":
        return tr
    for k, vals in sorted(S.trailing_domain(kind, tfm).items()):
        tr[k] = d.pick(f"trailing.{k}", vals)
    trig_vals = {"r": S.BE_TRIGGER_R, "atr": S.BE_TRIGGER_ATR, "points": S.STOP_POINTS[tfm][:2]}
    if kind == "breakeven":
        t = d.pick("trailing.be.kind", ("r", "atr", "points"))
        tr["trigger"] = {"type": t, "value": d.pick("trailing.be.value", trig_vals[t])}
        tr["offset"] = d.pick("trailing.be.offset", S.BE_OFFSET)
        return tr
    at = d.pick("trailing.act.kind", S.TRAIL_ACTIVATION, S.TRAIL_ACTIVATION)
    av = d.pick("trailing.act.value", {"r": S.TRAIL_ACT_R, "atr": S.TRAIL_ACT_ATR, "points": S.STOP_POINTS[tfm][:2],
                                      "immediate": (0,)}[at])
    tr["activation"] = {"type": at, "value": None if at == "immediate" else av}
    tr["every_bars"] = d.pick("trailing.every", S.TRAIL_EVERY, S.TRAIL_EVERY)
    tr["only_new_extreme"] = kind in S.LEVEL_TRAILS and d.u("trailing.new_extreme") < 0.25
    tr["min_step"] = 0.0 if d.pick("trailing.step", S.TRAIL_STEP, S.TRAIL_STEP) == "none" else S.TRAIL_STEP_POINTS[tfm]
    off = d.pick("trailing.be_first.offset", S.BE_OFFSET)
    if at == "r" and av > 0.5 and d.u("trailing.be_first") < 0.3:          # breakeven BEFORE the normal trail
        tr["breakeven"] = {"trigger": {"type": "r", "value": 0.5}, "offset": off}
    return tr


# =============================================================================== windows
@dataclass
class Ctx:
    fam: S.Family
    p: dict
    tf: str
    tfm: int
    window: S.Window
    session_key: str
    entry_name: str
    hold_name: str
    sessions: dict = field(default_factory=dict)


def _windows(fam: S.Family, ch: Mapping) -> tuple[S.Window, str]:
    if fam.window:
        return fam.window(ch["family_params"]), f"{fam.fid}_anchored"
    return S.SESSION_PRESETS[ch["session"]], ch["session"]


def _ny_interval(tz: str, start: str, end: str) -> list[tuple[int, int]]:
    """[(start, end)] minutes since 18:00 NY of one instance, for every DST probe date."""
    out = []
    dur = (S.mins(end) - S.mins(start)) % 1440 or 1440
    for d in DST_PROBE_DATES:
        a = datetime(d.year, d.month, d.day, S.mins(start) // 60, S.mins(start) % 60, tzinfo=ZoneInfo(tz))
        a_ny = a.astimezone(ZoneInfo("America/New_York"))
        s0 = (a_ny.hour * 60 + a_ny.minute - 18 * 60) % 1440
        out.append((s0, s0 + dur))
    return out


def check_session(fam: S.Family, ch: Mapping, w: S.Window, tfm: int) -> None:
    flat = S.mins(w.flat) - S.FLAT_RULES[ch["flat_rule"]][1]
    e0, e1 = S.mins(w.entry_start), S.mins(w.entry_end)
    for name, t in (("entry_start", e0), ("entry_end", e1), ("flat", flat)):
        if t % tfm:
            raise Reject("session", "SESSION_TF_MISALIGNED", f"{name} {S.hm(t)} is not on the {tfm}m bar grid")
    if (e1 - e0) % 1440 < tfm:
        raise Reject("session", "SESSION_TOO_SHORT_FOR_TF", f"entry window shorter than one {tfm}m bar")
    if (flat - e1) % 1440 < tfm or (flat - e0) % 1440 <= (e1 - e0) % 1440:
        raise Reject("session", "FLAT_BEFORE_ENTRY_END", "flat time must leave at least one bar after the entry window")
    if fam.session_rule:
        r = fam.session_rule(ch["family_params"], ch["session"])
        if r:
            raise Reject("session", r, f"session {ch['session']!r} not valid for this setup")
    refs = fam.references(ch["family_params"]) if fam.references else {}
    for rname, (tz, wd, a, b) in refs.items():
        if (S.mins(a) % tfm) or (S.mins(b) % tfm):
            raise Reject("session", "SESSION_TF_MISALIGNED", f"reference window {a}-{b} is not on the {tfm}m grid")
        if (S.mins(b) - S.mins(a)) % 1440 < tfm:
            raise Reject("session", "SESSION_TOO_SHORT_FOR_TF", f"reference window {a}-{b} shorter than one bar")
        for (r0, r1), (x0, x1) in zip(_ny_interval(tz, a, b), _ny_interval(w.tz, w.entry_start, w.entry_end)):
            stable = x0 >= r1 or x1 <= r0 or (r0 <= x0 and x1 <= r1)
            if not stable:
                raise Reject("session", "REFERENCE_SESSION_OVERLAPS_ENTRY",
                             f"reference {a}-{b} {tz} completes inside the entry window (its level would switch)")


# =============================================================================== building
def _stop_level(fam, ch, ctx, s):
    st = ch["stop"]
    if st["type"] == "range_side":
        return S.off(S._ref_level(S._orb_ref(ctx.p), -s), st["buffer"], -s)
    if st["type"] == "zone":
        return S.off(fam.zone_stop(ctx.p, s), st["buffer"], -s)
    return S.stop_operand(st["type"], st, s)


def _stop_block(fam, ch, ctx, sides) -> dict:
    st = ch["stop"]
    if st["type"] in ("points", "atr"):
        return S.stop_operand(st["type"], st, 1)
    return {"type": "price", **{side: _stop_level(fam, ch, ctx, 1 if side == "long" else -1) for side in sides}}


def _target_block(ch, sides) -> dict:
    t = ch["target"]
    k = t["type"]
    if k == "none":
        return {"type": "none"}
    if k == "points":
        return {"type": "points", "points": float(t["points"])}
    if k == "atr":
        return {"type": "atr", "multiple": float(t["multiple"]), "period": 14}
    if k == "rr":
        return {"type": "risk_reward", "multiple": float(t["multiple"])}
    return {"type": "price", **{side: S.target_operand(k, t, 1 if side == "long" else -1) for side in sides}}


def _sizing(key: str) -> dict:
    """Sizing block. Every mode trades WHOLE contracts of the execution contract (ADR-63)."""
    out = _sizing_mode(key)
    out["contract"] = S.EXECUTION_CONTRACT
    return out


def _sizing_mode(key: str) -> dict:
    """No account size here: the strategy's identity must not depend on the account (the run supplies it)."""
    if key in S.FIXED_QUANTITY:
        return {"mode": "fixed", "quantity": S.FIXED_QUANTITY[key]}
    if key in S.EQUITY_RISK:
        return {"mode": "equity_risk", "risk_pct": S.EQUITY_RISK[key], "max_quantity": S.RISK_QUANTITY_CAP}
    if key in S.RISK_USD:
        return {"mode": "risk", "risk_usd": S.RISK_USD[key], "max_quantity": S.RISK_QUANTITY_CAP}
    raise KeyError(key)


def build_definition(fam: S.Family, ch: Mapping) -> tuple[dict, Ctx]:
    tf = ch["timeframe"]
    tfm = timeframe_minutes(tf)
    p = dict(ch["family_params"])
    w, skey = _windows(fam, ch)
    flat = S.mins(w.flat) - S.FLAT_RULES[ch["flat_rule"]][1]
    hold_end = S.hm(flat - tfm)
    ename = S.session_name("FXE", w.tz, w.weekdays, w.entry_start, w.entry_end)
    hname = S.session_name("FXH", w.tz, w.weekdays, w.entry_start, hold_end)
    ctx = Ctx(fam, p, tf, tfm, w, skey, ename, hname)
    sessions = {ename: S.session_def(w.tz, w.weekdays, w.entry_start, w.entry_end),
                hname: S.session_def(w.tz, w.weekdays, w.entry_start, hold_end)}
    if fam.references:
        for rn, (tz, wd, a, b) in fam.references(p).items():
            sessions[rn] = S.session_def(tz, wd, a, b)
    ctx.sessions = sessions
    direction = ch["direction"]
    sides = {"long": ("long",), "short": ("short",), "both": ("long", "short")}[direction]
    otype = ch["order"]["type"]
    entry: dict = {"direction": direction, "session": ename}
    order: dict = {"type": otype}
    signal: dict = {}
    for side in sides:
        s = 1 if side == "long" else -1
        if otype == "market":
            core = S.lagged(fam.entry(p, s, ctx), ch["entry_delay"])
        else:
            core, level = (fam.stop_entry if otype == "stop" else fam.limit_entry)(p, s, ctx)
            order[f"{side}_price"] = level
        conds = [core, S.regime_cond(ch["regime"], s), S.confirm_cond(ch["confirm"], s)]
        if ch["mtf"]:
            conds.append(S.mtf_cond(ch["mtf"]["filter"], ch["mtf"]["htf"], s))
        if ch["stop_bounds"] != "none":
            ref = order.get(f"{side}_price", S.CLOSE)
            dist = S.ar("sub", ref, _stop_level(fam, ch, ctx, s)) if s > 0 else \
                S.ar("sub", _stop_level(fam, ch, ctx, s), ref)
            conds += [S.cmp(dist, ">=", S.ar("mul", 0.5, S.ATR)), S.cmp(dist, "<=", S.ar("mul", 3.0, S.ATR))]
        entry[side] = S.ALL(*conds)
        exits = [flat_condition(hname)]
        if ch["signal_exit"] == "opposite_signal":
            exits.append(fam.entry(p, -s, ctx))
        elif ch["signal_exit"] == "reversal":
            exits.append(fam.reversal(p, s, ctx))
        signal[side] = {"any": exits} if len(exits) > 1 else exits[0]
    if otype != "market":
        order["expiry_bars"] = ch["order"]["expiry_bars"]
    entry["order"] = order
    wd = S.WEEKDAY_SETS[ch["weekdays"]][1]
    if wd:
        entry["trading_weekdays"] = list(wd)
    cd = S.COOLDOWNS[ch["cooldown"]][1]
    if cd == "window":
        entry["cooldown_bars"] = max(1, ((S.mins(w.entry_end) - S.mins(w.entry_start)) % 1440) // tfm)
    elif cd:
        entry["cooldown_bars"] = cd // tfm
    hold_minutes = (flat - S.mins(w.entry_start)) % 1440
    te = ch["time_exit"]
    max_hold = (hold_minutes // tfm) if te == "window" else te // tfm
    if ch["max_trades"]:
        entry["max_trades_per_day"] = int(ch["max_trades"])
    rk = ch["reentry"]["type"]
    if rk == "block_after_stop":
        entry["reentry"] = {"block_day_after": "stop"}
    elif rk == "block_after_target":
        entry["reentry"] = {"block_day_after": "target"}
    elif rk == "cooldown_after_exit":
        entry["reentry"] = {"cooldown_bars": ch["reentry"]["minutes"] // tfm}
    exit_ = {"stop": _stop_block(fam, ch, ctx, sides), "target": _target_block(ch, sides),
             "max_hold_bars": int(max_hold), "signal": signal}
    npg = ch["no_progress"]
    if npg["type"] == "yes":
        exit_["no_progress"] = {"bars": int(npg["bars"]), "min_progress": {"type": npg["kind"], "value": float(npg["value"])}}
        if npg["kind"] == "atr":
            exit_["no_progress"]["atr_period"] = 14
    trailing = S.trailing_block(ch["trailing"], sides)
    if trailing:
        exit_["trailing"] = trailing
    defn = {"dsl_version": DSL_VERSION, "name": f"{fam.fid}_candidate",
            "description": describe(fam, ch),
            "family": {"id": fam.fid, "name": fam.name, "category": fam.group, "hypothesis": fam.hypothesis},
            "timeframe": tf, "sessions": sessions, "entry": entry, "exit": exit_, "sizing": _sizing(ch["sizing"])}
    return defn, ctx


def describe(fam: S.Family, ch: Mapping) -> str:
    """Plain-language rule summary (no performance language)."""
    p = ", ".join(f"{k}={v}" for k, v in sorted(ch["family_params"].items()))
    m = f"; HTF {ch['mtf']['filter']}@{ch['mtf']['htf']}" if ch["mtf"] else ""
    return (f"{fam.name} [{p}] on {ch['timeframe']}{m}; session {ch['session'] or 'anchored'} ({ch['flat_rule']}); "
            f"{ch['direction']}; stop {ch['stop']['type']}; target {ch['target']['type']}; "
            f"trailing {ch['trailing']['type']}; "
            f"generated by {FACTORY_VERSION} (day-trading, flat same NY trading date)")


# =============================================================================== validation
def _check_param_domain(fam: S.Family, ch: Mapping) -> None:
    p = ch["family_params"]
    if set(p) != set(fam.params):
        raise Reject("parameter_domain", "PARAMETER_SET_MISMATCH", f"expected {sorted(fam.params)}")
    for k, v in p.items():
        if v not in fam.params[k]:
            raise Reject("parameter_domain", "PARAMETER_OUT_OF_DOMAIN", f"{k}={v!r} not in {list(fam.params[k])}")
    if fam.inactive:
        for k in fam.inactive(p):
            if p[k] != fam.params[k][0]:
                raise Reject("parameter_domain", "INACTIVE_PARAMETER_VARIED", f"{k} has no effect for this variant")
    if fam.constraint:
        r = fam.constraint(p)
        if r:
            raise Reject("parameter_domain", r, str(p))


def _check_structure(fam: S.Family, ch: Mapping) -> None:
    tk = (ch.get("trailing") or {}).get("type", "none")
    if tk in S.NOT_EXECUTABLE["trailing"]:
        code = S.NOT_EXECUTABLE["trailing"][tk]
        raise Reject("trailing", code, S.NOT_EXECUTABLE_REASONS.get(code, tk))
    if tk not in S.TRAIL_KINDS:
        raise Reject("trailing", "TRAILING_UNKNOWN", f"unknown trailing kind {tk!r}")
    if ch["sizing"] not in S.SIZING:
        code = S.NOT_EXECUTABLE["sizing"].get(ch["sizing"], "UNKNOWN_SIZING")
        raise Reject("sizing", code, S.NOT_EXECUTABLE_REASONS.get(code, f"unknown sizing {ch['sizing']!r}"))
    tf = ch["timeframe"]
    if tf not in S.TIMEFRAMES or tf not in fam.timeframes:
        raise Reject("dataset_timeframe", "TIMEFRAME_NOT_DERIVABLE", f"{tf} is not a supported factory timeframe")
    tfm = timeframe_minutes(tf)
    if ch["mtf"]:
        if not fam.mtf:
            raise Reject("mtf_causality", "MTF_NOT_APPLICABLE", fam.fid)
        if ch["mtf"]["htf"] not in S.MTF_PAIRS.get(tf, ()):
            raise Reject("mtf_causality", "MTF_PAIR_NOT_ALLOWED", f"{tf} -> {ch['mtf']['htf']}")
        if ch["mtf"]["filter"] not in S.MTF_FILTERS:
            raise Reject("mtf_causality", "MTF_FILTER_UNKNOWN", ch["mtf"]["filter"])
    if not fam.window and ch["session"] not in fam.sessions:
        raise Reject("session", "SESSION_NOT_APPLICABLE", f"{ch['session']!r} for {fam.fid}")
    if ch["regime"] not in fam.regimes:
        code = S.NOT_EXECUTABLE["regime"].get(ch["regime"], "REDUNDANT_OR_INAPPLICABLE_FILTER")
        raise Reject("entry_exit", code, f"regime {ch['regime']!r} for {fam.fid}")
    if ch["regime"] == "prior_day_nr" and fam.fid == "nr_breakout" and ch["family_params"].get("scope") == "day":
        raise Reject("entry_exit", "REDUNDANT_OR_INAPPLICABLE_FILTER", "the day-scope NR7 family already requires it")
    if ch["confirm"] not in fam.confirms:
        raise Reject("entry_exit", "REDUNDANT_OR_INAPPLICABLE_FILTER", f"confirm {ch['confirm']!r} for {fam.fid}")
    otype = ch["order"]["type"]
    if (otype == "stop" and not fam.stop_entry) or (otype == "limit" and not fam.limit_entry):
        raise Reject("entry_exit", "ORDER_TYPE_NOT_APPLICABLE", f"{otype} entry for {fam.fid}")
    if otype != "market" and ch["entry_delay"]:
        raise Reject("entry_exit", "DELAY_WITH_RESTING_ORDER", "entry delay applies to market entries only")
    if ch["signal_exit"] == "reversal" and not (fam.reversal_exit and fam.reversal):
        raise Reject("entry_exit", "REVERSAL_EXIT_NOT_DEFINED", fam.fid)
    st, tg = ch["stop"], ch["target"]
    if st["type"] not in fam.stops:
        code = S.NOT_EXECUTABLE["stop"].get(st["type"], "STOP_TYPE_NOT_APPLICABLE")
        raise Reject("stop_target", code, st["type"])
    if tg["type"] not in fam.targets:
        code = S.NOT_EXECUTABLE["target"].get(tg["type"], "TARGET_TYPE_NOT_APPLICABLE")
        raise Reject("stop_target", code, tg["type"])
    if tg["type"] == "points" and st["type"] == "points":
        raise Reject("stop_target", "EQUIVALENT_REPRESENTATION", "points target with a points stop == risk_reward")
    if tg["type"] == "atr" and st["type"] == "atr":
        raise Reject("stop_target", "EQUIVALENT_REPRESENTATION", "ATR target with an ATR(14) stop == risk_reward")
    if st["type"] in ("points", "atr") and ch["stop_bounds"] != "none":
        raise Reject("stop_target", "REDUNDANT_STOP_BOUNDS", "a fixed-distance stop is already bounded")
    te = ch["time_exit"]
    if te not in S.TIME_EXITS:
        code = S.NOT_EXECUTABLE["time_exit"].get(str(te), "TIME_EXIT_UNKNOWN")
        raise Reject("entry_exit", code, str(te))
    if tg["type"] == "none" and ch["signal_exit"] == "none" and te == "window":
        raise Reject("entry_exit", "NO_TARGET_WITHOUT_EXIT_RULE",
                     "no target needs a signal exit or a time exit shorter than the session")
    w, _ = _windows(fam, ch)
    check_session(fam, ch, w, tfm)
    flat = S.mins(w.flat) - S.FLAT_RULES[ch["flat_rule"]][1]
    hold = (flat - S.mins(w.entry_start)) % 1440
    if te != "window":
        if te < 2 * tfm or te % tfm:
            raise Reject("entry_exit", "TIME_EXIT_TOO_SHORT_FOR_TF", f"{te} min on {tf}")
        if te >= hold:
            raise Reject("entry_exit", "REDUNDANT_TIME_EXIT", f"{te} min >= the {hold} min holding window")
    bars = (hold if te == "window" else te) // tfm
    reach = 1.5 * bars ** 0.5
    if tg["type"] == "atr" and tg["multiple"] > reach:
        raise Reject("stop_target", "TARGET_UNREACHABLE_IN_WINDOW",
                     f"{tg['multiple']} ATR target > 1.5*sqrt({bars} bars) in the holding window")
    if tg["type"] == "rr" and st["type"] == "atr" and st["multiple"] * tg["multiple"] > reach:
        raise Reject("stop_target", "TARGET_UNREACHABLE_IN_WINDOW",
                     f"{st['multiple']}x{tg['multiple']} ATR target > 1.5*sqrt({bars} bars)")
    _check_trailing(ch, tfm, bars)
    _check_management(fam, ch, tfm, bars, w, flat)
    cd = S.COOLDOWNS[ch["cooldown"]][1]
    e_len = (S.mins(w.entry_end) - S.mins(w.entry_start)) % 1440
    if isinstance(cd, int) and cd < tfm:
        raise Reject("entry_exit", "COOLDOWN_SHORTER_THAN_BAR", f"{cd} min on {tf}")
    if isinstance(cd, int) and cd >= e_len:
        raise Reject("entry_exit", "REDUNDANT_COOLDOWN", f"{cd} min >= the {e_len} min entry window "
                                                         "(identical to one_per_window)")
    if ch["flat_rule"] != "session_flat" and te != "window":
        latest_fill = S.mins(w.entry_end) + (ch["order"].get("expiry_bars", 1) - 1) * tfm
        if (latest_fill + te - S.mins(w.entry_start)) % 1440 <= (flat - S.mins(w.entry_start)) % 1440:
            raise Reject("entry_exit", "REDUNDANT_FLAT_RULE", "the time exit always binds before the earlier flat")


def _check_management(fam: S.Family, ch: Mapping, tfm: int, bars: int, w: S.Window, flat: int) -> None:
    """No-progress exit, per-strategy trade cap and exit-based re-entry (ADR-62): domains and redundancy."""
    npg, cap, re_ = ch["no_progress"], ch["max_trades"], ch["reentry"]
    tgt = ch["target"]
    if npg["type"] == "yes":
        ok = {"r": S.NO_PROGRESS_R, "atr": S.NO_PROGRESS_ATR, "points": S.STOP_POINTS[tfm][:1]}.get(npg["kind"])
        if npg["bars"] not in S.NO_PROGRESS_BARS or ok is None or npg["value"] not in ok:
            raise Reject("entry_exit", "NO_PROGRESS_PARAM_OUT_OF_DOMAIN", str(npg))
        if npg["bars"] >= bars:
            raise Reject("entry_exit", "REDUNDANT_NO_PROGRESS", f"check at bar {npg['bars']} but the window has {bars} bars")
        if (npg["kind"] == "r" and tgt["type"] == "rr" and npg["value"] >= tgt["multiple"]) or \
                (npg["kind"] == "points" and tgt["type"] == "points" and npg["value"] >= tgt["points"]):
            raise Reject("entry_exit", "NO_PROGRESS_ABOVE_TARGET", "the required progress reaches the target itself")
    if cap not in S.MAX_TRADES:
        raise Reject("entry_exit", "MAX_TRADES_OUT_OF_DOMAIN", str(cap))
    rk = re_["type"]
    if rk not in S.REENTRY:
        raise Reject("entry_exit", S.NOT_EXECUTABLE["frequency"].get(rk, "REENTRY_UNKNOWN"), str(rk))
    if cap == 1 and (rk != "none" or ch["cooldown"] == "one_per_window"):
        raise Reject("entry_exit", "REDUNDANT_REENTRY", "a one-trade-per-day cap already forbids every re-entry")
    if rk == "block_after_target" and tgt["type"] == "none":
        raise Reject("entry_exit", "REDUNDANT_REENTRY", "no target: a target exit can never happen")
    if rk == "cooldown_after_exit":
        m = re_.get("minutes")
        e_len = (S.mins(w.entry_end) - S.mins(w.entry_start)) % 1440
        if m not in S.REENTRY_MINUTES or m % tfm or m < tfm:
            raise Reject("entry_exit", "COOLDOWN_SHORTER_THAN_BAR", f"{m} min on {tfm}m bars")
        if m >= e_len:
            raise Reject("entry_exit", "REDUNDANT_REENTRY", f"{m} min >= the {e_len} min entry window")

def _check_trailing(ch: Mapping, tfm: int, bars: int) -> None:
    """Trailing-specific admissibility (ADR-61): domains, redundancy, room to act. Never repairs."""
    tr = ch["trailing"]
    k = tr["type"]
    if k == "none":
        return
    for name, vals in S.trailing_domain(k, tfm).items():
        if tr.get(name) not in vals:
            raise Reject("trailing", "TRAILING_PARAM_OUT_OF_DOMAIN", f"{k}.{name}={tr.get(name)!r} not in {list(vals)}")
    if bars < 3:
        raise Reject("trailing", "TRAIL_NO_ROOM", f"only {bars} bars in the holding window")
    tgt_r = ch["target"].get("multiple") if ch["target"]["type"] == "rr" else None
    tgt_pts = ch["target"].get("points") if ch["target"]["type"] == "points" else None

    def binds(kind: str, value: float) -> None:
        if (kind == "r" and tgt_r is not None and value >= tgt_r) or \
                (kind == "points" and tgt_pts is not None and value >= tgt_pts):
            raise Reject("trailing", "REDUNDANT_TRAIL_ACTIVATION",
                         f"{kind} {value} is not reached before the {tgt_r or tgt_pts} target")

    if k == "breakeven":
        trg = tr.get("trigger") or {}
        ok = {"r": S.BE_TRIGGER_R, "atr": S.BE_TRIGGER_ATR, "points": S.STOP_POINTS[tfm][:2]}.get(trg.get("type"))
        if ok is None or trg.get("value") not in ok or tr.get("offset") not in S.BE_OFFSET:
            raise Reject("trailing", "TRAILING_PARAM_OUT_OF_DOMAIN", f"breakeven {trg} offset {tr.get('offset')}")
        binds(trg["type"], trg["value"])
        return
    act = tr["activation"]
    ok = {"immediate": (None,), "r": S.TRAIL_ACT_R, "atr": S.TRAIL_ACT_ATR, "points": S.STOP_POINTS[tfm][:2]}.get(act["type"])
    if ok is None or act["value"] not in ok:
        raise Reject("trailing", "TRAILING_PARAM_OUT_OF_DOMAIN", f"activation {act}")
    if act["type"] != "immediate":
        binds(act["type"], act["value"])
    if tr["every_bars"] not in S.TRAIL_EVERY:
        raise Reject("trailing", "TRAILING_PARAM_OUT_OF_DOMAIN", f"every_bars {tr['every_bars']}")
    if tr["every_bars"] >= bars:
        raise Reject("trailing", "TRAIL_NEVER_UPDATES", f"every {tr['every_bars']} bars in a {bars}-bar window")
    if tr["only_new_extreme"] and k not in S.LEVEL_TRAILS:
        raise Reject("trailing", "REDUNDANT_ONLY_NEW_EXTREME", "a distance trail only improves on a new extreme anyway")
    if tr["min_step"] not in (0.0, S.TRAIL_STEP_POINTS[tfm]):
        raise Reject("trailing", "TRAILING_PARAM_OUT_OF_DOMAIN", f"min_step {tr['min_step']}")
    be = tr.get("breakeven")
    if be:
        if act["type"] != "r" or act["value"] <= be["trigger"]["value"] or be["offset"] not in S.BE_OFFSET:
            raise Reject("trailing", "BE_NOT_BEFORE_TRAIL", "breakeven must trigger strictly before the trail activates")
        binds("r", be["trigger"]["value"])


@dataclass
class Candidate:
    family: S.Family
    seq: int
    choice: dict
    definition: dict | None = None
    identity: dict | None = None
    rejection: dict | None = None


def validate_candidate(fam: S.Family, ch: Mapping) -> tuple[dict, dict]:
    """-> (definition, identity) or raises Reject. Order: structural stages, day-trading, DSL, identity."""
    _check_param_domain(fam, ch)
    _check_structure(fam, ch)
    defn, ctx = build_definition(fam, ch)
    dt = validate_day_trading(defn)
    if dt:
        raise Reject("day_trading", dt[0]["code"], dt[0]["detail"])
    ent = defn["entry"]
    if ent["direction"] == "both" and _json_key(canonical_condition(ent["long"])) == \
            _json_key(canonical_condition(ent["short"])):            # the engine drops every such bar
        raise Reject("entry_exit", "AMBIGUOUS_BOTH_DIRECTIONS", "long and short trigger on identical conditions")
    try:
        cd = compile_definition(defn, {})
    except StrategyValidationError as exc:
        errs = exc.result.errors
        first = errs[0] if errs else None
        causal = first is not None and ("future" in first.message or "causal" in first.message)
        raise Reject("causality" if causal else "schema", "DSL_INVALID",
                     "; ".join(f"{i.path}: {i.message}" for i in errs[:3])) from None
    except Exception as exc:                             # compiler refusal (feature params, ...)
        raise Reject("schema", "COMPILE_FAILED", str(exc)[:300]) from None
    lh = cd.identity.logic_hash
    defn["name"] = f"{fam.fid}_{lh[:10]}"                # cosmetic, derived from the logic (not in logic hash)
    cd = compile_definition(defn, {})
    if cd.identity.logic_hash != lh:
        raise Reject("identity", "IDENTITY_UNSTABLE", "renaming changed the logic hash")
    return defn, cd.identity.to_dict()


# =============================================================================== tags / lineage
def explorer_tags(fam: S.Family, ch: Mapping, defn: Mapping) -> dict:
    st = ch["stop"]["type"]
    if st in ("swing", "recent_extreme", "prev_bar", "day_structure", "channel", "range_side") and \
            ch["stop"].get("buffer", 0):
        st = f"{st}+atr_buffer"
    risk = ch["sizing"]
    w, skey = _windows(fam, ch)
    return {"family_id": fam.fid, "family_num": fam.num, "group": fam.group, "timeframe": ch["timeframe"],
            "mtf": bool(ch["mtf"]), "htf": ch["mtf"]["htf"] if ch["mtf"] else None,
            "mtf_filter": ch["mtf"]["filter"] if ch["mtf"] else None, "session": skey,
            "session_timezone": w.tz, "flat_rule": ch["flat_rule"], "weekday_filter": ch["weekdays"],
            "direction": ch["direction"], "order_type": ch["order"]["type"], "confirm": ch["confirm"],
            "entry_delay": ch["entry_delay"], "regime_filter": ch["regime"], "stop_type": st,
            "target_type": ch["target"]["type"], "time_exit": str(ch["time_exit"]), "signal_exit": ch["signal_exit"],
            "trailing_type": ch["trailing"]["type"],
            "trailing_activation": (ch["trailing"].get("activation") or {}).get("type", "n/a"),
            "breakeven": bool(ch["trailing"].get("breakeven") or ch["trailing"]["type"] == "breakeven"),
            "execution_contract": S.EXECUTION_CONTRACT, "risk_model": risk, "sizing_mode": ("fixed" if risk in S.FIXED_QUANTITY else
                                                  "equity_risk" if risk in S.EQUITY_RISK else "risk"),
            "no_progress": ch["no_progress"]["type"], "max_trades_per_day": ch["max_trades"] or "none",
            "reentry": ch["reentry"]["type"], "cooldown": ch["cooldown"],
            "setup": ch["family_params"].get("setup")}


def prop_inputs(defn: Mapping, ch: Mapping) -> dict:
    """What a later prop-account simulation needs besides the trades (never evaluated here)."""
    sess = defn["sessions"]
    e = sess[defn["entry"]["session"]]
    return {"sizing": defn["sizing"], "trading_weekdays": defn["entry"].get("trading_weekdays", "all"),
            "entry_window": {k: e[k] for k in ("timezone", "start", "end")},
            "max_hold_bars": defn["exit"]["max_hold_bars"], "timeframe": defn["timeframe"],
            "same_trading_date": True, "overnight": False}


def lineage_record(row: Mapping):
    """The library LineageRecord for a manifest row (for a later, explicit materialization step)."""
    from edgelab.strategy.lineage import LineageRecord
    lin = row["lineage"]
    return LineageRecord(row["strategy_id"], row["logic_hash"], row["definition_hash"], row["family_id"],
                         "factory_variant", parent_strategy_id=None, changes=[],
                         generation_parameters={"variation": row["variation"], "candidate_seq": row["candidate_seq"],
                                                "allocation_bucket": row["allocation_bucket"],
                                                "parent_template": lin["parent_template"], "seed": lin["seed"]},
                         generation_batch_id=row.get("manifest_id"),
                         versions={k: lin[k] for k in ("factory_version", "variation_space_version",
                                                       "allocation_version", "dsl_version", "compiler_version")})


# =============================================================================== generation
@dataclass
class FactoryResult:
    header: dict
    strategies: list = field(default_factory=list)
    rejections: list = field(default_factory=list)
    duplicates: list = field(default_factory=list)

    @property
    def manifest_id(self) -> str:
        return self.header["manifest_id"]


def generate(seed: int = DEFAULT_SEED, quotas: Mapping[str, int] | None = None,
             families: Iterable[str] | None = None, progress=None) -> FactoryResult:
    """Generate the candidate universe. ``quotas`` defaults to the frozen allocation (10,000)."""
    alloc = S.allocate()
    q = dict(quotas) if quotas is not None else dict(alloc)
    fams = [f for f in S.FAMILIES if (families is None or f.fid in set(families)) and q.get(f.fid, 0) > 0]
    seen: dict[str, str] = {}                            # logic_hash -> strategy_id
    seen_def: dict[str, str] = {}
    strategies, rejections, duplicates = [], [], []
    attempts = {}
    for fam in fams:
        want, got, seq = q[fam.fid], 0, 0
        limit = want * MAX_ATTEMPTS_PER_ACCEPTED
        while got < want:
            if seq >= limit:
                raise FactoryError(f"{fam.fid}: only {got}/{want} unique valid variants after {seq} candidates "
                                   f"(space too small or too many rejections) - refusing to under-fill silently")
            ch = sample_choice(fam, seed, seq)
            try:
                defn, ident = validate_candidate(fam, ch)
            except Reject as r:
                rejections.append({"family_id": fam.fid, "candidate_seq": seq, "valid": False,
                                   "rejection": r.to_dict(), "variation": ch})
                seq += 1
                continue
            lh, dh = ident["logic_hash"], ident["definition_hash"]
            if lh in seen:
                duplicates.append({"family_id": fam.fid, "candidate_seq": seq, "logic_hash": lh,
                                   "duplicate_of": seen[lh], "same_definition_hash": dh in seen_def,
                                   "variation": ch})
                seq += 1
                continue
            seen[lh] = ident["strategy_id"]
            seen_def[dh] = ident["strategy_id"]
            strategies.append({
                "strategy_id": ident["strategy_id"], "logic_hash": lh, "definition_hash": dh, "valid": True,
                "family_id": fam.fid, "family_name": fam.name, "group": fam.group,
                "allocation_bucket": f"{fam.group}/{fam.fid}", "candidate_seq": seq, "family_rank": got,
                "variation": ch, "tags": explorer_tags(fam, ch, defn), "prop_inputs": prop_inputs(defn, ch),
                "lineage": {"generation_method": "factory_variant", "parent_template": f"{fam.fid}@{S.VARIATION_SPACE_VERSION}",
                            "family_spec_sha256": family_hash(fam), "setup": ch["family_params"].get("setup"),
                            "seed": seed, "factory_version": FACTORY_VERSION,
                            "variation_space_version": S.VARIATION_SPACE_VERSION,
                            "allocation_version": S.ALLOCATION_VERSION, "dsl_version": DSL_VERSION,
                            "compiler_version": COMPILER_VERSION},
                "definition": defn})
            got += 1
            seq += 1
        attempts[fam.fid] = seq
        if progress:
            progress(fam.fid, got, seq)
    header = _header(seed, q, alloc, strategies, rejections, duplicates, attempts, quotas is None)
    return FactoryResult(header, strategies, rejections, duplicates)


def _counts(rows: list, key) -> dict:
    out: dict = {}
    for r in rows:
        k = key(r)
        out[str(k)] = out.get(str(k), 0) + 1
    return dict(sorted(out.items()))


def distributions(strategies: list) -> dict:
    keys = [k for k in (strategies[0]["tags"] if strategies else {})]
    return {k: _counts(strategies, lambda r, k=k: r["tags"][k]) for k in keys}


def _header(seed, quotas, alloc, strategies, rejections, duplicates, attempts, full: bool) -> dict:
    ident = {
        "manifest_format": MANIFEST_FORMAT, "factory_version": FACTORY_VERSION,
        "variation_space_version": S.VARIATION_SPACE_VERSION, "allocation_version": S.ALLOCATION_VERSION,
        "dsl_version": DSL_VERSION, "compiler_version": COMPILER_VERSION, "seed": int(seed),
        "catalog_sha256": catalog_hash(), "capability_sha256": hash_obj(capabilities.matrix()), "quotas": dict(sorted(quotas.items())), "full_allocation": full,
        "strategies_sha256": hash_obj([[r["strategy_id"], r["definition_hash"], r["family_id"], r["candidate_seq"]]
                                       for r in strategies]),
        "rejections_sha256": hash_obj([[r["family_id"], r["candidate_seq"], r["rejection"]["code"]] for r in rejections]),
        "duplicates_sha256": hash_obj([[r["family_id"], r["candidate_seq"], r["logic_hash"]] for r in duplicates]),
    }
    mid = "FM_" + hash_obj(ident)[:16].upper()
    fams = [{**family_spec(f), "family_spec_sha256": family_hash(f), "scores": dict(f.scores),
             "score_total": sum(f.scores.values()), "allocation": alloc[f.fid]} for f in S.FAMILIES]
    n_gen = sum(attempts.values())
    return {"manifest_id": mid, "identity": ident, "families": fams,
            "allocation": {"method": S.ALLOCATION_METHOD, "per_family": dict(alloc),
                           "per_group": {g: sum(alloc[f.fid] for f in S.FAMILIES if f.group == g) for g in S.GROUPS},
                           "group_share": dict(S.GROUP_SHARE), "min_per_family": S.MIN_PER_FAMILY},
            "day_trading_policy": DAY_TRADING_POLICY,
            "engine_config_requirements": "edgelab.strategy.daytrading.check_engine_config(backtest) must return []",
            "dimensions": S.dimension_catalog(),
            "validation_stages": list(STAGES), "capability_matrix": capabilities.matrix(),
            "counts": {"candidates_generated": n_gen, "valid_unique": len(strategies), "rejected": len(rejections),
                       "duplicates": len(duplicates), "per_family_candidates": dict(sorted(attempts.items())),
                       "per_family_valid": _counts(strategies, lambda r: r["family_id"]),
                       "per_group_valid": _counts(strategies, lambda r: r["group"]),
                       "rejections_by_code": _counts(rejections, lambda r: r["rejection"]["code"]),
                       "rejections_by_stage": _counts(rejections, lambda r: r["rejection"]["stage"])},
            "distributions": distributions(strategies),
            "protocol": {"numerical_trials_executed": 0, "holdout_looks": 0, "market_data_read": False,
                         "note": "generation only; evaluating this universe needs a dedicated research protocol whose "
                                 "trial budget covers it. No existing protocol is modified.",
                         "future_trial_identity": "research.protocol.trial_key(protocol_id, logic_hash, "
                                                  "eval_content_hash, config_hash)"},
            "regenerate": f"python -m edgelab.cli factory generate --seed {int(seed)}"}


# =============================================================================== persistence / explorer
def write_manifest(res: FactoryResult, out_dir: str | Path) -> Path:
    d = Path(out_dir)
    d.mkdir(parents=True, exist_ok=True)
    for name, rows in (("strategies.jsonl", res.strategies), ("rejections.jsonl", res.rejections),
                       ("duplicates.jsonl", res.duplicates)):
        tmp = d / (name + ".tmp")
        with tmp.open("w") as fh:
            for r in rows:
                fh.write(json.dumps(r, sort_keys=True, default=str) + "\n")
        tmp.replace(d / name)
    tmp = d / "manifest.json.tmp"
    tmp.write_text(json.dumps(res.header, indent=1, sort_keys=True, default=str))
    tmp.replace(d / "manifest.json")
    return d


def read_header(manifest_dir: str | Path) -> dict:
    return json.loads((Path(manifest_dir) / "manifest.json").read_text())


def iter_rows(manifest_dir: str | Path, kind: str = "strategies"):
    with (Path(manifest_dir) / f"{kind}.jsonl").open() as fh:
        for line in fh:
            if line.strip():
                yield json.loads(line)


EXPLORER_FILTERS = ("family_id", "group", "timeframe", "mtf", "htf", "session", "risk_model", "sizing_mode",
                    "no_progress", "max_trades_per_day", "reentry", "stop_type",
                    "target_type", "trailing_type", "trailing_activation", "weekday_filter", "regime_filter", "direction", "order_type",
                    "setup")


def query(manifest_dir: str | Path, filters: Mapping[str, Any] | None = None, limit: int = 100,
          offset: int = 0, include_definition: bool = False) -> dict:
    """Explorer query over the manifest (tag equality filters + strategy_id / logic_hash / version)."""
    f = dict(filters or {})
    unknown = set(f) - set(EXPLORER_FILTERS) - {"strategy_id", "logic_hash", "factory_version"}
    if unknown:
        raise ValueError(f"unknown filters {sorted(unknown)}; available: {EXPLORER_FILTERS + ('strategy_id', 'logic_hash', 'factory_version')}")
    rows, total = [], 0
    for r in iter_rows(manifest_dir):
        ok = True
        for k, v in f.items():
            if k in ("strategy_id", "logic_hash"):
                ok &= str(r[k]).startswith(str(v)) if k == "logic_hash" else r[k] == v
            elif k == "factory_version":
                ok &= r["lineage"]["factory_version"] == v
            elif k == "mtf":
                ok &= bool(r["tags"]["mtf"]) == (str(v).lower() in ("1", "true", "yes"))
            else:
                ok &= str(r["tags"].get(k)) == str(v)
        if not ok:
            continue
        total += 1
        if offset <= total - 1 < offset + limit:
            rows.append(r if include_definition else {k: r[k] for k in r if k != "definition"})
    return {"total": total, "offset": offset, "limit": limit, "rows": rows}


def summary(manifest_dir: str | Path) -> dict:
    h = read_header(manifest_dir)
    return {k: h[k] for k in ("manifest_id", "identity", "counts", "allocation", "distributions", "protocol",
                              "day_trading_policy", "regenerate")}


def verify(manifest_dir: str | Path) -> dict:
    """Regenerate from the recorded seed/quotas and compare identities (reproducibility check)."""
    h = read_header(manifest_dir)
    idn = h["identity"]
    for k, v in (("factory_version", FACTORY_VERSION), ("variation_space_version", S.VARIATION_SPACE_VERSION),
                 ("allocation_version", S.ALLOCATION_VERSION), ("dsl_version", DSL_VERSION),
                 ("compiler_version", COMPILER_VERSION)):
        if idn[k] != v:
            return {"reproducible": False, "reason": f"{k} differs: manifest {idn[k]} vs code {v}"}
    res = generate(idn["seed"], None if idn["full_allocation"] else idn["quotas"])
    return {"reproducible": res.manifest_id == h["manifest_id"], "manifest_id": h["manifest_id"],
            "regenerated_manifest_id": res.manifest_id}


__all__ = ["generate", "write_manifest", "read_header", "iter_rows", "query", "summary", "verify",
           "validate_candidate", "sample_choice", "build_definition", "check_engine_config", "FactoryError",
           "FACTORY_VERSION", "DEFAULT_SEED"]
