"""AI Discovery orchestration and its file-based record (``<data root>/ai_discovery/``).

    generations/<AIG_...>.json   one provider call: normalized request, request_id, context hash +
                                 version, provider metadata, the provider's raw output hash, and
                                 every proposal envelope with its gate report (written once)
    decisions/<AIP_...>.json     the human review of one proposal (accept / reject, note) and, once
                                 saved, the library strategy it became (append-only history)

ids: request_id = hash(normalized request, context_hash, provider, model); generation_id =
hash(request_id, raw output hash); proposal_id = hash(generation_id, index, content). A
deterministic provider therefore reproduces the same ids; a new output gets new ids. Nothing
here reads results, ranks proposals or feeds anything back to a provider.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from edgelab.ai.context import build_context, resolve_scope
from edgelab.ai.gate import gate_proposal
from edgelab.ai.schema import PROPOSAL_SCHEMA_VERSION, normalize_request
from edgelab.core.identity import hash_obj

DECISIONS = ("accepted", "rejected")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class DiscoveryStore:
    def __init__(self, root: str | Path):
        self.root = Path(root)
        (self.root / "generations").mkdir(parents=True, exist_ok=True)
        (self.root / "decisions").mkdir(parents=True, exist_ok=True)

    def _write(self, path: Path, obj: Mapping) -> None:
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(obj, indent=1, sort_keys=True, default=str) + "\n", encoding="utf-8")
        tmp.replace(path)

    def save_generation(self, g: Mapping) -> bool:
        p = self.root / "generations" / f"{g['generation_id']}.json"
        if p.exists():
            return False                                   # immutable: same id = same content
        self._write(p, g)
        return True

    def generation(self, gid: str) -> dict:
        p = self.root / "generations" / f"{gid}.json"
        if not p.exists():
            raise KeyError(f"generation {gid} not found")
        return json.loads(p.read_text(encoding="utf-8"))

    def generations(self) -> list[dict]:
        out = []
        for p in sorted((self.root / "generations").glob("AIG_*.json")):
            g = json.loads(p.read_text(encoding="utf-8"))
            out.append({"generation_id": g["generation_id"], "request_id": g["request_id"], "created_at": g["created_at"],
                        "mode": g["request"]["mode"], "provider": g["provider"], "scope": g["scope"],
                        "n_proposals": len(g["proposals"]),
                        "n_valid": sum(p["gate"]["status"] == "valid" for p in g["proposals"])})
        return sorted(out, key=lambda r: r["created_at"], reverse=True)

    def find_proposal(self, pid: str) -> tuple[dict, dict]:
        for p in (self.root / "generations").glob("AIG_*.json"):
            g = json.loads(p.read_text(encoding="utf-8"))
            for prop in g["proposals"]:
                if prop["proposal_id"] == pid:
                    return g, prop
        raise KeyError(f"proposal {pid} not found")

    def decision(self, pid: str) -> dict | None:
        p = self.root / "decisions" / f"{pid}.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def record_decision(self, pid: str, entry: Mapping) -> dict:
        cur = self.decision(pid) or {"proposal_id": pid, "history": []}
        cur["history"].append(dict(entry))
        for k in ("decision", "note", "decided_at", "saved_strategy_id", "saved_at"):
            if k in entry:
                cur[k] = entry[k]
        self._write(self.root / "decisions" / f"{pid}.json", cur)
        return cur


def generate(svc, raw_request: Any, provider) -> dict:
    """Normalize -> resolve scope -> blind context -> provider -> envelopes -> gate -> store."""
    from edgelab.features.spec import all_defs
    fids = [d.feature_id for d in all_defs() if not d.feature_id.startswith("_")]
    req = normalize_request(raw_request, fids, sorted(svc.sessions))
    scope = resolve_scope(svc, req["scope"])
    base = parent = None
    if req["mode"] == "modify":
        doc = svc.library.load(req["base_strategy_id"])      # KeyError if absent
        base = doc["definition"]
        parent = {"strategy_id": req["base_strategy_id"], "logic_hash": doc.get("logic_hash"),
                  "definition_hash": doc.get("definition_hash"), "definition": base}
    ctx = build_context(svc, req, scope, base)
    pmeta = provider.describe()
    request_id = "AIR_" + hash_obj({"request": req, "context_hash": ctx["context_hash"],
                                     "provider": pmeta["kind"], "model": pmeta.get("model")}, 12).upper()
    out = provider.generate_proposals(req, ctx)
    generation_id = "AIG_" + hash_obj({"request_id": request_id, "raw_sha256": out.raw_sha256}, 12).upper()
    created = _now()
    cfg_hash = svc._config_hash()
    props, seen = [], {}
    for i, content in enumerate(out.proposals[:req["n_proposals"]]):
        pid = "AIP_" + hash_obj({"generation_id": generation_id, "index": i, "content": content}, 12).upper()
        gate = gate_proposal(content, req, scope, svc.sessions, cfg_hash, parent)
        if gate["status"] == "valid":
            lh = gate["identity"]["logic_hash"]
            if lh in seen:
                gate["status"] = "rejected"
                gate["stages"][-1] = {"stage": "identity", "status": "failed",
                                      "reasons": [f"identical logic to proposal {seen[lh]} in this generation"]}
                gate["rejection_reasons"] = [f"identity: identical logic to proposal {seen[lh]} in this generation"]
                gate["identity"] = None
            else:
                seen[lh] = pid
                sid = gate["identity"]["strategy_id"]
                if svc.library.exists(sid):
                    gate["warnings"].append(f"identical logic is already in the library as {sid}; saving adds a "
                                            "lineage record, not a new strategy")
        props.append({"proposal_id": pid, "schema_version": PROPOSAL_SCHEMA_VERSION, "request_id": request_id,
                      "generation_id": generation_id, "index": i, "provider": pmeta,
                      "generation": {"created_at": created, "raw_output_sha256": out.raw_sha256,
                                     "context_hash": ctx["context_hash"], "context_version": ctx["context_version"],
                                     "config_hash": cfg_hash},
                      "parent": ({k: parent[k] for k in ("strategy_id", "logic_hash", "definition_hash")}
                                 if parent else None),
                      "content": content, "gate": gate})
    dropped = max(0, len(out.proposals) - req["n_proposals"])
    g = {"object": "edgelab.ai_generation", "schema_version": PROPOSAL_SCHEMA_VERSION,
         "generation_id": generation_id, "request_id": request_id, "created_at": created, "request": req,
         "scope": scope, "context_hash": ctx["context_hash"], "context_version": ctx["context_version"],
         "provider": pmeta, "raw_output_sha256": out.raw_sha256, "provider_notes": out.notes,
         "dropped_beyond_bound": dropped, "proposals": props,
         "note": "proposals are hypotheses as data; none has been tested. The gate checks form, causality and "
                 "constraints only - it says nothing about performance."}
    store = DiscoveryStore(svc.data_root / "ai_discovery")
    if not store.save_generation(g):
        g = store.generation(generation_id)                 # identical deterministic output: keep the first record
    return with_decisions(store, g)


def with_decisions(store: DiscoveryStore, g: dict) -> dict:
    g = dict(g)
    g["proposals"] = [{**p, "decision": store.decision(p["proposal_id"])} for p in g["proposals"]]
    return g


def decide(svc, pid: str, decision: str, note: str = "") -> dict:
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}")
    if not isinstance(note, str) or len(note) > 2000:
        raise ValueError("note must be text of at most 2000 characters")
    store = DiscoveryStore(svc.data_root / "ai_discovery")
    _, prop = store.find_proposal(pid)
    if decision == "accepted" and prop["gate"]["status"] != "valid":
        raise ValueError(f"proposal {pid} was rejected by the gate and cannot be accepted: "
                         + "; ".join(prop["gate"]["rejection_reasons"][:5]))
    cur = store.decision(pid)
    if cur and cur.get("saved_strategy_id") and decision == "rejected":
        raise ValueError(f"proposal {pid} is already saved as {cur['saved_strategy_id']}; archive the strategy instead")
    return store.record_decision(pid, {"decision": decision, "note": note, "decided_at": _now()})


def save(svc, pid: str) -> dict:
    """Save an ACCEPTED, gate-valid proposal as an ordinary library strategy with full provenance.
    The definition is re-compiled now; a different identity than the gate recorded is refused."""
    from edgelab.strategy.compiler import COMPILER_VERSION, compile_definition
    from edgelab.strategy.dsl import DSL_VERSION, canonical_definition
    from edgelab.strategy.lineage import LineageRecord
    store = DiscoveryStore(svc.data_root / "ai_discovery")
    g, prop = store.find_proposal(pid)
    dec = store.decision(pid)
    if not dec or dec.get("decision") != "accepted":
        raise ValueError(f"proposal {pid} must be accepted before it is saved")
    if prop["gate"]["status"] != "valid":
        raise ValueError(f"proposal {pid} was rejected by the gate")
    defn = prop["content"]["definition"]
    cfg_hash = svc._config_hash()
    cc = compile_definition(defn, svc.sessions, cfg_hash)
    ident = cc.identity.to_dict()
    if ident["logic_hash"] != prop["gate"]["identity"]["logic_hash"]:
        raise ValueError("the proposal compiles to a different identity under the current configuration; "
                         "regenerate it (nothing was saved)")
    parent = prop.get("parent")
    method = "mode_b_modification" if parent else "mode_b_proposal"
    if parent and not svc.library.exists(parent["strategy_id"]):
        raise KeyError(f"base strategy {parent['strategy_id']} is no longer in the library")
    changes = [{"parameter": c, "old": None, "new": None, "category": "ai_stated_change"}
               for c in prop["content"].get("changes") or []]
    changes += [{"parameter": c, "old": None, "new": None, "category": "computed_change"}
                for c in (prop["gate"].get("summary") or {}).get("computed_changes") or []]
    rec = LineageRecord(ident["strategy_id"], ident["logic_hash"], ident["definition_hash"], cc.family_id, method,
                        parent["strategy_id"] if parent else None, changes=changes,
                        generation_parameters={"proposal_id": pid, "request_id": prop["request_id"],
                                               "generation_id": prop["generation_id"],
                                               "proposal_schema_version": prop["schema_version"],
                                               "provider": prop["provider"],
                                               "context_hash": prop["generation"]["context_hash"],
                                               "hypothesis": prop["content"].get("hypothesis"),
                                               "scope": g["scope"],
                                               "parent_definition_hash": parent["definition_hash"] if parent else None},
                        generation_batch_id=None,
                        versions={"dsl_version": DSL_VERSION, "compiler_version": COMPILER_VERSION,
                                  "config_hash": cfg_hash})
    created = svc.library.save(canonical_definition(defn), ident, rec)
    store.record_decision(pid, {"decision": "accepted", "saved_strategy_id": ident["strategy_id"],
                                "saved_at": _now(), "created_new_strategy": created})
    return {**ident, "proposal_id": pid, "created": created, "generation_method": method,
            "parent_strategy_id": parent["strategy_id"] if parent else None,
            "note": None if created else "identical logic already stored; a lineage record was added"}


def lineage(svc, pid: str) -> dict:
    """AI request -> proposal -> strategy version -> runs (with their status and prop simulations).
    For the human reviewer; never sent to a provider."""
    store = DiscoveryStore(svc.data_root / "ai_discovery")
    g, prop = store.find_proposal(pid)
    dec = store.decision(pid)
    out = {"request": {"request_id": g["request_id"], "request": g["request"], "scope": g["scope"],
                       "context_hash": g["context_hash"], "provider": g["provider"]},
           "generation_id": g["generation_id"],
           "proposal": {k: prop[k] for k in ("proposal_id", "schema_version", "parent", "gate")},
           "decision": dec, "strategy": None, "runs": [],
           "note": "random-entry controls are returned, not stored, so they do not appear here"}
    sid = (dec or {}).get("saved_strategy_id")
    if sid and svc.library.exists(sid):
        r = svc.strategy_research(sid)
        out["strategy"] = {**r["strategy"], "lineage": r["lineage"]}
        out["runs"] = r["runs"]
    return out
