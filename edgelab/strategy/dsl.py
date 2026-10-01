"""EdgeLab Strategy DSL (version 1): definition, validation, canonical form, identity.

A strategy document is plain data (YAML/JSON/dict). This module answers only:
"is this a valid, causal, executable definition, and what is its identity?"
It never evaluates performance. See STRATEGY_DSL.md for the full schema.

Pipeline
    load_definition(src)                -> raw dict
    validate(raw, sessions)             -> ValidationResult (all issues, with paths)
    resolve(raw)                        -> parameters substituted, disabled conditions pruned
    canonical_logic(resolved)           -> normalized logic  -> logic_hash  -> strategy_id
    canonical_definition(raw)           -> normalized document -> definition_hash
"""
from __future__ import annotations

import copy
import difflib
import json
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from edgelab.core.identity import hash_obj
from edgelab.data.calendar import WEEKDAYS
from edgelab.data.schema import timeframe_minutes
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import FeatureError, FeatureSpec, all_defs, get_def

DSL_VERSION = 1
COMPARISONS = (">", ">=", "<", "<=", "==", "!=")
CROSSES = ("crosses_above", "crosses_below")
OPERATORS = COMPARISONS + CROSSES
BAR_FIELDS = ("open", "high", "low", "close", "volume")
ARITH_OPS = ("add", "sub", "mul", "div")
PARAM_TYPES = ("integer", "float", "boolean", "choice", "timeframe")
DIRECTIONS = ("long", "short", "both")
ENTRY_ORDER_TYPES = ("market", "stop", "limit")
STOP_TYPES = ("points", "atr", "price")
TARGET_TYPES = ("none", "points", "atr", "price", "risk_reward")
SIZING_MODES = ("fixed", "risk", "equity_risk")
WEEKDAY_NAMES = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

# Concepts the numerical engine cannot execute. Named explicitly so users get a precise
# refusal instead of a generic "unknown key" (and so nobody fakes them in the compiler).
UNSUPPORTED = {
    "trailing_stop": "trailing stops are not supported under 'trailing_stop'; declare them in exit.trailing (mode distance | level | breakeven)",
    "breakeven": "breakeven is not supported as a top-level key; declare it inside exit.trailing.breakeven",
    "partial_exits": "partial exits / scaling out are not supported (one fill in, one fill out)",
    "scale_out": "partial exits / scaling out are not supported (one fill in, one fill out)",
    "pyramiding": "pyramiding / scaling in is not supported (one position at a time)",
    "scale_in": "pyramiding / scaling in is not supported (one position at a time)",
    "cooldown_after_exit": "a top-level cooldown_after_exit is not supported; use entry.cooldown_bars (measured "
                           "between signals) or entry.reentry (measured from the trade exit inside the backtest).",
    "lead": "leads / negative lags would use future bars (non-causal)",
    "future": "future references are not allowed (non-causal)",
}

TOP_KEYS = {"dsl_version", "name", "description", "family", "timeframe", "sessions", "parameters",
            "entry", "exit", "sizing"}
ENTRY_KEYS = {"direction", "order", "long", "short", "session", "trading_weekdays", "cooldown_bars",
              "max_trades_per_day", "reentry"}
EXIT_KEYS = {"stop", "target", "time_stop_bars", "max_hold_bars", "signal", "trailing", "no_progress"}
TRAIL_MODES = ("distance", "level", "breakeven")
TRAIL_ACTIVATIONS = ("immediate", "points", "r", "atr")
TRAIL_TRIGGERS = ("points", "r", "atr")


# =============================================================================== errors
@dataclass(frozen=True)
class Issue:
    path: str
    message: str
    hint: str = ""
    severity: str = "error"          # error | warning

    def to_dict(self) -> dict:
        return {"path": self.path, "message": self.message, "hint": self.hint, "severity": self.severity}

    def text(self) -> str:
        return f"{self.path}:\n  {self.message}" + (f"\n  {self.hint}" if self.hint else "")


@dataclass
class ValidationResult:
    issues: list[Issue] = field(default_factory=list)

    @property
    def errors(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "error"]

    @property
    def warnings(self) -> list[Issue]:
        return [i for i in self.issues if i.severity == "warning"]

    @property
    def valid(self) -> bool:
        return not self.errors

    def report(self) -> str:
        if self.valid:
            head = "Strategy validation passed."
        else:
            head = f"Strategy validation failed ({len(self.errors)} error(s)):"
        parts = [head] + [i.text() for i in self.errors]
        if self.warnings:
            parts += ["Warnings:"] + [i.text() for i in self.warnings]
        return "\n\n".join(parts)


class StrategyValidationError(ValueError):
    def __init__(self, result: ValidationResult):
        self.result = result
        super().__init__(result.report())


def _suggest(name: str, options) -> str:
    name, opts = str(name), [str(o) for o in options]
    close = difflib.get_close_matches(name, opts, n=3, cutoff=0.5)
    close += [o for o in opts if o not in close and (name.startswith(o) or o.startswith(name))]
    return f"did you mean: {', '.join(close)}?" if close else f"available: {', '.join(sorted(options))}"


# =============================================================================== loading
def load_definition(src: Any) -> dict:
    """dict (copied), path to .yaml/.yml/.json, or YAML/JSON text -> dict."""
    import yaml
    if isinstance(src, Mapping):
        return copy.deepcopy(dict(src))
    if isinstance(src, Path) or (isinstance(src, str) and "\n" not in src and
                                 src.rsplit(".", 1)[-1].lower() in ("yaml", "yml", "json")):
        p = Path(src)
        text = p.read_text()
        data = json.loads(text) if p.suffix.lower() == ".json" else yaml.safe_load(text)
    else:
        data = yaml.safe_load(src)
    if not isinstance(data, dict):
        raise StrategyValidationError(ValidationResult([Issue("$", "document must be a mapping")]))
    return data


# =============================================================================== parameters
def _is_ref(v) -> bool:
    return isinstance(v, str) and v.startswith("$")


def _num(v) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


def _on_grid(x: float, lo: float, step: float) -> bool:
    k = (x - lo) / step
    return abs(k - round(k)) < 1e-9


