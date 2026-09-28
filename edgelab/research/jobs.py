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

    def cancel(self, job_id: str) -> dict:
        job = self._get(job_id)
        if job.state in ACTIVE:
            job.cancel_requested.set()
        return self.status(job_id)

    def status(self, job_id: str) -> dict:
        job = self._get(job_id)
        return {**job.snapshot(), "progress": self.progress(job.search_id)}

    def progress(self, search_id: str) -> dict:
        """Counts for polling, from the stored batch row and its CURRENT cells."""
        store = self.services.store
        with self.lock:
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
