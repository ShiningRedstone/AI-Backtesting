"""Phase 4 search runner (sequential, or worker processes for workers > 1).

`run_search` validates and plans a search (`research.search.plan_search`), persists the batch
and EVERY planned cell before executing anything, then runs the eligible cells in plan order
through `Services._run_cell` - the same pipeline as `backtest_strategy`:
load -> [period restriction] -> compile -> costs -> bind -> run_backtest(sizing=strat.sizing)
-> metrics -> record_run. Nothing here computes a backtest itself.

Durable cell states: `ineligible` (readiness reasons, never executed, no run), `pending`,
`completed` (a normal run in the `runs` table, zero-trade results included) and `failed`
(the error; no run) and `cancelled` (a job was cancelled before the cell started; runs again on
resume). Runs keep status IN_SAMPLE; nothing is labelled validated.

Resume: the same search (same search_id and config) skips cells already `completed` whose run
still exists; they are counted as `skipped_resume`, never as new trials. Failed and pending
cells run again. A different config is a different search (config_hash is in search_hash).

Accounting per invocation: evaluated = eligible cells executed now (completed + failed);
trials = evaluated; skipped_resume, failed, cancelled. Totals over all invocations are derived
from the stored cells of the CURRENT plan (`cumulative`); cells from an earlier plan (e.g. a
family that has since changed) are kept as `historical_cells`, never counted.

Parallel execution (workers > 1; the worker count never changes the search identity):
  * worker processes (spawned, never forked - the parent may hold threads and locks) run ONLY
    the pure part of `Services._run_cell` (record=False) on a read-only context: datasets
    loaded and re-validated once by the parent, strategy definitions, config, sessions and the
    shared on-disk feature cache. A worker has no ResultStore at all, so it cannot write runs,
    allocate run ids or touch search rows.
  * the parent submits cells in plan order (at most `workers` in flight) and consumes results
    strictly in plan order, so completion order never affects cell rows, run-record order or
    run ids (`next_run_id` stays parent-only). Recording uses `Services._record_cell`, the
    record step of `_run_cell`.
  * feature cache: workers share the disk layer (each has its own in-memory LRU). Entries are
    immutable and deterministic, every file is written to a unique temp file and moved into
    place with os.replace, metadata before arrays, and reads verify checksums, so concurrent
    writers of one key write identical content and readers never trust a partial entry.
  * a cell that raises is a `failed` cell exactly as in sequential mode; a crashed worker
    process fails only the cells it could not return (they re-run on resume).
"""
from __future__ import annotations

import json
import os
from collections import deque
from contextlib import nullcontext
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

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


# ============================================================ worker processes
class _CellContext:
    """Read-only stand-in for `Services` inside a worker process: exactly the attributes the pure
    part of `Services._run_cell` reads. There is deliberately no `store` (parent-only writes)."""

    def __init__(self, cfg: Mapping, config_hash: str, datasets: Mapping, cache_args: Mapping):
        from edgelab.features.cache import FeatureCache
        from edgelab.features.sessions import load_sessions
        self.cfg, self._cfg_hash, self._datasets = cfg, config_hash, dict(datasets)
        self.sessions = load_sessions(cfg)
        self.cache = FeatureCache(**cache_args)

    def load_dataset(self, dataset_id: str):
        return self._datasets[dataset_id]                   # loaded and re-validated by the parent

    def _definition(self, src: Mapping) -> dict:
        return dict(src)

    def _config_hash(self) -> str:
        return self._cfg_hash

    @staticmethod
    def _is_synthetic(m) -> bool:
        from edgelab.services import Services
        return Services._is_synthetic(m)


_WORKER: dict = {}


def _worker_init(cfg: Mapping, config_hash: str, datasets: Mapping, cache_args: Mapping) -> None:
    _WORKER["ctx"] = _CellContext(cfg, config_hash, datasets, cache_args)


def _worker_cell(definition: Mapping, dataset_id: str, period) -> dict:
    """Pure cell computation in a worker: never records, never touches the store."""
    from edgelab.services import Services
    try:
        out = Services._run_cell(_WORKER["ctx"], definition, dataset_id, record=False, period=period)
    except Exception as exc:                                # same failed-cell text as sequential mode
        return {"error": f"{type(exc).__name__}: {exc}", "pid": os.getpid()}
    return {"result": out["result"], "metrics": out["metrics"], "synthetic": out["synthetic"], "pid": os.getpid()}


def resolve_workers(canon: Mapping, workers: int | None) -> int:
    n = canon["workers"] if workers is None else workers
    if not isinstance(n, int) or isinstance(n, bool) or n < 1:
        raise SearchSpecError(f"workers must be an integer >= 1 (got {n!r})")
    return n


