"""DSL -> executable Strategy (deterministic, no indicator math of its own).

The compiler maps a validated definition onto contracts that already exist:
  entry order / stop / target / time stop  -> Phase 1 OrderSpec + SignalSet price arrays
  signal exits                             -> SignalSet.exit_long / exit_short (Phase 3 engine extension)
  every indicator / session / level        -> Phase 2 FeatureSpecs, read from the FeatureFrame
  sizing                                   -> the backtester's existing sizing config
The resulting DSLStrategy is a FeatureStrategy, so it is bound per dataset, uses the shared
feature cache, and goes through the Phase 1 causality check before every backtest.

Evaluation semantics (documented in STRATEGY_DSL.md):
  * three-valued logic: a comparison involving NaN (warm-up, missing data) is UNKNOWN, not
    false; all/any/not follow Kleene logic; a signal fires only when its condition is known-true;
  * a signal at bar t is decided at the close of bar t using bars <= t (Phase 1 timing).
"""
from __future__ import annotations

import hashlib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from edgelab.data.schema import BarArrays, timeframe_minutes
from edgelab.engine.signals import OrderSpec, SignalSet
from edgelab.features.engine import FeatureFrame
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import FeatureSpec
from edgelab.features.strategy_api import FeatureContext, FeatureStrategy
from edgelab.strategy.dsl import (DSL_VERSION, WEEKDAY_NAMES, StrategyIdentity, canonical_logic,
                                  identity, require_valid, resolve, session_from_dict)

COMPILER_VERSION = "edgelab-dsl-compiler/1"


class StrategyCompileError(ValueError):
    """The definition is valid but cannot run against this dataset/instrument."""


def compiler_source_hash() -> str:
    h = hashlib.sha256()
    for p in sorted(Path(__file__).parent.glob("*.py")):
        h.update(p.name.encode())
        h.update(p.read_bytes())
    return h.hexdigest()


# ------------------------------------------------------------------------------- compiled form
@dataclass
class CompiledDefinition:
    raw: dict
    logic: dict                              # canonical resolved logic (identity source)
    identity: StrategyIdentity
    order: OrderSpec
    sizing: dict                             # backtester sizing config
    feature_specs: list[FeatureSpec]
    local_sessions: dict[str, SessionWindow]
    tf_minutes: int
    family_id: str
    provenance: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {**self.identity.to_dict(), "name": self.raw.get("name"), "family": self.family_id,
                "timeframe": f"{self.tf_minutes}m", "order": asdict(self.order), "sizing": self.sizing,
                "features": [s.label for s in self.feature_specs], "provenance": self.provenance}


def _feature_spec(o: Mapping) -> FeatureSpec:
    return FeatureSpec.make(o["feature"], o["params"], o["timeframe"])


def _walk_operands(node: Any, out: list) -> list:
    if isinstance(node, Mapping):
        if "feature" in node and "output" in node:
            out.append(node)
        for v in node.values():
            _walk_operands(v, out)
    elif isinstance(node, list):
        for v in node:
            _walk_operands(v, out)
    return out


def _atr_spec(part: Mapping) -> FeatureSpec:
    return FeatureSpec.make("atr", {"period": part.get("period", 14)}, part.get("timeframe"))


def _session_spec(name: str) -> FeatureSpec:
    return FeatureSpec.make("session", {"session": name})


TOD_SPEC_ARGS = ("time_of_day", {"timezone": "America/New_York"})