def check_parameter(name: str, d: Any, path: str) -> list[Issue]:
    out: list[Issue] = []
    if not _NAME_RE.match(str(name)):
        out.append(Issue(path, f"parameter name {name!r} must match [a-z][a-z0-9_]*"))
    if not isinstance(d, Mapping):
        return out + [Issue(path, "parameter must be a mapping with at least 'type' and 'value'")]
    unknown = set(d) - {"type", "value", "min", "max", "step", "choices", "description"}
    if unknown:
        out.append(Issue(path, f"unknown keys {sorted(unknown)}", "allowed: type, value, min, max, step, choices, description"))
    t, v = d.get("type"), d.get("value")
    if t not in PARAM_TYPES:
        return out + [Issue(f"{path}.type", f"invalid type {t!r}", f"one of {PARAM_TYPES}")]
    if "value" not in d:
        return out + [Issue(f"{path}.value", "missing value")]
    if t in ("integer", "float"):
        ok_type = (isinstance(v, int) and not isinstance(v, bool)) if t == "integer" else _num(v)
        if not ok_type:
            return out + [Issue(f"{path}.value", f"{t} parameter needs a {t} value, got {v!r}")]
        lo, hi, st = d.get("min"), d.get("max"), d.get("step")
        for k, x in (("min", lo), ("max", hi), ("step", st)):
            if x is not None and not _num(x):
                out.append(Issue(f"{path}.{k}", f"{k} must be a number"))
            if t == "integer" and x is not None and _num(x) and not float(x).is_integer():
                out.append(Issue(f"{path}.{k}", f"{k} must be an integer for an integer parameter"))
        if out:
            return out
        if lo is not None and hi is not None and lo > hi:
            out.append(Issue(path, f"min {lo} > max {hi}"))
        if lo is not None and v < lo or hi is not None and v > hi:
            out.append(Issue(f"{path}.value", f"value {v} outside [{lo}, {hi}]"))
        if st is not None:
            if st <= 0:
                out.append(Issue(f"{path}.step", f"step must be > 0, got {st}"))
            elif lo is None or hi is None:
                out.append(Issue(f"{path}.step", "a step needs both min and max"))
            elif not _on_grid(hi, lo, st):
                out.append(Issue(f"{path}.step", f"(max - min) = {hi - lo} is not a multiple of step {st}"))
            elif not _on_grid(v, lo, st):
                out.append(Issue(f"{path}.value", f"value {v} is not on the grid min + k*step ({lo} + k*{st})"))
    elif t == "boolean":
        if not isinstance(v, bool):
            out.append(Issue(f"{path}.value", f"boolean parameter needs true/false, got {v!r}"))
        for k in ("min", "max", "step", "choices"):
            if k in d:
                out.append(Issue(f"{path}.{k}", f"'{k}' is not valid for a boolean parameter"))
    elif t in ("choice", "timeframe"):
        ch = d.get("choices")
        if t == "choice" and not ch:
            out.append(Issue(f"{path}.choices", "choice parameter needs a non-empty 'choices' list"))
        if ch is not None:
            if not isinstance(ch, list) or len(set(map(str, ch))) != len(ch):
                out.append(Issue(f"{path}.choices", "choices must be a list of unique values"))
            elif v not in ch:
                out.append(Issue(f"{path}.value", f"value {v!r} not in choices {ch}"))
        if t == "timeframe":
            for x in ([v] + list(ch or [])):
                try:
                    timeframe_minutes(str(x))
                except ValueError:
                    out.append(Issue(f"{path}", f"invalid timeframe {x!r}", "use e.g. 1m, 5m, 15m, 1h"))
        for k in ("min", "max", "step"):
            if k in d:
                out.append(Issue(f"{path}.{k}", f"'{k}' is not valid for a {t} parameter"))
    return out


def _collect_refs(node: Any, path: str, out: list):
    if _is_ref(node):
        out.append((path, node[1:]))
    elif isinstance(node, Mapping):
        for k, v in node.items():
            _collect_refs(v, f"{path}.{k}" if path else str(k), out)
    elif isinstance(node, list):
        for i, v in enumerate(node):
            _collect_refs(v, f"{path}[{i}]", out)


def param_values(doc: Mapping) -> dict:
    return {k: v["value"] for k, v in (doc.get("parameters") or {}).items() if isinstance(v, Mapping)}


def _subst(node: Any, values: Mapping) -> Any:
    if _is_ref(node):
        return values[node[1:]]
    if isinstance(node, Mapping):
        return {k: _subst(v, values) for k, v in node.items()}
    if isinstance(node, list):
        return [_subst(v, values) for v in node]
    return node


def _prune(cond: Any) -> Any:
    """Drop conditions whose 'enabled' is false; remove 'enabled'/'label' keys."""
    if not isinstance(cond, Mapping):
        return cond
    if cond.get("enabled", True) is False:
        return None
    c = {k: v for k, v in cond.items() if k not in ("enabled", "label")}
    for k in ("all", "any"):
        if k in c and isinstance(c[k], list):
            c[k] = [x for x in (_prune(y) for y in c[k]) if x is not None]
    if "not" in c:
        inner = _prune(c["not"])
        if inner is None:
            return None
        c["not"] = inner
    return c


def resolve(doc: Mapping, overrides: Mapping | None = None) -> dict:
    """Substitute parameter values (optionally overridden) and prune disabled conditions."""
    values = {**param_values(doc), **(overrides or {})}
    body = {k: v for k, v in doc.items() if k != "parameters"}
    r = _subst(body, values)
    for side in ("long", "short"):
        if isinstance(r.get("entry"), Mapping) and r["entry"].get(side) is not None:
            r["entry"][side] = _prune(r["entry"][side])
        sig = (r.get("exit") or {}).get("signal") if isinstance(r.get("exit"), Mapping) else None
        if isinstance(sig, Mapping) and sig.get(side) is not None:
            sig[side] = _prune(sig[side])
    return r


