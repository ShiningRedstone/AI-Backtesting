"""Strategy Lab read models (Phase 8): machine-readable provenance, run comparison, equity curves.

Everything here READS existing stores (strategy library, run registry, search cells, prop
simulations) and re-uses existing analytics (compute_metrics outputs stored with each run,
``breakeven_cost_multiplier``, ``sample_scope``). No research logic is added or changed: no
ranking, no score, no "best" label, no promotion. Results keep the status of the runs they come
from (IN_SAMPLE / OUT_OF_SAMPLE / WALK_FORWARD) and say so in every row.

The dictionaries returned are the objects a later AI layer receives (it never reconstructs state
from UI text): strategy id, definition/logic hash, parent id/hash, dataset id, provider,
instrument, timeframe, cost profile/status, variation configuration, run status, run id.
"""
from __future__ import annotations

import math
from typing import Any, Iterable, Mapping

import numpy as np

SCOPE_LABEL = {
    "IN_SAMPLE": "In-sample (exploratory)",
    "OUT_OF_SAMPLE": "Out-of-sample",
    "WALK_FORWARD": "Walk-forward (out-of-sample test window)",
}
COMPARE_METRICS = ("trade_count", "sample_label", "gross_r", "net_r", "cost_r", "expectancy_r", "profit_factor",
                   "max_drawdown_r", "win_rate", "net_usd", "max_drawdown_usd")
MAX_COMPARE_RUNS = 500
MAX_CURVE_POINTS = 5000


def _finite(x):
    try:
        return x if x is None or math.isfinite(float(x)) else None
    except (TypeError, ValueError):
        return x


def run_summary(rec: Mapping, prop_counts: Mapping[str, int] | None = None) -> dict:
    """One stored run as a machine-readable row (no trades)."""
    d, a = rec.get("dataset") or {}, rec.get("assumptions") or {}
    s = rec.get("strategy") or {}
    dsl = s.get("dsl") or {}
    status = rec.get("status") or "IN_SAMPLE"
    hm = rec.get("headline_metrics") or {}
    notes = str(rec.get("notes") or "")
    return {
        "run_id": rec.get("run_id"), "created_at": rec.get("created_at"), "status": status,
        "scope": SCOPE_LABEL.get(status, status),
        "validated": False,                                  # a run is never "validated" by itself (Phase 6 owns it)
        "synthetic": notes.startswith("SYNTHETIC"),
        "notes": notes,
        "strategy_id": s.get("strategy_id"), "strategy_name": dsl.get("name"),
        "definition_hash": dsl.get("definition_hash"), "logic_hash": dsl.get("logic_hash"),
        "parent_strategy_id": s.get("parent_strategy_id"),
        "dataset_id": d.get("dataset_id"), "dataset_name": d.get("dataset_name"),
        "parent_dataset_id": d.get("parent_dataset_id"), "provider": d.get("provider"),
        "instrument": d.get("instrument"), "timeframe": d.get("timeframe"),
        "period": {"start": str(d.get("start")), "end": str(d.get("end"))},
        "cost_profile": (a.get("costs") or {}).get("profile"), "cost_status": a.get("cost_status"),
        "cost_scenario": (a.get("costs") or {}).get("scenario") or None,
        "cost_basis": (a.get("costs") or {}).get("basis") or None,
        "config_hash": rec.get("config_hash"), "trades_hash": rec.get("trades_hash"),
        "code_version": rec.get("code_version"),
        "metrics": {k: _finite(hm.get(k)) if not isinstance(hm.get(k), str) else hm.get(k) for k in COMPARE_METRICS},
        "prop_simulations": int((prop_counts or {}).get(rec.get("run_id"), 0)),
    }


def _prop_counts(svc) -> dict[str, int]:
    out: dict[str, int] = {}
    for s in svc.list_prop_simulations():
        out[s["source_run_id"]] = out.get(s["source_run_id"], 0) + 1
    return out


def _strategy_parameters(defn: Mapping) -> dict:
    return {k: (v or {}).get("value") for k, v in (defn.get("parameters") or {}).items()}


def descendants(svc, strategy_id: str) -> list[str]:
    """The strategy and every stored descendant (breadth-first, from lineage records)."""
    seen, queue = [strategy_id], [strategy_id]
    while queue:
        for c in svc.library.children(queue.pop(0)):
            if c not in seen:
                seen.append(c)
                queue.append(c)
    return seen