# ============================================================ runner
def run_search(services, spec: Mapping, workers: int | None = None, *, lock=None,
               cancel: Callable[[], bool] | None = None) -> dict:
    """Plan and run a search. `workers` (default: the spec's `workers`) > 1 computes cells in
    worker processes; results are always written by this process in plan order. Optional hooks
    for the background job manager (both default to the plain synchronous behaviour):
      lock   - the service lock; held ONLY around store/library operations, never while a cell's
               backtest runs (`_run_cell` takes it around its own dataset load and run record).
      cancel - checked before each cell starts; once it returns True no new cell starts, the
               remaining pending cells become `cancelled` and the batch ends `cancelled`."""
    guard = lock if lock is not None else nullcontext()
    canon = canonical_search_spec(spec)                    # refuses an invalid spec before anything else
    n_workers = resolve_workers(canon, workers)
    store = services.store
    store._require_search_storage()
    with guard:
        plan: SearchPlan = plan_search(spec, services)      # references, eligibility, max_cells refusal
        sid = plan.search_id
        existing = store.get_search_batch(sid)
        if existing and existing["config_hash"] != plan.config_hash:
            raise SearchSpecError(
                f"{sid} was stored under config {existing['config_hash'][:12]} but the current config is "
                f"{plan.config_hash[:12]}; its cells cannot be resumed under a different config")
        prior = {c["cell_id"]: c for c in store.list_search_cells(sid)} if existing else {}
        if existing and existing.get("protocol_id") != plan.protocol_id:     # ADR-56: never re-attribute
            from edgelab.research.protocol import ProtocolRefusal
            raise ProtocolRefusal("PROTOCOL_MISMATCH", f"{sid} was stored under protocol {existing.get('protocol_id')} "
                                  f"but the governing protocol is now {plan.protocol_id}; its cells cannot be resumed",
                                  search_id=sid, stored_protocol_id=existing.get("protocol_id"),
                                  protocol_id=plan.protocol_id)
        budget = getattr(services, "_protocol_budget_check", None)
        if budget is not None and plan.protocol_id:
            budget(plan.protocol_id, [c for c in plan.eligible_cells()
                                      if not (prior.get(c["cell_id"]) and prior[c["cell_id"]]["status"] == "completed")])

    from edgelab.core.identity import code_version
    counts = {"n_planned": plan.counts["planned"], "n_eligible": plan.counts["eligible"],
              "n_ineligible": plan.counts["ineligible"], "n_evaluated": 0, "n_skipped_resume": 0,
              "n_failed": 0, "n_cancelled": 0, "n_trials": 0}

    def done(c: dict | None) -> bool:
        return bool(c and c["status"] == "completed" and c["run_id"] and store.has_run(c["run_id"]))

    def cell_row(c: dict, **kw) -> dict:
        return {"search_id": sid, "cell_id": c["cell_id"], "plan_index": c["plan_index"],
                "strategy_id": c["strategy_id"], "dataset_id": c["dataset_id"],
                "dataset_content_hash": c["dataset_content_hash"], "run_id": None, "trades_hash": None,
                "reasons_json": _dumps(c["reasons"]), "error": None, "headline_json": None, "current": 1, **kw}

    with guard:
        store.upsert_search_batch({
            "search_id": sid, "search_hash": plan.search_hash, "config_hash": plan.config_hash,
            "code_version": _dumps(code_version()), "created_at": existing["created_at"] if existing else _now(),
            "finished_at": None, "status": "running", "spec_json": _dumps(plan.spec),
            "shortlist_json": existing["shortlist_json"] if existing else None,
            "warnings_json": _dumps(plan.warnings), "protocol_id": plan.protocol_id, **counts})
        for c in plan.cells:                               # every planned cell is durable before execution
            if not done(prior.get(c["cell_id"])):
                store.upsert_search_cell(cell_row(c, status="pending" if c["eligible"] else "ineligible"))
        store.mark_current_search_cells(sid, [c["cell_id"] for c in plan.cells])   # older rows stay, as history

    sources = {s["strategy_id"]: s["sources"] for s in plan.strategies}
    period = None if plan.period is None else (plan.period["start"], plan.period["end"])
    eligible = plan.eligible_cells()
    pids: set[int] = set()

    def record(c: dict, lineage: tuple, outcome: dict) -> None:
        """Persist one executed cell (parent only, plan order)."""
        if "error" in outcome:
            row = cell_row(c, status="failed", error=outcome["error"])
            counts["n_failed"] += 1
        else:
            run_id = outcome.get("run_id") or services._record_cell(
                outcome["result"], outcome["metrics"], outcome["synthetic"],
                notes=f"search {sid} cell {c['cell_id']}", parent_strategy_id=lineage[0], mutation=lineage[1],
                lock=lock)
            met = outcome["metrics"]
            headline = {k: v for k, v in met.items() if not isinstance(v, (dict, tuple, list))}
            row = cell_row(c, status="completed", run_id=run_id, trades_hash=outcome["result"].trades_hash,
                           headline_json=_dumps(headline))
        counts["n_evaluated"] += 1
        counts["n_trials"] += 1
        with guard:
            if plan.protocol_id:                            # ADR-56: every executed cell enters the program ledger
                services._protocol_record_search_cell(plan.protocol_id, sid, c, outcome, row.get("run_id"))
            store.upsert_search_cell(row)
            store.update_search_batch(sid, n_evaluated=counts["n_evaluated"], n_failed=counts["n_failed"],
                                      n_trials=counts["n_trials"])

    def cancel_rest(cells: list[dict]) -> None:
        with guard:
            for rest in cells:
                if not done(prior.get(rest["cell_id"])):
                    store.upsert_search_cell(cell_row(rest, status="cancelled"))
                    counts["n_cancelled"] += 1
            store.update_search_batch(sid, n_cancelled=counts["n_cancelled"])

    status = "completed"
    try:
        if n_workers == 1:
            for k, c in enumerate(eligible):
                if cancel is not None and cancel():         # cooperative: between cells only
                    cancel_rest(eligible[k:])
                    status = "cancelled"
                    break
                with guard:
                    skip = done(prior.get(c["cell_id"]))
                    if skip:
                        counts["n_skipped_resume"] += 1
                        store.update_search_batch(sid, n_skipped_resume=counts["n_skipped_resume"])
                    else:
                        lin = _lineage(services.library, c["strategy_id"], sources[c["strategy_id"]])
                if skip:
                    continue
                try:                                        # the backtest runs WITHOUT the service lock
                    out = services._run_cell(c["strategy_id"], c["dataset_id"], record=True,
                                             parent_strategy_id=lin[0], mutation=lin[1],
                                             notes=f"search {sid} cell {c['cell_id']}", period=period,
                                             entry_point="search_cell",
                                             **({} if lock is None else {"lock": lock}))
                except Exception as exc:                    # recorded per cell, never silently dropped
                    out = {"error": f"{type(exc).__name__}: {exc}"}
                record(c, lin, out)
        else:
            status = _run_parallel(services, plan, eligible, n_workers, guard, lock, cancel, prior, done,
                                   sources, period, counts, record, cancel_rest, pids)
    except Exception:
        with guard:
            store.update_search_batch(sid, status="failed", finished_at=_now())
        raise
    with guard:
        store.update_search_batch(sid, status=status, finished_at=_now())
        out = search_summary(store, sid)
    out["execution"] = {"workers": n_workers, "mode": "sequential" if n_workers == 1 else "processes",
                        "worker_processes": len(pids)}
    return out


