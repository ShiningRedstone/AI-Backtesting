"""Noise band set (ADR-116): the user's noise band breakout as 10,000 frozen DSL strategies under their OWN protocol.

    status  -> read-only: the design, the datasets per timeframe, the set protocol (if created) and its counters
    create  -> the ONE noise band set protocol of the ACTIVE research protocol (``strategy/noiseband_space``: 10,000
               distinct versions; the parent's data, windows, execution and config; its own trial budget = the set,
               its own holdout looks); the set's rows go to a workspace file whose hash is in the protocol; the
               strategies are saved to the library (no evaluation)
    run     -> one protocol-gated search of exactly the registered set on the discovery window (``batch.run_search``
               -> ``Services._run_cell``: the same engine, costs, fills, prop audit and trial ledger as every strategy;
               one trial per strategy, counted in the set protocol); resumable, every core the Settings allow

Every registered strategy is governed by the set protocol (``Services._governing_protocol(logic_hash=)``), never by the
parent, so the parent's trial budget and holdout looks are untouched. Survivors go through the normal Holdout backtest
under the set protocol's own looks.
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Any, Callable, Mapping

from edgelab.core.identity import hash_obj
from edgelab.research import protocol as rp
from edgelab.strategy import noiseband_space as N

FOLDER = "noise_band"
NAME = "Noise band set"
_SETS: dict[str, list] = {}


class NoiseBandError(ValueError):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


# ======================================================================================== protocols
def parent_protocol(svc) -> dict:
    """The ACTIVE research protocol (never a companion) whose data and windows the set uses."""
    act = [p for p in svc.store.list_protocols(status="ACTIVE") if not rp.is_companion(p)]
    if not act:
        raise NoiseBandError("NO_PROTOCOL", "there is no active research protocol; the noise band set uses its data, "
                                            "discovery and holdout windows")
    if len(act) > 1:
        raise NoiseBandError("PROTOCOL_AMBIGUOUS", "several research protocols are active",
                             active=[p["protocol_id"] for p in act])
    rp.verify_record(act[0])
    return act[0]


def set_protocol(svc, parent: Mapping) -> dict | None:
    """The noise band set of ``parent`` (at most one is created; any status)."""
    sets = [p for p in svc._set_companions(parent) if rp.is_noise_set(p)]
    return sets[-1] if sets else None


def _path(svc, h: str) -> Path:
    return Path(svc.data_root) / FOLDER / "sets" / f"{h}.json"


def load_set(svc, p: Mapping) -> list[dict]:
    """The registered rows of a set protocol, checked against the content hash in its material."""
    ss = p["material"]["strategy_set"]
    h = ss["hash"]
    hit = _SETS.get(h)
    if hit is not None:
        return hit
    path = _path(svc, h)
    try:
        rows = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise NoiseBandError("SET_FILE_MISSING", "the noise band set's strategy list is missing or unreadable",
                             path=str(path), error=str(exc)) from None
    if hash_obj(rows) != h or len(rows) != int(ss["count"]):
        raise NoiseBandError("SET_FILE_CHANGED", "the noise band set's strategy list does not match its protocol",
                             path=str(path))
    _SETS[h] = rows
    return rows


def strategy_ids(svc) -> set[str]:
    """The registered strategy ids of the active protocol's noise band set (empty when there is none). Display only."""
    try:
        sp = set_protocol(svc, parent_protocol(svc))
    except NoiseBandError:
        return set()
    return set() if sp is None else {r["strategy_id"] for r in load_set(svc, sp)}


# ======================================================================================== read-only status
def status(svc) -> dict:
    from edgelab.research import campaign as C
    out: dict = {"design": N.design(), "design_hash": N.design_hash(), "name": NAME}
    try:
        parent = parent_protocol(svc)
    except NoiseBandError as exc:
        return {**out, "ready": False, "problem": exc.to_dict()}
    pm = parent["material"]
    by_tf, problems = C.resolve_datasets(svc, parent, set(N.TIMEFRAMES))
    out.update(ready=True, parent={"protocol_id": parent["protocol_id"], "name": pm.get("name")},
               datasets_by_timeframe=by_tf, dataset_problems=problems + C.dataset_problems(svc, parent, by_tf),
               discovery_period=C.discovery_period(pm), holdout=pm["windows"]["holdout"])
    sp = set_protocol(svc, parent)
    if sp is None:
        out["set"] = None
        return out
    mat = sp["material"]
    looks = [a for a in svc.store.list_holdout_access(sp["protocol_id"]) if a["status"] != "refused"]
    out["set"] = {"protocol_id": sp["protocol_id"], "status": sp["status"], "created_at": sp["created_at"],
                  "count": mat["strategy_set"]["count"], "by_timeframe": mat["strategy_set"].get("by_timeframe"),
                  "trials_used": svc.store.count_trials(sp["protocol_id"]),
                  "trial_budget": mat["trial_budget"]["max_unique_trials"],
                  "holdout_looks_used": len(looks),
                  "holdout_looks": mat["holdout_budget"]["max_unique_candidate_evaluations"],
                  "datasets_by_timeframe": mat["search"]["datasets_by_timeframe"], "generation": mat["generation"]}
    out["results"] = results_counts(svc)
    return out