def strategy_research(svc, strategy_id: str) -> dict:
    """Machine-readable provenance of one strategy version and the research attached to it."""
    doc = svc.library.load(strategy_id)
    lineage = doc.get("lineage") or []
    first = lineage[0] if lineage else {}
    parent = first.get("parent_strategy_id")
    parent_hash = None
    if parent:
        try:
            parent_hash = svc.library.load(parent)["definition_hash"]
        except KeyError:
            parent_hash = None
    batch_id = first.get("generation_batch_id")
    batch = None
    if batch_id:
        try:
            b = svc.library.load_batch(batch_id)
            batch = {"batch_id": batch_id, "kind": b.get("kind", "variation"), "spec": b.get("spec"),
                     "base": b.get("base"), "generator_version": b.get("generator_version")}
        except (KeyError, FileNotFoundError):
            batch = {"batch_id": batch_id, "missing": True}
    counts = _prop_counts(svc)
    runs = []
    for r in svc.store.list_runs().to_dict("records"):
        if r["strategy_id"] == strategy_id:
            rec, _ = svc.store.load_run(r["run_id"])
            runs.append(run_summary({**rec, "run_id": r["run_id"]}, counts))
    statuses = sorted({r["status"] for r in runs})
    based_on = [b["batch_id"] for b in svc.library.list_batches() if (b.get("base_strategy_id") == strategy_id)]
    defn = doc["definition"]
    return {
        "object": "edgelab.strategy_research", "schema_version": 1,
        "strategy": {"strategy_id": strategy_id, "name": doc.get("name"), "family_id": doc.get("family_id"),
                     "logic_hash": doc.get("logic_hash"), "definition_hash": doc.get("definition_hash"),
                     "timeframe": defn.get("timeframe"), "dsl_version": defn.get("dsl_version"),
                     "parameters": _strategy_parameters(defn), "archived": bool(doc.get("archived"))},
        "lineage": {"generation_method": first.get("generation_method"), "parent_strategy_id": parent,
                    "parent_definition_hash": parent_hash, "changes": first.get("changes") or [],
                    "generation_parameters": first.get("generation_parameters") or {},
                    "generation_batch": batch, "generation_timestamp": first.get("generation_timestamp"),
                    "records": len(lineage),
                    "ancestry": [a.get("strategy_id") for a in svc.library.ancestry(strategy_id)],
                    "children": svc.library.children(strategy_id)},
        "variation_batches_from_this_strategy": based_on,
        "runs": runs,
        "validation_state": {
            "run_statuses": statuses,
            "has_out_of_sample_runs": any(s in ("OUT_OF_SAMPLE", "WALK_FORWARD") for s in statuses),
            "validated": False,
            "note": "Stored runs keep their own status. In-sample runs are exploratory; out-of-sample and walk-forward "
                    "runs are evaluations of this fixed definition on held-out time. Nothing here promotes a strategy.",
        },
    }


def _source_run_ids(svc, run_ids=None, strategy_id=None, lineage_of=None, batch_id=None, search_id=None) -> tuple[list[str], dict]:
    chosen = [x for x in (run_ids, strategy_id, lineage_of, batch_id, search_id) if x]
    if len(chosen) != 1:
        raise ValueError("choose exactly one source: run_ids, strategy_id, lineage_of, batch_id or search_id")
    all_runs = svc.store.list_runs().to_dict("records")
    if run_ids:
        ids = list(dict.fromkeys(run_ids))
        known = {r["run_id"] for r in all_runs}
        missing = [i for i in ids if i not in known]
        if missing:
            raise KeyError(f"unknown run ids {missing}")
        return ids, {"kind": "run_ids", "run_ids": ids}
    if search_id:
        s = svc.get_search(search_id)
        ids = [c["run_id"] for c in s["cells"] if c.get("run_id")]
        return ids, {"kind": "search", "search_id": search_id, "search_status": s.get("status")}
    if batch_id:
        b = svc.library.load_batch(batch_id)
        base = (b.get("base") or {}).get("strategy_id") or b.get("base_strategy_id")
        members = ([base] if base else []) + list(b.get("children", []))
        return [r["run_id"] for r in all_runs if r["strategy_id"] in members], \
            {"kind": "variation_batch", "batch_id": batch_id, "strategies": members}
    if strategy_id:
        svc.library.load(strategy_id)
        return [r["run_id"] for r in all_runs if r["strategy_id"] == strategy_id], {"kind": "strategy", "strategy_id": strategy_id}
    svc.library.load(lineage_of)
    members = descendants(svc, lineage_of)
    return [r["run_id"] for r in all_runs if r["strategy_id"] in members], \
        {"kind": "lineage", "root": lineage_of, "strategies": members}


