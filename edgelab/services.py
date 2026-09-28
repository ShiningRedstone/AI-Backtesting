"""Backend service contracts for the future web application.

Every public method returns JSON-serializable dicts/lists (no numpy, no DataFrames),
so the same functions back the CLI today and HTTP endpoints later:

  DATA CENTER            list_datasets, dataset_detail, inspect_file, import_file, compare_feeds
  FEATURE LAB            feature_catalog, feature_detail, feature_cache_status, build_features,
                         feature_values (for charts)
  RESEARCH CONFIGURATION research_config_options
  STRATEGY LAB (Phase 3)  validate_strategy, preview_strategy, compile_strategy, save_strategy,
                         edit_strategy, duplicate_strategy, load_strategy, list_strategies,
                         strategy_families, strategy_lineage, generate_variations,
                         proposal_menu, ingest_proposals, backtest_strategy

Nothing here fabricates data: when something is unavailable the response says so. Nothing
here judges strategies: backtest results are returned with their sample-size labels only.
"""
from __future__ import annotations

import math
from pathlib import Path
from typing import Any, Mapping

import numpy as np

from edgelab.core.config import load_config
from edgelab.data.calendar import load_calendars
from edgelab.data.importer import ImportOptions, import_dataset, inspect_file, load_validated
from edgelab.data.store import open_store
from edgelab.features.cache import FeatureCache
from edgelab.features.engine import FeatureEngine
from edgelab.features.sessions import load_sessions
from edgelab.features.spec import FeatureSpec, all_defs, get_def
from edgelab.instruments import load_instruments