def _run_parallel(services, plan, eligible, n_workers, guard, lock, cancel, prior, done, sources, period,
                  counts, record, cancel_rest, pids) -> str:
    """Workers compute; the parent consumes results in plan order and is the only writer."""
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    from concurrent.futures.process import BrokenProcessPool
    store = services.store
    with guard:                                             # read everything workers need, once
        todo = []
        for c in eligible:
            if done(prior.get(c["cell_id"])):
                todo.append((c, None, None))
            else:
                todo.append((c, services.library.load(c["strategy_id"])["definition"],
                             _lineage(services.library, c["strategy_id"], sources[c["strategy_id"]])))
        need = sorted({c["dataset_id"] for c, d, _ in todo if d is not None})
        datasets = {did: services.load_dataset(did) for did in need}
    cache = services.cache
    cache_args = {"root": cache.root, "verify": cache.verify, "memory_entries": cache.memory_entries}
    pool = ProcessPoolExecutor(max_workers=n_workers, mp_context=multiprocessing.get_context("spawn"),
                               initializer=_worker_init,
                               initargs=(dict(services.cfg), plan.config_hash, datasets, cache_args))
    inflight: deque = deque()                               # futures in plan order
    nxt = 0
    stop = False
    try:
        for k, (c, definition, lin) in enumerate(todo):
            while not stop and nxt < len(todo) and len(inflight) < n_workers:   # keep `workers` busy
                if cancel is not None and cancel():         # no new cell starts after cancellation
                    stop = True
                    break
                cc, dd, _ = todo[nxt]
                if dd is None:
                    inflight.append(None)
                else:
                    try:
                        inflight.append(pool.submit(_worker_cell, dd, cc["dataset_id"], period))
                    except BrokenProcessPool as exc:        # a worker died earlier: fail, do not abort
                        inflight.append(exc)
                nxt += 1
            if k >= nxt:                                    # never submitted: cancelled
                cancel_rest([t[0] for t in todo[k:]])
                return "cancelled"
            fut = inflight.popleft()
            if fut is None:                                 # completed earlier: resume skip
                counts["n_skipped_resume"] += 1
                with guard:
                    store.update_search_batch(plan.search_id, n_skipped_resume=counts["n_skipped_resume"])
                continue
            try:
                if isinstance(fut, BaseException):
                    raise fut
                outcome = fut.result()
            except BrokenProcessPool as exc:                # the worker died: this cell failed
                outcome = {"error": f"BrokenProcessPool: {exc}"}
            pid = outcome.pop("pid", None)
            if pid is not None:
                pids.add(pid)
            record(c, lin, outcome)
    finally:
        pool.shutdown(wait=True, cancel_futures=True)
    return "completed"


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