def compare_runs(svc, run_ids: Iterable[str] | None = None, strategy_id: str | None = None,
                 lineage_of: str | None = None, batch_id: str | None = None, search_id: str | None = None) -> dict:
    """Side-by-side rows for stored runs (one source). Transparent metrics only; rows are returned
    in run order and the caller sorts/filters. Nothing is ranked or scored here."""
    from edgelab.analytics.metrics import breakeven_cost_multiplier
    from edgelab.analytics.research import research_labels
    ids, source = _source_run_ids(svc, list(run_ids) if run_ids else None, strategy_id, lineage_of, batch_id, search_id)
    if len(ids) > MAX_COMPARE_RUNS:
        raise ValueError(f"{len(ids)} runs exceed the comparison limit of {MAX_COMPARE_RUNS}; choose a narrower source")
    counts = _prop_counts(svc)
    params_cache: dict[str, dict] = {}
    rows, records = [], []
    for rid in ids:
        rec, trades = svc.store.load_run(rid)
        rec = {**rec, "run_id": rid}
        records.append(rec)
        row = run_summary(rec, counts)
        be = breakeven_cost_multiplier(trades) if len(trades) and "cost_r_base" in trades else None
        row["breakeven_cost_multiplier"] = None if be is None or not math.isfinite(be) else float(be)
        row["breakeven_note"] = ("no costs recorded" if be is not None and not math.isfinite(be) else
                                 "gross R is not positive: no cost level makes it profitable" if be is not None and be <= 0
                                 else None)
        sid = row["strategy_id"]
        if sid not in params_cache:
            try:
                lib = svc.library.load(sid)
                lin = (lib.get("lineage") or [{}])[0]
                params_cache[sid] = {"parameters": _strategy_parameters(lib["definition"]),
                                     "generation_method": lin.get("generation_method"),
                                     "library_parent": lin.get("parent_strategy_id"),
                                     "changes": lin.get("changes") or [],
                                     "generation_batch_id": lin.get("generation_batch_id")}
            except KeyError:
                params_cache[sid] = {"parameters": {}, "generation_method": None, "library_parent": None,
                                     "changes": [], "generation_batch_id": None}
        row.update(params_cache[sid])
        rows.append(row)
    return {
        "object": "edgelab.run_comparison", "schema_version": 1, "source": source, "n_runs": len(rows),
        "rows": rows,
        "labels": (research_labels(records) if records else []) + [
            "Transparent side-by-side metrics of stored runs; rows are not ranked or scored, and no row is a "
            "recommendation. Each row keeps its own sample scope: in-sample rows are exploratory and are not "
            "equivalent to out-of-sample or walk-forward rows."],
    }


def run_curve(svc, run_id: str) -> dict:
    """Cumulative net R and drawdown (R) by trade exit, from the stored trades."""
    rec, trades = svc.store.load_run(run_id)
    status = rec.get("status") or "IN_SAMPLE"
    if not len(trades):
        return {"run_id": run_id, "status": status, "scope": SCOPE_LABEL.get(status, status), "n_trades": 0, "points": []}
    t = trades.sort_values(["exit_ts", "entry_ts"], kind="mergesort")
    r = t["net_r"].to_numpy(float)
    eq = np.cumsum(r)
    dd = np.maximum.accumulate(np.concatenate([[0.0], eq]))[1:] - eq
    idx = np.arange(len(r))
    if len(r) > MAX_CURVE_POINTS:                            # evenly thinned for display; endpoints kept
        idx = np.unique(np.linspace(0, len(r) - 1, MAX_CURVE_POINTS).round().astype(int))
    ts = t["exit_ts"].astype(str).to_numpy()
    return {"run_id": run_id, "status": status, "scope": SCOPE_LABEL.get(status, status), "n_trades": int(len(r)),
            "thinned": bool(len(idx) < len(r)), "final_net_r": float(eq[-1]), "max_drawdown_r": float(dd.max()),
            "points": [{"i": int(i) + 1, "exit_ts": str(ts[i]), "equity_r": float(eq[i]), "drawdown_r": float(dd[i])}
                       for i in idx],
            "note": "Cumulative net R by trade exit under the run's stated costs; historical, not a forecast."}


def oos_control(svc, src: Any, dataset_id: str, split_at: Any, n_controls: int = 20, seed: int = 0) -> dict:
    """The unchanged random-entry control restricted to the OOS window of an evaluate_oos split:
    period = research.validation.oos_windows(dataset start, dataset end, split)[1], labelled
    OUT_OF_SAMPLE (the convention of run_oos_random_controls_5y.py)."""
    from edgelab.research.validation import oos_windows
    m = svc.store.get_manifest(dataset_id)
    _, oos = oos_windows(m.start, m.end, split_at)
    out = svc.random_entry_control(src, dataset_id, n_controls=n_controls, seed=seed, period=(oos.start, oos.end),
                                   sample_status="OUT_OF_SAMPLE")
    out["oos_window"] = {"split_at": str(oos.start), "start": str(oos.start), "end": str(oos.end)}
    return out
