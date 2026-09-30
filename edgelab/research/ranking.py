"""Phase 4 ranking and shortlist of stored search cells.

Ranking is a pure, deterministic ordering of the CURRENT plan's completed cells (one strategy on
one dataset each - datasets are never merged or averaged) by one metric, higher first:
expectancy_r (default), profit_factor or net_r. Win rate is refused as a ranking metric.

It is an IN-SAMPLE research view. Every output states the metric, the minimum sample label,
the number of trials the ranking was drawn from, `in_sample: true` and `validated: false`.
Nothing here promotes a run's status; out-of-sample / walk-forward validation is Phase 6.

Excluded (and counted by reason, never silently): historical cells, ineligible, pending,
cancelled and failed cells, completed cells without trades (no metric value is invented),
cells whose metric is unavailable and cells below the minimum sample label. A profit factor of
+inf (no losing trade; stored as null) is recognised from the stored loss_rate and net_r and
ranks first with `value_infinite: true` and `value: null`; an undefined profit factor (no wins
and no losses) stays unavailable. Cell records are never modified.

A shortlist is only a research tag stored on `search_batches.shortlist_json`.
"""
from __future__ import annotations

import json
import math
from datetime import datetime, timezone
from typing import Any, Iterable, Mapping

from edgelab.research.batch import cumulative_counts
from edgelab.research.search import RANKING_METRICS, SAMPLE_LABELS

DEFAULT_METRIC = "expectancy_r"
DEFAULT_MIN_SAMPLE_LABEL = "MODERATE SAMPLE"
SHOWN_METRICS = ("trade_count", "sample_label", "expectancy_r", "profit_factor", "net_r", "max_drawdown_r")
NOTE = ("In-sample ranking under the stated assumptions: not validated, not a forecast, and not evidence "
        "of an edge. The more trials a ranking is drawn from, the more likely its top results are chance; "
        "out-of-sample and walk-forward validation (Phase 6) are required before any conclusion.")


class RankingError(ValueError):
    pass


def check_ranking_args(metric: str, min_sample_label: str) -> None:
    if metric == "win_rate":
        raise RankingError(f"win rate is never a ranking metric; use one of {', '.join(RANKING_METRICS)}")
    if metric not in RANKING_METRICS:
        raise RankingError(f"invalid ranking metric {metric!r}; use one of {', '.join(RANKING_METRICS)}")
    if min_sample_label not in SAMPLE_LABELS:
        raise RankingError(f"invalid min_sample_label {min_sample_label!r}; use one of {', '.join(SAMPLE_LABELS)}")


def _usable(x: Any) -> bool:
    return isinstance(x, (int, float)) and not isinstance(x, bool) and math.isfinite(x)


def _infinite_profit_factor(h: Mapping) -> bool:
    """A stored null profit factor that is mathematically +inf. `metrics.profit_factor` is inf
    exactly when no trade lost and gains > 0 (nan when nothing won or lost); non-finite values
    are stored as null, but the same headline keeps `loss_rate` (share of trades with net R < 0)
    and `net_r` (their sum), which identify the infinite case without guessing."""
    return ("profit_factor" in h and h["profit_factor"] is None and h.get("trade_count")
            and _usable(h.get("loss_rate")) and h["loss_rate"] == 0 and _usable(h.get("net_r")) and h["net_r"] > 0)


