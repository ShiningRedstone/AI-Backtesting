"""Mode A: controlled, deterministic strategy variations.

    base strategy (hypothesis + declared parameter domains)
      + variation spec (which parameters to vary, over which values, how to combine)
      -> child definitions, each validated AND compiled, each with lineage

Rules
  * only parameters DECLARED by the base strategy can vary, and only within their declared
    domain (min/max/step or choices) - the variation spec cannot widen the hypothesis;
  * modes: grid (cartesian product), one_at_a_time (sensitivity: one parameter moves, the
    rest stay at base), random_sample (a seeded sample of the grid - seed is REQUIRED);
  * the combination count is checked against max_variants BEFORE generation (loud failure,
    never silent truncation);
  * children with identical logic are de-duplicated and reported (e.g. a threshold varied
    while its filter is disabled), so strategy counts are not inflated;
  * any invalid child fails the whole batch with the offending combinations listed;
  * the batch id is a hash of (base logic, base definition, spec, generator version): the same
    inputs always regenerate the same children.
"""
from __future__ import annotations

import copy
import itertools
import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping

import numpy as np

from edgelab.core.identity import hash_obj
from edgelab.features.sessions import SessionWindow
from edgelab.strategy.compiler import COMPILER_VERSION, CompiledDefinition, compile_definition
from edgelab.strategy.dsl import (DSL_VERSION, Issue, StrategyValidationError, ValidationResult,
                                  _num, _suggest, canonical_definition, check_parameter, load_definition,
                                  param_values, require_valid)
from edgelab.strategy.lineage import Change, LineageRecord

VARIATION_SPEC_VERSION = 1
GENERATOR_VERSION = "edgelab-mode-a/1"
MODES = ("grid", "one_at_a_time", "random_sample")
SPEC_KEYS = {"variation_spec_version", "name", "description", "mode", "dimensions", "max_variants",
             "include_base", "seed", "sample_size"}
DIM_KEYS = {"parameter", "values", "range", "category"}
MAX_GRID = 10_000_000


class VariationError(ValueError):
    pass


def _dim_values(dim: Mapping) -> list:
    if "values" in dim:
        return list(dim["values"])
    r = dim["range"]
    lo, hi, st = r["min"], r["max"], r["step"]
    k = int(round((hi - lo) / st))
    vals = [lo + i * st for i in range(k + 1)]
    if all(isinstance(x, int) and not isinstance(x, bool) for x in (lo, hi, st)):
        return [int(v) for v in vals]
    return [round(v, 10) for v in vals]


def validate_spec(spec: Mapping, base: Mapping) -> ValidationResult:
    iss: list[Issue] = []
    if not isinstance(spec, Mapping):
        return ValidationResult([Issue("$", "variation spec must be a mapping")])
    for k in spec:
        if k not in SPEC_KEYS:
            iss.append(Issue(k, f"unknown key {k!r}", _suggest(k, SPEC_KEYS)))
    if spec.get("variation_spec_version", VARIATION_SPEC_VERSION) != VARIATION_SPEC_VERSION:
        iss.append(Issue("variation_spec_version", f"unsupported version; this build reads {VARIATION_SPEC_VERSION}"))
    mode = spec.get("mode", "grid")
    if mode not in MODES:
        iss.append(Issue("mode", f"invalid mode {mode!r}", f"one of {MODES}"))
    mv = spec.get("max_variants", 1000)
    if not isinstance(mv, int) or isinstance(mv, bool) or mv < 1:
        iss.append(Issue("max_variants", "max_variants must be an integer >= 1"))
    decls = base.get("parameters") or {}
    dims = spec.get("dimensions")
    if not isinstance(dims, list) or not dims:
        iss.append(Issue("dimensions", "dimensions must be a non-empty list"))
        return ValidationResult(iss)
    seen = set()
    for i, d in enumerate(dims):
        p = f"dimensions[{i}]"
        if not isinstance(d, Mapping):
            iss.append(Issue(p, "dimension must be a mapping"))
            continue
        for k in d:
            if k not in DIM_KEYS:
                iss.append(Issue(f"{p}.{k}", f"unknown key {k!r}", _suggest(k, DIM_KEYS)))
        name = d.get("parameter")
        if name not in decls:
            iss.append(Issue(f"{p}.parameter", f"base strategy declares no parameter {name!r}",
                             _suggest(name, decls) if decls else "declare parameters in the base strategy"))
            continue
        if name in seen:
            iss.append(Issue(f"{p}.parameter", f"parameter {name!r} appears in two dimensions"))
        seen.add(name)
        if ("values" in d) == ("range" in d):
            iss.append(Issue(p, "give exactly one of 'values' or 'range'"))
            continue
        decl = decls[name]
        if "range" in d:
            r = d["range"]
            if decl.get("type") not in ("integer", "float"):
                iss.append(Issue(f"{p}.range", f"range needs a numeric parameter; {name!r} is {decl.get('type')}"))
                continue
            if not isinstance(r, Mapping) or not all(_num(r.get(k)) for k in ("min", "max", "step")):
                iss.append(Issue(f"{p}.range", "range needs numeric min, max, step"))
                continue
            if r["step"] <= 0:
                iss.append(Issue(f"{p}.range.step", f"step must be > 0, got {r['step']}"))
                continue
            if r["min"] > r["max"]:
                iss.append(Issue(f"{p}.range", f"min {r['min']} > max {r['max']}"))
                continue
            k = (r["max"] - r["min"]) / r["step"]
            if abs(k - round(k)) > 1e-9:
                iss.append(Issue(f"{p}.range.step", f"(max - min) is not a multiple of step {r['step']}"))
                continue
        else:
            if not isinstance(d["values"], list) or not d["values"]:
                iss.append(Issue(f"{p}.values", "values must be a non-empty list"))
                continue
            if len({repr(v) for v in d["values"]}) != len(d["values"]):
                iss.append(Issue(f"{p}.values", "duplicate values"))
        for v in _dim_values(d):
            probs = check_parameter(name, {**decl, "value": v}, f"{p}")
            if probs:
                iss.append(Issue(f"{p}", f"value {v!r} is outside the domain declared by the base strategy "
                                         f"for {name!r}: {probs[0].message}",
                                 "the variation spec can only explore the declared hypothesis"))
                break
        if d.get("category") is not None and not isinstance(d["category"], str):
            iss.append(Issue(f"{p}.category", "category must be a string"))
    if mode == "random_sample":
        if not isinstance(spec.get("seed"), int) or isinstance(spec.get("seed"), bool):
            iss.append(Issue("seed", "random_sample requires an explicit integer seed (reproducibility)"))
        ss = spec.get("sample_size")
        if not isinstance(ss, int) or isinstance(ss, bool) or ss < 1:
            iss.append(Issue("sample_size", "random_sample requires sample_size >= 1"))
    elif "seed" in spec or "sample_size" in spec:
        iss.append(Issue("seed", f"seed/sample_size only apply to random_sample (mode is {mode})", severity="warning"))
    return ValidationResult(iss)