# =============================================================================== validation
class _Checker:
    def __init__(self, sessions: Mapping[str, SessionWindow]):
        self.sessions = dict(sessions)
        self.issues: list[Issue] = []
        self.tf: int | None = None

    def err(self, path, msg, hint=""):
        self.issues.append(Issue(path, msg, hint))

    def errors_present(self) -> bool:
        return any(i.severity == "error" for i in self.issues)

    def warn(self, path, msg, hint=""):
        self.issues.append(Issue(path, msg, hint, "warning"))

    def unknown_keys(self, node: Mapping, allowed: set, path: str):
        for k in node:
            if k in UNSUPPORTED and k not in allowed:
                self.err(f"{path}.{k}" if path else k, UNSUPPORTED[k])
            elif k not in allowed:
                self.err(f"{path}.{k}" if path else k, f"unknown key {k!r}", _suggest(k, allowed))

    # ---- operands --------------------------------------------------------------------
    def operand(self, o: Any, path: str) -> None:
        if _num(o):
            return
        if isinstance(o, bool) or o is None or isinstance(o, str):
            self.err(path, f"invalid operand {o!r}",
                     "use a number, {bar: close}, {feature: ...}, or {arith: add, args: [a, b]}")
            return
        if not isinstance(o, Mapping):
            self.err(path, "operand must be a number or a mapping")
            return
        kinds = [k for k in ("const", "bar", "feature", "arith") if k in o]
        if len(kinds) != 1:
            bad = [k for k in o if k in UNSUPPORTED]
            if bad:
                self.err(f"{path}.{bad[0]}", UNSUPPORTED[bad[0]])
            else:
                self.err(path, "operand needs exactly one of const / bar / feature / arith")
            return
        kind = kinds[0]
        if kind == "const":
            self.unknown_keys(o, {"const"}, path)
            if not _num(o["const"]):
                self.err(f"{path}.const", "const must be a finite number")
        elif kind == "bar":
            self.unknown_keys(o, {"bar", "lag"}, path)
            if o["bar"] not in BAR_FIELDS:
                self.err(f"{path}.bar", f"unknown bar field {o['bar']!r}", f"one of {BAR_FIELDS}")
            self.lag(o, path)
        elif kind == "arith":
            self.unknown_keys(o, {"arith", "args"}, path)
            if o["arith"] not in ARITH_OPS:
                self.err(f"{path}.arith", f"unknown arithmetic {o['arith']!r}", f"one of {ARITH_OPS}")
            args = o.get("args")
            if not isinstance(args, list) or len(args) != 2:
                self.err(f"{path}.args", "arith needs exactly two args")
            else:
                for i, a in enumerate(args):
                    self.operand(a, f"{path}.args[{i}]")
        else:
            self.feature_operand(o, path)

    def lag(self, o: Mapping, path: str):
        lag = o.get("lag", 0)
        if not isinstance(lag, int) or isinstance(lag, bool):
            self.err(f"{path}.lag", "lag must be a non-negative integer (bars back)")
        elif lag < 0:
            self.err(f"{path}.lag", f"lag {lag} looks into the future (non-causal)",
                     "lag counts bars BACK; only lag >= 0 is allowed")

    def feature_operand(self, o: Mapping, path: str):
        self.unknown_keys(o, {"feature", "params", "output", "timeframe", "lag", "version"}, path)
        fid = o["feature"]
        names = {d.feature_id for d in all_defs() if not d.feature_id.startswith("_")}
        if fid not in names:
            self.err(f"{path}.feature", f"unknown feature {fid!r}", _suggest(fid, names))
            return
        fd = get_def(fid)
        if not fd.causal:
            self.err(f"{path}.feature", f"feature {fid!r} is not causal and cannot be used by a strategy")
        try:
            spec = FeatureSpec.make(fid, o.get("params") or {}, o.get("timeframe"), o.get("version"))
        except (FeatureError, ValueError) as exc:
            self.err(f"{path}.params" if "param" in str(exc) else path, str(exc))
            return
        out = o.get("output")
        if out is None:
            if len(fd.outputs) != 1:
                self.err(f"{path}.output", f"feature {fid!r} has several outputs; name one",
                         f"outputs: {', '.join(fd.output_names)}")
        elif out not in fd.output_names:
            self.err(f"{path}.output", f"feature {fid!r} has no output {out!r}", _suggest(out, fd.output_names))
        if spec.timeframe and self.tf:
            ftf = timeframe_minutes(spec.timeframe)
            if ftf < self.tf or ftf % self.tf:
                self.err(f"{path}.timeframe", f"feature timeframe {spec.timeframe} must be a multiple of the "
                                              f"strategy timeframe {self.tf}m (lower timeframes are not visible)")
        for pname in fd.session_params:
            sname = spec.param_dict[pname]
            if sname != "trading_day" and sname not in self.sessions:
                self.err(f"{path}.params.{pname}", f"unknown session {sname!r}", _suggest(sname, self.sessions))
        self.lag(o, path)

    # ---- conditions ------------------------------------------------------------------
    def condition(self, c: Any, path: str) -> int:
        """Validate; returns the number of comparisons (0 = empty condition)."""
        if not isinstance(c, Mapping):
            self.err(path, "condition must be a mapping")
            return 0
        keys = [k for k in ("all", "any", "not", "op") if k in c]
        if len(keys) != 1:
            self.err(path, "condition needs exactly one of: all, any, not, or a comparison (left/op/right)")
            return 0
        k = keys[0]
        if k in ("all", "any"):
            self.unknown_keys(c, {k}, path)
            items = c[k]
            if not isinstance(items, list):
                self.err(f"{path}.{k}", f"'{k}' must be a list")
                return 0
            if k == "any" and not items:
                self.err(f"{path}.any", "'any' with no conditions can never be true")
            return sum(self.condition(x, f"{path}.{k}[{i}]") for i, x in enumerate(items))
        if k == "not":
            self.unknown_keys(c, {"not"}, path)
            return self.condition(c["not"], f"{path}.not")
        self.unknown_keys(c, {"left", "op", "right"}, path)
        op = c["op"]
        if op not in OPERATORS:
            self.err(f"{path}.op", f"invalid operator {op!r}", f"one of {', '.join(OPERATORS)}")
        for side in ("left", "right"):
            if side not in c:
                self.err(f"{path}.{side}", f"comparison is missing '{side}'")
            else:
                self.operand(c[side], f"{path}.{side}")
        if "left" in c and "right" in c:
            L, R = canonical_operand(c["left"]), canonical_operand(c["right"])
            if "const" in L and "const" in R:
                self.err(path, "comparison of two constants is always true or always false")
            elif L == R:
                self.err(path, "comparison of an operand with itself is always true or always false")
        return 1


def _validate_sessions(ck: _Checker, sess: Any) -> None:
    if sess is None:
        return
    if not isinstance(sess, Mapping):
        ck.err("sessions", "sessions must be a mapping of name -> {timezone, start, end}")
        return
    for name, w in sess.items():
        p = f"sessions.{name}"
        if not isinstance(w, Mapping) or not {"timezone", "start", "end"} <= set(w):
            ck.err(p, "session needs timezone, start, end")
            continue
        ck.unknown_keys(w, {"timezone", "start", "end", "weekdays"}, p)
        try:
            win = session_from_dict(name, w)
            win.membership(np.array([0], dtype=np.int64))
        except Exception as exc:
            ck.err(p, f"invalid session window: {exc}")
            continue
        if name in ck.sessions and ck.sessions[name].definition() != win.definition():
            ck.err(p, f"session {name!r} is also configured with a DIFFERENT definition",
                   "rename the strategy-local session")
        ck.sessions[name] = win


