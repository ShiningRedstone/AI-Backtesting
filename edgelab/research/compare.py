"""Compare datasets WITHOUT combining them.

The same strategy object (same strategy_id) is run independently on each dataset -
e.g. NQ futures, NAS100 CFD from provider A, NAS100 CFD from provider B - each with
its own calendar, costs, feature cache and validation. Results are reported side by
side with full provenance. Nothing is concatenated or averaged: the question is whether
behaviour survives a change of feed, not what a blended feed would say.

``compare_feeds`` answers a different question: how similar are two feeds' prices on the
timestamps they share (coverage, basis, return correlation)? Futures vs CFD will show a
basis (financing/dividends) - it is reported, never "corrected".
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Mapping, Sequence

import numpy as np
import pandas as pd

from edgelab.analytics.metrics import compute_metrics
from edgelab.core.identity import hash_obj
from edgelab.data.validation import ValidatedDataset, validate_and_freeze
from edgelab.engine.backtester import run_backtest
from edgelab.engine.costs import CostModel
from edgelab.engine.signals import Strategy
from edgelab.features.cache import FeatureCache
from edgelab.features.sessions import SessionWindow
from edgelab.features.strategy_api import FeatureContext, FeatureStrategy

PROVENANCE_FIELDS = ("dataset_id", "dataset_name", "provider", "asset_type", "instrument", "symbol",
                     "timeframe", "content_hash", "price_basis", "volume_type", "has_spread",
                     "start", "end", "n_bars", "quality_status")
TABLE_METRICS = ("trade_count", "sample_label", "expectancy_r", "expectancy_ci95", "win_rate",
                 "profit_factor", "net_r", "net_usd", "max_drawdown_r", "max_drawdown_usd")


@dataclass
class DatasetRun:
    provenance: dict
    strategy_id: str
    strategy_spec: dict
    cost_model: dict
    feature_cache_keys: dict
    assumptions: dict
    metrics: dict
    trades: pd.DataFrame
    error: str | None = None


@dataclass
class ComparisonResult:
    comparison_id: str
    strategy_id: str
    runs: list[DatasetRun]
    warnings: list[str] = field(default_factory=list)
    config_hash: str = ""
    code_version: str = ""

    def table(self) -> pd.DataFrame:
        rows = []
        for r in self.runs:
            row = {k: r.provenance.get(k) for k in ("dataset_id", "provider", "asset_type", "instrument",
                                                   "timeframe", "start", "end")}
            row["cost_status"] = r.cost_model.get("status")
            row.update({k: r.metrics.get(k) for k in TABLE_METRICS})
            row["error"] = r.error
            rows.append(row)
        return pd.DataFrame(rows)

    def to_dict(self) -> dict:
        return {"comparison_id": self.comparison_id, "strategy_id": self.strategy_id,
                "config_hash": self.config_hash, "code_version": self.code_version,
                "warnings": self.warnings,
                "runs": [{"provenance": r.provenance, "strategy_id": r.strategy_id,
                          "strategy_spec": r.strategy_spec, "cost_model": r.cost_model,
                          "feature_cache_keys": r.feature_cache_keys, "assumptions": r.assumptions,
                          "metrics": r.metrics, "n_trades": int(len(r.trades)), "error": r.error}
                         for r in self.runs]}


def comparison_warnings(manifests: Sequence, check_periods: bool = True) -> list[str]:
    """Caveats for reading results side by side (identical content, mixed timeframes,
    different periods). `check_periods=False` when a common period is imposed anyway."""
    warnings = []
    by_hash: dict[str, list[str]] = {}
    for m in manifests:
        by_hash.setdefault(m.content_hash, []).append(m.dataset_id)
    for h, same in by_hash.items():
        if len(same) > 1:
            warnings.append(f"datasets {same} have IDENTICAL bar content - agreement between them is "
                            "not independent evidence")
    tfs = {m.timeframe for m in manifests}
    if len(tfs) > 1:
        warnings.append(f"datasets have different timeframes {sorted(tfs)}; bar-count parameters mean "
                        "different durations")
    periods = {(m.start[:10], m.end[:10]) for m in manifests}
    if check_periods and len(periods) > 1:
        warnings.append("datasets cover different periods; use restrict_to_period(common_period(...)) "
                        "for a like-for-like comparison")
    return warnings


def run_across_datasets(strategy: Strategy, datasets: Sequence[ValidatedDataset],
                        costs_for: Callable[[ValidatedDataset], CostModel], cfg: Mapping,
                        sessions: Mapping[str, SessionWindow] | None = None,
                        cache: FeatureCache | None = None, sizing_cfg: Mapping | None = None,
                        contract_for: Callable[[Mapping | None], object] | None = None) -> ComparisonResult:
    """Run one strategy independently on each dataset. A failure on one dataset (e.g. an
    unconfigured cost profile) is recorded for that dataset and does not stop the others."""
    from edgelab.core.config import config_hash
    from edgelab.core.identity import code_version
    ids = [d.manifest.dataset_id for d in datasets]
    if len(set(ids)) != len(ids):
        raise ValueError(f"duplicate dataset ids in comparison: {ids}")
    warnings = comparison_warnings([d.manifest for d in datasets])
    bt_cfg = cfg["backtest"]
    sizing_cfg = sizing_cfg if sizing_cfg is not None else bt_cfg.get("sizing")
    runs = []
    for ds in datasets:
        prov = {k: getattr(ds.manifest, k) for k in PROVENANCE_FIELDS}
        prov["manifest_hash"] = ds.manifest.manifest_hash()
        strat, keys = strategy, {}
        try:
            if isinstance(strategy, FeatureStrategy):
                ctx = FeatureContext(ds, sessions or {}, cache)
                strat = strategy.bind(ctx)
                keys = {fs.label: ctx.engine.cache_key(fs) for fs in strategy.feature_specs()}
            costs = costs_for(ds)
            res = run_backtest(ds, strat, costs, bt_cfg, sizing=sizing_cfg,
                               contract=contract_for(sizing_cfg) if contract_for else None)
            met = compute_metrics(res.trades, sample_thresholds=cfg.get("sample_size"))
            runs.append(DatasetRun(prov, strategy.strategy_id, strategy.spec, costs.to_dict(), keys,
                                   res.assumptions, met, res.trades))
        except Exception as exc:          # recorded per dataset, never silently dropped
            runs.append(DatasetRun(prov, strategy.strategy_id, strategy.spec, {}, keys, {}, {},
                                   pd.DataFrame(), error=f"{type(exc).__name__}: {exc}"))
    cid = "CMP_" + hash_obj({"strategy": strategy.strategy_id, "datasets": [
        (d.manifest.dataset_id, d.manifest.content_hash) for d in datasets]})[:12].upper()
    return ComparisonResult(cid, strategy.strategy_id, runs, warnings, config_hash(cfg), code_version())


def common_period(datasets: Sequence[ValidatedDataset]) -> tuple[pd.Timestamp, pd.Timestamp]:
    start = max(pd.Timestamp(d.manifest.start) for d in datasets)
    end = min(pd.Timestamp(d.manifest.end) for d in datasets)
    if start >= end:
        raise ValueError("datasets do not overlap in time")
    return start, end


def restrict_to_period(ds: ValidatedDataset, start, end, thresholds=None) -> ValidatedDataset:
    """A new, RE-VALIDATED dataset holding bars with open in [start, end] (inclusive).
    Its id records the parent and the window; the parent is untouched."""
    s, e = pd.Timestamp(start), pd.Timestamp(end)
    s = s.tz_localize("UTC") if s.tz is None else s.tz_convert("UTC")
    e = e.tz_localize("UTC") if e.tz is None else e.tz_convert("UTC")
    ts = ds.bars.ts_ns
    mask = (ts >= s.value) & (ts <= e.value)
    if not mask.any():
        raise ValueError(f"{ds.manifest.dataset_id}: no bars in [{s}, {e}]")
    frame = ds.bars.to_frame().loc[mask].reset_index(drop=True)
    m = ds.manifest
    keep = {k: getattr(m, k) for k in ("dataset_name", "asset_type", "symbol", "source_timezone",
                                       "source_timestamp_convention", "source_file_sha256",
                                       "volume_type", "price_basis", "has_bid_ask", "spread_source",
                                       "provider_notes", "import_version")}
    did = f"{m.dataset_id}__{s:%Y%m%d}_{e:%Y%m%d}"
    return validate_and_freeze(frame, ds.instrument, ds.calendar, m.timeframe, ds.bars.tf_minutes,
                               m.provider, did, thresholds, source_detail={"restricted_from": m.dataset_id},
                               contract=m.contract, adjustment=m.adjustment, parent_dataset_id=m.dataset_id,
                               derivation=f"period restriction [{s.isoformat()}, {e.isoformat()}]", **keep)


def compare_feeds(a: ValidatedDataset, b: ValidatedDataset) -> dict:
    """Bar-level agreement between two feeds on shared timestamps (diagnostic only)."""
    if a.bars.tf_minutes != b.bars.tf_minutes:
        raise ValueError("compare_feeds needs equal timeframes (resample one of them first)")
    ta, tb = a.bars.ts_ns, b.bars.ts_ns
    common, ia, ib = np.intersect1d(ta, tb, assume_unique=True, return_indices=True)
    out = {"a": a.manifest.dataset_id, "b": b.manifest.dataset_id,
           "a_asset_type": a.manifest.asset_type, "b_asset_type": b.manifest.asset_type,
           "a_price_basis": a.manifest.price_basis, "b_price_basis": b.manifest.price_basis,
           "bars_a": int(len(ta)), "bars_b": int(len(tb)), "common_bars": int(len(common)),
           "only_in_a": int(len(ta) - len(common)), "only_in_b": int(len(tb) - len(common)),
           "notes": []}
    if a.manifest.asset_type != b.manifest.asset_type:
        out["notes"].append("different asset types: a price basis (e.g. futures vs cash CFD) is expected")
    if a.manifest.price_basis != b.manifest.price_basis:
        out["notes"].append("different price bases (bid/ask/mid/last): part of any difference is the spread")
    if len(common) < 2:
        out["notes"].append("fewer than 2 shared timestamps: no price comparison possible")
        return out
    ca, cb = a.bars.close[ia], b.bars.close[ib]
    diff = ca - cb
    # returns on each feed's OWN consecutive bars, compared where both have them
    # 1-bar returns only where BOTH feeds have the bar and its predecessor bar
    step = np.int64(a.bars.tf_minutes) * 60_000_000_000
    ok_a = np.r_[False, np.diff(ta) == step][ia]
    ok_b = np.r_[False, np.diff(tb) == step][ib]
    both = ok_a & ok_b
    corr = None
    if both.sum() > 2:
        xa = (a.bars.close[ia] - a.bars.close[np.maximum(ia - 1, 0)])[both]
        xb = (b.bars.close[ib] - b.bars.close[np.maximum(ib - 1, 0)])[both]
        if xa.std() > 0 and xb.std() > 0:
            corr = float(np.corrcoef(xa, xb)[0, 1])
    out.update({
        "overlap_start": str(pd.Timestamp(common[0], tz="UTC")),
        "overlap_end": str(pd.Timestamp(common[-1], tz="UTC")),
        "mean_close_diff_a_minus_b": float(diff.mean()),
        "median_abs_close_diff": float(np.median(np.abs(diff))),
        "p95_abs_close_diff": float(np.quantile(np.abs(diff), 0.95)),
        "bar_return_correlation": corr,
        "high_low_order_disagreement": int(((a.bars.high[ia] - b.bars.high[ib]) *
                                            (a.bars.low[ia] - b.bars.low[ib]) < 0).sum()),
    })
    return out