@dataclass
class Variant:
    strategy_id: str
    name: str
    definition: dict
    identity: dict
    lineage: LineageRecord
    overrides: dict


@dataclass
class VariationBatch:
    batch_id: str
    base_strategy_id: str
    variants: list[Variant]
    duplicates: list[dict] = field(default_factory=list)
    same_as_base: list[dict] = field(default_factory=list)
    record: dict = field(default_factory=dict)

    def summary(self) -> dict:
        return {"batch_id": self.batch_id, "base_strategy_id": self.base_strategy_id,
                "generated": len(self.variants), "duplicates_removed": len(self.duplicates),
                "same_as_base": len(self.same_as_base), "combinations": self.record.get("combinations"),
                "varied_parameters": [d["parameter"] for d in self.record.get("spec", {}).get("dimensions", [])],
                "variants": [{"strategy_id": v.strategy_id, "name": v.name, "overrides": dict(v.overrides),
                              "changes": [c.__dict__ for c in v.lineage.changes]} for v in self.variants],
                "duplicates": list(self.duplicates), "same_as_base_combinations": list(self.same_as_base)}


def _fmt(v: Any) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, float):
        return f"{v:g}"
    return str(v)


def _combos(spec: Mapping, dims: list, base_vals: Mapping) -> list[dict]:
    names = [d["parameter"] for d in dims]
    values = [_dim_values(d) for d in dims]
    mode = spec.get("mode", "grid")
    if mode == "one_at_a_time":
        out = []
        for n, vals in zip(names, values):
            out += [{n: v} for v in vals if v != base_vals[n]]
        return out
    sizes = [len(v) for v in values]
    total = math.prod(sizes)
    if mode == "grid":
        return [dict(zip(names, c)) for c in itertools.product(*values)]
    rng = np.random.default_rng(spec["seed"])
    picks = sorted(rng.choice(total, size=min(spec["sample_size"], total), replace=False).tolist())
    out = []
    for idx in picks:                       # mixed-radix decode: no full enumeration needed
        combo, rem = {}, idx
        for n, vals, s in zip(reversed(names), reversed(values), reversed(sizes)):
            combo[n] = vals[rem % s]
            rem //= s
        out.append({n: combo[n] for n in names})
    return out


def combination_count(spec: Mapping) -> int:
    dims = spec["dimensions"]
    sizes = [len(_dim_values(d)) for d in dims]
    mode = spec.get("mode", "grid")
    if mode == "one_at_a_time":
        return sum(sizes)
    total = math.prod(sizes)
    return min(spec.get("sample_size", total), total) if mode == "random_sample" else total