def results_counts(svc) -> dict:
    """Backtested / net positive / survivors among the set's latest discovery results (read-only, display)."""
    from edgelab.research import results_view as rv
    rows, _ = rv._latest_scoped(svc, "in_sample", "set:noise")
    tested = [x["ref"] for x in rows if x["ref"]]
    return {"backtested": len(tested), "with_trades": sum(1 for r in tested if r["trade_count"] > 0),
            "net_positive": sum(1 for r in tested if (r.get("net_r") or 0) > 0),
            "survivors": sum(1 for r in tested if r.get("survivor"))}


# ======================================================================================== create (writes)
def create(svc, holdout_looks: int | None = None, progress: Callable[[str], None] | None = None) -> dict:
    """Create the noise band set protocol of the active research protocol (once) and save its strategies to the
    library. No evaluation."""
    from edgelab.core.fsutil import atomic_write_text
    from edgelab.core.identity import code_version
    from edgelab.research import campaign as C
    say = progress or (lambda _m: None)
    parent = parent_protocol(svc)
    pm = parent["material"]
    if set_protocol(svc, parent) is not None:
        raise NoiseBandError("SET_EXISTS", "this research protocol already has its noise band set (one per protocol)",
                             protocol_id=parent["protocol_id"])
    if svc._config_hash() != pm["config_hash"]:
        raise NoiseBandError("PROTOCOL_CONFIG_CHANGED", "the research config differs from the protocol's",
                             protocol_id=parent["protocol_id"])
    by_tf, problems = C.resolve_datasets(svc, parent, set(N.TIMEFRAMES))
    bad = {p["timeframe"] for p in C.dataset_problems(svc, parent, by_tf) if p.get("timeframe")}
    tfs = tuple(tf for tf in N.TIMEFRAMES if tf in by_tf and tf not in bad)
    if not tfs:
        raise NoiseBandError("NO_DATASETS", "none of the noise band timeframes (1m, 5m, 15m, 30m) has a runnable dataset "
                                            "of the protocol's NQ import", problems=problems)
    by_tf = {tf: by_tf[tf] for tf in tfs}
    say(f"Designing {N.TARGET_TOTAL:,} noise band versions ({', '.join(tfs)})")
    gen = N.generate(svc.sessions, svc._config_hash(), total=N.TARGET_TOTAL, timeframes=tfs)
    rows = [{"seq": r["seq"], "strategy_id": r["identity"]["strategy_id"], "logic_hash": r["identity"]["logic_hash"],
             "definition_hash": r["identity"]["definition_hash"], "timeframe": r["variation"]["timeframe"],
             "dataset_id": by_tf[r["variation"]["timeframe"]]["dataset_id"], "variation": r["variation"]}
            for r in gen["rows"]]
    h = hash_obj(rows)
    path = _path(svc, h)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(rows, sort_keys=True, separators=(",", ":")))
    counts: dict[str, int] = {}
    for r in rows:
        counts[r["timeframe"]] = counts.get(r["timeframe"], 0) + 1
    looks_used = sum(1 for a in svc.store.list_holdout_access(parent["protocol_id"]) if a["status"] != "refused")
    material = rp.build_noise_set_material(
        parent, strategy_set={"file": f"{FOLDER}/sets/{h}.json", "hash": h, "count": len(rows), "by_timeframe": counts},
        design={**N.design(), "design_hash": N.design_hash()},
        generation={k: gen[k] for k in ("seed", "attempts", "invalid", "duplicates", "timeframes")},
        datasets_by_timeframe=by_tf, discovery_period=C.discovery_period(pm), parent_looks_used=looks_used,
        holdout_looks=holdout_looks or rp.DEFAULT_HOLDOUT_LOOKS, name=NAME)
    rec = rp.make_record(material, {"code_version": code_version()})
    key = svc._scope_key(pm["scope"]["instrument"], pm["scope"]["provider"]) + rp.NOISE_SET_SCOPE_SUFFIX
    svc.store.save_protocol(rec, key)
    say(f"Saving {len(rows):,} strategies to the library")
    saved = _save(svc, rec, [(r, d["definition"], d["identity"]) for r, d in zip(rows, gen["rows"])])
    return {"protocol_id": rec["protocol_id"], "parent_protocol_id": parent["protocol_id"], "count": len(rows),
            "by_timeframe": counts, "library": saved, "skipped_timeframes": sorted(set(N.TIMEFRAMES) - set(tfs)),
            "holdout_looks": material["holdout_budget"]["max_unique_candidate_evaluations"]}