def compile_definition(doc: Any, sessions: Mapping[str, SessionWindow],
                       config_hash: str | None = None) -> CompiledDefinition:
    raw = require_valid(doc, sessions)
    local = {n: session_from_dict(n, w) for n, w in (raw.get("sessions") or {}).items()}
    allsess = {**dict(sessions), **local}
    resolved = resolve(raw)
    logic = canonical_logic(resolved, allsess)
    ident = identity(raw, allsess)
    ent, ex = logic["entry"], logic["exit"]
    stop, tgt = ex["stop"], ex["target"]
    stop_points = stop["points"] if stop["type"] == "points" else None
    target_points = None
    if tgt["type"] == "points":
        target_points = tgt["points"]
    elif tgt["type"] == "risk_reward" and stop_points is not None:
        target_points = tgt["multiple"] * stop_points
    order = OrderSpec(entry_type=ent["order"]["type"], stop_points=stop_points, target_points=target_points,
                      time_exit_bars=ex["time_stop_bars"], max_hold_bars=ex["max_hold_bars"],
                      entry_expiry_bars=ent["order"].get("expiry_bars", 1))
    sz = logic["sizing"]
    sizing = ({"mode": "fixed", "contracts": sz["quantity"]} if sz["mode"] == "fixed" else
              {"mode": "risk", "risk_usd": sz["risk_usd"], "max_contracts": sz.get("max_quantity")})
    specs: dict[FeatureSpec, None] = {}
    for o in _walk_operands({"entry": ent, "exit": ex}, []):
        specs[_feature_spec(o)] = None
    for part in (stop, tgt):
        if part["type"] == "atr":
            specs[_atr_spec(part)] = None
    if ent.get("session"):
        specs[_session_spec(ent["session"])] = None
    if ent.get("trading_weekdays"):
        specs[FeatureSpec.make(*TOD_SPEC_ARGS)] = None
    fam = raw.get("family") or {}
    prov = {"dsl_version": DSL_VERSION, "compiler_version": COMPILER_VERSION,
            "compiler_source_sha256": compiler_source_hash(),
            "features": [{"spec": s.to_dict(), "spec_id": s.spec_id, "impl_hash": s.definition.impl_hash}
                         for s in specs],
            "sessions": logic["sessions"], "config_hash": config_hash}
    return CompiledDefinition(raw, logic, ident, order, sizing, list(specs), local,
                              timeframe_minutes(logic["timeframe"]), fam.get("id", "unassigned"), prov)


def compile_strategy(doc: Any, sessions: Mapping[str, SessionWindow],
                     config_hash: str | None = None) -> "DSLStrategy":
    return DSLStrategy(compile_definition(doc, sessions, config_hash))


# ------------------------------------------------------------------------------- evaluation
def _shift(x: np.ndarray, k: int) -> np.ndarray:
    if k == 0:
        return x
    out = np.full(len(x), np.nan)
    if k < len(x):
        out[k:] = x[:-k]
    return out


class _Eval:
    def __init__(self, bars: BarArrays, f: FeatureFrame):
        self.bars, self.f, self.n = bars, f, len(bars)
        self.memo: dict[str, np.ndarray] = {}

    def operand(self, o: Mapping) -> np.ndarray:
        import json
        key = json.dumps(o, sort_keys=True, default=str)
        if key in self.memo:
            return self.memo[key]
        if "const" in o:
            v = np.full(self.n, float(o["const"]))
        elif "bar" in o:
            v = _shift(np.asarray(getattr(self.bars, o["bar"]), float), o["lag"])
        elif "feature" in o:
            v = _shift(self.f.get_output(_feature_spec(o), o["output"]), o["lag"])
        else:
            a, b = (self.operand(x) for x in o["args"])
            with np.errstate(divide="ignore", invalid="ignore"):
                v = {"add": np.add, "sub": np.subtract, "mul": np.multiply, "div": np.divide}[o["arith"]](a, b)
            v = np.where(np.isfinite(v), v, np.nan)
        self.memo[key] = v
        return v

    def cond(self, c: Mapping) -> tuple[np.ndarray, np.ndarray]:
        """-> (value, known). value is only meaningful where known."""
        n = self.n
        if "all" in c or "any" in c:
            items = [self.cond(x) for x in c.get("all", c.get("any"))]
            if not items:
                return np.ones(n, bool), np.ones(n, bool)
            kt = np.all([k & v for v, k in items], axis=0)       # known-true
            kf = np.any([k & ~v for v, k in items], axis=0) if "all" in c else \
                np.all([k & ~v for v, k in items], axis=0)       # known-false
            if "all" in c:
                return kt, kt | kf
            anyt = np.any([k & v for v, k in items], axis=0)
            return anyt, anyt | kf
        if "not" in c:
            v, k = self.cond(c["not"])
            return ~v & k, k
        a, b = self.operand(c["left"]), self.operand(c["right"])
        known = np.isfinite(a) & np.isfinite(b)
        op = c["op"]
        with np.errstate(invalid="ignore"):
            if op == "crosses_above":
                a1, b1 = _shift(a, 1), _shift(b, 1)
                known &= np.isfinite(a1) & np.isfinite(b1)
                val = (a > b) & (a1 <= b1)
            else:
                val = {">": np.greater, ">=": np.greater_equal, "==": np.equal,
                       "!=": np.not_equal}[op](a, b)
        return val & known, known

    def true(self, c: Mapping | None) -> np.ndarray | None:
        if c is None:
            return None
        v, k = self.cond(c)
        return v & k