def _jsonable(x: Any) -> Any:
    if isinstance(x, dict):
        return {str(k): _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return [_jsonable(v) for v in x.tolist()]
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    if isinstance(x, np.integer):
        return int(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


class Services:
    def __init__(self, cfg: Mapping | None = None, root: str | Path = "."):
        self.cfg = cfg or load_config(Path(root) / "configs")
        self.root = Path(root)
        st = self.cfg["storage"]
        data_root = self.root / st.get("root", "data")
        self.store = open_store(self.cfg, root=data_root)
        fcfg = self.cfg.get("features", {})
        self.cache = FeatureCache(data_root / fcfg.get("cache_dir", "feature_cache"),
                                  verify=fcfg.get("verify_cache_checksums", True),
                                  memory_entries=fcfg.get("memory_cache_entries", 512))
        self.sessions = load_sessions(self.cfg)
        self.reports_dir = self.root / "reports" / "imports"
        from edgelab.strategy.lineage import StrategyLibrary
        self.library = StrategyLibrary(data_root / "strategy_library")

    # ============================================================ DATA CENTER
    def list_datasets(self) -> list[dict]:
        out = []
        for m in self.store.list_datasets():
            out.append({k: m.get(k) for k in (
                "dataset_id", "dataset_name", "provider", "asset_type", "instrument", "symbol",
                "timeframe", "start", "end", "n_bars", "quality_status", "content_hash",
                "volume_type", "has_spread", "price_basis", "parent_dataset_id", "missing_bars")})
        return _jsonable(out)

    def dataset_detail(self, dataset_id: str) -> dict:
        m = self.store.get_manifest(dataset_id)
        children = [d["dataset_id"] for d in self.store.list_datasets()
                    if d.get("parent_dataset_id") == dataset_id]
        return _jsonable({"manifest": m.to_dict(), "manifest_hash": m.manifest_hash(),
                          "validation_report": self.store.get_report(dataset_id),
                          "derived_datasets": children,
                          "limitations": self._limitations(m)})

    @staticmethod
    def _limitations(m) -> list[str]:
        out = []
        if m.volume_type in ("none",):
            out.append("no volume: VWAP / relative-volume features unavailable")
        if m.volume_type == "tick":
            out.append("tick volume (activity proxy), not exchange-traded volume")
        if not m.has_spread and m.asset_type == "CFD":
            out.append("no spread data: CFD costs must use a configured fixed spread")
        if m.price_basis == "unknown":
            out.append("price basis (bid/ask/mid/last) unknown")
        if m.asset_type == "SYNTHETIC" or m.provider.lower().startswith("synthetic"):
            out.append("SYNTHETIC data: engine testing only, no market conclusions")
        return out

    def inspect_file(self, options: Mapping) -> dict:
        return _jsonable(inspect_file(ImportOptions(**options), self.cfg))

    def import_file(self, options: Mapping) -> dict:
        r = import_dataset(ImportOptions(**options), self.cfg, self.store, self.cache, self.reports_dir)
        return _jsonable(r.to_dict())

    def load_dataset(self, dataset_id: str):
        return load_validated(self.store, self.cfg, dataset_id)

    def compare_feeds(self, a: str, b: str) -> dict:
        from edgelab.research.compare import compare_feeds
        return _jsonable(compare_feeds(self.load_dataset(a), self.load_dataset(b)))

    # ============================================================ FEATURE LAB
    def feature_catalog(self) -> list[dict]:
        return _jsonable([d.describe() for d in all_defs()])

    def feature_detail(self, feature_id: str) -> dict:
        return _jsonable(get_def(feature_id).describe())

    def feature_cache_status(self, dataset_id: str) -> dict:
        entries = self.cache.entries(dataset_id)
        return _jsonable({"dataset_id": dataset_id, "entries": len(entries),
                          "bytes": sum(e.get("bytes", 0) for e in entries),
                          "features": sorted({(e.get("label"), e.get("tf")) for e in entries}),
                          "detail": [{k: e.get(k) for k in ("label", "tf", "key", "created_at", "n",
                                                            "impl_hash", "bytes")} for e in entries]})

    def build_features(self, dataset_id: str, specs: list[Mapping] | None = None) -> dict:
        ds = self.load_dataset(dataset_id)
        eng = FeatureEngine.for_dataset(ds, self.sessions, self.cache)
        spec_dicts = specs or self.cfg.get("features", {}).get("default_set", [])
        built, skipped = [], {}
        for d in spec_dicts:
            s = FeatureSpec.from_dict(d)
            ok, why = eng.availability(s)
            if not ok:
                skipped[s.label] = why
                continue
            r = eng.compute(s)
            built.append({"label": s.label, "spec_id": s.spec_id, "cache_key": r.cache_key,
                          "cache": r.cache_source or "computed", "seconds": round(r.seconds, 4)})
        return _jsonable({"dataset_id": dataset_id, "built": built, "skipped": skipped})

    def feature_values(self, dataset_id: str, spec: Mapping, start: int = 0, count: int = 500) -> dict:
        """A window of feature values + bars (for Feature Lab charts)."""
        ds = self.load_dataset(dataset_id)
        eng = FeatureEngine.for_dataset(ds, self.sessions, self.cache)
        s = FeatureSpec.from_dict(spec)
        ok, why = eng.availability(s)
        if not ok:
            return {"available": False, "reason": why}
        r = eng.compute(s)
        sl = slice(start, start + count)
        b = ds.bars
        return _jsonable({"available": True, "spec": s.to_dict(), "label": s.label,
                          "known_at": r.known_at, "cache_key": r.cache_key,
                          "ts": [str(t) for t in b.ts[sl]],
                          "bars": {"open": b.open[sl], "high": b.high[sl], "low": b.low[sl],
                                   "close": b.close[sl]},
                          "values": {k: v[sl] for k, v in r.arrays.items()}})

    # ============================================================ RESEARCH CONFIGURATION
    def research_config_options(self) -> dict:
        insts = load_instruments(self.cfg)
        datasets = self.list_datasets()
        return _jsonable({
            "instruments": [{"symbol": k, "asset_class": v.asset_class, "underlying": v.underlying,
                             "calendar": v.calendar, "tick_size": v.tick_size} for k, v in insts.items()],
            "datasets": [{k: d[k] for k in ("dataset_id", "instrument", "asset_type", "provider",
                                           "timeframe", "start", "end")} for d in datasets],
            "timeframes_by_dataset": {d["dataset_id"]: d["timeframe"] for d in datasets},
            "sessions": {k: v.definition() for k, v in self.sessions.items()},
            "calendars": sorted(load_calendars(self.cfg)),
            "features": [{"id": d.feature_id, "version": d.version, "category": d.category,
                          "params": [p.describe() for p in d.params], "requires": list(d.requires)}
                         for d in all_defs()],
            "strategies": "NOT IMPLEMENTED (Phase 3: strategy DSL)",
        })

    # ============================================================ STRATEGY LAB (Phase 3)
    def _definition(self, src: Any) -> dict:
        """dict / YAML or JSON text / file path / stored strategy id -> raw definition."""
        from edgelab.strategy.dsl import load_definition
        if isinstance(src, str) and src.startswith("STR_") and "\n" not in src:
            return dict(self.library.load(src)["definition"])
        return load_definition(src)

    def _config_hash(self) -> str:
        from edgelab.core.config import config_hash
        return config_hash(self.cfg)

    def validate_strategy(self, src: Any) -> dict:
        from edgelab.strategy.dsl import identity, validate
        raw = self._definition(src)
        res = validate(raw, self.sessions)
        out = {"valid": res.valid, "errors": [i.to_dict() for i in res.errors],
               "warnings": [i.to_dict() for i in res.warnings], "report": res.report()}
        if res.valid:
            out["identity"] = identity(raw, self._all_sessions(raw)).to_dict()
        return _jsonable(out)

    def _all_sessions(self, raw: Mapping) -> dict:
        from edgelab.strategy.dsl import session_from_dict
        return {**self.sessions, **{n: session_from_dict(n, w) for n, w in (raw.get("sessions") or {}).items()}}

    def compile_strategy(self, src: Any) -> dict:
        from edgelab.strategy.compiler import compile_definition, explain
        c = compile_definition(self._definition(src), self.sessions, self._config_hash())
        return _jsonable({**c.summary(), "explain": explain(c), "logic": c.logic})

    def preview_strategy(self, src: Any) -> dict:
        from edgelab.strategy.compiler import compile_definition, explain
        from edgelab.strategy.dsl import canonical_definition
        raw = self._definition(src)
        c = compile_definition(raw, self.sessions, self._config_hash())
        return _jsonable({"identity": c.identity.to_dict(), "explain": explain(c),
                          "canonical_definition": canonical_definition(raw)})

    def save_strategy(self, src: Any, generation_method: str = "user",
                      parent_strategy_id: str | None = None) -> dict:
        from edgelab.strategy.compiler import COMPILER_VERSION, compile_definition
        from edgelab.strategy.dsl import DSL_VERSION, canonical_definition
        from edgelab.strategy.lineage import LineageRecord
        raw = self._definition(src)
        c = compile_definition(raw, self.sessions, self._config_hash())
        rec = LineageRecord(c.identity.strategy_id, c.identity.logic_hash, c.identity.definition_hash,
                            c.family_id, generation_method, parent_strategy_id,
                            versions={"dsl_version": DSL_VERSION, "compiler_version": COMPILER_VERSION,
                                      "config_hash": self._config_hash()})
        created = self.library.save(canonical_definition(raw), c.identity.to_dict(), rec)
        return _jsonable({**c.identity.to_dict(), "created": created,
                          "note": None if created else "identical logic already stored; lineage record added"})

    def edit_strategy(self, parent_strategy_id: str, new_src: Any) -> dict:
        self.library.load(parent_strategy_id)
        return self.save_strategy(new_src, "manual_edit", parent_strategy_id)

    def duplicate_strategy(self, strategy_id: str, new_name: str) -> dict:
        """Returns an editable DRAFT copy (not saved: identical logic would be the same strategy)."""
        d = dict(self.library.load(strategy_id)["definition"])
        d["name"] = new_name
        return _jsonable({"draft": d, "duplicated_from": strategy_id})

    def load_strategy(self, strategy_id: str) -> dict:
        return _jsonable(self.library.load(strategy_id))

    def list_strategies(self, family_id: str | None = None) -> list[dict]:
        return _jsonable(self.library.list(family_id))

    def strategy_families(self) -> dict:
        return _jsonable(self.library.families())

    def strategy_lineage(self, strategy_id: str) -> dict:
        doc = self.library.load(strategy_id)
        return _jsonable({"strategy_id": strategy_id, "records": doc["lineage"],
                          "ancestry": self.library.ancestry(strategy_id),
                          "children": self.library.children(strategy_id)})

    def generate_variations(self, base: Any, spec: Any, save: bool = True) -> dict:
        from edgelab.strategy.variations import generate_variations
        raw = self._definition(base)
        batch = generate_variations(raw, spec, self.sessions, self._config_hash())
        if save:
            self.save_strategy(raw)
            for v in batch.variants:
                self.library.save(v.definition, v.identity, v.lineage)
            self.library.save_batch(batch.record)
        return _jsonable({**batch.summary(), "saved": save})

    def proposal_menu(self, n_families: int = 20, instructions: str = "") -> dict:
        from edgelab.strategy.proposals import ProposalRequest
        return _jsonable(ProposalRequest(n_families, instructions).capability_menu(self.sessions))

    def ingest_proposals(self, batch: Any, save: bool = True) -> dict:
        from edgelab.strategy.proposals import ingest_proposals
        rep = ingest_proposals(self._definition(batch) if not isinstance(batch, Mapping) else batch,
                               self.sessions, self._config_hash())
        if save:
            for rec in rep.lineage:
                self.library.save(rep.definitions[rec.strategy_id], rep.identities[rec.strategy_id], rec)
        return _jsonable({**rep.to_dict(), "saved": save})

    def backtest_strategy(self, src: Any, dataset_id: str) -> dict:
        """One backtest through the existing engine (causality-checked). Returns measurements with
        sample-size labels; draws no conclusions. CFD datasets need configured broker costs."""
        from edgelab.analytics.metrics import compute_metrics
        from edgelab.engine.backtester import run_backtest
        from edgelab.engine.costs import cost_model_from_config
        from edgelab.features.strategy_api import FeatureContext
        from edgelab.strategy.compiler import compile_strategy
        ds = self.load_dataset(dataset_id)
        strat = compile_strategy(self._definition(src), self.sessions, self._config_hash())
        costs = cost_model_from_config(self.cfg, ds.instrument.symbol, provider=ds.manifest.provider)
        bound = strat.bind(FeatureContext(ds, self.sessions, self.cache))
        res = run_backtest(ds, bound, costs, self.cfg["backtest"], sizing=strat.sizing)
        met = compute_metrics(res.trades, sample_thresholds=self.cfg.get("sample_size"))
        return _jsonable({"strategy_id": strat.strategy_id, "dataset_id": dataset_id,
                          "dataset": {k: getattr(ds.manifest, k) for k in ("provider", "asset_type", "instrument",
                                                                           "timeframe", "start", "end")},
                          "cost_status": costs.status, "metrics": met, "signal_diagnostics": bound.last_diagnostics,
                          "skipped": dict(res.skipped),
                          "note": "historical result under the stated assumptions; not a conclusion"})