def session_from_dict(name: str, w: Mapping) -> SessionWindow:
    days = w.get("weekdays", ["mon", "tue", "wed", "thu", "fri"])
    return SessionWindow(name, w["timezone"], str(w["start"]), str(w["end"]),
                         tuple(WEEKDAYS[str(d).lower()[:3]] for d in days))


def _validate_resolved(ck: _Checker, r: Mapping) -> None:
    ck.unknown_keys(r, TOP_KEYS, "")
    # timeframe
    try:
        ck.tf = timeframe_minutes(str(r.get("timeframe")))
    except ValueError:
        ck.err("timeframe", f"invalid or missing timeframe {r.get('timeframe')!r}", "e.g. 1m, 5m, 15m, 1h")
    # entry
    entry = r.get("entry")
    if not isinstance(entry, Mapping):
        ck.err("entry", "missing entry block")
        entry = {}
    ck.unknown_keys(entry, ENTRY_KEYS, "entry")
    direction = entry.get("direction")
    if direction not in DIRECTIONS:
        ck.err("entry.direction", f"invalid direction {direction!r}", f"one of {DIRECTIONS}")
    sides = {"long": ("long",), "short": ("short",), "both": ("long", "short")}.get(direction, ())
    for side in ("long", "short"):
        if side in sides:
            if entry.get(side) is None:
                ck.err(f"entry.{side}", f"missing entry condition for direction {direction!r}")
            elif ck.condition(entry[side], f"entry.{side}") == 0:
                ck.err(f"entry.{side}", "entry condition is empty (all conditions disabled?) - "
                                        "it would signal on every bar")
        elif entry.get(side) is not None:
            ck.err(f"entry.{side}", f"'{side}' condition given but direction is {direction!r} (contradictory)")
    order = entry.get("order", {"type": "market"})
    if not isinstance(order, Mapping):
        ck.err("entry.order", "order must be a mapping")
        order = {}
    ck.unknown_keys(order, {"type", "long_price", "short_price", "expiry_bars"}, "entry.order")
    otype = order.get("type", "market")
    if otype not in ENTRY_ORDER_TYPES:
        ck.err("entry.order.type", f"unsupported entry order {otype!r}", f"supported: {ENTRY_ORDER_TYPES}")
    if otype == "market":
        for k in ("long_price", "short_price", "expiry_bars"):
            if k in order:
                ck.err(f"entry.order.{k}", f"'{k}' only applies to stop/limit entries")
    elif otype in ("stop", "limit"):
        for side in sides:
            key = f"{side}_price"
            if key not in order:
                ck.err(f"entry.order.{key}", f"{otype} entries need a {key} operand")
            else:
                ck.operand(order[key], f"entry.order.{key}")
        exp = order.get("expiry_bars", 1)
        if not isinstance(exp, int) or isinstance(exp, bool) or exp < 1:
            ck.err("entry.order.expiry_bars", "expiry_bars must be an integer >= 1")
    if entry.get("session") is not None and entry["session"] not in ck.sessions:
        ck.err("entry.session", f"unknown session {entry['session']!r}", _suggest(entry["session"], ck.sessions))
    wd = entry.get("trading_weekdays")
    if wd is not None:
        if not isinstance(wd, list) or not wd or any(str(d).lower()[:3] not in WEEKDAY_NAMES for d in wd):
            ck.err("entry.trading_weekdays", "must be a non-empty list of weekday names", f"{WEEKDAY_NAMES}")
    cd = entry.get("cooldown_bars", 0)
    if not isinstance(cd, int) or isinstance(cd, bool) or cd < 0:
        ck.err("entry.cooldown_bars", "cooldown_bars must be an integer >= 0")
    mt = entry.get("max_trades_per_day")
    if mt is not None and (not isinstance(mt, int) or isinstance(mt, bool) or mt < 1):
        ck.err("entry.max_trades_per_day", "max_trades_per_day must be an integer >= 1 (executed trades per trading date)")
    ree = entry.get("reentry")
    if ree is not None:
        if not isinstance(ree, Mapping) or not ree:
            ck.err("entry.reentry", "reentry must be a non-empty mapping")
        else:
            ck.unknown_keys(ree, {"cooldown_bars", "block_day_after"}, "entry.reentry")
            rc = ree.get("cooldown_bars", 0)
            if not isinstance(rc, int) or isinstance(rc, bool) or rc < 0:
                ck.err("entry.reentry.cooldown_bars", "cooldown_bars must be an integer >= 0 (bars after a trade exit)")
            if ree.get("block_day_after") not in (None, "stop", "target", "any"):
                ck.err("entry.reentry.block_day_after", "must be stop | target | any")
            if not rc and ree.get("block_day_after") is None:
                ck.err("entry.reentry", "reentry needs cooldown_bars > 0 and/or block_day_after")
    # exit
    ex = r.get("exit")
    if not isinstance(ex, Mapping):
        ck.err("exit", "missing exit block (a protective stop is required)")
        ex = {}
    ck.unknown_keys(ex, EXIT_KEYS, "exit")
    stop = ex.get("stop")
    if not isinstance(stop, Mapping):
        ck.err("exit.stop", "a protective stop is required")
        stop = {}
    else:
        _stop_or_target(ck, stop, "exit.stop", STOP_TYPES, sides)
    tgt = ex.get("target", {"type": "none"})
    if isinstance(tgt, Mapping):
        _stop_or_target(ck, tgt, "exit.target", TARGET_TYPES, sides)
    else:
        ck.err("exit.target", "target must be a mapping")
    for k in ("time_stop_bars", "max_hold_bars"):
        v = ex.get(k)
        if v is not None and (not isinstance(v, int) or isinstance(v, bool) or v < 1):
            ck.err(f"exit.{k}", f"{k} must be an integer >= 1")
    if isinstance(ex.get("time_stop_bars"), int) and isinstance(ex.get("max_hold_bars"), int) \
            and ex["max_hold_bars"] >= ex["time_stop_bars"]:
        ck.warn("exit.max_hold_bars", "max_hold_bars >= time_stop_bars never binds")
    if ex.get("trailing") is not None:
        _validate_trailing(ck, ex["trailing"], sides, stop)
    if ex.get("no_progress") is not None:
        _validate_no_progress(ck, ex["no_progress"], ex.get("trailing"))
    sig = ex.get("signal")
    if sig is not None:
        if not isinstance(sig, Mapping):
            ck.err("exit.signal", "signal must be a mapping with long and/or short conditions")
        else:
            ck.unknown_keys(sig, {"long", "short"}, "exit.signal")
            if not any(sig.get(s) is not None for s in ("long", "short")):
                ck.err("exit.signal", "signal exit needs a long and/or short condition")
            for side in ("long", "short"):
                if sig.get(side) is None:
                    continue
                if side not in sides:
                    ck.err(f"exit.signal.{side}", f"exit condition for {side} positions, but direction is "
                                                  f"{direction!r} (contradictory)")
                elif ck.condition(sig[side], f"exit.signal.{side}") == 0:
                    ck.err(f"exit.signal.{side}", "exit condition is empty")
    # sizing
    sz = r.get("sizing", {"mode": "fixed", "quantity": 1})
    if not isinstance(sz, Mapping):
        ck.err("sizing", "sizing must be a mapping")
        return
    mode = sz.get("mode")
    _validate_contract(ck, sz)
    if mode not in SIZING_MODES:
        ck.err("sizing.mode", f"unsupported sizing mode {mode!r}", f"supported: {SIZING_MODES}")
    elif mode == "fixed":
        ck.unknown_keys(sz, {"mode", "quantity", "contract"}, "sizing")
        if not _num(sz.get("quantity")) or sz["quantity"] <= 0:
            ck.err("sizing.quantity", "fixed sizing needs quantity > 0 (contracts or CFD units)")
    elif mode == "equity_risk":
        ck.unknown_keys(sz, {"mode", "risk_pct", "max_quantity", "contract"}, "sizing")
        if "starting_equity" in sz:
            ck.err("sizing.starting_equity", "the account size is a run/evaluation parameter, not part of a strategy "
                                             "(its identity must not depend on the account)")
        if not _num(sz.get("risk_pct")) or not 0 < sz["risk_pct"] <= 10:
            ck.err("sizing.risk_pct", "equity_risk needs risk_pct in (0, 10] (percent of the run account's equity at the signal)")
        mq = sz.get("max_quantity")
        if mq is not None and (not isinstance(mq, int) or isinstance(mq, bool) or mq < 1):
            ck.err("sizing.max_quantity", "max_quantity must be an integer >= 1")
    else:
        ck.unknown_keys(sz, {"mode", "risk_usd", "max_quantity", "contract"}, "sizing")
        if not _num(sz.get("risk_usd")) or sz["risk_usd"] <= 0:
            ck.err("sizing.risk_usd", "risk sizing needs risk_usd > 0 (your risk budget per trade)")
        mq = sz.get("max_quantity")
        if mq is not None and (not isinstance(mq, int) or isinstance(mq, bool) or mq < 1):
            ck.err("sizing.max_quantity", "max_quantity must be an integer >= 1")