# ------------------------------------------------------------------------------- strategy
class DSLStrategy(FeatureStrategy):
    """A compiled DSL definition behind the standard Strategy interface."""

    def __init__(self, compiled: CompiledDefinition):
        super().__init__(compiled.order, logic=compiled.logic)
        self.compiled = compiled
        self.family = compiled.family_id
        self.last_diagnostics: dict = {}

    @property
    def strategy_id(self) -> str:
        return self.compiled.identity.strategy_id

    @property
    def spec(self) -> dict:
        s = super().spec
        s["dsl"] = {**self.compiled.identity.to_dict(), "compiler_version": COMPILER_VERSION,
                    "dsl_version": DSL_VERSION, "name": self.compiled.raw.get("name")}
        return s

    @property
    def sizing(self) -> dict:
        return dict(self.compiled.sizing)

    def feature_specs(self) -> list[FeatureSpec]:
        return list(self.compiled.feature_specs)

    def bind(self, context: FeatureContext) -> "DSLStrategy":
        ds = context.dataset
        c = self.compiled
        if ds.bars.tf_minutes != c.tf_minutes:
            raise StrategyCompileError(
                f"strategy {c.identity.strategy_id} is defined on {c.tf_minutes}m bars; dataset "
                f"{ds.manifest.dataset_id} is {ds.bars.tf_minutes}m (use or derive a {c.tf_minutes}m dataset)")
        if c.sizing["mode"] == "fixed":
            q, inst = c.sizing["contracts"], ds.instrument
            steps = q / inst.size_step
            if q < inst.min_size or abs(steps - round(steps)) > 1e-9:
                raise StrategyCompileError(
                    f"fixed quantity {q:g} is not valid for {inst.symbol} (min {inst.min_size:g}, "
                    f"step {inst.size_step:g}); instrument rules are never changed silently")
        if c.local_sessions:
            for n, w in c.local_sessions.items():
                if n in context.sessions and context.sessions[n].definition() != w.definition():
                    raise StrategyCompileError(f"session {n!r} conflicts with a configured session")
            context = FeatureContext(ds, {**context.sessions, **c.local_sessions}, context.engine.cache)
        return super().bind(context)

    def signals_from_features(self, bars: BarArrays, f: FeatureFrame) -> SignalSet:
        ev = _Eval(bars, f)
        diag = {"bars": len(bars)}
        return self._emit(bars, ev, self._entry_directions(bars, f, ev, diag), diag)

    def _entry_directions(self, bars: BarArrays, f: FeatureFrame, ev: "_Eval", diag: dict) -> np.ndarray:
        """The candidate's trigger stage: +1/-1/0 per bar (entry conditions, session/weekday, no
        ambiguous both-way bars). Levels, validity and cooldown come after, in ``_emit``."""
        ent, n = self.compiled.logic["entry"], len(bars)
        allowed = self._entry_allowed(f, n)
        long_ = ev.true(ent["long"]) if ent["long"] is not None else np.zeros(n, bool)
        short = ev.true(ent["short"]) if ent["short"] is not None else np.zeros(n, bool)
        long_ &= allowed
        short &= allowed
        both = long_ & short
        diag.update(raw_long=int(long_.sum()), raw_short=int(short.sum()), ambiguous_both=int(both.sum()))
        long_ &= ~both
        short &= ~both
        return np.where(long_, 1, np.where(short, -1, 0)).astype(np.int8)

    # The pieces below are shared with the research random-entry control (research/controls.py):
    # everything a candidate does at an entry EXCEPT deciding when/which way to enter.
    def _entry_allowed(self, f: FeatureFrame, n: int) -> np.ndarray:
        """Session / weekday eligibility of each bar (causal: session features at bar t)."""
        ent = self.compiled.logic["entry"]
        allowed = np.ones(n, bool)
        if ent.get("session"):
            allowed &= f.get_output(_session_spec(ent["session"]), "in_session") == 1.0
        if ent.get("trading_weekdays"):
            wd = f.get_output(FeatureSpec.make(*TOD_SPEC_ARGS), "trading_weekday")
            allowed &= np.isin(wd, [WEEKDAY_NAMES.index(d) for d in ent["trading_weekdays"]])
        return allowed

    def _orders(self, bars: BarArrays, ev: "_Eval", d: np.ndarray, diag: dict):
        """Entry reference, stop and target for direction array ``d`` (+1/-1/0) and which of those
        entries are executable (finite reference, stop and target on the correct sides)."""
        c, n = self.compiled, len(bars)
        ent, ex = c.logic["entry"], c.logic["exit"]
        long_, short = d == 1, d == -1
        dirf = d.astype(float)
        on = d != 0
        otype = ent["order"]["type"]
        if otype == "market":
            ref = np.asarray(bars.close, float)
        else:
            ref = np.full(n, np.nan)
            for side, mask in (("long", long_), ("short", short)):
                key = f"{side}_price"
                if key in ent["order"]:
                    ref = np.where(mask, ev.operand(ent["order"][key]), ref)

        stop, tgt = ex["stop"], ex["target"]
        stop_px = np.full(n, np.nan)
        if stop["type"] == "atr":
            atr = ev.f.get_output(_atr_spec(stop), "atr")
            stop_px = ref - dirf * stop["multiple"] * atr
        elif stop["type"] == "price":
            for side, mask in (("long", long_), ("short", short)):
                if side in stop:
                    stop_px = np.where(mask, ev.operand(stop[side]), stop_px)
        tgt_px = np.full(n, np.nan)
        if tgt["type"] == "atr":
            tgt_px = ref + dirf * tgt["multiple"] * ev.f.get_output(_atr_spec(tgt), "atr")
        elif tgt["type"] == "price":
            for side, mask in (("long", long_), ("short", short)):
                if side in tgt:
                    tgt_px = np.where(mask, ev.operand(tgt[side]), tgt_px)
        elif tgt["type"] == "risk_reward" and stop["type"] != "points":
            tgt_px = ref + dirf * tgt["multiple"] * np.abs(ref - stop_px)

        ok = on & np.isfinite(ref)
        diag["missing_entry_price"] = int((on & ~np.isfinite(ref)).sum())
        with np.errstate(invalid="ignore"):
            if stop["type"] != "points":
                good_stop = np.isfinite(stop_px) & (dirf * (ref - stop_px) > 0)
                diag["invalid_stop"] = int((ok & ~good_stop).sum())
                ok &= good_stop
            if tgt["type"] in ("atr", "price") or (tgt["type"] == "risk_reward" and stop["type"] != "points"):
                good_tgt = np.isfinite(tgt_px) & (dirf * (tgt_px - ref) > 0)
                diag["invalid_target"] = int((ok & ~good_tgt).sum())
                ok &= good_tgt
        return ref, stop_px, tgt_px, ok

    def _apply_cooldown(self, ok: np.ndarray, diag: dict) -> np.ndarray:
        cd = self.compiled.logic["entry"].get("cooldown_bars", 0)
        if not cd:
            return ok
        idx = np.flatnonzero(ok)
        keep, last = [], -10**12
        for i in idx:                         # causal: depends only on earlier kept signals
            if i - last > cd:
                keep.append(i)
                last = i
        kept = np.zeros(len(ok), bool)
        kept[np.asarray(keep, dtype=np.int64)] = True
        diag["cooldown_suppressed"] = int(ok.sum() - kept.sum())
        return kept

    def _emit(self, bars: BarArrays, ev: "_Eval", d: np.ndarray, diag: dict) -> SignalSet:
        """Levels, validity, cooldown and signal exits for entry directions ``d`` -> SignalSet."""
        n = len(bars)
        ent, ex = self.compiled.logic["entry"], self.compiled.logic["exit"]
        sig = SignalSet.empty(n)
        ref, stop_px, tgt_px, ok = self._orders(bars, ev, d, diag)
        if ent["order"]["type"] != "market":
            sig.entry_price[:] = np.where(d != 0, ref, np.nan)
        ok = self._apply_cooldown(ok, diag)
        sig.direction[:] = np.where(ok, d, 0)
        sig.stop_price[:] = np.where(ok, stop_px, np.nan)
        sig.target_price[:] = np.where(ok, tgt_px, np.nan)
        if ent["order"]["type"] != "market":
            sig.entry_price[:] = np.where(ok, ref, np.nan)
        sx = ex.get("signal") or {}
        if sx:
            sig.exit_long = ev.true(sx.get("long")) if sx.get("long") is not None else np.zeros(n, bool)
            sig.exit_short = ev.true(sx.get("short")) if sx.get("short") is not None else np.zeros(n, bool)
        diag["signals"] = int(ok.sum())
        self.last_diagnostics = diag
        return sig


