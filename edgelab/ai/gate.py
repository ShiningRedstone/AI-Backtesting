"""The strict proposal gate. Every stage reports exact reasons; nothing is ever repaired.

    schema -> dsl_validation -> supported_features -> causality -> parameter_domain
           -> request_constraints -> compile -> identity

A proposal is ACCEPTED by the gate only when every stage passes; the human then reviews it. The
DSL validator's issues are assigned to the stage they belong to (unknown/unsupported feature ->
supported_features, non-causal reference -> causality, parameter declaration/value/domain ->
parameter_domain, everything else -> dsl_validation); the compile and identity stages run only
when all earlier stages pass.
"""
from __future__ import annotations

from typing import Any, Mapping

from edgelab.ai.schema import (CONTENT_KEYS, CONTENT_REQUIRED, FAMILY_KEYS, PROPOSAL_SCHEMA_VERSION,
                               code_phrases, _strings)
from edgelab.strategy import dsl
from edgelab.strategy.proposals import claim_phrases

STAGES = ("schema", "dsl_validation", "supported_features", "causality", "parameter_domain",
          "request_constraints", "compile", "identity")


def _stage_of(issue: dsl.Issue) -> str:
    m, p = issue.message.lower(), issue.path
    if "non-causal" in m or "not causal" in m or "future" in m or p.startswith("lead"):
        return "causality"
    if ("unknown feature" in m or "has no output" in m or "several outputs" in m or "feature timeframe" in m
            or p.endswith(".feature")):
        return "supported_features"
    if p.startswith("parameters") or ".params" in p or "parameter" in m or "outside [" in m:
        return "parameter_domain"
    return "dsl_validation"


def _walk(node: Any):
    if isinstance(node, Mapping):
        yield node
        for v in node.values():
            yield from _walk(v)
    elif isinstance(node, list):
        for v in node:
            yield from _walk(v)


def features_used(defn: Mapping) -> list[str]:
    body = {k: v for k, v in defn.items() if k in ("entry", "exit")}
    out = {n["feature"] for n in _walk(body) if isinstance(n.get("feature"), str)}
    ex = defn.get("exit") or {}
    for part in ("stop", "target"):
        if isinstance(ex.get(part), Mapping) and ex[part].get("type") == "atr":
            out.add("atr")
    return sorted(out)


def n_conditions(defn: Mapping) -> int:
    body = {"entry": {k: v for k, v in (defn.get("entry") or {}).items() if k in ("long", "short")},
            "signal": (defn.get("exit") or {}).get("signal")}
    return sum(1 for n in _walk(body) if "op" in n)


