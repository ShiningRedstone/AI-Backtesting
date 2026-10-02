"""Phase 4 background search jobs.

One JobManager per Services instance, one background worker thread, at most ONE active job
(a second start raises JobConflict, which the web layer maps to HTTP 409). The worker runs the
ordinary synchronous `batch.run_search` with two hooks:
  * the injected service lock - taken only around short store/library operations, never while
    a cell's backtest runs, so status polls and cancel requests stay responsive;
  * a cancel flag - checked between cells (a running cell always finishes; no new cell starts).

Lifecycle (in memory, per job):  queued -> running -> completed | failed | cancelled.
Durable state is the existing `search_batches` / `search_cells` rows; there is no job table.
When a JobManager is created, any persisted batch still marked `running` belongs to a process
that is gone: it is marked `interrupted` (never resumed automatically - `run_search` on the same
spec resumes it explicitly, skipping its completed cells).
"""
from __future__ import annotations

import threading
from contextlib import nullcontext
import uuid
from datetime import datetime, timezone
from typing import Mapping

from edgelab.research.batch import run_search
from edgelab.research.search import SearchSpecError, canonical_search_spec, plan_search

ACTIVE = ("queued", "running")
FINAL = ("completed", "failed", "cancelled")


class JobConflict(RuntimeError):
    """Another search job is already active (one job at a time)."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Job:
    def __init__(self, job_id: str, search_id: str, spec: Mapping):
        self.job_id, self.search_id, self.spec = job_id, search_id, dict(spec)
        self.state = "queued"
        self.history = ["queued"]
        self.created_at, self.started_at, self.finished_at = _now(), None, None
        self.error: str | None = None
        self.cancel_requested = threading.Event()

    def _set(self, state: str) -> None:
        self.state = state
        self.history.append(state)

    def snapshot(self) -> dict:
        return {"job_id": self.job_id, "search_id": self.search_id, "state": self.state,
                "history": list(self.history), "created_at": self.created_at, "started_at": self.started_at,
                "finished_at": self.finished_at, "error": self.error,
                "cancel_requested": self.cancel_requested.is_set()}


class CampaignJob(Job):
    """A frozen-campaign run (ADR-69): the campaign's own preflight + its frozen search, scoped to selected families."""

    def __init__(self, job_id: str, campaign_id: str, search_id: str, families: list[str] | None,
                 strategy_ids: list[str] | None = None):
        super().__init__(job_id, search_id, {})
        self.kind, self.campaign_id, self.families, self.strategy_ids = "campaign", campaign_id, families, strategy_ids
        self.processes = 1                    # ADR-77: CPU cores computing cells at once (execution only)
        self.live: dict = {"status": "queued", "phase": "queued - starting the background worker"}

    def snapshot(self) -> dict:
        return {**super().snapshot(), "kind": "campaign", "campaign_id": self.campaign_id,
                "families": self.families, "n_strategy_ids": None if self.strategy_ids is None else len(self.strategy_ids),
                "processes": self.processes, "live": dict(self.live)}


class HoldoutJob(Job):
    """Holdout backtests of chosen survivors (ADR-85): one strategy at a time through Services.evaluate_holdout."""

    def __init__(self, job_id: str, protocol_id: str, items: list[dict]):
        super().__init__(job_id, "", {})
        self.kind, self.protocol_id = "holdout", protocol_id
        self.items = [{**it, "state": "queued"} for it in items]
        self.live: dict = {"current": None, "done": 0}

    def snapshot(self) -> dict:
        return {**super().snapshot(), "kind": "holdout", "protocol_id": self.protocol_id,
                "items": [dict(x) for x in self.items], "n_items": len(self.items), "live": dict(self.live)}