def _posnum(v) -> bool:
    return _num(v) and v > 0


def _validate_trailing(ck: _Checker, tr: Any, sides: tuple, stop: Mapping) -> None:
    """exit.trailing (ADR-61). Semantics live in engine/fills.py; this only checks the declaration."""
    p = "exit.trailing"
    if not isinstance(tr, Mapping):
        ck.err(p, "trailing must be a mapping")
        return
    ck.unknown_keys(tr, {"mode", "distance", "level", "activation", "breakeven", "atr_period", "update"}, p)
    mode = tr.get("mode")
    if mode not in TRAIL_MODES:
        ck.err(f"{p}.mode", f"invalid trailing mode {mode!r}", f"one of {TRAIL_MODES}")
        return
    if stop.get("type") is None:
        ck.err(p, "trailing needs a protective initial stop (exit.stop)")
    uses_atr = False
    dist = tr.get("distance")
    if mode == "distance":
        if not isinstance(dist, Mapping) or dist.get("type") not in ("points", "atr"):
            ck.err(f"{p}.distance", "distance trailing needs distance: {type: points|atr, points|multiple: > 0}")
        else:
            ck.unknown_keys(dist, {"type", "points" if dist["type"] == "points" else "multiple"}, f"{p}.distance")
            key = "points" if dist["type"] == "points" else "multiple"
            if not _posnum(dist.get(key)):
                ck.err(f"{p}.distance.{key}", f"{key} must be > 0")
            uses_atr |= dist["type"] == "atr"
    elif dist is not None:
        ck.err(f"{p}.distance", f"distance applies to mode 'distance' only (mode is {mode!r})")
    lv = tr.get("level")
    if mode == "level":
        if not isinstance(lv, Mapping):
            ck.err(f"{p}.level", "level trailing needs level: {long: operand, short: operand}")
        else:
            ck.unknown_keys(lv, {"long", "short"}, f"{p}.level")
            for side in ("long", "short"):
                if side in sides and side not in lv:
                    ck.err(f"{p}.level.{side}", f"level trailing needs a '{side}' operand")
                elif side in lv and side not in sides:
                    ck.err(f"{p}.level.{side}", f"level for {side} positions, but direction does not trade {side}")
                elif side in lv:
                    ck.operand(lv[side], f"{p}.level.{side}")
    elif lv is not None:
        ck.err(f"{p}.level", f"level applies to mode 'level' only (mode is {mode!r})")
    act = tr.get("activation", {"type": "immediate"})
    if mode == "breakeven" and "activation" not in tr:
        pass
    elif not isinstance(act, Mapping) or act.get("type") not in TRAIL_ACTIVATIONS:
        ck.err(f"{p}.activation", "activation needs type immediate | points | r | atr")
    else:
        ck.unknown_keys(act, {"type", "value"}, f"{p}.activation")
        if mode == "breakeven":
            ck.err(f"{p}.activation", "breakeven mode has no trailing activation (use breakeven.trigger)")
        elif act["type"] == "immediate":
            if "value" in act:
                ck.err(f"{p}.activation.value", "an immediate activation takes no value")
        elif not _posnum(act.get("value")):
            ck.err(f"{p}.activation.value", "a triggered activation needs value > 0")
        uses_atr |= act.get("type") == "atr"
    be = tr.get("breakeven")
    if be is None:
        if mode == "breakeven":
            ck.err(f"{p}.breakeven", "mode 'breakeven' needs a breakeven block")
    elif not isinstance(be, Mapping):
        ck.err(f"{p}.breakeven", "breakeven must be a mapping")
    else:
        ck.unknown_keys(be, {"trigger", "offset_points"}, f"{p}.breakeven")
        trg = be.get("trigger")
        if not isinstance(trg, Mapping) or trg.get("type") not in TRAIL_TRIGGERS or not _posnum(trg.get("value")):
            ck.err(f"{p}.breakeven.trigger", "trigger needs {type: points|r|atr, value: > 0}")
        else:
            ck.unknown_keys(trg, {"type", "value"}, f"{p}.breakeven.trigger")
            uses_atr |= trg["type"] == "atr"
        off = be.get("offset_points", 0)
        if not _num(off) or off < 0:
            ck.err(f"{p}.breakeven.offset_points", "offset_points must be a number >= 0")
    ap = tr.get("atr_period", 14)
    if not isinstance(ap, int) or isinstance(ap, bool) or ap < 1:
        ck.err(f"{p}.atr_period", "atr_period must be an integer >= 1")
    elif "atr_period" in tr and not uses_atr:
        ck.err(f"{p}.atr_period", "atr_period is given but no ATR-based distance, activation or trigger uses it")
    up = tr.get("update", {})
    if not isinstance(up, Mapping):
        ck.err(f"{p}.update", "update must be a mapping")
    else:
        ck.unknown_keys(up, {"every_bars", "only_new_extreme", "min_step_points"}, f"{p}.update")
        eb = up.get("every_bars", 1)
        if not isinstance(eb, int) or isinstance(eb, bool) or eb < 1:
            ck.err(f"{p}.update.every_bars", "every_bars must be an integer >= 1")
        if not isinstance(up.get("only_new_extreme", False), bool):
            ck.err(f"{p}.update.only_new_extreme", "only_new_extreme must be true/false")
        ms = up.get("min_step_points", 0)
        if not _num(ms) or ms < 0:
            ck.err(f"{p}.update.min_step_points", "min_step_points must be a number >= 0")
        if mode == "breakeven" and up:
            ck.err(f"{p}.update", "breakeven mode has no trailing update rule")


