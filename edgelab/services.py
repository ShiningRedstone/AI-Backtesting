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
  WEB UI (Phase 3.5)     system_status, builder_options, render_strategy, variation_preview,
                         archive_strategy, restore_strategy, list_variation_batches,
                         get_variation_batch, family_detail, backtest_readiness, list_runs,
                         get_run, list_import_files

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

    def _run_cell(self, src: Any, dataset_id: str, record: bool = False, *,
                  parent_strategy_id: str | None = None, mutation: str | None = None,
                  notes: str | None = None) -> dict:
        """One (strategy, dataset) cell through the existing engine: load (re-validated) ->
        compile -> cost model (CFD refusal) -> bind -> causality-checked backtest with the
        strategy's OWN sizing -> metrics -> optional run record. Returns the internal objects
        (not JSON); `backtest_strategy` and Phase 4 batch search both use this one path.
        `notes=None` keeps the Strategy Lab note (synthetic data is always labelled)."""
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
        synthetic = self._is_synthetic(ds.manifest)
        run_id = None
        if record:
            from edgelab.research.runs import record_run
            if synthetic:
                notes = "SYNTHETIC DEMONSTRATION - not evidence of trading performance"
            elif notes is None:
                notes = "single backtest (Strategy Lab)"
            run_id = record_run(self.store, self.cfg, res, met, notes=notes,
                                parent_strategy_id=parent_strategy_id, mutation=mutation)
        return {"ds": ds, "strategy": strat, "bound": bound, "costs": costs, "result": res,
                "metrics": met, "synthetic": synthetic, "run_id": run_id}

    def backtest_strategy(self, src: Any, dataset_id: str, record: bool = False) -> dict:
        """One backtest through the existing engine (causality-checked). Returns measurements with
        sample-size labels; draws no conclusions. CFD datasets need configured broker costs."""
        cell = self._run_cell(src, dataset_id, record)
        ds, res, met = cell["ds"], cell["result"], cell["metrics"]
        exits = res.trades["exit_reason"].value_counts().to_dict() if len(res.trades) else {}
        return _jsonable({"strategy_id": cell["strategy"].strategy_id, "dataset_id": dataset_id,
                          "run_id": cell["run_id"], "synthetic": cell["synthetic"], "exit_reasons": exits,
                          "n_signals": res.n_signals, "trades_hash": res.trades_hash,
                          "dataset": {k: getattr(ds.manifest, k) for k in ("provider", "asset_type", "instrument",
                                                                           "timeframe", "start", "end")},
                          "cost_status": cell["costs"].status, "metrics": met,
                          "signal_diagnostics": cell["bound"].last_diagnostics,
                          "skipped": dict(res.skipped),
                          "note": "historical result under the stated assumptions; not a conclusion"})

    # ============================================================ WEB UI (Phase 3.5)
    @staticmethod
    def _is_synthetic(m) -> bool:
        return m.asset_type == "SYNTHETIC" or str(m.provider).lower().startswith("synthetic")

    def system_status(self) -> dict:
        from edgelab.core.identity import code_version
        runs = self.store.list_runs()
        return _jsonable({"backend": "ok", "code_version": code_version(), "config_hash": self._config_hash(),
                          "store_backend": self.store.backend,
                          "datasets": len(self.store.list_datasets()),
                          "strategies": len(self.library.list()),
                          "archived_strategies": len(self.library.list(include_archived=True)) - len(self.library.list()),
                          "families": len(self.library.families()),
                          "variation_batches": len(self.library.list_batches()),
                          "runs": len(runs),
                          "last_run": None if runs.empty else runs.iloc[-1].to_dict()})

    def builder_options(self, timeframes: list[str] | None = None) -> dict:
        """Everything the visual builder may offer, straight from the backend's own tables."""
        from edgelab.data.schema import timeframe_minutes
        from edgelab.strategy import dsl
        tfs = sorted({f"{timeframe_minutes(t)}m" for t in (timeframes or ["1m", "5m", "15m", "30m", "60m"])}
                     | {d["timeframe"] for d in self.list_datasets() if d.get("timeframe")},
                     key=timeframe_minutes)
        htf = {t: [h for h in tfs if timeframe_minutes(h) >= timeframe_minutes(t)
                   and timeframe_minutes(h) % timeframe_minutes(t) == 0] for t in tfs}
        return _jsonable({
            "dsl_version": dsl.DSL_VERSION, "timeframes": tfs, "htf_options": htf,
            "comparison_operators": list(dsl.OPERATORS), "bar_fields": list(dsl.BAR_FIELDS),
            "arithmetic": list(dsl.ARITH_OPS), "directions": list(dsl.DIRECTIONS),
            "entry_order_types": list(dsl.ENTRY_ORDER_TYPES), "stop_types": list(dsl.STOP_TYPES),
            "target_types": list(dsl.TARGET_TYPES), "sizing_modes": list(dsl.SIZING_MODES),
            "parameter_types": list(dsl.PARAM_TYPES), "weekdays": list(dsl.WEEKDAY_NAMES),
            "unsupported": dict(dsl.UNSUPPORTED),
            "sessions": {k: v.definition() for k, v in self.sessions.items()},
            "features": [{**d.describe(), "session_params": list(d.session_params)}
                         for d in all_defs() if not d.feature_id.startswith("_")],
            "session_flatten": dict(self.cfg["backtest"].get("session", {})),
            "variation_modes": ["grid", "one_at_a_time", "random_sample"]})

    def render_strategy(self, src: Any) -> dict:
        """Live DSL preview: the draft as YAML/JSON (exactly what will be validated) plus, when
        valid, the backend canonical form and identity. Never raises on an invalid draft."""
        import yaml
        from edgelab.strategy.dsl import canonical_definition, validate
        raw = self._definition(src)
        res = validate(raw, self.sessions)
        out = {"yaml": yaml.safe_dump(raw, sort_keys=False, allow_unicode=True),
               "valid": res.valid, "errors": [i.to_dict() for i in res.errors],
               "warnings": [i.to_dict() for i in res.warnings], "canonical": None, "canonical_yaml": None,
               "identity": None}
        if res.valid:
            from edgelab.strategy.dsl import identity
            canon = canonical_definition(raw)
            out.update(canonical=canon, canonical_yaml=yaml.safe_dump(canon, sort_keys=False, allow_unicode=True),
                       identity=identity(raw, self._all_sessions(raw)).to_dict())
        return _jsonable(out)

    def variation_preview(self, base: Any, spec: Any) -> dict:
        """Count combinations and check the spec against the base WITHOUT generating."""
        from edgelab.strategy.dsl import load_definition, validate
        from edgelab.strategy.variations import MAX_GRID, _dim_values, combination_count, validate_spec
        raw = self._definition(base)
        base_res = validate(raw, self.sessions)
        if not base_res.valid:
            return _jsonable({"ok": False, "errors": [{"path": "base." + i.path, "message": i.message,
                                                       "hint": i.hint} for i in base_res.errors]})
        sp = load_definition(spec)
        res = validate_spec(sp, raw)
        out = {"ok": res.valid, "errors": [i.to_dict() for i in res.errors],
               "warnings": [i.to_dict() for i in res.warnings],
               "max_variants": sp.get("max_variants", 1000), "mode": sp.get("mode", "grid")}
        if res.valid:
            n = combination_count(sp)
            grid = 1
            for d in sp["dimensions"]:
                grid *= len(_dim_values(d))
            out.update(combinations=n, full_grid=grid,
                       values={d["parameter"]: _dim_values(d) for d in sp["dimensions"]})
            if n > out["max_variants"] or grid > MAX_GRID:
                out["ok"] = False
                out["errors"].append({"path": "max_variants", "severity": "error", "hint": "",
                                      "message": f"{n:,} combinations exceed max_variants {out['max_variants']:,}; "
                                                 "generation would be refused (never truncated)"})
        return _jsonable(out)

    def archive_strategy(self, strategy_id: str) -> dict:
        self.library.archive(strategy_id)
        return {"strategy_id": strategy_id, "archived": True}

    def restore_strategy(self, strategy_id: str) -> dict:
        self.library.restore(strategy_id)
        return {"strategy_id": strategy_id, "archived": False}

    def list_variation_batches(self) -> list[dict]:
        return _jsonable(self.library.list_batches())

    def get_variation_batch(self, batch_id: str) -> dict:
        b = self.library.load_batch(batch_id)
        children = []
        for sid in b.get("children", []):
            try:
                rec = self.library.load(sid)
            except KeyError:
                children.append({"strategy_id": sid, "missing": True})
                continue
            lin = next((r for r in rec["lineage"] if r.get("generation_batch_id") == batch_id), rec["lineage"][0])
            children.append({"strategy_id": sid, "name": rec.get("name"), "archived": rec.get("archived", False),
                             "overrides": (lin.get("generation_parameters") or {}).get("overrides", {}),
                             "changes": lin.get("changes", [])})
        return _jsonable({**b, "children_detail": children})

    def family_detail(self, family_id: str) -> dict:
        rows = self.library.list(family_id, include_archived=True)
        if not rows:
            raise KeyError(family_id)
        fam = {}
        for r in rows:
            fam = (self.library.load(r["strategy_id"])["definition"].get("family") or {})
            if fam.get("hypothesis"):
                break
        ids = {r["strategy_id"] for r in rows}
        nodes = []
        for r in rows:
            recs = self.library.load(r["strategy_id"])["lineage"]
            nodes.append({**r, "parents": sorted({x.get("parent_strategy_id") for x in recs
                                                  if x.get("parent_strategy_id")}),
                          "changes": recs[0].get("changes", [])})
        return _jsonable({"family_id": family_id, "family": fam, "instances": nodes,
                          "roots": [n["strategy_id"] for n in nodes if not (set(n["parents"]) & ids)]})

    def backtest_readiness(self, strategy_src: Any = None) -> dict:
        """Every dataset with the reasons it can or cannot run the given strategy (nothing invented)."""
        from edgelab.data.schema import timeframe_minutes
        tf = None
        if strategy_src is not None:
            raw = self._definition(strategy_src)
            try:
                tf = timeframe_minutes(str(raw.get("timeframe")))
            except ValueError:
                tf = None
        out = [self._dataset_eligibility(d, tf) for d in self.list_datasets()]
        return _jsonable({"strategy_timeframe": None if tf is None else f"{tf}m", "datasets": out})

    def _dataset_eligibility(self, d: Mapping, tf_minutes: int | None = None) -> dict:
        """Whether one dataset (a `list_datasets` row) can run a strategy on `tf_minutes` bars
        (None = timeframe not checked), with every reason it cannot. Nothing is invented: an
        unconfigured cost profile makes the dataset ineligible."""
        from edgelab.engine.costs import CostConfigError, cost_model_from_config
        from edgelab.data.schema import timeframe_minutes
        m = self.store.get_manifest(d["dataset_id"])
        reasons = []
        try:
            cm = cost_model_from_config(self.cfg, m.instrument, provider=m.provider)
            cost = {"status": cm.status, "profile": getattr(cm, "profile", None)}
        except CostConfigError as exc:
            cost = {"status": "unconfigured", "reason": str(exc)}
            reasons.append("broker/provider cost profile is unconfigured - configure verified costs first")
        except KeyError as exc:
            cost = {"status": "unconfigured", "reason": f"no cost profile for {exc}"}
            reasons.append("no cost profile for this instrument")
        if d.get("quality_status") == "FAIL":
            reasons.append("dataset failed validation")
        if tf_minutes is not None and timeframe_minutes(d["timeframe"]) != tf_minutes:
            reasons.append(f"timeframe {d['timeframe']} does not match the strategy timeframe {tf_minutes}m")
        return {**d, "cost": cost, "synthetic": self._is_synthetic(m),
                "limitations": self._limitations(m), "runnable": not reasons, "reasons": reasons}

    def list_runs(self) -> list[dict]:
        rows = []
        for r in self.store.list_runs().to_dict("records"):
            rec, _ = self.store.load_run(r["run_id"])
            ds = rec.get("dataset", {})
            rows.append({**r, "strategy_name": rec.get("strategy", {}).get("dsl", {}).get("name"),
                         "notes": rec.get("notes"), "synthetic": str(rec.get("notes", "")).startswith("SYNTHETIC"),
                         "instrument": ds.get("instrument"), "timeframe": ds.get("timeframe"),
                         "headline_metrics": rec.get("headline_metrics", {})})
        return _jsonable(rows)

    def get_run(self, run_id: str, max_trades: int = 500) -> dict:
        rec, trades = self.store.load_run(run_id)
        rec = {k: v for k, v in rec.items() if k not in ("config", "environment")}
        t = trades.head(max_trades)
        for c in t.columns:
            if str(t[c].dtype).startswith("datetime"):
                t[c] = t[c].astype(str)
        return _jsonable({"record": rec, "synthetic": str(rec.get("notes", "")).startswith("SYNTHETIC"),
                          "n_trades": len(trades), "trades_shown": len(t),
                          "trades": t.to_dict("records")})

    def list_import_files(self, import_dirs: list[str]) -> list[dict]:
        out = []
        for d in import_dirs:
            base = (self.root / d).resolve()
            if not base.is_dir():
                continue
            for p in sorted(base.rglob("*")):
                if p.is_file() and p.suffix.lower() in (".csv", ".txt"):
                    out.append({"path": str(p.relative_to(self.root.resolve())), "bytes": p.stat().st_size})
        return out
