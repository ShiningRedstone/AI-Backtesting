"""Phase 4 search specification and planner (no execution).

A search spec says WHICH stored strategies run on WHICH datasets; the planner expands it
into deterministic strategy x dataset cells:

  * strategy sources: explicit ids, Mode A variation batches (VB_...), Mode B proposal
    batches (PB_...), strategy families. Duplicate strategy ids collapse to one.
  * every cell is either eligible or ineligible with the same reasons `backtest_readiness`
    reports (unconfigured costs, failed validation, timeframe mismatch) plus "no bars in
    the period". Nothing is invented: CFD datasets without broker costs stay ineligible.
  * `max_cells` caps the ELIGIBLE cells; a larger search is refused before anything runs,
    never truncated.
  * datasets are never merged or averaged: each cell is one strategy on one dataset.

Identity:
  search_hash = hash(canonical strategy sources, datasets, period, seed, spec version)
                (ranking, max_cells and workers are excluded: ranking analyses stored results,
                max_cells is a safety cap, workers never changes results)
  cell_id     = hash(strategy_id, dataset_id, dataset content_hash, config_hash, search_hash)
Collections are canonicalised (sorted, de-duplicated) so input ordering is cosmetic.
Ranking settings are validated here and applied later (in-sample only, never validation).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any, Mapping

import pandas as pd

from edgelab.core.identity import hash_obj
from edgelab.data.schema import timeframe_minutes
from edgelab.research.compare import common_period, comparison_warnings
from edgelab.strategy.dsl import Issue, ValidationResult, _suggest

SEARCH_SPEC_VERSION = 1
SPEC_KEYS = {"search_spec_version", "strategies", "datasets", "period", "ranking", "max_cells", "seed",
             "workers"}
SOURCE_KEYS = {"ids": "STR_", "variation_batches": "VB_", "proposal_batches": "PB_", "families": None}
PERIOD_KEYS = {"start", "end"}
RANKING_KEYS = {"metric", "min_sample_label"}
RANKING_METRICS = ("expectancy_r", "profit_factor", "net_r")
SAMPLE_LABELS = ("LOW SAMPLE SIZE", "MODERATE SAMPLE", "ADEQUATE SAMPLE")    # analytics.metrics.sample_label
DEFAULT_MAX_CELLS = 1000
DEFAULTS = {"search_spec_version": SEARCH_SPEC_VERSION, "period": None,
            "ranking": {"metric": "expectancy_r", "min_sample_label": "MODERATE SAMPLE"},
            "max_cells": DEFAULT_MAX_CELLS, "seed": None, "workers": 1}
NOT_HASHED = ("ranking", "max_cells", "workers")


class SearchSpecError(ValueError):
    """A search spec or plan was refused. `issues` lists every problem found."""

    def __init__(self, message: str, issues: list[Issue] | None = None):
        super().__init__(message)
        self.issues = list(issues or [])


def _is_int(x: Any) -> bool:
    return isinstance(x, int) and not isinstance(x, bool)


def _utc(x: Any, path: str, iss: list[Issue]) -> pd.Timestamp | None:
    """An explicit-timezone timestamp -> UTC. A naive time is refused (no timezone guessing)."""
    try:
        t = pd.Timestamp(x)
    except (TypeError, ValueError):
        iss.append(Issue(path, f"not a timestamp: {x!r}", "ISO 8601 with an explicit offset, e.g. 2024-03-04T00:00:00Z"))
        return None
    if t is pd.NaT or t.tz is None:
        iss.append(Issue(path, f"timestamp {x!r} has no timezone; the timezone is never guessed",
                         "add an explicit offset, e.g. 2024-03-04T00:00:00Z"))
        return None
    return t.tz_convert("UTC")


# ============================================================ spec validation (no library / store access)
def validate_search_spec(spec: Any) -> ValidationResult:
    """Structural validation: keys, types, values. References are checked by `plan_search`."""
    iss: list[Issue] = []
    if not isinstance(spec, Mapping):
        return ValidationResult([Issue("$", "search spec must be a mapping")])
    for k in spec:
        if k not in SPEC_KEYS:
            iss.append(Issue(str(k), f"unknown key {k!r}", _suggest(k, SPEC_KEYS)))
    if spec.get("search_spec_version", SEARCH_SPEC_VERSION) != SEARCH_SPEC_VERSION:
        iss.append(Issue("search_spec_version", f"unsupported version; this build reads {SEARCH_SPEC_VERSION}"))

    src = spec.get("strategies")
    if not isinstance(src, Mapping) or not src:
        iss.append(Issue("strategies", "strategies must be a non-empty mapping",
                         f"keys: {', '.join(sorted(SOURCE_KEYS))}"))
    else:
        n_refs = 0
        for k, v in src.items():
            if k not in SOURCE_KEYS:
                iss.append(Issue(f"strategies.{k}", f"unknown strategy source {k!r}", _suggest(k, SOURCE_KEYS)))
                continue
            if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
                iss.append(Issue(f"strategies.{k}", "must be a list of non-empty strings"))
                continue
            prefix = SOURCE_KEYS[k]
            for i, x in enumerate(v):
                if prefix and not x.startswith(prefix):
                    iss.append(Issue(f"strategies.{k}[{i}]", f"{x!r} is not a {prefix}... id"))
            n_refs += len(v)
        if n_refs == 0 and not any(i.path.startswith("strategies") for i in iss):
            iss.append(Issue("strategies", "no strategy references given"))

    ds = spec.get("datasets")
    if not isinstance(ds, list) or not ds or not all(isinstance(x, str) and x for x in ds):
        iss.append(Issue("datasets", "datasets must be a non-empty list of dataset ids"))
    elif len(set(ds)) != len(ds):
        dup = sorted({x for x in ds if ds.count(x) > 1})
        iss.append(Issue("datasets", f"duplicate dataset ids {dup}", "list each dataset once"))

    per = spec.get("period")
    if per is not None and per != "common":
        if not isinstance(per, Mapping):
            iss.append(Issue("period", "period must be 'common' or a mapping with start and end"))
        else:
            for k in per:
                if k not in PERIOD_KEYS:
                    iss.append(Issue(f"period.{k}", f"unknown key {k!r}", _suggest(k, PERIOD_KEYS)))
            missing = PERIOD_KEYS - set(per)
            if missing:
                iss.append(Issue("period", f"missing {sorted(missing)}"))
            else:
                s, e = _utc(per["start"], "period.start", iss), _utc(per["end"], "period.end", iss)
                if s is not None and e is not None and s >= e:
                    iss.append(Issue("period", "period.start must be before period.end"))

    rk = spec.get("ranking")
    if rk is not None:
        if not isinstance(rk, Mapping):
            iss.append(Issue("ranking", "ranking must be a mapping"))
        else:
            for k in rk:
                if k not in RANKING_KEYS:
                    iss.append(Issue(f"ranking.{k}", f"unknown key {k!r}", _suggest(k, RANKING_KEYS)))
            m = rk.get("metric", DEFAULTS["ranking"]["metric"])
            if m == "win_rate":
                iss.append(Issue("ranking.metric", "win rate is never a ranking metric",
                                 f"one of {', '.join(RANKING_METRICS)}"))
            elif m not in RANKING_METRICS:
                iss.append(Issue("ranking.metric", f"invalid metric {m!r}", f"one of {', '.join(RANKING_METRICS)}"))
            lab = rk.get("min_sample_label", DEFAULTS["ranking"]["min_sample_label"])
            if lab not in SAMPLE_LABELS:
                iss.append(Issue("ranking.min_sample_label", f"invalid label {lab!r}",
                                 f"one of {', '.join(SAMPLE_LABELS)}"))

    mc = spec.get("max_cells", DEFAULT_MAX_CELLS)
    if not _is_int(mc) or mc < 1:
        iss.append(Issue("max_cells", "max_cells must be an integer >= 1"))
    seed = spec.get("seed")
    if seed is not None and not _is_int(seed):
        iss.append(Issue("seed", "seed must be an integer (or omitted)"))
    w = spec.get("workers", 1)
    if not _is_int(w) or w < 1:
        iss.append(Issue("workers", "workers must be an integer >= 1"))
    return ValidationResult(iss)


def _refuse(res: ValidationResult, head: str) -> None:
    if not res.valid:
        lines = "\n".join(f"  {i.path}: {i.message}" + (f" ({i.hint})" if i.hint else "") for i in res.errors)
        raise SearchSpecError(f"{head} ({len(res.errors)} error(s)):\n{lines}", res.errors)


def canonical_search_spec(spec: Mapping) -> dict:
    """Defaults filled, collections sorted and de-duplicated, period in UTC. Refuses invalid specs."""
    _refuse(validate_search_spec(spec), "search spec is invalid")
    per = spec.get("period")
    if isinstance(per, Mapping):
        iss: list[Issue] = []
        per = {k: _utc(per[k], f"period.{k}", iss).isoformat() for k in ("start", "end")}
    return {
        "search_spec_version": SEARCH_SPEC_VERSION,
        "strategies": {k: sorted(set(spec["strategies"].get(k, []))) for k in sorted(SOURCE_KEYS)},
        "datasets": sorted(spec["datasets"]),
        "period": per,
        "ranking": {**DEFAULTS["ranking"], **dict(spec.get("ranking") or {})},
        "max_cells": spec.get("max_cells", DEFAULT_MAX_CELLS),
        "seed": spec.get("seed"),
        "workers": spec.get("workers", 1),
    }


def search_hash(spec: Mapping) -> str:
    return _hash_canonical(canonical_search_spec(spec))


def _hash_canonical(canon: Mapping) -> str:
    return hash_obj({k: v for k, v in canon.items() if k not in NOT_HASHED})


def cell_id(strategy_id: str, dataset_id: str, content_hash: str, config_hash: str, s_hash: str) -> str:
    """dataset_id is part of the identity: two datasets may hold IDENTICAL bars (e.g. a futures
    and a CFD import of the same file) yet differ in instrument, costs and calendar."""
    return "CELL_" + hash_obj({"strategy_id": strategy_id, "dataset_id": dataset_id,
                               "dataset_content_hash": content_hash, "config_hash": config_hash,
                               "search_hash": s_hash}, 16).upper()


# ============================================================ planning
@dataclass
class SearchPlan:
    search_id: str
    search_hash: str
    config_hash: str
    spec: dict                                   # canonical
    strategies: list[dict]                       # [{strategy_id, timeframe, sources}]
    datasets: list[dict]                         # list_datasets rows (+ eligibility per timeframe in cells)
    period: dict | None                          # resolved UTC window, or None (full datasets)
    cells: list[dict]
    counts: dict
    excluded: list[dict] = field(default_factory=list)   # archived members of batches/families
    warnings: list[str] = field(default_factory=list)
    plan_hash: str = ""

    def eligible_cells(self) -> list[dict]:
        return [c for c in self.cells if c["eligible"]]

    def to_dict(self) -> dict:
        return {"search_id": self.search_id, "search_hash": self.search_hash, "config_hash": self.config_hash,
                "plan_hash": self.plan_hash, "spec": self.spec, "strategies": self.strategies,
                "datasets": self.datasets, "period": self.period, "cells": self.cells, "counts": self.counts,
                "excluded": self.excluded, "warnings": self.warnings,
                "note": "plan only: nothing has been executed"}


def _resolve_strategies(canon: Mapping, library) -> tuple[dict[str, dict], list[dict], dict, list[Issue]]:
    """strategy_id -> {sources, timeframe}; archived batch/family members are excluded and reported."""
    found: dict[str, dict] = {}
    excluded: dict[str, dict] = {}
    n = {"references": 0, "collapsed": 0}
    iss: list[Issue] = []

    def add(sid: str, source: str, explicit: bool, path: str):
        n["references"] += 1
        try:
            doc = library.load(sid)
        except KeyError:
            iss.append(Issue(path, f"strategy {sid} is not in the strategy library"))
            return
        if doc.get("archived"):
            if explicit:
                iss.append(Issue(path, f"strategy {sid} is archived", "restore it before searching it"))
            else:
                excluded.setdefault(sid, {"strategy_id": sid, "reason": "archived", "sources": []})["sources"].append(source)
            return
        if sid in found:
            n["collapsed"] += 1
        row = found.setdefault(sid, {"strategy_id": sid, "timeframe": doc["definition"].get("timeframe"),
                                     "sources": []})
        row["sources"].append(source)

    src = canon["strategies"]
    for sid in src["ids"]:
        add(sid, "ids", True, "strategies.ids")
    for bid in src["variation_batches"]:
        try:
            batch = library.load_batch(bid)
        except FileNotFoundError:
            iss.append(Issue("strategies.variation_batches", f"variation batch {bid} not found"))
            continue
        children = batch.get("children") or []
        if not children:
            iss.append(Issue("strategies.variation_batches", f"variation batch {bid} has no children"))
        for sid in children:
            add(sid, bid, False, "strategies.variation_batches")
    if src["proposal_batches"]:
        members: dict[str, list[str]] = {b: [] for b in src["proposal_batches"]}
        for row in library.list(include_archived=True):
            for rec in library.load(row["strategy_id"])["lineage"]:
                b = rec.get("generation_batch_id")
                if b in members and row["strategy_id"] not in members[b]:
                    members[b].append(row["strategy_id"])
        for bid in src["proposal_batches"]:
            if not members[bid]:
                iss.append(Issue("strategies.proposal_batches",
                                 f"no stored strategies belong to proposal batch {bid}",
                                 "proposals must be ingested with save=True before they can be searched"))
            for sid in sorted(members[bid]):
                add(sid, bid, False, "strategies.proposal_batches")
    for fid in src["families"]:
        rows = library.list(fid, include_archived=True)
        if not rows:
            iss.append(Issue("strategies.families", f"no strategies in family {fid!r}"))
        for r in rows:
            add(r["strategy_id"], f"family:{fid}", False, "strategies.families")
    if not iss and not found:
        iss.append(Issue("strategies", "every referenced strategy is archived; nothing to search"))
    for r in list(found.values()) + list(excluded.values()):
        r["sources"] = sorted(set(r["sources"]))
    return found, [excluded[s] for s in sorted(excluded)], n, iss


def plan_search(spec: Mapping, services) -> SearchPlan:
    """Expand a search spec into cells WITHOUT executing anything. `services` is an
    `edgelab.services.Services` (library, dataset listing, eligibility, config hash).
    Raises SearchSpecError on any invalid spec or reference, or when the eligible cells
    exceed max_cells (the search is refused, never truncated)."""
    canon = canonical_search_spec(spec)
    s_hash = _hash_canonical(canon)
    cfg_hash = services._config_hash()

    strategies, excluded, ref_counts, iss = _resolve_strategies(canon, services.library)
    n_refs, n_collapsed = ref_counts["references"], ref_counts["collapsed"]
    rows = {d["dataset_id"]: d for d in services.list_datasets()}
    for did in canon["datasets"]:
        if did not in rows:
            iss.append(Issue("datasets", f"dataset {did} is not in the store", _suggest(did, rows) if rows else ""))
    _refuse(ValidationResult(iss), "search references are invalid")
    datasets = [rows[did] for did in canon["datasets"]]
    manifests = [services.store.get_manifest(did) for did in canon["datasets"]]

    period = None
    if canon["period"] == "common":
        try:
            s, e = common_period([SimpleNamespace(manifest=m) for m in manifests])
        except ValueError as exc:
            raise SearchSpecError(f"period 'common': {exc}", [Issue("period", str(exc))]) from exc
        period = {"mode": "common", "start": s.isoformat(), "end": e.isoformat()}
    elif canon["period"] is not None:
        period = {"mode": "explicit", **canon["period"]}
    warnings = comparison_warnings(manifests, check_periods=period is None)

    def outside(d: Mapping) -> str | None:
        if period is None:
            return None
        s, e = pd.Timestamp(period["start"]), pd.Timestamp(period["end"])
        ds_s, ds_e = pd.Timestamp(d["start"]), pd.Timestamp(d["end"])
        ds_s = ds_s.tz_localize("UTC") if ds_s.tz is None else ds_s
        ds_e = ds_e.tz_localize("UTC") if ds_e.tz is None else ds_e
        if ds_e < s or ds_s > e:
            return f"dataset covers {d['start']} .. {d['end']}, outside the period {period['start']} .. {period['end']}"
        return None

    elig: dict[tuple[str, int | None], dict] = {}
    cells = []
    for sid in sorted(strategies):
        tf_text = strategies[sid]["timeframe"]
        try:
            tf = timeframe_minutes(str(tf_text))
        except ValueError:
            tf = None
        for d in datasets:
            key = (d["dataset_id"], tf)
            if key not in elig:
                elig[key] = services._dataset_eligibility(d, tf)
            e = elig[key]
            reasons = list(e["reasons"])
            if (r := outside(d)) is not None:
                reasons.append(r)
            cells.append({"cell_id": cell_id(sid, d["dataset_id"], d["content_hash"], cfg_hash, s_hash),
                          "plan_index": len(cells),
                          "strategy_id": sid, "dataset_id": d["dataset_id"],
                          "dataset_content_hash": d["content_hash"], "strategy_timeframe": tf_text,
                          "cost_status": e["cost"]["status"], "synthetic": e["synthetic"],
                          "eligible": not reasons, "reasons": reasons})
    n_elig = sum(c["eligible"] for c in cells)
    counts = {"strategy_references": n_refs, "strategies": len(strategies),
              "duplicate_references_collapsed": n_collapsed,
              "excluded_archived": len(excluded), "datasets": len(datasets),
              "planned": len(cells), "eligible": n_elig, "ineligible": len(cells) - n_elig}
    if n_elig > canon["max_cells"]:
        msg = (f"{n_elig:,} eligible cells exceed max_cells {canon['max_cells']:,}; "
               "the search is refused (never truncated) - raise max_cells or narrow the search")
        raise SearchSpecError(msg, [Issue("max_cells", msg)])
    if n_elig == 0:
        warnings.append("no eligible cells: every strategy x dataset combination is ineligible (see reasons)")
    plan_hash = hash_obj({"search_hash": s_hash, "config_hash": cfg_hash, "period": period,
                          "cells": [(c["cell_id"], c["eligible"], c["reasons"]) for c in cells]})
    return SearchPlan("SRCH_" + s_hash[:12].upper(), s_hash, cfg_hash, canon,
                      [strategies[s] for s in sorted(strategies)], datasets, period, cells, counts,
                      excluded, warnings, plan_hash)
