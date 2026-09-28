"""Research run registry.

A run record captures everything required to reproduce a result: strategy spec
and ID, dataset manifest (incl. content hash), full config snapshot + hash, code
version (git commit + source hash), environment, seed, execution assumptions,
costs, and the resulting trades hash. Re-running with the same inputs must give
the same trades hash - tests/test_reproducibility.py enforces this.

Every new run starts at status IN_SAMPLE. Nothing is ever labelled "validated"
by a backtest alone; later phases promote status only through OOS/walk-forward
gates.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Mapping

from edgelab.core.config import config_hash
from edgelab.core.identity import code_version, environment_fingerprint
from edgelab.data.store import ResultStore
from edgelab.engine.backtester import BacktestResult

STATUSES = ("UNTESTED", "IN_SAMPLE", "VALIDATION", "OUT_OF_SAMPLE", "WALK_FORWARD",
            "PAPER", "LIVE", "REJECTED")


def build_run_record(cfg: Mapping, result: BacktestResult, metrics: Mapping,
                     seed: int | None = None, status: str = "IN_SAMPLE",
                     notes: str = "", ai_hypotheses: list | None = None,
                     parent_strategy_id: str | None = None, mutation: str | None = None) -> dict[str, Any]:
    if status not in STATUSES:
        raise ValueError(f"status must be one of {STATUSES}")
    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "status": status,
        "strategy": {"strategy_id": result.strategy_id, **result.strategy_spec,
                     "parent_strategy_id": parent_strategy_id, "mutation": mutation},
        "dataset": result.dataset,
        "intrabar": result.intrabar,
        "config": dict(cfg),
        "config_hash": config_hash(cfg),
        "code_version": code_version(),
        "environment": environment_fingerprint(),
        "seed": seed,
        "assumptions": result.assumptions,
        "causality_check": None if result.causality is None else {
            "passed": result.causality.passed, "cuts_tested": len(result.causality.cuts_tested)},
        "n_signals": result.n_signals,
        "skipped_signals": result.skipped,
        "trades_hash": result.trades_hash,
        "headline_metrics": {k: v for k, v in metrics.items() if not isinstance(v, (dict, tuple))},
        "ai_hypotheses": ai_hypotheses or [],
        "notes": notes,
        "disclaimer": "Historical result under the stated assumptions; not a forecast.",
    }


def record_run(store: ResultStore, cfg: Mapping, result: BacktestResult, metrics: Mapping,
               **kwargs) -> str:
    run_id = store.next_run_id()
    record = build_run_record(cfg, result, metrics, **kwargs)
    record["run_id"] = run_id
    store.save_run(run_id, record, result.trades, dict(metrics))
    return run_id