def _validate_contract(ck: _Checker, sz: Mapping) -> None:
    """sizing.contract (ADR-63): the execution contract (a symbol in configs/instruments.yaml, e.g. MNQ). A named
    contract trades WHOLE contracts: a fixed quantity and any cap must be integers."""
    c = sz.get("contract")
    if c is None:
        return
    if not isinstance(c, str) or not re.fullmatch(r"[A-Z][A-Z0-9_]*", c):
        ck.err("sizing.contract", "contract must be an instrument symbol such as MNQ")
        return
    q = sz.get("quantity")
    if sz.get("mode") == "fixed" and _num(q) and not float(q).is_integer():
        ck.err("sizing.quantity", f"a named contract ({c}) trades whole contracts; {q!r} is fractional")


def _validate_no_progress(ck: _Checker, npg: Any, trailing: Any) -> None:
    """exit.no_progress (ADR-62): exit at the close of bar N if the favorable excursion is still below a threshold."""
    p = "exit.no_progress"
    if not isinstance(npg, Mapping):
        ck.err(p, "no_progress must be a mapping")
        return
    ck.unknown_keys(npg, {"bars", "min_progress", "atr_period"}, p)
    b = npg.get("bars")
    if not isinstance(b, int) or isinstance(b, bool) or b < 2:
        ck.err(f"{p}.bars", "bars must be an integer >= 2 (checked at the close of bar N since entry; entry bar = 1)")
    mp = npg.get("min_progress")
    if not isinstance(mp, Mapping) or mp.get("type") not in TRAIL_TRIGGERS or not _posnum(mp.get("value")):
        ck.err(f"{p}.min_progress", "min_progress needs {type: points|r|atr, value: > 0}")
        return
    ck.unknown_keys(mp, {"type", "value"}, f"{p}.min_progress")
    ap = npg.get("atr_period", 14)
    if not isinstance(ap, int) or isinstance(ap, bool) or ap < 1:
        ck.err(f"{p}.atr_period", "atr_period must be an integer >= 1")
    elif "atr_period" in npg and mp["type"] != "atr":
        ck.err(f"{p}.atr_period", "atr_period is given but min_progress is not ATR-based")
    elif mp["type"] == "atr" and isinstance(trailing, Mapping) and trailing.get("atr_period", 14) != ap \
            and _trail_uses_atr(trailing):
        ck.err(f"{p}.atr_period", "trailing and no_progress must use the same atr_period (one ATR series per strategy)")


def _trail_uses_atr(tr: Mapping) -> bool:
    d, a, b = tr.get("distance"), tr.get("activation"), (tr.get("breakeven") or {}).get("trigger")
    return any(isinstance(x, Mapping) and x.get("type") == "atr" for x in (d, a, b))


def _stop_or_target(ck: _Checker, d: Mapping, path: str, types: tuple, sides: tuple) -> None:
    t = d.get("type")
    if t not in types:
        ck.err(f"{path}.type", f"invalid type {t!r}", f"one of {types}")
        return
    allowed = {"none": {"type"}, "points": {"type", "points"}, "atr": {"type", "multiple", "period", "timeframe"},
               "price": {"type", "long", "short"}, "risk_reward": {"type", "multiple"}}[t]
    ck.unknown_keys(d, allowed, path)
    if t == "points" and not (_num(d.get("points")) and d["points"] > 0):
        ck.err(f"{path}.points", "points must be > 0")
    if t in ("atr", "risk_reward") and not (_num(d.get("multiple")) and d["multiple"] > 0):
        ck.err(f"{path}.multiple", "multiple must be > 0")
    if t == "atr":
        per = d.get("period", 14)
        if not isinstance(per, int) or isinstance(per, bool) or per < 1:
            ck.err(f"{path}.period", "ATR period must be an integer >= 1")
        if d.get("timeframe") is not None:
            ck.feature_operand({"feature": "atr", "params": {"period": per if isinstance(per, int) else 14},
                                "timeframe": d["timeframe"], "output": "atr"}, path)
    if t == "price":
        for side in sides:
            if side not in d:
                ck.err(f"{path}.{side}", f"price {path.split('.')[-1]} needs a '{side}' operand")
            else:
                ck.operand(d[side], f"{path}.{side}")