def _save(svc, p: Mapping, items: list[tuple]) -> dict:
    from edgelab.strategy.compiler import COMPILER_VERSION
    from edgelab.strategy.dsl import DSL_VERSION
    from edgelab.strategy.lineage import LineageRecord
    created = present = 0
    for row, d, ident in items:
        rec = LineageRecord(row["strategy_id"], row["logic_hash"], row["definition_hash"], N.FAMILY["id"],
                            "factory_variant", generation_parameters={"variation": row["variation"], "seq": row["seq"],
                                                                      "noise_set_protocol_id": p["protocol_id"],
                                                                      "design_hash": N.design_hash()},
                            generation_batch_id=p["protocol_id"],
                            versions={"noise_band_space_version": N.SPACE_VERSION, "dsl_version": DSL_VERSION,
                                      "compiler_version": COMPILER_VERSION})
        if svc.library.save(d, ident if ident is not None else _identity(svc, d, row), rec):
            created += 1
        else:
            present += 1
    return {"created": created, "already_present": present}


def _identity(svc, d: Mapping, ident: Mapping) -> dict:
    from edgelab.strategy.compiler import compile_definition
    got = compile_definition(d, svc.sessions, svc._config_hash()).identity.to_dict()
    if (got["strategy_id"], got["logic_hash"]) != (ident["strategy_id"], ident["logic_hash"]):
        raise NoiseBandError("SET_IDENTITY_CHANGED", "a registered noise band strategy no longer compiles to its "
                                                     "registered identity", strategy_id=ident["strategy_id"])
    return got


def materialize(svc, p: Mapping) -> dict:
    """Make sure every registered strategy is in the library (rebuilt from its variation; refuses if it no longer
    compiles to the registered identity). Idempotent."""
    todo = [(r, N.build_definition(r["variation"]), None) for r in load_set(svc, p)
            if not svc.library.exists(r["strategy_id"])]
    return _save(svc, p, todo) if todo else {"created": 0, "already_present": int(p["material"]["strategy_set"]["count"])}


# ======================================================================================== run
def search_spec(svc, p: Mapping) -> dict:
    """The set's search: exactly the registered strategies, their datasets, the discovery period, one cell each."""
    mat = p["material"]
    rows = load_set(svc, p)
    return {"search_spec_version": 1, "strategies": {"ids": sorted(r["strategy_id"] for r in rows)},
            "datasets": sorted({r["dataset_id"] for r in rows}), "period": dict(mat["search"]["period"]),
            "max_cells": len(rows) * len({r["dataset_id"] for r in rows}), "workers": 1, "seed": None}


def run(svc, protocol_id: str, *, processes: int = 1, lock=None, cancel: Callable[[], bool] | None = None,
        on_cell: Callable[..., None] | None = None) -> dict:
    """Run (or resume) the set's search through the normal protocol-gated search path."""
    from edgelab.research.batch import run_search
    guard = lock if lock is not None else nullcontext()
    with guard:
        p = svc.store.get_protocol(protocol_id)
        rp.verify_record(p)
        if not rp.is_noise_set(p):
            raise NoiseBandError("NOT_A_NOISE_SET", f"{protocol_id} is not a noise band set protocol")
        if p["status"] != "ACTIVE":
            raise NoiseBandError("PROTOCOL_NOT_ACTIVE", "the noise band set protocol is retired", protocol_id=protocol_id)
        materialize(svc, p)
        spec = search_spec(svc, p)
    return run_search(svc, spec, processes, lock=lock, cancel=cancel, on_cell=on_cell)