def canonical_spec(spec: Mapping) -> dict:
    s = {k: v for k, v in spec.items() if k not in ("name", "description")}
    s.setdefault("mode", "grid")
    s.setdefault("max_variants", 1000)
    s.setdefault("include_base", False)
    s["dimensions"] = [{"parameter": d["parameter"], "values": _dim_values(d),
                        "category": d.get("category", "parameter")} for d in spec["dimensions"]]
    return s


def generate_variations(base: Any, spec: Any, sessions: Mapping[str, SessionWindow],
                        config_hash: str | None = None, parent_strategy_id: str | None = None) -> VariationBatch:
    base_raw = require_valid(base, sessions)
    spec = load_definition(spec)
    vr = validate_spec(spec, base_raw)
    if not vr.valid:
        raise StrategyValidationError(vr)
    n_comb = combination_count(spec)
    mv = spec.get("max_variants", 1000)
    total_grid = math.prod(len(_dim_values(d)) for d in spec["dimensions"])
    if total_grid > MAX_GRID:
        raise VariationError(f"grid of {total_grid:,} combinations exceeds the hard limit {MAX_GRID:,}")
    if n_comb > mv:
        raise VariationError(f"spec produces {n_comb:,} combinations > max_variants {mv:,}. Narrow the "
                             "dimensions, use one_at_a_time or random_sample, or raise max_variants deliberately.")
    base_c = compile_definition(base_raw, sessions, config_hash)
    base_id = base_c.identity
    parent = parent_strategy_id or base_id.strategy_id
    base_vals = param_values(base_raw)
    cats = {d["parameter"]: d.get("category", "parameter") for d in spec["dimensions"]}
    cspec = canonical_spec(spec)
    batch_id = "VB_" + hash_obj({"base_logic": base_id.logic_hash, "base_definition": base_id.definition_hash,
                                 "spec": cspec, "generator": GENERATOR_VERSION}, 12).upper()
    ts = datetime.now(timezone.utc).isoformat()
    versions = {"dsl_version": DSL_VERSION, "compiler_version": COMPILER_VERSION,
                "generator_version": GENERATOR_VERSION, "config_hash": config_hash}
    variants, dups, same, failures = [], [], [], []
    seen: dict[str, str] = {}
    if spec.get("include_base"):
        seen[base_id.logic_hash] = base_id.strategy_id
    for combo in _combos(spec, spec["dimensions"], base_vals):
        child = copy.deepcopy(base_raw)
        for p, v in combo.items():
            child["parameters"][p]["value"] = v
        changed = {p: v for p, v in combo.items() if v != base_vals[p]}
        child["name"] = f"{base_raw['name']}__" + ("__".join(f"{p}-{_fmt(v)}" for p, v in changed.items()) or "base")
        try:
            cc: CompiledDefinition = compile_definition(child, sessions, config_hash)
        except StrategyValidationError as exc:
            failures.append({"combination": combo, "errors": [i.text() for i in exc.result.errors]})
            continue
        lh = cc.identity.logic_hash
        if lh == base_id.logic_hash and not spec.get("include_base"):
            same.append({"combination": combo, "strategy_id": base_id.strategy_id})
            continue
        if lh in seen:
            dups.append({"combination": combo, "duplicate_of": seen[lh]})
            continue
        seen[lh] = cc.identity.strategy_id
        rec = LineageRecord(
            strategy_id=cc.identity.strategy_id, logic_hash=lh, definition_hash=cc.identity.definition_hash,
            family_id=cc.family_id, generation_method="mode_a_variation", parent_strategy_id=parent,
            changes=[Change(p, base_vals[p], v, cats[p]) for p, v in changed.items()],
            generation_parameters={"variation_spec": spec.get("name"), "mode": cspec["mode"],
                                   "overrides": combo},
            generation_batch_id=batch_id, generation_timestamp=ts,
            versions={**versions, "features": [f["spec_id"] for f in cc.provenance["features"]]})
        variants.append(Variant(cc.identity.strategy_id, child["name"], canonical_definition(child),
                                cc.identity.to_dict(), rec, combo))
    if failures:
        detail = "\n".join(f"  {f['combination']}: {f['errors'][0]}" for f in failures[:10])
        raise VariationError(f"{len(failures)} generated combination(s) are invalid - batch rejected:\n{detail}")
    record = {"batch_id": batch_id, "created_at": ts, **versions,
              "base": {**base_id.to_dict(), "definition": canonical_definition(base_raw)},
              "parent_strategy_id": parent, "spec": cspec, "spec_hash": hash_obj(cspec),
              "combinations": n_comb, "generated": len(variants), "duplicates": dups,
              "same_as_base": same, "children": [v.strategy_id for v in variants]}
    return VariationBatch(batch_id, base_id.strategy_id, variants, dups, same, record)
