"""Phase 4 synchronous search runner (workers=1).

`run_search` validates and plans a search (`research.search.plan_search`), persists the batch
and EVERY planned cell before executing anything, then runs the eligible cells one by one in
plan order through `Services._run_cell` - the same pipeline as `backtest_strategy`:
load -> [period restriction] -> compile -> costs -> bind -> run_backtest(sizing=strat.sizing)
-> metrics -> record_run. Nothing here computes a backtest itself.

Durable cell states: `ineligible` (readiness reasons, never executed, no run), `pending`,
`completed` (a normal run in the `runs` table, zero-trade results included) and `failed`
(the error; no run). Runs keep status IN_SAMPLE; nothing is labelled validated.

Resume: the same search (same search_id and config) skips cells already `completed` whose run
still exists; they are counted as `skipped_resume`, never as new trials. Failed and pending
cells run again. A different config is a different search (config_hash is in search_hash).

Accounting per invocation: evaluated = eligible cells executed now (completed + failed);
trials = evaluated (workers=1); skipped_resume, failed, cancelled (0 here). Totals over all
invocations are derived from the stored cells of the CURRENT plan (`cumulative`); cells from an
earlier plan (e.g. a family that has since changed) are kept as `historical_cells`, never counted.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Mapping

from edgelab.research.search import SearchPlan, SearchSpecError, canonical_search_spec, plan_search


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _dumps(x: Any) -> str:
    from edgelab.services import _jsonable
    return json.dumps(_jsonable(x), sort_keys=True, allow_nan=False)


def _lineage(library, strategy_id: str, sources: list[str]) -> tuple[str | None, str | None]:
    """(parent_strategy_id, mutation) from the stored lineage record matching the search source
    (a VB_/PB_ batch), else the first record. mutation lists the recorded parameter changes."""
    recs = library.load(strategy_id)["lineage"]
    rec = next((r for r in recs if r.get("generation_batch_id") in sources), recs[0])
    changes = rec.get("changes") or []
    mutation = ", ".join(f"{c.get('parameter')}: {c.get('old')} -> {c.get('new')}" for c in changes) or None
    return rec.get("parent_strategy_id"), mutation


def cumulative_counts(cells: list[dict]) -> dict:
    """Totals over every invocation for the given (current-plan) cells. A cell is one trial at most."""
    n = {s: 0 for s in ("completed", "failed", "pending", "ineligible", "cancelled")}
    for c in cells:
        n[c["status"]] = n.get(c["status"], 0) + 1
    return {**n, "trials": n["completed"] + n["failed"]}


def run_search(services, spec: Mapping, workers: int = 1) -> dict:
    canon = canonical_search_spec(spec)                    # refuses an invalid spec before anything else
    if workers != 1 or canon["workers"] != 1:
        raise SearchSpecError("parallel search workers are not implemented yet; use workers: 1")
    store = services.store
    store._require_search_storage()
    plan: SearchPlan = plan_search(spec, services)          # references, eligibility, max_cells refusal
    sid = plan.search_id
    existing = store.get_search_batch(sid)
    if existing and existing["config_hash"] != plan.config_hash:
        raise SearchSpecError(
            f"{sid} was stored under config {existing['config_hash'][:12]} but the current config is "
            f"{plan.config_hash[:12]}; its cells cannot be resumed under a different config")
    prior = {c["cell_id"]: c for c in store.list_search_cells(sid)} if existing else {}

    from edgelab.core.identity import code_version
    counts = {"n_planned": plan.counts["planned"], "n_eligible": plan.counts["eligible"],
              "n_ineligible": plan.counts["ineligible"], "n_evaluated": 0, "n_skipped_resume": 0,
              "n_failed": 0, "n_cancelled": 0, "n_trials": 0}
    store.upsert_search_batch({
        "search_id": sid, "search_hash": plan.search_hash, "config_hash": plan.config_hash,
        "code_version": _dumps(code_version()), "created_at": existing["created_at"] if existing else _now(),
        "finished_at": None, "status": "running", "spec_json": _dumps(plan.spec),
        "shortlist_json": existing["shortlist_json"] if existing else None,
        "warnings_json": _dumps(plan.warnings), **counts})

    def done(c: dict | None) -> bool:
        return bool(c and c["status"] == "completed" and c["run_id"] and store.has_run(c["run_id"]))

    def cell_row(c: dict, **kw) -> dict:
        return {"search_id": sid, "cell_id": c["cell_id"], "plan_index": c["plan_index"],
                "strategy_id": c["strategy_id"], "dataset_id": c["dataset_id"],
                "dataset_content_hash": c["dataset_content_hash"], "run_id": None, "trades_hash": None,
                "reasons_json": _dumps(c["reasons"]), "error": None, "headline_json": None, "current": 1, **kw}

    for c in plan.cells:                                   # every planned cell is durable before execution
        if not done(prior.get(c["cell_id"])):
            store.upsert_search_cell(cell_row(c, status="pending" if c["eligible"] else "ineligible"))
    store.mark_current_search_cells(sid, [c["cell_id"] for c in plan.cells])   # older rows stay, as history

    sources = {s["strategy_id"]: s["sources"] for s in plan.strategies}
    period = None if plan.period is None else (plan.period["start"], plan.period["end"])
    try:
        for c in plan.eligible_cells():
            if done(prior.get(c["cell_id"])):
                counts["n_skipped_resume"] += 1
                store.update_search_batch(sid, n_skipped_resume=counts["n_skipped_resume"])
                continue
            parent, mutation = _lineage(services.library, c["strategy_id"], sources[c["strategy_id"]])
            try:
                out = services._run_cell(c["strategy_id"], c["dataset_id"], record=True,
                                         parent_strategy_id=parent, mutation=mutation,
                                         notes=f"search {sid} cell {c['cell_id']}", period=period)
            except Exception as exc:                        # recorded per cell, never silently dropped
                store.upsert_search_cell(cell_row(c, status="failed", error=f"{type(exc).__name__}: {exc}"))
                counts["n_failed"] += 1
            else:
                met = out["metrics"]
                headline = {k: v for k, v in met.items() if not isinstance(v, (dict, tuple, list))}
                store.upsert_search_cell(cell_row(c, status="completed", run_id=out["run_id"],
                                                  trades_hash=out["result"].trades_hash,
                                                  headline_json=_dumps(headline)))
            counts["n_evaluated"] += 1
            counts["n_trials"] += 1
            store.update_search_batch(sid, n_evaluated=counts["n_evaluated"], n_failed=counts["n_failed"],
                                      n_trials=counts["n_trials"])
    except Exception:
        store.update_search_batch(sid, status="failed", finished_at=_now())
        raise
    store.update_search_batch(sid, status="completed", finished_at=_now())
    return search_summary(store, sid)


def search_summary(store, search_id: str) -> dict:
    """The stored batch (JSON fields decoded), its cells in plan order and cumulative totals."""
    from edgelab.services import _jsonable
    b = store.get_search_batch(search_id)
    if b is None:
        raise KeyError(search_id)
    cells, history = store.list_search_cells(search_id, current=True), store.list_search_cells(search_id, current=False)
    for c in cells + history:
        for k in ("reasons_json", "headline_json"):
            raw = c.pop(k)
            c[k[:-5]] = None if raw is None else json.loads(raw)
        c["current"] = bool(c["current"])
    return _jsonable({**_decode_batch(b), "cells": cells, "historical_cells": history,
                      "cumulative": cumulative_counts(cells),
                      "note": "in-sample research results under the stated assumptions; nothing is validated"})


def _decode_batch(b: dict) -> dict:
    out = dict(b)
    for k in ("spec_json", "shortlist_json", "warnings_json"):
        raw = out.pop(k)
        out[k[:-5]] = None if raw is None else json.loads(raw)
    out["code_version"] = None if out["code_version"] is None else json.loads(out["code_version"])
    return out