class JobManager:
    def __init__(self, services, lock):
        self.services, self.lock = services, lock
        self._jobs: dict[str, Job] = {}
        self._active: Job | None = None
        self._mu = threading.Lock()          # guards the job table only (not a service lock)
        self._thread: threading.Thread | None = None
        self.interrupted = self._reconcile()

    # ------------------------------------------------------------------ restart reconciliation
    def _reconcile(self) -> list[str]:
        """Batches persisted as `running` by a process that no longer exists -> `interrupted`."""
        store = self.services.store
        with self.lock:
            ids = [b["search_id"] for b in store.list_search_batches() if b["status"] == "running"]
            for sid in ids:
                store.update_search_batch(sid, status="interrupted", finished_at=_now())
        from edgelab.research.campaign import reconcile_run_records
        self.interrupted_campaign_runs = reconcile_run_records(self.services)
        return ids

    # ------------------------------------------------------------------ jobs
    def start(self, spec: Mapping) -> dict:
        canon = canonical_search_spec(spec)                  # invalid specs are refused up front
        if canon["workers"] != 1:
            raise SearchSpecError("parallel search workers are not implemented yet; use workers: 1")
        with self._mu:
            if self._active is not None and self._active.state in ACTIVE:
                raise JobConflict(f"search job {self._active.job_id} ({self._active.search_id}) is still "
                                  f"{self._active.state}; one search job runs at a time")
            with self.lock:
                self.services.store._require_search_storage()
                plan = plan_search(spec, self.services)      # references, eligibility, max_cells refusal
            job = Job("JOB_" + uuid.uuid4().hex[:12].upper(), plan.search_id, spec)
            self._jobs[job.job_id] = job
            self._active = job
            snap = job.snapshot()
            self._thread = threading.Thread(target=self._work, args=(job,), name=f"edgelab-{job.job_id}",
                                            daemon=True)
            self._thread.start()
        return {**snap, "progress": self.progress(job.search_id)}

    def _work(self, job: Job) -> None:
        job.started_at = _now()
        job._set("running")
        try:
            out = run_search(self.services, job.spec, 1, lock=self.lock, cancel=job.cancel_requested.is_set)
            final = out["status"] if out["status"] in FINAL else "completed"
        except BaseException as exc:                         # recorded, never swallowed silently
            job.error = f"{type(exc).__name__}: {exc}"
            final = "failed"
            try:
                with self.lock:
                    b = self.services.store.get_search_batch(job.search_id)
                    if b is not None and b["status"] == "running":
                        self.services.store.update_search_batch(job.search_id, status="failed", finished_at=_now())
            except Exception as exc2:                        # keep the original error visible
                job.error += f" (and the batch could not be marked failed: {type(exc2).__name__}: {exc2})"
        job.finished_at = _now()
        job._set(final)

    # ------------------------------------------------------------------ campaign jobs (ADR-69)
    def start_campaign(self, campaign_id: str, families: list[str] | None, max_failures: int = 0,
                       strategy_ids: list[str] | None = None, processes: int = 1) -> dict:
        """Run a frozen campaign (or a family scope of it) in the background; the same one-job-at-a-time rule."""
        from edgelab.research import campaign as C
        spec = C.load(self.services, campaign_id)               # refuses unknown / tampered campaigns up front
        with self.lock:
            _, header, rows = C.load_manifest(self.services, spec["manifest"]["manifest_id"])
        C.scope_ids(header, rows, families, strategy_ids)       # refuses unknown families / ids up front
        with self._mu:
            if self._active is not None and self._active.state in ACTIVE:
                raise JobConflict(f"job {self._active.job_id} is still {self._active.state}; one research job runs at a time")
            job = CampaignJob("JOB_" + uuid.uuid4().hex[:12].upper(), campaign_id, spec["search"]["search_id"],
                              None if families is None else list(families),
                              None if strategy_ids is None else list(strategy_ids))
            self._jobs[job.job_id] = job
            self._active = job
            job.processes = processes
            self._thread = threading.Thread(target=self._work_campaign, args=(job, max_failures),
                                            name=f"edgelab-{job.job_id}", daemon=True)
            self._thread.start()
        return job.snapshot()

    def _work_campaign(self, job: CampaignJob, max_failures: int) -> None:
        from edgelab.research import campaign as C
        job.started_at = _now()
        job._set("running")
        job.live = {**job.live, "status": "preflight", "phase": "starting the preflight"}

        def on_progress(rec: dict) -> None:
            job.live = {**rec, "job_id": job.job_id}

        try:
            out = C.run_scope(self.services, job.campaign_id, families=job.families, strategy_ids=job.strategy_ids,
                              max_failures=max_failures,
                              lock=self.lock, cancel=job.cancel_requested.is_set, on_progress=on_progress,
                              source="desktop", processes=getattr(job, "processes", 1))
            final = "cancelled" if out["run_status"] == "cancelled" else "completed"
        except BaseException as exc:                         # recorded, never swallowed silently
            job.error = f"{type(exc).__name__}: {exc}"
            final = "failed"
            try:
                with self.lock:
                    b = self.services.store.get_search_batch(job.search_id)
                    if b is not None and b["status"] == "running":
                        self.services.store.update_search_batch(job.search_id, status="failed", finished_at=_now())
            except Exception as exc2:
                job.error += f" (and the batch could not be marked failed: {type(exc2).__name__}: {exc2})"
        job.finished_at = _now()
        job._set(final)

    # ------------------------------------------------------------------ holdout jobs (ADR-85)
    def start_holdout(self, strategy_ids: list[str]) -> dict:
        """Holdout backtests of a checked survivor selection; the same one-research-job-at-a-time rule."""
        from edgelab.research import holdout as H
        reading = getattr(self.services, "in_read_context", lambda: False)()
        with (nullcontext() if reading else self.lock):
            plan = H.plan(self.services, strategy_ids)            # refuses non-survivors, tested ones, over-budget
        with self._mu:
            if self._active is not None and self._active.state in ACTIVE:
                raise JobConflict(f"job {self._active.job_id} is still {self._active.state}; one research job runs at a time")
            job = HoldoutJob("JOB_" + uuid.uuid4().hex[:12].upper(), plan["protocol"]["protocol_id"], plan["items"])
            self._jobs[job.job_id] = job
            self._active = job
            self._thread = threading.Thread(target=self._work_holdout, args=(job,), name=f"edgelab-{job.job_id}",
                                            daemon=True)
            self._thread.start()
        return job.snapshot()

    def _work_holdout(self, job: HoldoutJob) -> None:
        from edgelab.research import holdout as H
        job.started_at = _now()
        job._set("running")
        try:
            H.run_items(self.services, job, self.lock)
            final = "cancelled" if job.cancel_requested.is_set() else "completed"
        except BaseException as exc:                         # recorded, never swallowed silently
            job.error = f"{type(exc).__name__}: {exc}"
            final = "failed"
        job.finished_at = _now()
        job._set(final)

    def holdout_status(self, job_id: str) -> dict:
        job = self._get(job_id)
        if not isinstance(job, HoldoutJob):
            raise KeyError(job_id)
        return job.snapshot()

    def campaign_status(self, job_id: str) -> dict:
        """Lock-free: the in-memory live record of a campaign job (durable copy: the run record file)."""
        job = self._get(job_id)
        if not isinstance(job, CampaignJob):
            raise KeyError(job_id)
        return job.snapshot()

    def active(self) -> dict | None:
        a = self._active
        return None if a is None else a.snapshot()

    def cancel(self, job_id: str) -> dict:
        job = self._get(job_id)
        if job.state in ACTIVE:
            job.cancel_requested.set()
        return job.snapshot() if isinstance(job, (CampaignJob, HoldoutJob)) else self.status(job_id)

    def status(self, job_id: str) -> dict:
        job = self._get(job_id)
        if isinstance(job, (CampaignJob, HoldoutJob)):
            return job.snapshot()
        return {**job.snapshot(), "progress": self.progress(job.search_id)}

    def progress(self, search_id: str) -> dict:
        """Counts for polling, from the stored batch row and its CURRENT cells."""
        store = self.services.store
        reading = getattr(self.services, "in_read_context", lambda: False)()   # ADR-78: own read-only connection
        with (nullcontext() if reading else self.lock):
            b = store.get_search_batch(search_id)
            cells = store.list_search_cells(search_id, current=True) if b else []
        if b is None:
            return {"stored": False}
        by: dict[str, int] = {}
        for c in cells:
            by[c["status"]] = by.get(c["status"], 0) + 1
        eligible = b["n_eligible"] or 0
        handled = (b["n_evaluated"] or 0) + (b["n_skipped_resume"] or 0) + (b["n_cancelled"] or 0)
        return {"stored": True, "batch_status": b["status"], "planned": b["n_planned"], "eligible": eligible,
                "ineligible": b["n_ineligible"], "evaluated": b["n_evaluated"], "failed": b["n_failed"],
                "skipped_resume": b["n_skipped_resume"], "cancelled": b["n_cancelled"], "trials": b["n_trials"],
                "pending": by.get("pending", 0), "cell_status": by,
                "fraction_done": None if not eligible else round(handled / eligible, 6)}

    def list(self) -> list[dict]:
        return [j.snapshot() for j in self._jobs.values()]

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker (tests / shutdown). True when no worker is running."""
        t = self._thread
        if t is not None:
            t.join(timeout)
        return t is None or not t.is_alive()

    def _get(self, job_id: str) -> Job:
        try:
            return self._jobs[job_id]
        except KeyError:
            raise KeyError(job_id) from None