def rank_cells(cells: Iterable[Mapping], metric: str = DEFAULT_METRIC,
               min_sample_label: str = DEFAULT_MIN_SAMPLE_LABEL) -> dict:
    """Pure: rank decoded cell records (as in `batch.search_summary(...)["cells"]`).
    Ties: strategy_id, then cell_id. Returns new dicts; the input is not modified."""
    check_ranking_args(metric, min_sample_label)
    floor = SAMPLE_LABELS.index(min_sample_label)
    current = [c for c in cells if c.get("current", True)]
    excluded = {k: 0 for k in ("historical", "ineligible", "pending", "cancelled", "failed", "no_trades",
                               "metric_unavailable", "unknown_sample_label", "below_min_sample")}
    excluded["historical"] = sum(1 for c in cells if not c.get("current", True))
    rows = []
    for c in current:
        st = c.get("status")
        if st != "completed":
            excluded[st if st in excluded else "failed"] += 1
            continue
        h = c.get("headline") or {}
        if not h.get("trade_count"):
            excluded["no_trades"] += 1
            continue
        infinite = metric == "profit_factor" and _infinite_profit_factor(h)
        if not infinite and not _usable(h.get(metric)):
            excluded["metric_unavailable"] += 1
            continue
        if h.get("sample_label") not in SAMPLE_LABELS:
            excluded["unknown_sample_label"] += 1
            continue
        if SAMPLE_LABELS.index(h["sample_label"]) < floor:
            excluded["below_min_sample"] += 1
            continue
        rows.append({"strategy_id": c["strategy_id"], "dataset_id": c["dataset_id"], "cell_id": c["cell_id"],
                     "run_id": c.get("run_id"), "trades_hash": c.get("trades_hash"),
                     "value": None if infinite else float(h[metric]), "value_infinite": bool(infinite),
                     "metrics": {k: h[k] for k in SHOWN_METRICS if k in h}})
    # +inf (no losing trade) ranks above every finite value; it is flagged, never replaced by a number
    rows.sort(key=lambda r: (not r["value_infinite"], -(r["value"] or 0.0), r["strategy_id"], r["cell_id"]))
    for i, r in enumerate(rows, 1):
        r["rank"] = i
    n_trials = cumulative_counts(current)["trials"]
    return {"metric": metric, "direction": "descending", "min_sample_label": min_sample_label,
            "n_trials": n_trials, "n_current_cells": len(current), "n_ranked": len(rows), "excluded": excluded,
            "in_sample": True, "status": "IN_SAMPLE", "validated": False, "ranked": rows,
            "label": f"IN-SAMPLE ranking of {len(rows)} result(s) drawn from {n_trials} trial(s) - NOT VALIDATED",
            "note": NOTE}


def rank_search(store, search_id: str, metric: str | None = None, min_sample_label: str | None = None) -> dict:
    """Rank a stored search. Unspecified arguments use the search spec's own ranking settings
    (whose defaults are expectancy_r / MODERATE SAMPLE)."""
    from edgelab.research.batch import search_summary
    s = search_summary(store, search_id)
    spec_rk = (s.get("spec") or {}).get("ranking") or {}
    out = rank_cells(s["cells"], metric or spec_rk.get("metric", DEFAULT_METRIC),
                     min_sample_label or spec_rk.get("min_sample_label", DEFAULT_MIN_SAMPLE_LABEL))
    program = None
    if s.get("protocol_id"):                     # ADR-56: the program-wide count, not only this search's
        program = {"protocol_id": s["protocol_id"], "program_unique_trials": store.count_trials(s["protocol_id"]),
                   "note": "multiple testing is judged against the program-wide count of unique numerical trials; "
                           "an in-sample rank is never acceptance"}
    return {"search_id": search_id, "search_status": s["status"], **out, "program": program}


def select_shortlist(store, search_id: str, strategy_ids: Any) -> dict:
    """Tag strategies of the CURRENT search plan as a shortlist (first occurrence order, duplicates
    dropped). Only `search_batches.shortlist_json` changes: no run, run status or cell is touched."""
    if isinstance(strategy_ids, str) or not isinstance(strategy_ids, (list, tuple)) or \
            not all(isinstance(x, str) and x for x in strategy_ids):
        raise RankingError("strategy_ids must be a list of strategy ids")
    if store.get_search_batch(search_id) is None:
        raise KeyError(search_id)
    members = {c["strategy_id"] for c in store.list_search_cells(search_id, current=True)}
    chosen = list(dict.fromkeys(strategy_ids))
    missing = [x for x in chosen if x not in members]
    if missing:
        raise RankingError(f"not in the current plan of {search_id}: {missing}")
    tag = {"strategy_ids": chosen, "selected_at": datetime.now(timezone.utc).isoformat(),
           "in_sample": True, "validated": False, "protocol_id": store.get_search_batch(search_id).get("protocol_id"),
           "note": "research tag only: selection changes no run status and implies no validation"}
    store.update_search_batch(search_id, shortlist_json=json.dumps(tag, sort_keys=True))
    return {"search_id": search_id, **tag, "duplicates_removed": len(strategy_ids) - len(chosen)}