def validate(doc: Any, sessions: Mapping[str, SessionWindow] | None = None) -> ValidationResult:
    """Full validation: schema, parameters, references, resolved logic, causality rules."""
    raw = load_definition(doc)
    ck = _Checker(sessions or {})
    if raw.get("dsl_version", DSL_VERSION) != DSL_VERSION:
        ck.err("dsl_version", f"unsupported dsl_version {raw.get('dsl_version')!r}; this build reads {DSL_VERSION}")
    if not raw.get("name") or not isinstance(raw.get("name"), str):
        ck.err("name", "missing strategy name")
    fam = raw.get("family")
    if fam is not None:
        if not isinstance(fam, Mapping) or not fam.get("id"):
            ck.err("family", "family needs at least an 'id'")
        else:
            ck.unknown_keys(fam, {"id", "name", "hypothesis", "category"}, "family")
            if not _NAME_RE.match(str(fam["id"])):
                ck.err("family.id", "family id must match [a-z][a-z0-9_]*")
    params = raw.get("parameters") or {}
    if not isinstance(params, Mapping):
        ck.err("parameters", "parameters must be a mapping")
        params = {}
    for name, decl in params.items():
        ck.issues += check_parameter(name, decl, f"parameters.{name}")
    refs: list = []
    _collect_refs({k: v for k, v in raw.items() if k != "parameters"}, "", refs)
    for path, name in refs:
        if name not in params:
            ck.err(path, f"reference to undeclared parameter ${name}", _suggest(name, params) if params else
                   "declare it under 'parameters'")
    used = {n for _, n in refs}
    for name in params:
        if name not in used:
            ck.warn(f"parameters.{name}", f"parameter {name!r} is declared but never used")
    if ck.errors_present():
        return ValidationResult(ck.issues)
    _validate_sessions(ck, raw.get("sessions"))
    r = resolve(raw)
    _validate_resolved(ck, r)
    return ValidationResult(ck.issues)


def require_valid(doc: Any, sessions: Mapping[str, SessionWindow] | None = None) -> dict:
    raw = load_definition(doc)
    res = validate(raw, sessions)
    if not res.valid:
        raise StrategyValidationError(res)
    return raw


# =============================================================================== canonical form
def canonical_operand(o: Any) -> Any:
    if _is_ref(o):
        return o
    if _num(o):
        return {"const": float(o)}
    if not isinstance(o, Mapping):
        return o
    if "const" in o:
        return {"const": float(o["const"]) if _num(o["const"]) else o["const"]}
    if "bar" in o:
        return {"bar": o["bar"], "lag": o.get("lag", 0)}
    if "arith" in o:
        args = [canonical_operand(a) for a in o.get("args", [])]
        if o["arith"] in ("add", "mul"):
            args.sort(key=lambda a: json.dumps(a, sort_keys=True, default=str))
        return {"arith": o["arith"], "args": args}
    if "feature" in o:
        fid, params = o["feature"], dict(o.get("params") or {})
        tf = o.get("timeframe")
        try:
            fd = get_def(fid)
            if any(_is_ref(v) for v in params.values()) or _is_ref(tf):
                full = {p.name: params.get(p.name, p.default) for p in fd.params}
                version = fd.version
                tf_c = tf if _is_ref(tf) or tf is None else FeatureSpec.make(fid, None, tf).timeframe
            else:
                spec = FeatureSpec.make(fid, params, tf)
                full, version, tf_c = spec.param_dict, spec.version, spec.timeframe
            out = o.get("output") or (fd.output_names[0] if len(fd.outputs) == 1 else None)
        except FeatureError:
            full, version, tf_c, out = params, None, tf, o.get("output")
        return {"feature": fid, "version": version, "params": dict(sorted(full.items())),
                "output": out, "timeframe": tf_c, "lag": o.get("lag", 0)}
    return o


def _key(x) -> str:
    return json.dumps(x, sort_keys=True, default=str)


def canonical_condition(c: Any) -> Any:
    if not isinstance(c, Mapping):
        return c
    base = {k: v for k, v in c.items() if k not in ("label",)}
    if "all" in base or "any" in base:
        k = "all" if "all" in base else "any"
        items = [canonical_condition(x) for x in base[k]]
        flat = []
        for x in items:                                   # all[all[a,b],c] == all[a,b,c]
            flat += x[k] if isinstance(x, Mapping) and set(x) == {k} else [x]
        out = {k: sorted(flat, key=_key)}
        if "enabled" in base:
            out["enabled"] = base["enabled"]
        return out
    if "not" in base:
        inner = canonical_condition(base["not"])
        if isinstance(inner, Mapping) and set(inner) == {"not"}:
            return inner["not"]                            # not(not x) == x
        out = {"not": inner}
        if "enabled" in base:
            out["enabled"] = base["enabled"]
        return out
    op = base.get("op")
    L, R = canonical_operand(base.get("left")), canonical_operand(base.get("right"))
    if op in ("<", "<="):                                  # a < b  ==  b > a
        op, L, R = {"<": ">", "<=": ">="}[op], R, L
    elif op == "crosses_below":                            # crosses_below(a, b) == crosses_above(b, a)
        op, L, R = "crosses_above", R, L
    elif op in ("==", "!=") and _key(L) > _key(R):
        L, R = R, L
    out = {"left": L, "op": op, "right": R}
    if "enabled" in base:
        out["enabled"] = base["enabled"]
    return out


def _canonical_exit_part(d: Mapping) -> dict:
    d = dict(d)
    t = d.get("type")
    for k in ("long", "short"):
        if k in d:
            d[k] = canonical_operand(d[k])
    for k in ("points", "multiple"):
        if _num(d.get(k)):
            d[k] = float(d[k])
    if t == "atr":
        d.setdefault("period", 14)
        tf = d.get("timeframe")
        d["timeframe"] = tf if tf is None or _is_ref(tf) else FeatureSpec.make("atr", None, tf).timeframe
    return dict(sorted(d.items()))