def gate_proposal(content: Any, request: Mapping, scope: Mapping, sessions: Mapping, config_hash: str | None,
                  parent: Mapping | None = None) -> dict:
    """-> {status, stages: [{stage, status, reasons}], identity?, summary?, warnings}."""
    from edgelab.strategy.compiler import compile_definition
    from edgelab.strategy.dsl import StrategyValidationError, canonical_definition
    st = {s: [] for s in STAGES}
    warnings: list[str] = []
    ran = set()

    def report(identity=None, summary=None):
        rows, failed = [], False
        for s in STAGES:
            if failed and s not in ran:
                rows.append({"stage": s, "status": "not_run", "reasons": []})
                continue
            status = "failed" if st[s] else "passed"
            failed = failed or bool(st[s])
            rows.append({"stage": s, "status": status, "reasons": st[s]})
        ok = not failed
        return {"status": "valid" if ok else "rejected", "stages": rows, "identity": identity if ok else None,
                "summary": summary, "warnings": warnings,
                "rejection_reasons": [f"{r['stage']}: {x}" for r in rows for x in r["reasons"]]}

    # ---- schema ---------------------------------------------------------------------------
    ran.add("schema")
    sch = st["schema"]
    if not isinstance(content, Mapping):
        sch.append("a proposal must be a JSON object")
        return report()
    for k in content:
        if k not in CONTENT_KEYS:
            sch.append(f"unknown key {k!r} (proposals may only contain {sorted(CONTENT_KEYS)}; "
                       "performance estimates are not accepted)")
    for k in CONTENT_REQUIRED:
        if content.get(k) in (None, "", [], {}):
            sch.append(f"missing required field {k!r}")
    for path, text in _strings(content):
        if hits := code_phrases(text):
            sch.append(f"{path}: contains code {hits[:3]} - proposals are data; code is never accepted")
        if path.startswith(("definition.entry", "definition.exit", "definition.parameters",
                                                    "definition.sizing", "definition.sessions")):
            continue                                         # DSL identifiers/values: checked by the DSL
        if hits := claim_phrases(text):
            sch.append(f"{path}: performance claim language {hits[:3]} - proposals state hypotheses; only "
                       "the numerical engine reports performance")
    defn = content.get("definition")
    if defn is not None and not isinstance(defn, Mapping):
        sch.append("definition must be a DSL strategy object (data), not text or code")
    fam = content.get("family")
    if fam is not None:
        if not isinstance(fam, Mapping) or not fam.get("id"):
            sch.append("family must be {id, name, category}")
        else:
            for k in fam:
                if k not in FAMILY_KEYS:
                    sch.append(f"family.{k}: unknown key")
    for k in ("assumptions", "constraints", "changes"):
        v = content.get(k)
        if v is not None and not (isinstance(v, list) and all(isinstance(x, str) for x in v)):
            sch.append(f"{k} must be a list of strings")
    if content.get("parameters") is not None and not isinstance(content.get("parameters"), Mapping):
        sch.append("parameters must map parameter names to values")
    if request["mode"] == "modify" and not content.get("changes"):
        sch.append("a modification must list its changes")
    if not sch and isinstance(defn, Mapping):
        dfam = defn.get("family") if isinstance(defn.get("family"), Mapping) else {}
        for k in ("id", "name", "category"):
            if k in fam and dfam.get(k) != fam[k]:
                sch.append(f"family.{k} {fam[k]!r} disagrees with definition.family.{k} {dfam.get(k)!r}")
        if content.get("timeframe") != defn.get("timeframe"):
            sch.append(f"timeframe {content.get('timeframe')!r} disagrees with definition.timeframe "
                       f"{defn.get('timeframe')!r}")
        if content.get("strategy_name") != defn.get("name"):
            sch.append(f"strategy_name {content.get('strategy_name')!r} disagrees with definition.name "
                       f"{defn.get('name')!r}")
        if content.get("instrument") not in (None, scope["instrument"]):
            sch.append(f"instrument {content.get('instrument')!r} is not the scoped instrument {scope['instrument']!r}")
        sess = (defn.get("entry") or {}).get("session") if isinstance(defn.get("entry"), Mapping) else None
        if content.get("session") != sess:
            sch.append(f"session {content.get('session')!r} disagrees with definition.entry.session {sess!r}")
        declared = defn.get("parameters") if isinstance(defn.get("parameters"), Mapping) else {}
        for k, v in (content.get("parameters") or {}).items():
            dv = declared.get(k, {}).get("value") if isinstance(declared.get(k), Mapping) else None
            if k not in declared:
                sch.append(f"parameters.{k} is not declared in the definition")
            elif dv != v:
                sch.append(f"parameters.{k}={v!r} disagrees with the definition value {dv!r}")
        for k in declared:
            if k not in (content.get("parameters") or {}):
                sch.append(f"parameters.{k} is declared in the definition but not listed")
    if sch:
        return report()

    # ---- DSL validation, split into its stages ----------------------------------------------
    for s in ("dsl_validation", "supported_features", "causality", "parameter_domain"):
        ran.add(s)
    try:
        res = dsl.validate(defn, sessions)
    except Exception as exc:                                   # noqa: BLE001 - malformed shapes
        st["dsl_validation"].append(f"definition could not be read as DSL: {exc}")
        return report()
    for i in res.issues:
        text = f"{i.path}: {i.message}" + (f" ({i.hint})" if i.hint else "")
        if i.severity == "error":
            st[_stage_of(i)].append(text)
        else:
            warnings.append(text)
    feats = features_used(defn)
    allowed = request["constraints"].get("features")
    if allowed is not None:
        extra = [f for f in feats if f not in allowed]
        if extra:
            st["supported_features"].append(f"uses features {extra} outside the requested set {allowed}")
    limits = request["constraints"].get("parameter_limits") or {}
    for name, lim in limits.items():
        p = (defn.get("parameters") or {}).get(name)
        if not isinstance(p, Mapping):
            continue
        for key in ("value", "min", "max"):
            v = p.get(key)
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                if "min" in lim and v < lim["min"]:
                    st["parameter_domain"].append(f"parameters.{name}.{key} {v} is below the requested limit {lim['min']}")
                if "max" in lim and v > lim["max"]:
                    st["parameter_domain"].append(f"parameters.{name}.{key} {v} is above the requested limit {lim['max']}")
    mp = request["constraints"].get("max_parameters")
    if mp is not None and len(defn.get("parameters") or {}) > mp:
        st["parameter_domain"].append(f"{len(defn.get('parameters') or {})} parameters > requested maximum {mp}")
    if any(st[s] for s in ("dsl_validation", "supported_features", "causality", "parameter_domain")):
        return report()

    # ---- request constraints (scope + construct allow-lists) ---------------------------------
    ran.add("request_constraints")
    rc = st["request_constraints"]
    r = dsl.resolve(defn)
    ent, ex = r.get("entry") or {}, r.get("exit") or {}
    if r.get("timeframe") != scope["timeframe"]:
        rc.append(f"timeframe {r.get('timeframe')} is not the research dataset's timeframe {scope['timeframe']}")
    if scope.get("session") and ent.get("session") != scope["session"]:
        rc.append(f"entry.session {ent.get('session')!r} is not the requested session {scope['session']!r}")
    if scope.get("direction") and ent.get("direction", "both") != scope["direction"]:
        rc.append(f"entry.direction {ent.get('direction')!r} is not the requested direction {scope['direction']!r}")
    cons = request["constraints"]
    checks = (("entry_orders", (ent.get("order") or {}).get("type", "market"), "entry order"),
              ("stop_types", (ex.get("stop") or {}).get("type"), "stop type"),
              ("target_types", (ex.get("target") or {}).get("type", "none"), "target type"),
              ("sizing_modes", (r.get("sizing") or {}).get("mode"), "sizing mode"))
    for key, val, label in checks:
        if cons.get(key) is not None and val not in cons[key]:
            rc.append(f"{label} {val!r} is outside the requested {cons[key]}")
    if cons.get("exit_kinds") is not None:
        used = [k for k in ("stop", "target", "time_stop_bars", "max_hold_bars", "signal")
                if ex.get(k) not in (None, {"type": "none"})]
        extra = [k for k in used if k not in cons["exit_kinds"]]
        if extra:
            rc.append(f"exit kinds {extra} are outside the requested {cons['exit_kinds']}")
    cd = ent.get("cooldown_bars")
    if cons.get("max_cooldown_bars") is not None and isinstance(cd, int) and cd > cons["max_cooldown_bars"]:
        rc.append(f"cooldown_bars {cd} > requested maximum {cons['max_cooldown_bars']}")
    nc = n_conditions(r)
    if cons.get("max_conditions") is not None and nc > cons["max_conditions"]:
        rc.append(f"{nc} conditions > requested maximum {cons['max_conditions']}")
    if rc:
        return report()

    # ---- canonical compile + identity -------------------------------------------------------
    ran.update({"compile", "identity"})
    try:
        cc = compile_definition(defn, sessions, config_hash)
    except StrategyValidationError as exc:
        st["compile"] += [f"{x.path}: {x.message}" for x in exc.result.errors]
        return report()
    except Exception as exc:                                   # noqa: BLE001 - reported, not repaired
        st["compile"].append(f"{type(exc).__name__}: {exc}")
        return report()
    ident = cc.identity.to_dict()
    if parent is not None and ident.get("logic_hash") == parent.get("logic_hash"):
        st["identity"].append(f"identical logic to the base strategy {parent.get('strategy_id')} - not a new version")
    summary = {"name": defn.get("name"), "family_id": cc.family_id, "timeframe": f"{cc.tf_minutes}m",
               "session": ent.get("session"), "direction": ent.get("direction", "both"),
               "entry_order": (ent.get("order") or {}).get("type", "market"),
               "entry": {k: ent.get(k) for k in ("long", "short") if ent.get(k) is not None},
               "exit": {k: v for k, v in ex.items() if v is not None},
               "stop": ex.get("stop"), "target": ex.get("target"), "sizing": r.get("sizing"),
               "parameters": {k: v.get("value") for k, v in (defn.get("parameters") or {}).items()},
               "features": feats, "complexity": {"conditions": nc, "parameters": len(defn.get("parameters") or {}),
                                                 "features": len(feats)},
               "canonical_definition": canonical_definition(defn)}
    if parent is not None:
        summary["computed_changes"] = _diff(parent.get("definition") or {}, defn)
    return report(ident, summary)


def _diff(a: Any, b: Any, path: str = "") -> list[str]:
    """Paths whose values differ between two definitions (EdgeLab-computed; not the provider's claim)."""
    if isinstance(a, Mapping) and isinstance(b, Mapping):
        out = []
        for k in sorted(set(a) | set(b), key=str):
            if k in ("name", "description"):
                continue
            p = f"{path}.{k}" if path else str(k)
            if k not in a:
                out.append(f"{p}: added")
            elif k not in b:
                out.append(f"{p}: removed")
            else:
                out += _diff(a[k], b[k], p)
        return out
    return [] if a == b else [f"{path}: {a!r} -> {b!r}"]


def schema_version() -> int:
    return PROPOSAL_SCHEMA_VERSION