# ------------------------------------------------------------------------------- preview
def _op_text(o: Mapping) -> str:
    if "const" in o:
        return f"{o['const']:g}"
    lag = f"[{o['lag']} bars ago]" if o.get("lag") else ""
    if "bar" in o:
        return f"{o['bar']}{lag}"
    if "feature" in o:
        p = ",".join(f"{k}={v}" for k, v in o["params"].items())
        tf = f"@{o['timeframe']}" if o.get("timeframe") else ""
        return f"{o['feature']}({p}){tf}.{o['output']}{lag}"
    sym = {"add": "+", "sub": "-", "mul": "*", "div": "/"}[o["arith"]]
    return f"({_op_text(o['args'][0])} {sym} {_op_text(o['args'][1])})"


def _cond_text(c: Mapping | None, indent: int = 0) -> list[str]:
    pad = "  " * indent
    if c is None:
        return [pad + "(none)"]
    if "all" in c or "any" in c:
        k = "ALL of" if "all" in c else "ANY of"
        out = [pad + k + ":"]
        for x in c.get("all", c.get("any")):
            out += _cond_text(x, indent + 1)
        return out
    if "not" in c:
        return [pad + "NOT:"] + _cond_text(c["not"], indent + 1)
    L, op, R = c["left"], c["op"], c["right"]
    if "const" in L and op in (">", ">="):          # display "rsi < 25", not canonical "25 > rsi"
        L, op, R = R, {">": "<", ">=": "<="}[op], L
    op = "crosses above" if op == "crosses_above" else op
    return [pad + f"{_op_text(L)} {op} {_op_text(R)}"]