def canonical_trailing(tr: Mapping) -> dict:
    """Normal form: defaults filled, irrelevant keys dropped, numbers as floats (no cosmetic variants)."""
    mode = tr["mode"]
    f = lambda v: float(v)
    out: dict = {"mode": mode}
    uses_atr = False
    if mode == "distance":
        d = tr["distance"]
        key = "points" if d["type"] == "points" else "multiple"
        out["distance"] = {"type": d["type"], key: f(d[key])}
        uses_atr |= d["type"] == "atr"
    if mode == "level":
        out["level"] = {k: canonical_operand(v) for k, v in sorted(tr["level"].items())}
    if mode != "breakeven":
        a = tr.get("activation") or {"type": "immediate"}
        out["activation"] = {"type": a["type"]} if a["type"] == "immediate" else {"type": a["type"], "value": f(a["value"])}
        uses_atr |= a["type"] == "atr"
        u = tr.get("update") or {}
        out["update"] = {"every_bars": int(u.get("every_bars", 1)), "only_new_extreme": bool(u.get("only_new_extreme", False)),
                         "min_step_points": f(u.get("min_step_points", 0))}
    if tr.get("breakeven"):
        b = tr["breakeven"]
        out["breakeven"] = {"trigger": {"type": b["trigger"]["type"], "value": f(b["trigger"]["value"])},
                            "offset_points": f(b.get("offset_points", 0))}
        uses_atr |= b["trigger"]["type"] == "atr"
    out["atr_period"] = int(tr.get("atr_period", 14)) if uses_atr else None
    return out


def _canonical_body(r: Mapping) -> dict:
    entry = dict(r.get("entry") or {})
    order = dict(entry.get("order") or {"type": "market"})
    for k in ("long_price", "short_price"):
        if k in order:
            order[k] = canonical_operand(order[k])
    if order.get("type", "market") != "market":
        order.setdefault("expiry_bars", 1)
    order.setdefault("type", "market")
    ce = {"direction": entry.get("direction"), "order": dict(sorted(order.items())),
          "long": canonical_condition(entry["long"]) if entry.get("long") is not None else None,
          "short": canonical_condition(entry["short"]) if entry.get("short") is not None else None,
          "session": entry.get("session"),
          "trading_weekdays": (sorted({str(d).lower()[:3] for d in entry["trading_weekdays"]},
                                      key=WEEKDAY_NAMES.index) if entry.get("trading_weekdays") and
                               not _is_ref(entry["trading_weekdays"]) else entry.get("trading_weekdays")),
          "cooldown_bars": entry.get("cooldown_bars", 0)}
    if entry.get("max_trades_per_day") is not None:          # only when declared: other identities are unchanged
        ce["max_trades_per_day"] = int(entry["max_trades_per_day"])
    if entry.get("reentry"):
        ce["reentry"] = {"cooldown_bars": int(entry["reentry"].get("cooldown_bars", 0)),
                         "block_day_after": entry["reentry"].get("block_day_after")}
    ex = dict(r.get("exit") or {})
    sig = ex.get("signal")
    cx = {"stop": _canonical_exit_part(ex.get("stop") or {}),
          "target": _canonical_exit_part(ex.get("target") or {"type": "none"}),
          "time_stop_bars": ex.get("time_stop_bars"), "max_hold_bars": ex.get("max_hold_bars"),
          "signal": None if not sig else {s: canonical_condition(sig[s]) if sig.get(s) is not None else None
                                          for s in ("long", "short")}}
    sz = dict(r.get("sizing") or {"mode": "fixed", "quantity": 1})
    if sz.get("contract") is None:
        sz.pop("contract", None)                 # absent stays absent: identities without a contract are unchanged
    for k in ("quantity", "risk_usd", "risk_pct"):
        if _num(sz.get(k)):
            sz[k] = float(sz[k])
    if ex.get("trailing"):               # only when declared: identities of strategies without it are unchanged
        cx["trailing"] = canonical_trailing(ex["trailing"])
    if ex.get("no_progress"):
        npg = ex["no_progress"]
        cx["no_progress"] = {"bars": int(npg["bars"]), "type": npg["min_progress"]["type"],
                             "value": float(npg["min_progress"]["value"]),
                             "atr_period": int(npg.get("atr_period", 14)) if npg["min_progress"]["type"] == "atr" else None}
    tf = r.get("timeframe")
    try:
        tf = f"{timeframe_minutes(str(tf))}m" if not _is_ref(tf) else tf
    except ValueError:
        pass
    return {"timeframe": tf, "entry": ce, "exit": cx, "sizing": dict(sorted(sz.items()))}


def _referenced_sessions(body: Any, out: set) -> set:
    if isinstance(body, Mapping):
        if "feature" in body and isinstance(body.get("params"), Mapping):
            try:
                for pn in get_def(body["feature"]).session_params:
                    v = body["params"].get(pn)
                    if isinstance(v, str) and v != "trading_day":
                        out.add(v)
            except FeatureError:
                pass
        for v in body.values():
            _referenced_sessions(v, out)
    elif isinstance(body, list):
        for v in body:
            _referenced_sessions(v, out)
    return out


def canonical_logic(resolved: Mapping, sessions: Mapping[str, SessionWindow]) -> dict:
    """Everything that affects signals/orders, nothing that doesn't (names, labels, parameter
    domains). Referenced session windows are included with their RESOLVED definitions, so a
    changed window changes the identity."""
    body = _canonical_body(resolved)
    names = _referenced_sessions(body, set())
    if body["entry"].get("session"):
        names.add(body["entry"]["session"])
    local = {n: session_from_dict(n, w) for n, w in (resolved.get("sessions") or {}).items()}
    allw = {**dict(sessions), **local}
    body["sessions"] = {n: allw[n].definition() for n in sorted(names) if n in allw}
    body["dsl_version"] = DSL_VERSION
    return body


def canonical_definition(raw: Mapping) -> dict:
    """The whole document in normal form (for definition_hash and storage)."""
    body = _canonical_body(raw)
    params = {k: dict(sorted(v.items())) for k, v in sorted((raw.get("parameters") or {}).items())}
    fam = raw.get("family")
    return {"dsl_version": raw.get("dsl_version", DSL_VERSION), "name": raw.get("name"),
            "description": raw.get("description", ""),
            "family": dict(sorted(fam.items())) if isinstance(fam, Mapping) else None,
            "sessions": {k: dict(sorted(v.items())) for k, v in sorted((raw.get("sessions") or {}).items())},
            "parameters": params, **body}


@dataclass(frozen=True)
class StrategyIdentity:
    strategy_id: str
    logic_hash: str
    definition_hash: str

    def to_dict(self) -> dict:
        return {"strategy_id": self.strategy_id, "logic_hash": self.logic_hash,
                "definition_hash": self.definition_hash}


def identity(raw: Mapping, sessions: Mapping[str, SessionWindow]) -> StrategyIdentity:
    lh = hash_obj(canonical_logic(resolve(raw), sessions))
    return StrategyIdentity(f"STR_{lh[:12].upper()}", lh, hash_obj(canonical_definition(raw)))
