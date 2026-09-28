"""Mode B: interface for AI (or human) strategy-family proposals.

    proposer (AI, later) -> ProposalBatch (structured data) -> ingest_proposals()
        -> each strategy: DSL validation -> compilation -> lineage (mode_b_proposal)
        -> accepted strategies wait for the numerical engine (Phase 4)

Guarantees enforced here, not merely documented:
  * a proposal is data, never code; its strategy goes through the SAME validator/compiler;
  * the schema has no place for performance: unknown keys anywhere in a proposal are rejected,
    and claim language ("profitable", "win rate", "guaranteed", ...) in its text is rejected -
    the AI proposes hypotheses, the backtester measures;
  * duplicates are rejected (same logic) and near-duplicates flagged (same structure, only
    numbers differ: that is a variation, not a distinct family).
No LLM is called anywhere in this package.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Mapping, Protocol

from edgelab.core.identity import hash_obj
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import all_defs
from edgelab.strategy import dsl
from edgelab.strategy.compiler import COMPILER_VERSION, compile_definition
from edgelab.strategy.dsl import StrategyValidationError, canonical_definition, load_definition
from edgelab.strategy.lineage import LineageRecord
from edgelab.strategy.variations import validate_spec

PROPOSAL_BATCH_VERSION = 1
BATCH_KEYS = {"proposal_batch_version", "request", "source", "proposals"}
SOURCE_KEYS = {"kind", "model", "prompt_sha256", "notes"}
PROPOSAL_KEYS = {"family", "rationale", "strategy", "variations"}
FAMILY_KEYS = {"id", "name", "hypothesis", "category"}
SOURCE_KINDS = ("ai", "human")
CLAIM_PATTERNS = [r"\bprofitab", r"\bguarantee", r"win[\s_-]?rate", r"high[\s_-]probability", r"\bsharpe\b",
                  r"profit[\s_-]factor", r"\bbest[\s_-]strateg", r"strong[\s_-]edge", r"\bproven\b",
                  r"\breturns?\s+of\s+\d", r"\d+\s*%\s*(return|accuracy|win)", r"\bexpectancy\b", r"\bcan'?t\s+lose\b"]


def claim_phrases(text: str) -> list[str]:
    return [m.group(0) for p in CLAIM_PATTERNS for m in re.finditer(p, text or "", re.IGNORECASE)]


@dataclass(frozen=True)
class ProposalRequest:
    """What a proposer is asked for, plus the machine-readable menu of what the DSL can express."""
    n_families: int
    instructions: str = ""
    timeframes: tuple[str, ...] = ("1m", "5m", "15m", "60m")

    def capability_menu(self, sessions: Mapping[str, SessionWindow]) -> dict:
        return {"dsl_version": dsl.DSL_VERSION, "requested_families": self.n_families,
                "instructions": self.instructions,
                "rules": ["return data matching the proposal_batch schema; no code",
                          "each family must be a genuinely different market hypothesis",
                          "do not state or estimate performance: the numerical engine measures it"],
                "operators": list(dsl.OPERATORS), "bar_fields": list(dsl.BAR_FIELDS),
                "arithmetic": list(dsl.ARITH_OPS), "entry_orders": list(dsl.ENTRY_ORDER_TYPES),
                "directions": list(dsl.DIRECTIONS), "stop_types": list(dsl.STOP_TYPES),
                "target_types": list(dsl.TARGET_TYPES), "sizing_modes": list(dsl.SIZING_MODES),
                "unsupported": dict(dsl.UNSUPPORTED), "timeframes": list(self.timeframes),
                "sessions": {k: v.definition() for k, v in sessions.items()},
                "features": [{"id": d.feature_id, "params": [p.describe() for p in d.params],
                              "outputs": [n for n, _ in d.outputs], "requires": list(d.requires)}
                             for d in all_defs() if not d.feature_id.startswith("_")]}


class StrategyProposer(Protocol):
    """Anything that can propose strategy families (a future AI layer, a human form, a file)."""

    def propose(self, request: ProposalRequest, menu: Mapping) -> Mapping: ...


class StaticProposer:
    """Returns a fixed batch (tests, or proposals written by hand / by an offline AI session)."""

    def __init__(self, batch: Any):
        self.batch = load_definition(batch)

    def propose(self, request: ProposalRequest, menu: Mapping) -> Mapping:
        return self.batch


@dataclass
class IngestReport:
    batch_id: str
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    lineage: list = field(default_factory=list)          # LineageRecord per accepted proposal
    definitions: dict = field(default_factory=dict)      # strategy_id -> canonical definition
    identities: dict = field(default_factory=dict)
    source: dict = field(default_factory=dict)
    config_hash: str | None = None

    def to_dict(self) -> dict:
        return {"batch_id": self.batch_id, "accepted": self.accepted, "rejected": self.rejected,
                "warnings": self.warnings, "n_accepted": len(self.accepted), "n_rejected": len(self.rejected)}

    def record(self) -> dict:
        """The batch record the strategy library stores (kind: proposal). `children` are the
        accepted strategy ids; rejected proposals are kept with their reasons."""
        return {**self.to_dict(), "kind": "proposal",
                "created_at": self.lineage[0].generation_timestamp if self.lineage
                else datetime.now(timezone.utc).isoformat(),
                "source": dict(self.source), "config_hash": self.config_hash,
                "versions": {"dsl_version": dsl.DSL_VERSION, "compiler_version": COMPILER_VERSION},
                "children": [a["strategy_id"] for a in self.accepted]}


def _structure(node: Any) -> Any:
    """Logic with every number replaced by '#': equal structures differ only in settings."""
    if isinstance(node, bool) or node is None:
        return node
    if isinstance(node, (int, float)):
        return "#"
    if isinstance(node, dict):
        return {k: ("#" if k == "params" else _structure(v)) for k, v in node.items() if k != "sessions"}
    if isinstance(node, list):
        return sorted((_structure(v) for v in node), key=lambda x: hash_obj(x))
    return node


def _unknown(node: Mapping, allowed: set, path: str, errors: list) -> None:
    for k in node:
        if k not in allowed:
            errors.append(f"{path}.{k}: unknown key (proposals may only contain {sorted(allowed)}; "
                          "performance estimates are not accepted)")


def ingest_proposals(batch: Any, sessions: Mapping[str, SessionWindow],
                     config_hash: str | None = None) -> IngestReport:
    b = load_definition(batch)
    top_errors: list[str] = []
    _unknown(b, BATCH_KEYS, "batch", top_errors)
    if b.get("proposal_batch_version", PROPOSAL_BATCH_VERSION) != PROPOSAL_BATCH_VERSION:
        top_errors.append(f"unsupported proposal_batch_version; this build reads {PROPOSAL_BATCH_VERSION}")
    src = b.get("source")
    if not isinstance(src, Mapping) or src.get("kind") not in SOURCE_KINDS:
        top_errors.append(f"source.kind must be one of {SOURCE_KINDS} (who proposed these?)")
    else:
        _unknown(src, SOURCE_KEYS, "source", top_errors)
    props = b.get("proposals")
    if not isinstance(props, list) or not props:
        top_errors.append("proposals must be a non-empty list")
    batch_id = "PB_" + hash_obj(b, 12).upper()
    if top_errors:
        raise StrategyValidationError(dsl.ValidationResult([dsl.Issue("batch", e) for e in top_errors]))
    rep = IngestReport(batch_id, source=dict(b["source"]), config_hash=config_hash)
    fam_ids, logic_seen, struct_seen = set(), {}, {}
    for i, p in enumerate(props):
        errs: list[str] = []
        path = f"proposals[{i}]"
        if not isinstance(p, Mapping):
            rep.rejected.append({"index": i, "family_id": None, "errors": [f"{path}: must be a mapping"]})
            continue
        _unknown(p, PROPOSAL_KEYS, path, errs)
        fam = p.get("family")
        fid = fam.get("id") if isinstance(fam, Mapping) else None
        if not isinstance(fam, Mapping) or not fid or not fam.get("hypothesis"):
            errs.append(f"{path}.family: needs id and hypothesis")
        else:
            _unknown(fam, FAMILY_KEYS, f"{path}.family", errs)
            if fid in fam_ids:
                errs.append(f"{path}.family.id: duplicate family id {fid!r} in this batch")
        for field_name, text in (("family.name", (fam or {}).get("name", "") if isinstance(fam, Mapping) else ""),
                                 ("family.hypothesis", (fam or {}).get("hypothesis", "") if isinstance(fam, Mapping) else ""),
                                 ("rationale", p.get("rationale", "")),
                                 ("strategy.description", (p.get("strategy") or {}).get("description", "")
                                  if isinstance(p.get("strategy"), Mapping) else "")):
            hits = claim_phrases(str(text))
            if hits:
                errs.append(f"{path}.{field_name}: performance claim language {hits} - proposals state "
                            "hypotheses; only the numerical engine reports performance")
        strat = p.get("strategy")
        if not isinstance(strat, Mapping):
            errs.append(f"{path}.strategy: missing DSL strategy definition")
        if errs:
            rep.rejected.append({"index": i, "family_id": fid, "errors": errs})
            continue
        strat = dict(strat)
        sfam = strat.get("family")
        if sfam is None:
            strat["family"] = dict(fam)
        elif isinstance(sfam, Mapping) and sfam.get("id") != fid:
            rep.rejected.append({"index": i, "family_id": fid, "errors": [
                f"{path}.strategy.family.id {sfam.get('id')!r} != proposal family id {fid!r}"]})
            continue
        try:
            cc = compile_definition(strat, sessions, config_hash)
        except StrategyValidationError as exc:
            rep.rejected.append({"index": i, "family_id": fid,
                                 "errors": [f"{path}.strategy.{x.path}: {x.message}" +
                                            (f" ({x.hint})" if x.hint else "") for x in exc.result.errors]})
            continue
        if p.get("variations") is not None:
            vr = validate_spec(p["variations"], strat)
            if not vr.valid:
                rep.rejected.append({"index": i, "family_id": fid, "errors": [
                    f"{path}.variations.{x.path}: {x.message}" for x in vr.errors]})
                continue
        lh = cc.identity.logic_hash
        if lh in logic_seen:
            rep.rejected.append({"index": i, "family_id": fid, "errors": [
                f"{path}: identical logic to proposal {logic_seen[lh]} (not a distinct family)"]})
            continue
        logic_seen[lh] = i
        sk = hash_obj(_structure(cc.logic))
        if sk in struct_seen:
            rep.warnings.append(f"{path} ({fid}) has the same structure as proposal {struct_seen[sk]} - only "
                                "numeric settings differ, so it is a variation rather than a distinct family")
        else:
            struct_seen[sk] = i
        fam_ids.add(fid)
        rec = LineageRecord(strategy_id=cc.identity.strategy_id, logic_hash=lh,
                            definition_hash=cc.identity.definition_hash, family_id=fid,
                            generation_method="mode_b_proposal", parent_strategy_id=None,
                            generation_parameters={"source": dict(b["source"]), "proposal_index": i,
                                                   "proposed_variations": p.get("variations")},
                            generation_batch_id=batch_id,
                            versions={"dsl_version": dsl.DSL_VERSION, "compiler_version": COMPILER_VERSION,
                                      "config_hash": config_hash})
        rep.lineage.append(rec)
        rep.definitions[cc.identity.strategy_id] = canonical_definition(strat)
        rep.identities[cc.identity.strategy_id] = cc.identity.to_dict()
        rep.accepted.append({"index": i, "family_id": fid, "strategy_id": cc.identity.strategy_id,
                             "name": strat.get("name")})
    requested = (b.get("request") or {}).get("n_families") if isinstance(b.get("request"), Mapping) else None
    if requested and len(rep.accepted) < requested:
        rep.warnings.append(f"{requested} families requested, {len(rep.accepted)} accepted")
    return rep