def explain(compiled: CompiledDefinition) -> str:
    """Plain-language preview of the compiled logic (describes the rules; makes no claims)."""
    L = compiled.logic
    e, x = L["entry"], L["exit"]
    lines = [f"Strategy {compiled.identity.strategy_id}  ({compiled.raw.get('name')})",
             f"Family: {compiled.family_id}   Timeframe: {L['timeframe']}   Direction: {e['direction']}",
             f"Entry order: {e['order']['type']}" + (f", expires after {e['order']['expiry_bars']} bar(s)"
                                                      if e['order']['type'] != 'market' else " at next bar open")]
    if e.get("session"):
        lines.append(f"Only when the bar opens inside session {e['session']}: {L['sessions'].get(e['session'])}")
    if e.get("trading_weekdays"):
        lines.append(f"Only on trading days: {', '.join(e['trading_weekdays'])}")
    if e.get("cooldown_bars"):
        lines.append(f"At least {e['cooldown_bars']} bars between signals")
    for side in ("long", "short"):
        if e[side] is not None:
            lines += [f"{side.upper()} entry when:"] + _cond_text(e[side], 1)
            price = e["order"].get(f"{side}_price")
            if price:
                lines.append(f"  entry order price: {_op_text(price)}")
    lines.append(f"Stop: {x['stop']}")
    lines.append(f"Target: {x['target']}")
    if x.get("time_stop_bars"):
        lines.append(f"Time stop: close of bar {x['time_stop_bars']} after entry")
    if x.get("max_hold_bars"):
        lines.append(f"Max hold: {x['max_hold_bars']} bars")
    for side in ("long", "short"):
        if x.get("signal") and x["signal"].get(side) is not None:
            lines += [f"Exit {side} at next open when:"] + _cond_text(x["signal"][side], 1)
    lines.append(f"Sizing: {compiled.sizing}")
    lines.append("Features: " + "; ".join(s.label for s in compiled.feature_specs))
    return "\n".join(lines)
