"""Backend service contracts for the future web application.

Every public method returns JSON-serializable dicts/lists (no numpy, no DataFrames),
so the same functions back the CLI today and HTTP endpoints later:

  DATA CENTER            list_datasets, dataset_detail, inspect_file, import_file, compare_feeds
  FEATURE LAB            feature_catalog, feature_detail, feature_cache_status, build_features,
                         feature_values (for charts)
  RESEARCH CONFIGURATION research_config_options (incl. Phase 4 search spec options)
  STRATEGY LAB (Phase 3)  validate_strategy, preview_strategy, compile_strategy, save_strategy,
                         edit_strategy, duplicate_strategy, load_strategy, list_strategies,
                         strategy_families, strategy_lineage, generate_variations,
                         proposal_menu, ingest_proposals, backtest_strategy
  WEB UI (Phase 3.5)     system_status, builder_options, render_strategy, variation_preview,
                         archive_strategy, restore_strategy, list_variation_batches,
                         get_variation_batch, family_detail, backtest_readiness, list_runs,
                         get_run, list_import_files
  RESEARCH (Phase 4)     validate_search, plan_search, run_search, list_searches, get_search,
                         rank_search, select_shortlist, start_search_job, job_status, cancel_job
  STRATEGY LAB (Phase 8) strategy_research, compare_runs, run_curve, oos_random_control (read models
                         over stored strategies/runs; variation_preview lists exact combinations)
  PROP (Phase 6)         prop_configs, validate_prop_config, prop_simulate, list_prop_simulations,
                         get_prop_simulation (account rules over a stored run's trades; read-only)

Nothing here fabricates data: when something is unavailable the response says so. Nothing
here judges strategies: backtest results are returned with their sample-size labels only.
"""
from __future__ import annotations

import contextvars
import json
import math
import re
import threading
from contextlib import contextmanager, nullcontext
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

from edgelab.core.config import load_config
from edgelab.core.fsutil import atomic_write_text
from edgelab.data.calendar import load_calendars
from edgelab.data.importer import ImportOptions, import_dataset, inspect_file, load_validated
from edgelab.data.store import open_store
from edgelab.features.cache import FeatureCache
from edgelab.features.engine import FeatureEngine
from edgelab.features.sessions import load_sessions
from edgelab.features.spec import FeatureSpec, all_defs, get_def
from edgelab.instruments import contract_for, load_instruments


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


_READ_STORE: contextvars.ContextVar = contextvars.ContextVar("edgelab_read_store", default=None)


def _paper_balance(st: Mapping) -> float | None:
    """Current paper balance: the latest attempt's evaluation or funded balance (None before the first trade)."""
    att = (st or {}).get("attempts") or []
    return att[-1].get("balance") if att else None


class Services:
    # ADR-78: inside `read_context()` (page requests) `store` is a pooled READ-ONLY connection, so reads never wait
    # for the service lock a research run holds; everywhere else it is the one writer connection. The writer lives in
    # the instance __dict__ under "store" (assignment and mock.patch.object keep working).
    @property
    def store(self):
        rs = _READ_STORE.get()
        return rs[1] if rs is not None and rs[0] is self else self.__dict__["store"]

    @store.setter
    def store(self, value) -> None:
        self.__dict__["store"] = value

    @property
    def writer_store(self):
        return self.__dict__["store"]

    @contextmanager
    def read_context(self, page: bool = True):
        """Run reads on a pooled read-only connection, without the service lock (yields False when the store has no
        read-only connections, e.g. in-memory or DuckDB: the caller then takes the lock as before). `page`: a page
        request (its views may reuse a cache key for a few seconds during a research run, see campaign.db_token);
        research code passes page=False and always reads exact values."""
        writer = self.__dict__["store"]
        acquire = getattr(writer, "acquire_reader", None)
        reader = acquire() if acquire is not None else None
        if reader is None:
            yield False
            return
        token = _READ_STORE.set((self, reader, page))
        try:
            yield True
        finally:
            _READ_STORE.reset(token)
            writer.release_reader(reader)

    @staticmethod
    def in_read_context() -> bool:
        return _READ_STORE.get() is not None

    @staticmethod
    def in_page_read() -> bool:
        rs = _READ_STORE.get()
        return rs is not None and bool(rs[2])

    def __init__(self, cfg: Mapping | None = None, root: str | Path = "."):
        self.cfg = cfg or load_config(Path(root) / "configs")
        self.root = Path(root)
        st = self.cfg["storage"]
        data_root = self.root / st.get("root", "data")
        self.data_root = data_root
        self.store = open_store(self.cfg, root=data_root)
        fcfg = self.cfg.get("features", {})
        self.cache = FeatureCache(data_root / fcfg.get("cache_dir", "feature_cache"),
                                  verify=fcfg.get("verify_cache_checksums", True),
                                  memory_entries=fcfg.get("memory_cache_entries", 512))
        self.sessions = load_sessions(self.cfg)
        self.reports_dir = self.root / "reports" / "imports"
        from edgelab.strategy.lineage import StrategyLibrary
        self.library = StrategyLibrary(data_root / "strategy_library")
        # THE service lock: the web app serializes every service call with it and the Phase 4 job
        # manager takes it only around short store operations (never during a backtest).
        self.lock = threading.RLock()
        self._jobs = None

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
                          "identity": (idn := self._instrument_identity(m.instrument)),
                          "preferred": self.preferred_dataset_id() == dataset_id,
                          "limitations": self._limitations(m) + ([idn["calendar_caveat"]] if idn.get("calendar_caveat") else [])})

    @staticmethod
    def _limitations(m) -> list[str]:
        out = []
        if m.volume_type in ("none",):
            out.append("no volume: VWAP / relative-volume features unavailable")
        if m.volume_type == "tick":
            out.append("tick volume (activity proxy), not exchange-traded volume")
        if m.volume_type == "unknown":
            out.append("volume semantics unknown (provider-defined measure) - not exchange-traded volume")
        if not m.has_spread and m.asset_type == "CFD":
            out.append("no spread data: CFD costs must use a configured fixed spread")
        if m.price_basis == "unknown":
            out.append("price basis (bid/ask/mid/last) unknown")
        if m.asset_type == "SYNTHETIC" or m.provider.lower().startswith("synthetic"):
            out.append("SYNTHETIC data: engine testing only, no market conclusions")
        return out

    def dataset_quality(self, dataset_id: str) -> dict:
        """Descriptive gap classification + coverage of a stored dataset (read-only; nothing excluded)."""
        from edgelab.data.quality import gap_analysis
        return _jsonable({"dataset_id": dataset_id, **gap_analysis(self.load_dataset(dataset_id))})

    # ------------------------------------------------------------ preferred research dataset
    def preferred_dataset_id(self) -> str | None:
        """The workspace's Preferred Research Dataset, or None (unset, or no longer stored)."""
        from edgelab.data.preferences import load_prefs
        cur = (load_prefs(self.data_root).get("preferred_research_dataset") or {}).get("dataset_id")
        if cur and any(d["dataset_id"] == cur for d in self.store.list_datasets()):
            return cur
        return None

    def preferred_dataset(self) -> dict:
        """The preference with its current dataset row (eligibility, identity, cost status)."""
        from edgelab.data.preferences import load_prefs, prefs_path
        prefs = load_prefs(self.data_root)
        rec = prefs.get("preferred_research_dataset")
        row = None
        state = "unset"
        if rec:
            rows = [d for d in self.list_datasets() if d["dataset_id"] == rec["dataset_id"]]
            state = "set" if rows else "missing"
            if rows:
                row = self._dataset_eligibility(rows[0])
        return _jsonable({"preferred": rec, "state": state, "dataset": row, "stored_at": str(prefs_path(self.data_root)),
                          "history": prefs.get("preferred_history", []),
                          "note": "default for NEW research only; existing runs are never changed"})

    def set_preferred_dataset(self, dataset_id: str) -> dict:
        """Set the workspace default. Only a stored dataset that passes validation (PASS/WARN) and
        re-validates on load (content hash re-checked) is eligible. Changes no stored research."""
        from datetime import datetime, timezone
        from edgelab.data.preferences import HISTORY_LIMIT, load_prefs, save_prefs
        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError("dataset_id is required")
        rows = [d for d in self.list_datasets() if d["dataset_id"] == dataset_id]
        if not rows:
            raise KeyError(f"dataset {dataset_id} is not stored in this workspace")
        if rows[0].get("quality_status") == "FAIL":
            raise ValueError(f"dataset {dataset_id} failed validation and cannot be the preferred research dataset")
        ds = self.load_dataset(dataset_id)                  # re-validates + re-checks the content hash
        m = ds.manifest
        rec = {"dataset_id": dataset_id, "set_at": datetime.now(timezone.utc).isoformat(),
               "content_hash": m.content_hash, "manifest_hash": m.manifest_hash(), "provider": m.provider,
               "instrument": m.instrument, "timeframe": m.timeframe, "quality_status": m.quality_status,
               "synthetic": self._is_synthetic(m)}
        prefs = load_prefs(self.data_root)
        prev = prefs.get("preferred_research_dataset")
        hist = list(prefs.get("preferred_history", []))
        hist.append({"dataset_id": dataset_id, "set_at": rec["set_at"],
                     "previous": prev.get("dataset_id") if prev else None})
        prefs.update(preferred_research_dataset=rec, preferred_history=hist[-HISTORY_LIMIT:])
        save_prefs(self.data_root, prefs)
        return self.preferred_dataset()

    def clear_preferred_dataset(self) -> dict:
        from datetime import datetime, timezone
        from edgelab.data.preferences import HISTORY_LIMIT, load_prefs, save_prefs
        prefs = load_prefs(self.data_root)
        prev = prefs.pop("preferred_research_dataset", None)
        if prev:
            hist = list(prefs.get("preferred_history", []))
            hist.append({"dataset_id": None, "set_at": datetime.now(timezone.utc).isoformat(),
                         "previous": prev.get("dataset_id")})
            prefs["preferred_history"] = hist[-HISTORY_LIMIT:]
            save_prefs(self.data_root, prefs)
        return self.preferred_dataset()

    # ------------------------------------------------------------ display preference: risk per trade (ADR-73)
    DEFAULT_RISK_PER_TRADE_USD = 250.0

    def risk_per_trade(self) -> dict:
        """The dollar amount one R stands for in the results views (display only: R x amount). Stored in the workspace
        preferences, outside the research config and its hash; it never changes a backtest, its sizing or any record."""
        from edgelab.data.preferences import load_prefs
        v = load_prefs(self.data_root).get("risk_per_trade_usd")
        val = float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else self.DEFAULT_RISK_PER_TRADE_USD
        return {"risk_per_trade_usd": val, "default": val == self.DEFAULT_RISK_PER_TRADE_USD and v is None,
                "note": "dollar figures in the results views are R multiplied by this amount; backtests are unchanged"}

    def set_risk_per_trade(self, value: Any) -> dict:
        from edgelab.data.preferences import load_prefs, save_prefs
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not 0 < float(value) <= 1_000_000:
            raise ValueError("risk per trade must be a dollar amount between 0 and 1,000,000")
        prefs = load_prefs(self.data_root)
        prefs["risk_per_trade_usd"] = round(float(value), 2)
        save_prefs(self.data_root, prefs)
        return self.risk_per_trade()

    # ------------------------------------------------------------ display preferences (ADR-74; outside the config hash)
    UI_PREF_DEFAULTS = {"favorites": [], "prop_criteria_profile": "LUCID_LUCIDFLEX_50K", "show_ids": False,
                        "show_readonly": False, "research_processes": None,       # None = all cores but one (ADR-77)
                        "prop_fees": {}}                                          # ADR-81: per rule profile, USD

    def ui_preferences(self) -> dict:
        """Favorites, the prop account for pass criteria and the two display switches. Workspace preferences only:
        never part of a run record, a strategy, a dataset or the research config hash; no backtest reads them."""
        from edgelab.data.preferences import load_prefs
        raw = load_prefs(self.data_root).get("ui") or {}
        out = {k: raw.get(k, v) for k, v in self.UI_PREF_DEFAULTS.items()}
        out["favorites"] = [x for x in out["favorites"] if isinstance(x, str)]
        return out

    def _check_prop_fees(self, v) -> dict:
        """{profile_id: {eval_price, reset_fee, activation_fee}}: non-negative USD amounts or null (ADR-81)."""
        from edgelab.prop.service import default_profiles
        if not isinstance(v, Mapping):
            raise ValueError("prop_fees must be an object {profile_id: {eval_price, reset_fee, activation_fee}}")
        known = {p["profile_id"] for p in default_profiles(self.root)}
        out = {}
        for pid, f in v.items():
            if pid not in known:
                raise ValueError(f"unknown prop rule profile {pid!r}")
            if not isinstance(f, Mapping) or set(f) - {"eval_price", "reset_fee", "activation_fee"}:
                raise ValueError("each fee entry may only have eval_price, reset_fee, activation_fee")
            row = {}
            for k in ("eval_price", "reset_fee", "activation_fee"):
                x = f.get(k)
                if x in (None, ""):
                    row[k] = None
                    continue
                if isinstance(x, bool) or not isinstance(x, (int, float)) or not math.isfinite(float(x)) or x < 0:
                    raise ValueError(f"{pid}.{k} must be a non-negative amount in USD or empty")
                row[k] = float(x)
            out[pid] = row
        return out

    def prop_profile_choices(self) -> list[dict]:
        """The configured prop rule profiles with plain names (for the pass-criteria choice)."""
        from edgelab.research.results_view import profile_names
        return [{"profile_id": k, "name": v} for k, v in profile_names(self).items()]

    def set_ui_preferences(self, changes: Mapping) -> dict:
        from edgelab.data.preferences import load_prefs, save_prefs
        if not isinstance(changes, Mapping):
            raise ValueError("preferences must be an object")
        cur = self.ui_preferences()
        for k, v in changes.items():
            if k not in self.UI_PREF_DEFAULTS or k == "favorites":
                raise ValueError(f"unknown or read-only preference {k!r}")
            if k in ("show_ids", "show_readonly"):
                if not isinstance(v, bool):
                    raise ValueError(f"{k} must be true or false")
            elif k == "prop_fees":
                v = self._check_prop_fees(v)
            elif k == "research_processes":
                from edgelab.research.campaign import max_processes
                if v is not None and (not isinstance(v, int) or isinstance(v, bool) or not 1 <= v <= max_processes()):
                    raise ValueError(f"research_processes must be null (automatic) or 1..{max_processes()}")
            elif k == "prop_criteria_profile":
                from edgelab.prop.service import default_profiles
                if v not in {p["profile_id"] for p in default_profiles(self.root)}:
                    raise ValueError(f"unknown prop rule profile {v!r}")
            cur[k] = v
        prefs = load_prefs(self.data_root)
        prefs["ui"] = cur
        save_prefs(self.data_root, prefs)
        return self.ui_preferences()

    def set_favorite(self, strategy_id: str, on: bool) -> dict:
        """Star / unstar a TESTED strategy (one with a stored backtest). Display metadata only."""
        from edgelab.data.preferences import load_prefs, save_prefs
        if not isinstance(on, bool):
            raise ValueError("favorite must be true or false")
        self.library.load(strategy_id)                       # KeyError for an unknown strategy
        if on and not self.store._query("SELECT 1 FROM runs WHERE strategy_id = ? LIMIT 1",
                                                      (strategy_id,)):
            raise ValueError("only tested strategies (with a stored backtest) can be favorites")
        cur = self.ui_preferences()
        fav = [x for x in cur["favorites"] if x != strategy_id] + ([strategy_id] if on else [])
        prefs = load_prefs(self.data_root)
        prefs["ui"] = {**cur, "favorites": fav}
        save_prefs(self.data_root, prefs)
        return self.ui_preferences()

    def favorites_info(self) -> dict:
        """Favorites and the ids of every tested strategy (for the star buttons)."""
        # the runs.strategy_id column is written from the record's strategy id (store.save_run): no JSON parsing
        rows = self.store._query("SELECT DISTINCT strategy_id FROM runs")
        return {"favorites": self.ui_preferences()["favorites"], "tested": sorted(r[0] for r in rows if r[0])}

    def campaign_run_names(self, campaign_id: str) -> dict:
        """User-given names of research runs ({run record id: name}), kept beside the run records (never inside them)."""
        from edgelab.research import campaign
        p = campaign.runs_dir(self, campaign_id) / "names.json"
        try:
            d = json.loads(p.read_text(encoding="utf-8")) if p.is_file() else {}
        except (OSError, ValueError):
            d = {}
        return {k: v for k, v in d.items() if isinstance(k, str) and isinstance(v, str)}

    def rename_campaign_run(self, campaign_id: str, run_record_id: str, name: Any) -> dict:
        from edgelab.research import campaign
        if not isinstance(name, str) or len(name.strip()) > 80:
            raise ValueError("the run name must be text of at most 80 characters")
        ids = {r["run_record_id"] for r in campaign.run_records(self, campaign_id)}
        if run_record_id not in ids:
            raise KeyError(f"research run {run_record_id} is not stored in campaign {campaign_id}")
        names = self.campaign_run_names(campaign_id)
        if name.strip():
            names[run_record_id] = name.strip()
        else:
            names.pop(run_record_id, None)
        d = campaign.runs_dir(self, campaign_id)
        d.mkdir(parents=True, exist_ok=True)
        atomic_write_text(d / "names.json", json.dumps(names, indent=1, sort_keys=True))
        return {"campaign_id": campaign_id, "run_record_id": run_record_id, "name": names.get(run_record_id)}

    RESET_TABLES = ("trades", "metrics", "runs", "search_cells", "search_batches", "protocol_trials", "protocol_proposals",
                    "holdout_access", "research_protocols")
    RESET_DIRS = ("strategy_library", "campaigns", "strategy_factory", "controls", "prop_bootstrap", "prop_simulations",
                  "ai_discovery")

    def reset_workspace(self, confirm: Any) -> dict:
        """Delete every strategy and research result of this workspace and KEEP the price data (datasets, bars, their
        validation reports, the import folder and the feature cache) plus configs and settings. Deleted: the strategy
        library, backtests and their trades, searches, research campaigns with their frozen strategy manifests, random
        controls, prop simulations, evaluation-simulator cache, AI generations, research protocols with their trial
        ledger and holdout log, favorites and run names. Irreversible; requires confirm == "DELETE"; refused while a
        background job runs. Every reset is appended to <data>/workspace_reset_log.jsonl (what, when, how many)."""
        import shutil
        from datetime import datetime, timezone
        from edgelab.data.preferences import load_prefs, save_prefs
        if confirm != "DELETE":
            raise ValueError('type DELETE to confirm deleting all strategies and results')
        if not hasattr(self.store, "con"):
            raise ValueError("deleting results is only supported for the SQLite store")
        if self._jobs is not None and self._jobs.active():
            raise ValueError("a background job is running; wait for it to finish or cancel it first")
        from edgelab.prop import bootstrap as bs
        if any(j.get("state") == "running" for j in bs._jobs.values()):
            raise ValueError("an evaluation simulation is running; wait for it to finish first")
        with self.lock:
            existing = {r[0] for r in self.store._query("SELECT name FROM sqlite_master WHERE type='table'")}
            counts = {}
            for t in self.RESET_TABLES:
                if t in existing:
                    counts[t] = self.store._query(f"SELECT COUNT(*) FROM {t}")[0][0]
                    self.store.con.execute(f"DELETE FROM {t}")
            self.store.con.commit()
            removed = []
            for name in self.RESET_DIRS:
                d = self.data_root / name
                if d.is_dir():
                    shutil.rmtree(d)
                    removed.append(name)
            from edgelab.paper import store as paper_store       # ADR-81: paper accounts are results; the feed is price data
            if paper_store.delete_records(self.data_root):
                removed.append("paper/accounts")
            prefs = load_prefs(self.data_root)
            if prefs.get("ui"):
                prefs["ui"] = {**prefs["ui"], "favorites": []}
                save_prefs(self.data_root, prefs)
            from edgelab.strategy.lineage import StrategyLibrary
            self.library = StrategyLibrary(self.data_root / "strategy_library")
            from edgelab.research import overview as ov, results_view as rv
            ov._FACET_CACHE.clear()
            self.__dict__.pop("_calendar_trades", None)     # ADR-77: run ids restart after a reset
            rv._GROSS.clear()
            entry = {"at": datetime.now(timezone.utc).isoformat(), "deleted_rows": counts, "deleted_folders": removed,
                     "kept": "datasets, bars, dataset reports, import folder, feature cache, configs, settings"}
            with open(self.data_root / "workspace_reset_log.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry) + "\n")
        return _jsonable(entry)

    def inspect_file(self, options: Mapping) -> dict:
        return _jsonable(inspect_file(ImportOptions(**options), self.cfg))

    def import_file(self, options: Mapping) -> dict:
        r = import_dataset(ImportOptions(**options), self.cfg, self.store, self.cache, self.reports_dir)
        return _jsonable(r.to_dict())

    def load_dataset(self, dataset_id: str):
        return load_validated(self.store, self.cfg, dataset_id)

    # ADR-77: datasets for backtest / research cells, loaded and validated ONCE per app process. Stored datasets are
    # content-addressed and immutable, and a ValidatedDataset's arrays are read-only; the key is the stored manifest
    # (re-import or replacement changes it), the store object (workspace switch) and the research config hash
    # (instruments, calendars, validation thresholds). The first load of a process always goes through the full
    # `load_validated` gate (stored bars re-hashed and re-validated), as the parallel search parent already does once
    # per search (ADR-40); every backtest still re-hashes the bars it receives (`run_backtest` -> verify_unchanged).
    _CELL_DATASETS_MAX = 3

    def _cell_dataset(self, dataset_id: str, period: tuple | None = None, lock=None):
        guard = lock if lock is not None else nullcontext()
        with guard:                                                   # store reads only under the lock
            rows = self.store._query("SELECT manifest_json FROM datasets WHERE dataset_id = ?", (dataset_id,))
            if not rows:
                return self.load_dataset(dataset_id)                  # the gate raises the usual error
            key = (id(self.writer_store), rows[0][0], self._config_hash(),
                   None if period is None else (str(period[0]), str(period[1])))
            memo = self.__dict__.setdefault("_cell_ds", {})
            hit = memo.get(key)
            if hit is not None and hit[0] is self.writer_store:
                memo[key] = memo.pop(key)                             # most recently used last
                return hit[1]
            if period is None:
                ds = self.load_dataset(dataset_id)
        if period is not None:                                        # the window is cut and re-validated outside
            from edgelab.research.compare import restrict_to_period
            ds = restrict_to_period(self._cell_dataset(dataset_id, None, lock), period[0], period[1],
                                    self.cfg.get("validation"))
        with guard:
            memo[key] = (self.writer_store, ds)
            while len(memo) > 2 * self._CELL_DATASETS_MAX:           # full datasets plus their discovery windows
                memo.pop(next(iter(memo)))
        return ds

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
            "strategies": [{k: r[k] for k in ("strategy_id", "name", "family_id", "timeframe", "generation_method")}
                           for r in self.library.list()],
            "search": self._search_options(),
        })

    def _search_options(self) -> dict:
        """What a Phase 4 search spec may contain, from the search module's own tables."""
        from edgelab.research import search as rs
        return {"search_spec_version": rs.SEARCH_SPEC_VERSION, "keys": sorted(rs.SPEC_KEYS),
                "strategy_sources": {k: v for k, v in sorted(rs.SOURCE_KEYS.items())},
                "variation_batches": [b["batch_id"] for b in self.library.list_batches()],
                "proposal_batches": [b["batch_id"] for b in self.library.list_batches("proposal")],
                "families": sorted(self.library.families()),
                "period": ["omitted (full datasets)", "common", "{start, end} with an explicit timezone"],
                "ranking_metrics": list(rs.RANKING_METRICS), "sample_labels": list(rs.SAMPLE_LABELS),
                "refused_ranking_metrics": ["win_rate"], "defaults": rs.DEFAULTS,
                "not_in_search_hash": list(rs.NOT_HASHED), "background_job_workers": 1,
                "storage": "SQLite result store only", "example": "configs/search.example.yaml"}

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

    def list_strategies(self, family_id: str | None = None, include_archived: bool = False) -> list[dict]:
        """Library rows plus readable names (``display_name`` / ``short_name``; presentation only)."""
        from edgelab.research import overview as ov
        names = {f["strategy_id"]: f for f in ov.library_facets(self)}
        out = []
        for r in self.library.list(family_id, include_archived):
            f = names.get(r["strategy_id"])
            n = {"display_name": f["display_name"], "short_name": f["short_name"]} if f else ov.display_names(
                {"name": r.get("name"), "definition": {"name": r.get("name")}, "family_id": r.get("family_id")})
            out.append({**r, **n})
        return _jsonable(out)

    def strategy_families(self) -> dict:
        return _jsonable(self.library.families())

    def strategy_lineage(self, strategy_id: str) -> dict:
        doc = self.library.load(strategy_id)
        return _jsonable({"strategy_id": strategy_id, "records": doc["lineage"],
                          "ancestry": self.library.ancestry(strategy_id),
                          "children": self.library.children(strategy_id)})

    # ------------------------------------------------------------ prop lifecycle (ADR-64)
    def prop_profiles(self) -> dict:
        from edgelab.prop.service import profile_status
        return _jsonable(profile_status(self.root))

    def prop_lifecycle(self, run_id: str, profile_ids=None) -> dict:
        from edgelab.prop.service import lifecycle_for_run
        return _jsonable(lifecycle_for_run(self, run_id, profile_ids))

    # ------------------------------------------------------------ strategy factory (ADR-60)
    # Generation only: no data, no backtests, no protocol / trial ledger / holdout access.
    @property
    def factory_dir(self) -> Path:
        return self.data_root / "strategy_factory"

    def _factory_manifest_dir(self, manifest_id: str) -> Path:
        if not re.fullmatch(r"FM_[0-9A-F]{16}", str(manifest_id)):
            raise ValueError(f"invalid manifest id {manifest_id!r}")
        d = self.factory_dir / manifest_id
        if not (d / "manifest.json").exists():
            raise KeyError(f"unknown factory manifest {manifest_id}")
        return d

    def factory_generate(self, seed: int | None = None, quotas: Mapping[str, int] | None = None) -> dict:
        from edgelab.strategy import factory
        res = factory.generate(factory.DEFAULT_SEED if seed is None else int(seed), quotas)
        d = factory.write_manifest(res, self.factory_dir / res.manifest_id)
        return _jsonable({"manifest_id": res.manifest_id, "path": str(d), "counts": res.header["counts"]})

    def factory_manifests(self) -> list[dict]:
        from edgelab.strategy import factory
        out = []
        for d in sorted(self.factory_dir.glob("FM_*")) if self.factory_dir.exists() else []:
            if (d / "manifest.json").exists():
                h = factory.read_header(d)
                out.append({"manifest_id": h["manifest_id"], "seed": h["identity"]["seed"],
                            "factory_version": h["identity"]["factory_version"],
                            "full_allocation": h["identity"]["full_allocation"],
                            "valid_unique": h["counts"]["valid_unique"]})
        return out

    def factory_summary(self, manifest_id: str) -> dict:
        from edgelab.strategy import factory
        return _jsonable(factory.summary(self._factory_manifest_dir(manifest_id)))

    def factory_query(self, manifest_id: str, filters: Mapping | None = None, limit: int = 100,
                      offset: int = 0) -> dict:
        from edgelab.strategy import factory
        return _jsonable(factory.query(self._factory_manifest_dir(manifest_id), filters,
                                       max(1, min(int(limit), 1000)), max(0, int(offset))))

    def factory_verify(self, manifest_id: str) -> dict:
        from edgelab.strategy import factory
        d = self._factory_manifest_dir(manifest_id)
        if (factory.read_header(d)["identity"].get("pool") or {}).get("pool") == 2:      # ADR-86: pool 2 excludes pool 1
            from edgelab.research import pool2
            return _jsonable(pool2.verify(self, manifest_id))
        return _jsonable(factory.verify(d))

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

    # ------------------------------------------------------------ AI Discovery (Phase 9)
    def _user_settings(self) -> dict:
        from edgelab import runtime
        try:
            return runtime.load_settings()
        except Exception:                                    # noqa: BLE001 - settings are optional here
            return {}

    def ai_status(self) -> dict:
        from edgelab.ai.providers import provider_status
        from edgelab.ai.schema import MAX_PROPOSALS, MODES, PROPOSAL_SCHEMA_VERSION, REQUEST_VERSION, TEMPLATES
        from edgelab.features.spec import all_defs
        from edgelab.strategy import dsl
        return _jsonable({**provider_status(user_settings=self._user_settings()),
                          "request_version": REQUEST_VERSION, "proposal_schema_version": PROPOSAL_SCHEMA_VERSION,
                          "modes": list(MODES), "templates": TEMPLATES, "max_proposals": MAX_PROPOSALS,
                          "features": [d.feature_id for d in all_defs() if not d.feature_id.startswith("_")],
                          "sessions": sorted(self.sessions), "directions": list(dsl.DIRECTIONS),
                          "entry_orders": list(dsl.ENTRY_ORDER_TYPES), "stop_types": list(dsl.STOP_TYPES),
                          "target_types": list(dsl.TARGET_TYPES), "sizing_modes": list(dsl.SIZING_MODES),
                          "exit_kinds": ["stop", "target", "time_stop_bars", "max_hold_bars", "signal"],
                          "preferred_dataset_id": self.preferred_dataset_id()})

    def ai_context(self, request: Any) -> dict:
        """The exact (blind) context a provider would receive for this request."""
        from edgelab.ai.context import build_context, resolve_scope
        from edgelab.ai.schema import normalize_request
        from edgelab.features.spec import all_defs
        req = normalize_request(request, [d.feature_id for d in all_defs() if not d.feature_id.startswith("_")],
                                sorted(self.sessions))
        base = self.library.load(req["base_strategy_id"])["definition"] if req["mode"] == "modify" else None
        return _jsonable(build_context(self, req, resolve_scope(self, req["scope"]), base))

    def ai_generate(self, request: Any) -> dict:
        from edgelab.ai import discovery
        from edgelab.ai.providers import get_provider
        prov = get_provider((request or {}).get("provider") if isinstance(request, Mapping) else None,
                            user_settings=self._user_settings())
        g = discovery.generate(self, request, prov)
        pid = (g.get("scope") or {}).get("protocol_id")
        if pid:                                              # ADR-56: attempts are attributed, never invisible
            for pr in g["proposals"]:
                ident = pr["gate"].get("identity") or {}
                self.store.add_proposal_attempt({"protocol_id": pid, "proposal_id": pr["proposal_id"],
                                                 "source": "ai_generation", "generation_id": g["generation_id"],
                                                 "gate_status": pr["gate"]["status"],
                                                 "logic_hash": ident.get("logic_hash"),
                                                 "strategy_id": ident.get("strategy_id"),
                                                 "created_at": g["created_at"]})
        return _jsonable(g)

    def ai_generations(self) -> list[dict]:
        from edgelab.ai.discovery import DiscoveryStore
        return _jsonable(DiscoveryStore(self.data_root / "ai_discovery").generations())

    def ai_generation(self, generation_id: str) -> dict:
        from edgelab.ai.discovery import DiscoveryStore, with_decisions
        st = DiscoveryStore(self.data_root / "ai_discovery")
        return _jsonable(with_decisions(st, st.generation(generation_id)))

    def ai_decide(self, proposal_id: str, decision: str, note: str = "") -> dict:
        from edgelab.ai import discovery
        return _jsonable(discovery.decide(self, proposal_id, decision, note))

    def ai_save(self, proposal_id: str) -> dict:
        from edgelab.ai import discovery
        return _jsonable(discovery.save(self, proposal_id))

    def ai_lineage(self, proposal_id: str) -> dict:
        from edgelab.ai import discovery
        return _jsonable(discovery.lineage(self, proposal_id))

    def ingest_proposals(self, batch: Any, save: bool = True) -> dict:
        from edgelab.strategy.proposals import ingest_proposals
        rep = ingest_proposals(self._definition(batch) if not isinstance(batch, Mapping) else batch,
                               self.sessions, self._config_hash())
        if save:
            for rec in rep.lineage:
                self.library.save(rep.definitions[rec.strategy_id], rep.identities[rec.strategy_id], rec)
            self.library.save_batch(rep.record())          # Mode B batch record (kind: proposal)
        self._protocol_record_mode_b(rep)
        return _jsonable({**rep.to_dict(), "saved": save})

    def _run_cell(self, src: Any, dataset_id: str, record: bool = False, *,
                  parent_strategy_id: str | None = None, mutation: str | None = None,
                  notes: str | None = None, period: tuple | None = None, lock=None,
                  status: str = "IN_SAMPLE", entry_point: str = "backtest_strategy",
                  holdout_access_id: str | None = None) -> dict:
        """One (strategy, dataset) cell through the existing engine: load (re-validated) ->
        [optional period restriction, re-validated] -> compile -> cost model (CFD refusal) ->
        bind -> causality-checked backtest with the strategy's OWN sizing -> metrics ->
        optional run record. Returns the internal objects (not JSON); `backtest_strategy` and
        Phase 4 batch search both use this one path. `notes=None` keeps the Strategy Lab note;
        synthetic data is always labelled first. `lock` (background jobs) is held only around the
        store reads/writes (dataset load, run record), never around the backtest itself."""
        from edgelab.analytics.metrics import compute_metrics
        from edgelab.engine.backtester import run_backtest
        from edgelab.engine.costs import cost_model_from_config
        from edgelab.features.strategy_api import FeatureContext
        from edgelab.instruments import check_identity
        from edgelab.strategy.compiler import compile_strategy
        guard = lock if lock is not None else nullcontext()
        ds = self._cell_dataset(dataset_id, period, lock)          # ADR-77: validated once per process, then reused
        strat = compile_strategy(self._definition(src), self.sessions, self._config_hash())
        # ADR-56 research protocol: refuses holdout/overlap access and over-budget trials BEFORE anything
        # runs (worker processes have no gate; their search plan was checked by the parent)
        gate = getattr(self, "_protocol_gate", None)
        with guard:                                      # the gate reads the protocol ledger (store)
            pctx = gate(ds, strat, entry_point, holdout_access_id) if gate is not None else None
        check_identity(ds.instrument)           # provisional source identity: economics not interpretable
        costs = cost_model_from_config(self.cfg, ds.instrument.symbol, provider=ds.manifest.provider)
        bound = strat.bind(FeatureContext(ds, self.sessions, self.cache))
        res = run_backtest(ds, bound, costs, self.cfg["backtest"], sizing=strat.sizing,
                           contract=contract_for(self.cfg, strat.sizing))
        met = compute_metrics(res.trades, sample_thresholds=self.cfg.get("sample_size"))
        synthetic = self._is_synthetic(ds.manifest)
        # ADR-64: EVERY backtest runs the prop lifecycle layer (pure function of the trades; worker processes have no
        # profiles and leave it to the parent's record step, which computes it from the same trades)
        prop = None
        if hasattr(self, "root"):
            from edgelab.prop.service import outcomes as prop_outcomes
            prop = prop_outcomes(self.root, res.trades, assumptions=res.assumptions)
        run_id = None
        if record:
            run_id = self._record_cell(res, met, synthetic, notes=notes, parent_strategy_id=parent_strategy_id,
                                       mutation=mutation, lock=lock, status=status, prop=prop)
        if pctx is not None and pctx["stage"] == "discovery" and entry_point != "search_cell":
            with guard:                                  # search cells are recorded by the search runner
                pctx["counted"] = self._protocol_record(pctx, res.dataset, run_id, entry_point)
        return {"ds": ds, "strategy": strat, "bound": bound, "costs": costs, "result": res,
                "metrics": met, "synthetic": synthetic, "run_id": run_id, "protocol": pctx, "prop": prop}

    def _record_cell(self, res, met: Mapping, synthetic: bool, *, notes: str | None = None,
                     parent_strategy_id: str | None = None, mutation: str | None = None, lock=None,
                     status: str = "IN_SAMPLE", prop: Mapping | None = None) -> str:
        """The run-record step of `_run_cell` (also used by the parallel search parent, which records
        results computed in worker processes). Synthetic data is always labelled first."""
        from edgelab.research.runs import record_run
        if synthetic:
            label = "SYNTHETIC DEMONSTRATION - not evidence of trading performance"
            notes = label if notes is None else f"{label} | {notes}"
        elif notes is None:
            notes = "single backtest (Strategy Lab)"
        if prop is None:
            from edgelab.prop.service import outcomes as prop_outcomes
            prop = prop_outcomes(self.root, res.trades, assumptions=res.assumptions)
        with (lock if lock is not None else nullcontext()):
            return record_run(self.store, self.cfg, res, met, notes=notes, status=status,
                              parent_strategy_id=parent_strategy_id, mutation=mutation, prop=prop)

    def run_search(self, spec: Any, workers: int | None = None) -> dict:
        """Phase 4: plan and run a strategy x dataset search, storing every cell durably;
        re-running the same search resumes (completed cells are skipped). `workers` (default: the
        spec's `workers`, 1) > 1 computes cells in worker processes; this process alone writes.
        Results are in-sample measurements only."""
        from edgelab.research.batch import run_search
        return run_search(self, spec if isinstance(spec, Mapping) else self._definition(spec), workers)

    # ------------------------------------------------------------ frozen-manifest campaign (ADR-68)
    def campaign_freeze(self, manifest_id: str, protocol_id: str | None = None,
                        datasets: Mapping[str, str] | None = None) -> dict:
        """Freeze a discovery campaign over a factory manifest and materialize its strategies (no evaluation)."""
        from edgelab.research import campaign
        out = campaign.freeze(self, manifest_id, protocol_id=protocol_id, datasets=datasets)
        return _jsonable({k: out[k] for k in ("campaign_id", "path", "materialized")} | {
            "search_id": out["spec"]["search"]["search_id"], "protocol_id": out["spec"]["protocol"]["protocol_id"],
            "n_strategies": out["spec"]["manifest"]["n_strategies"],
            "datasets": out["spec"]["dataset_resolution"]["by_timeframe"]})

    def campaign_check(self, campaign_id: str) -> dict:
        """Read-only preflight of a frozen campaign (no backtest, no trial, no holdout access)."""
        from edgelab.research import campaign
        return _jsonable(campaign.check(self, campaign_id))

    def campaign_run(self, campaign_id: str, workers: int = 1, max_failures: int = 0, processes: int = 1) -> dict:
        """Run a frozen campaign through the protocol-gated search (preflight first; resumable). `processes` (ADR-77):
        CPU cores computing cells at once (identical results)."""
        from edgelab.research import campaign
        return _jsonable(campaign.run(self, campaign_id, workers=workers, max_failures=max_failures, processes=processes))

    def campaign_status(self, campaign_id: str) -> dict:
        from edgelab.research import campaign
        return _jsonable(campaign.status(self, campaign_id))

    # ------------------------------------------------------------ desktop research runs (ADR-69): control layer only
    def campaigns(self) -> list[dict]:
        """Frozen campaigns of this workspace with durable progress and latest run (read-only)."""
        from edgelab.research import campaign, pool2
        rows = campaign.list_campaigns(self)
        labels = pool2.run_names(self)                   # ADR-86: "Strategy pool 1/2" (+ retired protocol); display only
        return _jsonable([{**r, "label": labels.get(r["campaign_id"])} for r in rows])

    def campaign_detail(self, campaign_id: str) -> dict:
        from edgelab.research import campaign

        def build():
            d = campaign.detail(self, campaign_id)
            names = self.campaign_run_names(campaign_id)
            d["runs"] = [{**r, "name": names.get(r.get("run_record_id"))} for r in d.get("runs") or []]
            return _jsonable(d)
        return campaign.cached_view(self, "detail", campaign_id, build)

    def campaign_family_results(self, campaign_id: str, family_id: str) -> dict:
        from edgelab.research import campaign
        return _jsonable(campaign.family_results(self, campaign_id, family_id))

    def campaign_strategy_result(self, campaign_id: str, strategy_id: str) -> dict:
        from edgelab.research import campaign
        return _jsonable(campaign.strategy_result(self, campaign_id, strategy_id))

    def start_campaign_job(self, campaign_id: str, families: list[str] | None = None, max_failures: int = 0,
                           strategy_ids: list[str] | None = None, processes: int | None = None) -> dict:
        """Run a frozen campaign (all / families / explicit frozen strategy ids) in the background; the same runner as
        `campaign-run`. The scope never creates identities: ids must be the manifest's own. `processes` (ADR-77): CPU
        cores computing cells at once; None = the Settings choice (default: all cores but one)."""
        from edgelab.research import campaign
        n = self.research_processes()["processes"] if processes is None else processes
        if not isinstance(n, int) or isinstance(n, bool) or not 1 <= n <= campaign.max_processes():
            raise ValueError(f"processes must be 1..{campaign.max_processes()} (CPU cores of this machine)")
        return _jsonable(self.jobs.start_campaign(campaign_id, families, int(max_failures), strategy_ids, n))

    def research_processes(self) -> dict:
        """How many CPU cores research runs use (ADR-77): the Settings choice, else all cores but one. An execution
        setting only: results, ids and trial counting do not depend on it."""
        from edgelab.research import campaign
        mx, dflt = campaign.max_processes(), campaign.default_processes()
        try:
            chosen = self.ui_preferences().get("research_processes")
        except Exception:                                    # no workspace yet: defaults
            chosen = None
        n = chosen if isinstance(chosen, int) and not isinstance(chosen, bool) and 1 <= chosen <= mx else dflt
        return {"processes": n, "chosen": chosen if n == chosen else None, "default": dflt, "max": mx}

    def campaign_tree(self, campaign_id: str) -> dict:
        """Research browser: all strategies -> families -> strategies, with display names and persisted status."""
        from edgelab.research import campaign
        return campaign.cached_view(self, "tree", campaign_id, lambda: _jsonable(campaign.tree(self, campaign_id)))

    def campaign_run_scope(self, campaign_id: str, run_record_id: str) -> dict:
        from edgelab.research import campaign
        return _jsonable(campaign.load_scope(self, campaign_id, run_record_id))

    def campaign_job(self, job_id: str) -> dict:
        return _jsonable(self.jobs.campaign_status(job_id))

    def active_job(self) -> dict:
        return _jsonable({"job": self.jobs.active()})

    def validate_search(self, spec: Any) -> dict:
        """Structural check of a search spec (keys, types, values); references are checked by plan."""
        from edgelab.research.search import canonical_search_spec, search_hash, validate_search_spec
        raw = spec if isinstance(spec, Mapping) else self._definition(spec)
        res = validate_search_spec(raw)
        out = {"valid": res.valid, "errors": [i.to_dict() for i in res.errors],
               "warnings": [i.to_dict() for i in res.warnings]}
        if res.valid:
            out.update(canonical=canonical_search_spec(raw), search_hash=search_hash(raw, self._config_hash()))
        return _jsonable(out)

    def plan_search(self, spec: Any) -> dict:
        """The deterministic strategy x dataset plan (nothing is executed)."""
        from edgelab.research.search import plan_search
        return _jsonable(plan_search(spec if isinstance(spec, Mapping) else self._definition(spec), self).to_dict())

    def list_searches(self) -> list[dict]:
        """Stored searches (batch rows, JSON fields decoded; no cells)."""
        from edgelab.research.batch import _decode_batch
        return _jsonable([_decode_batch(b) for b in self.store.list_search_batches()])

    def get_search(self, search_id: str) -> dict:
        """One stored search: batch, current cells, historical cells, cumulative totals."""
        from edgelab.research.batch import search_summary
        return _jsonable(search_summary(self.store, search_id))

    @property
    def jobs(self):
        """The background search job manager (created on first use; creating it marks searches a
        previous process left `running` as `interrupted`)."""
        if self._jobs is None:
            from edgelab.research.jobs import JobManager
            self._jobs = JobManager(self, self.lock)
        return self._jobs

    def start_search_job(self, spec: Any) -> dict:
        """Run a search in the background (one job at a time; JobConflict otherwise)."""
        return _jsonable(self.jobs.start(spec if isinstance(spec, Mapping) else self._definition(spec)))

    def job_status(self, job_id: str) -> dict:
        return _jsonable(self.jobs.status(job_id))

    def cancel_job(self, job_id: str) -> dict:
        """Request cooperative cancellation: the running cell finishes, no new cell starts."""
        return _jsonable(self.jobs.cancel(job_id))

    def rank_search(self, search_id: str, metric: str | None = None, min_sample_label: str | None = None) -> dict:
        """In-sample ranking of a stored search's current cells (never validation)."""
        from edgelab.research.ranking import rank_search
        return _jsonable(rank_search(self.store, search_id, metric, min_sample_label))

    def select_shortlist(self, search_id: str, strategy_ids: list[str]) -> dict:
        """Tag strategies of a search as a shortlist; run status is never changed."""
        from edgelab.research.ranking import select_shortlist
        return _jsonable(select_shortlist(self.store, search_id, strategy_ids))

    def backtest_strategy(self, src: Any, dataset_id: str, record: bool = False, period: tuple | None = None) -> dict:
        """One backtest through the existing engine (causality-checked). Returns measurements with
        sample-size labels; draws no conclusions. CFD datasets need configured broker costs. Under an
        ACTIVE research protocol (ADR-56) the evaluated bars must lie inside its discovery window
        (pass `period`); the evaluation is attributed to the protocol's trial ledger."""
        cell = self._run_cell(src, dataset_id, record, period=period, entry_point="backtest_strategy")
        return self._backtest_payload(cell, dataset_id)

    def _backtest_payload(self, cell: Mapping, dataset_id: str) -> dict:
        """The JSON result of one backtest cell (shared by the synchronous call and background backtest jobs)."""
        ds, res, met = cell["ds"], cell["result"], cell["metrics"]
        exits = res.trades["exit_reason"].value_counts().to_dict() if len(res.trades) else {}
        return _jsonable({"strategy_id": cell["strategy"].strategy_id, "dataset_id": dataset_id,
                          "run_id": cell["run_id"], "synthetic": cell["synthetic"], "exit_reasons": exits,
                          "n_signals": res.n_signals, "trades_hash": res.trades_hash,
                          "dataset": {k: getattr(ds.manifest, k) for k in ("provider", "asset_type", "instrument",
                                                                           "timeframe", "start", "end")},
                          "cost_status": cell["costs"].status, "metrics": met,
                          "prop": cell["prop"],              # ADR-65: the prop audit, shown with every backtest result
                          "signal_diagnostics": cell["bound"].last_diagnostics,
                          "skipped": dict(res.skipped),
                          "protocol": None if cell["protocol"] is None else
                          {k: cell["protocol"].get(k) for k in ("protocol_id", "stage", "trial_id", "counted")},
                          "note": "historical result under the stated assumptions; not a conclusion"})

    # ------------------------------------------------------------ background single backtests (ADR-76)
    def start_backtest_job(self, src: Any, dataset_id: str, period: tuple | None = None) -> dict:
        """Run one recorded backtest in a background thread through the SAME path as `backtest_strategy`
        (`_run_cell`), holding the service lock only around its store steps (dataset load, protocol gate, run record),
        never during the computation, so other pages keep answering. Process-local registry, like search jobs."""
        import uuid
        from datetime import datetime, timezone
        jobs = self.__dict__.setdefault("_bt_jobs", {})
        job_id = "BTJ_" + uuid.uuid4().hex[:12].upper()
        job = {"job_id": job_id, "state": "running", "dataset_id": dataset_id, "error": None, "result": None,
               "created_at": datetime.now(timezone.utc).isoformat(), "finished_at": None}
        jobs[job_id] = job

        def work():
            try:
                cell = self._run_cell(src, dataset_id, True, period=period, lock=self.lock, entry_point="backtest_strategy")
                job["result"] = self._backtest_payload(cell, dataset_id)
                job["state"] = "completed"
            except Exception as exc:                    # noqa: BLE001 - reported to the page like a refused request
                job["error"] = {"kind": type(exc).__name__, "message": str(exc)}
                job["state"] = "failed"
            job["finished_at"] = datetime.now(timezone.utc).isoformat()
        threading.Thread(target=work, daemon=True, name=f"munyun-backtest-{job_id}").start()
        return {k: v for k, v in job.items() if k != "result"}

    def backtest_job(self, job_id: str) -> dict:
        job = self.__dict__.get("_bt_jobs", {}).get(job_id)
        if job is None:
            raise KeyError(f"backtest job {job_id} is not known to this app session")
        return dict(job)

    # ============================================================ PAPER TRADING (ADR-81)
    @property
    def paper(self):
        """The background paper manager (downloads completed days, recomputes running accounts); started by the
        launchers, created lazily everywhere else."""
        m = self.__dict__.get("_paper")
        if m is None:
            from edgelab.paper.manager import PaperManager
            m = self.__dict__["_paper"] = PaperManager(self)
        return m

    def paper_candidates(self, profile_id: str, show_all: bool = False) -> dict:
        """Strategies to choose from: survivors under ``profile_id`` (latest in-sample run), or every tested strategy."""
        from edgelab.prop.service import default_profiles
        from edgelab.research import overview as ov
        from edgelab.research.results_view import _latest_scoped
        if profile_id not in {p["profile_id"] for p in default_profiles(self.root)}:
            raise ValueError(f"unknown prop rule profile {profile_id!r}")
        rows, _ = _latest_scoped(self, "in_sample", None)
        running = {a["strategy_id"] for a in self._paper_store().list_accounts(self.data_root) if a.get("status") == "running"
                   and a["profile_id"] == profile_id}
        out = []
        for x in rows:
            ref = x["ref"]
            if not ref or not ref.get("trade_count"):
                continue
            r = ov.apply_criteria(ref, profile_id)
            if not show_all and not r["survivor"]:
                continue
            f = x["facets"]
            out.append({"strategy_id": f["strategy_id"], "display_name": f.get("display_name") or f.get("name"),
                        "family_id": f.get("family_id"), "timeframe": f.get("timeframe"), "survivor": r["survivor"],
                        "expectancy_r": ref.get("expectancy_r"), "trades": ref.get("trade_count"),
                        "run_id": ref.get("run_id"), "already_running": f["strategy_id"] in running})
        return _jsonable({"profile_id": profile_id, "show_all": bool(show_all), "strategies": out,
                          "n_survivors": sum(1 for o in out if o["survivor"])})

    @staticmethod
    def _paper_store():
        from edgelab.paper import store
        return store

    def paper_start(self, strategy_ids: list, profile_id: str, now=None) -> dict:
        """Start one paper account per strategy (shared settings): frozen strategy definitions, the profile's current
        registered version, the fee snapshot from Settings, start = the next trading date that has not begun."""
        from edgelab.paper import feed
        from edgelab.prop.service import default_profiles
        store = self._paper_store()
        if not isinstance(strategy_ids, list) or not strategy_ids or not all(isinstance(x, str) for x in strategy_ids):
            raise ValueError("choose at least one strategy")
        if len(set(strategy_ids)) != len(strategy_ids):
            raise ValueError("a strategy is listed twice")
        prof = next((p for p in default_profiles(self.root) if p["profile_id"] == profile_id), None)
        if prof is None:
            raise ValueError(f"unknown prop rule profile {profile_id!r}")
        fees = (self.ui_preferences().get("prop_fees") or {}).get(profile_id) or {}
        if fees.get("eval_price") is None:
            raise ValueError("enter the evaluation price for this prop account in Settings → Prop account fees first")
        docs = [self.library.load(sid) for sid in strategy_ids]          # KeyError for an unknown strategy
        unit = prof["rules"]["account.quantity_unit"]["value"] if "rules" in prof else None
        wrong = [sid for sid, d in zip(strategy_ids, docs)
                 if ((d.get("definition") or {}).get("sizing") or {}).get("contract") != unit]
        if wrong:
            raise ValueError(f"{len(wrong)} strateg{'y is' if len(wrong) == 1 else 'ies are'} not sized in {unit} "
                             f"contracts, so the prop account cannot trade {'it' if len(wrong) == 1 else 'them'}")
        start = feed.first_unstarted_date(self.cfg, now)
        same = {a["strategy_id"] for a in store.list_accounts(self.data_root) if a.get("status") == "running"
                and a["profile_id"] == profile_id and a["start_date"] == start.isoformat()}
        if same & set(strategy_ids):                      # an exact copy would trade identically: refuse, never count twice
            raise ValueError(f"{len(same & set(strategy_ids))} of these strategies already have a running paper account "
                             "on this prop account starting the same day")
        batch_id = store.new_id("PB")
        created = []
        for sid, doc in zip(strategy_ids, docs):
            acct = {"account_id": store.new_id("PA"), "batch_id": batch_id, "created_at": store.now_iso(),
                    "strategy_id": sid, "display_name": doc.get("display_name") or (doc.get("definition") or {}).get("name"),
                    "definition": doc["definition"], "logic_hash": doc.get("logic_hash"),
                    "definition_hash": doc.get("definition_hash"),
                    "profile_id": profile_id, "profile_version": prof["version"],
                    "fees": dict(fees), "start_date": start.isoformat(),
                    "start_ts": feed.session_open_utc(self.cfg, start).isoformat(), "status": "running",
                    "kind": "paper", "label": "simulated forward trading; never a research result or trial"}
            store.save_account(self.data_root, acct)
            created.append(acct["account_id"])
        store.save_batch(self.data_root, {"batch_id": batch_id, "created_at": store.now_iso(), "profile_id": profile_id,
                                          "profile_version": prof["version"], "strategy_ids": list(strategy_ids),
                                          "accounts": created, "fees": dict(fees), "start_date": start.isoformat()})
        self.paper.wake()
        return _jsonable({"batch_id": batch_id, "accounts": created, "start_date": start.isoformat()})

    def paper_accounts(self) -> list[dict]:
        store = self._paper_store()
        out = []
        for a in store.list_accounts(self.data_root):
            st = store.load_state(self.data_root, a["account_id"]) or {}
            out.append({k: a.get(k) for k in ("account_id", "batch_id", "created_at", "strategy_id", "display_name",
                                              "profile_id", "profile_version", "start_date", "status", "stop_reason")}
                       | {"state": st.get("state") or ("waiting" if a.get("status") == "running" else "stopped"),
                          "current_attempt": st.get("current_attempt", 0), "attempts": len(st.get("attempts") or []),
                          "passes": st.get("passes", 0), "payouts": len(st.get("payouts") or []),
                          "trader_payouts": st.get("trader_payouts", 0.0), "fees_total": st.get("fees_total", 0.0),
                          "net": st.get("net", 0.0), "n_trades": st.get("n_trades", 0),
                          "balance": _paper_balance(st), "last_day": (st.get("feed") or {}).get("last_day"),
                          "computed_at": st.get("computed_at")})
        return _jsonable(out)

    def paper_account(self, account_id: str) -> dict:
        store = self._paper_store()
        a = store.load_account(self.data_root, account_id)
        st = store.load_state(self.data_root, account_id)
        return _jsonable({"account": {k: v for k, v in a.items() if k != "definition"}, "state": st})

    def paper_set_status(self, account_id: str, action: str) -> dict:
        store = self._paper_store()
        a = store.load_account(self.data_root, account_id)
        if action == "delete":
            store.delete_account(self.data_root, account_id)
            return {"deleted": account_id}
        if action not in ("stop", "resume"):
            raise ValueError("action must be stop, resume or delete")
        a.update(status="stopped" if action == "stop" else "running", stop_reason="stopped by you" if action == "stop"
                 else None, stopped_at=store.now_iso() if action == "stop" else None)
        store.save_account(self.data_root, a)
        if action == "resume":
            self.paper.wake()
        return _jsonable({k: v for k, v in a.items() if k != "definition"})

    def paper_feed_status(self) -> dict:
        from edgelab.paper import feed
        from edgelab.paper.manager import is_paused
        chk = feed.read_source_check(self.data_root)
        return _jsonable({**feed.status(self.data_root), "next_start_date": feed.first_unstarted_date(self.cfg).isoformat(),
                          "source_check": chk, "paused": is_paused(chk),
                          "manager": {k: v for k, v in self.paper.status.items() if k != "trace"}})

    # ---------------------------------------------------------------- research-source check (ADR-83)
    def _paper_research_manifest(self) -> tuple[dict | None, str]:
        """The user's Dukascopy research dataset the paper feed is compared with: 1-minute NQ_DUKASCOPY with ASK OHLC,
        the Preferred Research Dataset's family first, else the one ending latest. (manifest, note)."""
        from contextlib import ExitStack
        from edgelab.paper import feed
        with ExitStack() as es:
            if not es.enter_context(self.read_context(page=False)):
                es.enter_context(self.lock)
            rows = [m for m in self.store.list_datasets() if m.get("instrument") == feed.INSTRUMENT
                    and m.get("has_ask_ohlc") and m.get("timeframe") == "1m"]
            pref = self.preferred_dataset_id()
            fam = next((m.get("dataset_name") for m in self.store.list_datasets() if m["dataset_id"] == pref), None)
        if not rows:
            return None, ("no 1-minute Dukascopy BID/ASK research dataset (NQ_DUKASCOPY with ASK OHLC) in this "
                          "workspace to compare with")
        rows.sort(key=lambda m: (bool(fam) and m.get("dataset_name") == fam, str(m.get("end"))))
        return rows[-1], ""

    def _paper_load_research(self, dataset_id: str):
        """Load and validate a research dataset from a background thread (read-only connection; no service lock)."""
        from contextlib import ExitStack
        with ExitStack() as es:
            if not es.enter_context(self.read_context(page=False)):
                es.enter_context(self.lock)
            return load_validated(self.store, self.cfg, dataset_id)

    def paper_check_source(self) -> dict:
        """'Check against my research data': runs on the paper thread (or a one-off thread when none is running)."""
        m = self.paper
        m.request_check()                                   # wakes the paper thread
        if m.thread is None:                                # not started by a launcher (tests, embedding): run here
            threading.Thread(target=m.run_once, daemon=True, name="munyun-paper-check").start()
        return {"started": True}

    def paper_continue_anyway(self) -> dict:
        """Keep paper accounts updating although the latest check found different prices (that check only)."""
        from datetime import datetime, timezone
        from edgelab.paper import feed
        from edgelab.paper.manager import is_paused
        rec = feed.read_source_check(self.data_root)
        if not is_paused(rec):
            raise ValueError("paper accounts are not paused by a data check")
        rec["continue_anyway"] = {"at": datetime.now(timezone.utc).isoformat()}
        feed.write_source_check(self.data_root, rec)
        self.paper.wake()
        return _jsonable(rec)

    def paper_update_now(self) -> dict:
        m = self.paper
        if m.thread is None:                              # not started by a launcher (tests, embedding): run here
            threading.Thread(target=m.run_once, daemon=True, name="munyun-paper-once").start()
        else:
            m.wake()
        return {"started": True}

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
            if n <= 500 and grid <= MAX_GRID:            # the exact combinations generation will evaluate
                from edgelab.strategy.variations import _combos
                base_vals = {k: (v or {}).get("value") for k, v in (raw.get("parameters") or {}).items()}
                out["combinations_list"] = _combos(sp, sp["dimensions"], base_vals)
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

    def list_variation_batches(self, kind: str | None = None) -> list[dict]:
        """Variation batches (default, unchanged); kind="proposal" lists Mode B proposal batches."""
        return _jsonable(self.library.list_batches(kind))

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
        return _jsonable({"strategy_timeframe": None if tf is None else f"{tf}m", "datasets": out,
                          "preferred_dataset_id": self.preferred_dataset_id()})

    def _dataset_eligibility(self, d: Mapping, tf_minutes: int | None = None) -> dict:
        """Whether one dataset (a `list_datasets` row) can run a strategy on `tf_minutes` bars
        (None = timeframe not checked), with every reason it cannot. Nothing is invented: an
        unconfigured cost profile makes the dataset ineligible."""
        from edgelab.engine.costs import CostConfigError, CostScenarioIncomplete, cost_model_from_config
        from edgelab.data.schema import timeframe_minutes
        m = self.store.get_manifest(d["dataset_id"])
        reasons, codes = [], []                     # codes: machine-readable, for the quote/spread refusals
        try:
            cm = cost_model_from_config(self.cfg, m.instrument, provider=m.provider)
            cost = {"status": cm.status, "profile": getattr(cm, "profile", None), "spread_source": cm.spread_source,
                    "quote_model": "directional_bid_ask" if cm.spread_source == "quotes" else "single_series"}
            if getattr(cm, "scenario", ""):
                cost["scenario"] = cm.scenario
            if cm.spread_source == "dataset" and not m.has_spread:      # mirrors the backtester's own refusal
                reasons.append("cost profile charges the dataset's per-bar spread, but this dataset has no spread "
                               "(BID-only) - use a BID/ASK dataset")
                codes.append("DATASET_SPREAD_REQUIRED")
            if cm.spread_source == "quotes" and not m.has_ask_ohlc:     # mirrors the backtester's own refusal
                reasons.append("cost profile uses directional BID/ASK execution (spread_source: quotes), but this "
                               "dataset has no ASK OHLC - import the ASK feed's OHLC (it is never inferred)")
                codes.append("ASK_OHLC_REQUIRED")
        except CostScenarioIncomplete as exc:          # configured status, but the named scenario lacks fields
            cost = {"status": exc.status, "incomplete": True, "scenario": exc.scenario, "missing": exc.missing,
                    "reason": str(exc)}
            name = f"'{exc.scenario}'" if isinstance(exc.scenario, str) and exc.scenario else "(unnamed)"
            reasons.append(f"cost scenario {name} ({exc.status}) is incomplete - missing {', '.join(exc.missing)}")
        except CostConfigError as exc:
            cost = {"status": "unconfigured", "reason": str(exc)}
            reasons.append("broker/provider cost profile is unconfigured - configure verified costs first")
        except KeyError as exc:
            cost = {"status": "unconfigured", "reason": f"no cost profile for {exc}"}
            reasons.append("no cost profile for this instrument")
        identity = self._instrument_identity(m.instrument)
        if identity.get("problem"):
            reasons.append("session calendar not yet verified against the real source file (DATA_IMPORT.md)"
                           if identity.get("calendar_status") == "provisional_unverified" else
                           "instrument source identity is provisional - state the source symbol, asset class "
                           "and contract economics first (DATA_IMPORT.md)"
                           if identity.get("identity_status") == "provisional" else identity["problem"])
        if d.get("quality_status") == "FAIL":
            reasons.append("dataset failed validation")
        if tf_minutes is not None and timeframe_minutes(d["timeframe"]) != tf_minutes:
            reasons.append(f"timeframe {d['timeframe']} does not match the strategy timeframe {tf_minutes}m")
        from edgelab.data.preferences import load_prefs
        pref = (load_prefs(self.data_root).get("preferred_research_dataset") or {}).get("dataset_id")
        return {**d, "cost": cost, "synthetic": self._is_synthetic(m), "identity": identity,
                "preferred": d["dataset_id"] == pref,
                "limitations": self._limitations(m) + ([identity["calendar_caveat"]] if identity.get("calendar_caveat") else []),
                "has_ask_ohlc": bool(m.has_ask_ohlc), "runnable": not reasons, "reasons": reasons,
                "reason_codes": codes}

    def _instrument_identity(self, symbol: str) -> dict:
        from edgelab.instruments import identity_info, identity_problem, load_instruments
        inst = load_instruments(self.cfg).get(symbol)
        if inst is None:
            return {"identity_status": "unknown", "problem": f"instrument {symbol} is not in configs/instruments.yaml"}
        return {**identity_info(inst), "problem": identity_problem(inst)}

    def list_runs(self) -> list[dict]:
        rows = []
        # ADR-77: the records come in one query (the trades of every run were loaded and thrown away before)
        recs = {rid: json.loads(raw) for rid, raw in self.store._query("SELECT run_id, record_json FROM runs")}
        for r in self.store.list_runs().to_dict("records"):
            rec = recs[r["run_id"]]
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

    def research_report(self, run_ids: list[str], hour_timezone: str = "America/New_York",
                        mc_sims: int = 1000, mc_seed: int = 0) -> dict:
        """Phase 5: descriptive analytics over STORED runs of ONE fixed strategy on distinct
        datasets (e.g. one run per year). Pooled + per-dataset metrics, stability counts, canonical
        session and entry-hour breakdowns, exact cost sensitivity at the configured multipliers,
        break-even cost multiple, and the caveats the runs' data/costs require. Reads only."""
        from edgelab.analytics import research as ra
        ids = list(dict.fromkeys(run_ids or []))
        if not ids:
            raise ValueError("research_report needs at least one run id")
        loaded = [(rid, *self.store.load_run(rid)) for rid in ids]
        strategies = sorted({rec["strategy"]["strategy_id"] for _, rec, _ in loaded})
        if len(strategies) != 1:
            raise ValueError(f"a research report covers ONE fixed strategy; got {strategies}")
        datasets = [rec["dataset"]["dataset_id"] for _, rec, _ in loaded]
        if len(set(datasets)) != len(datasets):
            raise ValueError("each dataset may appear once in a report (the same trades would be counted twice)")
        loaded.sort(key=lambda x: (str(x[1]["dataset"].get("start")), x[1]["dataset"]["dataset_id"]))
        groups = {rec["dataset"]["dataset_id"]: trades for _, rec, trades in loaded}
        thr = self.cfg.get("sample_size")
        rows = ra.group_table(groups, thr)
        for row, (rid, rec, _) in zip(rows, loaded):
            d, a = rec["dataset"], rec.get("assumptions") or {}
            row.update({"dataset_id": row["group"], "run_id": rid, "period": f"{str(d.get('start'))[:10]} .. {str(d.get('end'))[:10]}",
                        "cost_status": a.get("cost_status"), "cost_profile": (a.get("costs") or {}).get("profile")})
        pooled = ra.pooled_trades(groups)
        mults = self.cfg["backtest"].get("cost_sensitivity_multipliers", [0.5, 1.0, 1.5, 2.0, 3.0])
        first = loaded[0][1]
        return _jsonable({
            "report": "phase5_research_summary",
            "strategy_id": strategies[0],
            "strategy_name": (first["strategy"].get("dsl") or {}).get("name"),
            "labels": ra.research_labels([rec for _, rec, _ in loaded], load_instruments(self.cfg)),
            "cost_status": sorted({str((rec.get("assumptions") or {}).get("cost_status")) for _, rec, _ in loaded}),
            "runs": [{"run_id": rid, "dataset_id": rec["dataset"]["dataset_id"], "trades_hash": rec.get("trades_hash"),
                      "provider": rec["dataset"].get("provider"), "instrument": rec["dataset"].get("instrument"),
                      "timeframe": rec["dataset"].get("timeframe")} for rid, rec, _ in loaded],
            "pooled": ra.pooled_summary(groups, thr),
            "by_dataset": rows,
            "stability": ra.stability_summary(rows),
            "sessions": ra.session_breakdown(pooled, self.sessions, thr),
            "hours": ra.hour_breakdown(pooled, hour_timezone, thr),
            "cost_sensitivity": ra.cost_sensitivity_table(pooled, mults),
            "monte_carlo": self._monte_carlo(pooled, mc_sims, mc_seed),
            "deferred": ["randomized-entry control comparison", "trade-distribution plots",
                         "weekday/month breakdowns", "Research page UI"],
        })

    @staticmethod
    def _monte_carlo(trades, n_sims: int, seed: int) -> dict:
        from edgelab.research.validation import monte_carlo
        r = trades["net_r"].to_numpy(float) if len(trades) else []
        return {m: monte_carlo(r, n_sims, seed, m) for m in ("bootstrap", "shuffle")}

    # ------------------------------------------------------------ validation (fixed strategies)
    def _window_cell(self, frozen: Mapping, frozen_hash: str, dataset_id: str, window, status: str,
                     record: bool, note: str) -> dict:
        from edgelab.analytics import research as ra
        from edgelab.research.validation import copy_frozen, freeze_definition
        cell = self._run_cell(copy_frozen(frozen), dataset_id, record, period=(window.start, window.end),
                              notes=note, status=status, entry_point="internal_validation")
        if freeze_definition(frozen)[1] != frozen_hash:                  # nothing may alter the frozen definition
            raise RuntimeError("frozen strategy definition changed during validation")
        res = cell["result"]
        return {"window": window.to_dict(), "status": status, "run_id": cell["run_id"],
                "strategy_id": cell["strategy"].strategy_id,
                "dataset_id": res.dataset["dataset_id"], "parent_dataset_id": res.dataset.get("parent_dataset_id"),
                "cost_status": res.assumptions["cost_status"], "cost_profile": res.assumptions["costs"]["profile"],
                "trades_hash": res.trades_hash,
                "metrics": ra._pick(cell["metrics"], ra.REPORT_METRICS),
                "_trades": res.trades, "_record": {"assumptions": res.assumptions, "dataset": res.dataset, "status": status,
                                                   "notes": note if not cell["synthetic"] else "SYNTHETIC"}}

    def _validation_output(self, kind: str, vid: str, strategy_ids: set, frozen_hash: str, dataset_id: str,
                           cells: list[dict], extra: dict) -> dict:
        from edgelab.analytics import research as ra
        if len(strategy_ids) != 1:
            raise RuntimeError(f"windows compiled to different strategies: {sorted(strategy_ids)}")
        labels = ra.research_labels([c["_record"] for c in cells], load_instruments(self.cfg))
        labels.append("Fixed strategy: no window was used to choose parameters, so this measures stability "
                      "across time; it is not a test of a selection procedure.")
        clean = [{k: v for k, v in c.items() if not k.startswith("_")} for c in cells]
        return _jsonable({"validation": kind, "validation_id": vid, "strategy_id": next(iter(strategy_ids)),
                          "definition_hash": frozen_hash, "dataset_id": dataset_id,
                          "cost_status": sorted({c["cost_status"] for c in cells}),
                          "labels": labels, "windows": clean, **extra})

    def evaluate_oos(self, src: Any, dataset_id: str, split_at: Any, record: bool = False,
                     mc_sims: int = 1000, mc_seed: int = 0, bounds: tuple | None = None) -> dict:
        """Temporal holdout of ONE fixed strategy: [start, split) = train (run status IN_SAMPLE),
        [split, end] = out-of-sample (OUT_OF_SAMPLE). Same frozen definition and cost profile on both;
        each window is a re-validated restricted dataset linked to its parent."""
        from edgelab.research.validation import freeze_definition, oos_windows, validation_id
        frozen, fh = freeze_definition(self._definition(src))
        m = self.store.get_manifest(dataset_id)
        lo, hi = (m.start, m.end) if bounds is None else bounds    # bounds: e.g. a protocol's discovery window
        train, oos = oos_windows(lo, hi, split_at)
        self._protocol_precheck(m, [(train.start, train.end), (oos.start, oos.end)], "internal_validation")
        vid = validation_id("oos", fh, dataset_id, [train.to_dict(), oos.to_dict()])
        cells = [self._window_cell(frozen, fh, dataset_id, w, st, record, f"{vid} OOS evaluation: {w.role} window")
                 for w, st in ((train, "IN_SAMPLE"), (oos, "OUT_OF_SAMPLE"))]
        return self._validation_output("oos", vid, {c["strategy_id"] for c in cells}, fh, dataset_id, cells,
                                       {"monte_carlo_oos": self._monte_carlo(cells[1]["_trades"], mc_sims, mc_seed)})

    def walk_forward(self, src: Any, dataset_id: str, train_months: int, test_months: int,
                     anchored: bool = False, record: bool = False, mc_sims: int = 1000, mc_seed: int = 0,
                     bounds: tuple | None = None) -> dict:
        """Walk-forward of ONE fixed strategy over one dataset: consecutive non-overlapping test
        windows (run status WALK_FORWARD), each preceded by its train window (IN_SAMPLE; rolling or
        anchored). Nothing is re-fitted between windows. Pooled OOS = all test-window trades."""
        from edgelab.analytics import research as ra
        from edgelab.research.validation import freeze_definition, validation_id, walk_forward_windows
        frozen, fh = freeze_definition(self._definition(src))
        m = self.store.get_manifest(dataset_id)
        lo, hi = (m.start, m.end) if bounds is None else bounds
        segs = walk_forward_windows(lo, hi, train_months, test_months, anchored)
        self._protocol_precheck(m, [(w.start, w.end) for sg in segs for w in (sg["train"], sg["test"])],
                                "internal_validation")
        vid = validation_id("walk_forward", fh, dataset_id,
                            [{"train": s["train"].to_dict(), "test": s["test"].to_dict()} for s in segs])
        cells, tests = [], {}
        for sg in segs:
            for w, st in ((sg["train"], "IN_SAMPLE"), (sg["test"], "WALK_FORWARD")):
                c = self._window_cell(frozen, fh, dataset_id, w, st, record,
                                      f"{vid} walk-forward segment {sg['index']}: {w.role} window")
                c["segment"], c["partial"] = sg["index"], sg["partial"]
                cells.append(c)
                if w.role == "test":
                    tests[f"segment {sg['index']}"] = c["_trades"]
        test_rows = [{"segment": c["segment"], "partial": c["partial"], **c["metrics"]}
                     for c in cells if c["window"]["role"] == "test"]
        pooled = ra.pooled_trades(tests)
        return self._validation_output(
            "walk_forward", vid, {c["strategy_id"] for c in cells}, fh, dataset_id, cells,
            {"scheme": "anchored" if anchored else "rolling", "train_months": train_months,
             "test_months": test_months,
             "oos_pooled": ra.pooled_summary(tests, self.cfg.get("sample_size")),
             "oos_segments": test_rows,
             "oos_stability": ra.stability_summary(test_rows),
             "monte_carlo_oos": self._monte_carlo(pooled, mc_sims, mc_seed)})

    def random_entry_control(self, src: Any, dataset_id: str, n_controls: int = 20, seed: int = 0,
                             period: tuple | None = None, sample_status: str = "IN_SAMPLE",
                             holdout_access_id: str | None = None) -> dict:
        """Matched random-entry control for ONE fixed candidate (research/controls.py): the candidate
        runs once through the normal path; each seeded realization re-uses the candidate's compiled
        definition, costs, sizing and backtest config and randomizes only entry timing/direction
        among the candidate's own causal entry opportunities, matched on signal count and direction
        mix. Control results are returned and kept as CONTROL records (``<data>/controls``, ADR-73), never stored as
        run records, strategies or trials."""
        from edgelab.analytics import research as ra
        from edgelab.analytics.metrics import compute_metrics
        from edgelab.engine.backtester import run_backtest
        from edgelab.features.strategy_api import FeatureContext
        from edgelab.research import controls as rc
        from edgelab.research.validation import copy_frozen, freeze_definition, validation_id
        from edgelab.strategy.compiler import compile_strategy
        if not (isinstance(n_controls, int) and 1 <= n_controls <= 1000):
            raise ValueError("n_controls must be an integer in 1..1000")
        if sample_status not in ("IN_SAMPLE", "OUT_OF_SAMPLE", "WALK_FORWARD"):      # labelling only
            raise ValueError("sample_status must be IN_SAMPLE, OUT_OF_SAMPLE or WALK_FORWARD")
        frozen, fh = freeze_definition(self._definition(src))
        cell = self._run_cell(copy_frozen(frozen), dataset_id, False, period=period,
                              entry_point="random_control_candidate", holdout_access_id=holdout_access_id)
        ds, cand, costs, res = cell["ds"], cell["strategy"], cell["costs"], cell["result"]
        csig = cell["bound"].generate_signals(ds.bars)
        n_sig = int((csig.direction != 0).sum())                          # after cooldown (reported)
        n_pre, n_long_pre = rc.candidate_opportunities(cell["bound"], ds.bars)   # before cooldown (calibration)
        if n_pre == 0:
            raise ValueError("the candidate produced no entry signals on this data; nothing to match")
        p_long = n_long_pre / n_pre
        thr = self.cfg.get("sample_size")
        ctx = FeatureContext(ds, self.sessions, self.cache)
        seeds = rc.realization_seeds(seed, n_controls)
        reals = []
        for k, sd in enumerate(seeds):
            ctrl = rc.RandomEntryControl(compile_strategy(copy_frozen(frozen), self.sessions,
                                                          self._config_hash()).compiled, sd).bind(ctx)
            eligible = ctrl.eligible_count(ds.bars)
            ctrl.set_design(min(1.0, n_pre / eligible) if eligible else 0.0, p_long)
            r = run_backtest(ds, ctrl, costs, self.cfg["backtest"], sizing=cand.sizing,
                             contract=contract_for(self.cfg, cand.sizing))
            reals.append({"index": k, "seed": sd, "control_strategy_id": ctrl.strategy_id,
                          "eligible_bars": eligible, "signal_rate": ctrl.control["signal_rate"],
                          "pre_cooldown_signals": ctrl.pre_cooldown_fires(ds.bars), "signals": r.n_signals, "trades_hash": r.trades_hash,
                          "causality_passed": None if r.causality is None else r.causality.passed,
                          "cost_status": r.assumptions["cost_status"],
                          **ra._pick(compute_metrics(r.trades, sample_thresholds=thr), ra.REPORT_METRICS)})
        if freeze_definition(frozen)[1] != fh:
            raise RuntimeError("frozen strategy definition changed during the control run")
        cand_m = ra._pick(cell["metrics"], ra.REPORT_METRICS)
        d, a = res.dataset, res.assumptions
        config = {"method": rc.CONTROL_METHOD, "n_controls": n_controls, "base_seed": int(seed),
                  "realization_seeds": seeds, "period": None if period is None else [str(x) for x in period],
                  "matching": {"candidate_pre_cooldown_signals": n_pre, "candidate_signals": n_sig,
                               "candidate_long_share": p_long,
                               "rule": "each eligible bar fires with p = candidate valid entries before "
                                       "cooldown / eligible bars; direction long with the candidate's "
                                       "pre-cooldown long share; the same cooldown and engine rules then "
                                       "apply, so post-cooldown signal and trade counts are not forced"}}
        labels = ra.research_labels([{"assumptions": a, "dataset": d, "status": sample_status,
                                      "notes": "SYNTHETIC" if cell["synthetic"] else ""}], load_instruments(self.cfg))
        labels.append("Random-entry control: a conditional null (entry timing and direction randomized among "
                      "the candidate's own eligible bars, calibrated to its entries before cooldown and their "
                      "direction mix; the same cooldown then applies). It does not test exits, sizing, "
                      "cooldown or costs, which are identical on both sides.")
        out = _jsonable({
            "validation": "random_entry_control",
            "validation_id": validation_id("random_entry_control", fh, d["dataset_id"], [config]),
            "candidate": {"strategy_id": cand.strategy_id, "definition_hash": fh, "trades_hash": res.trades_hash,
                          "pre_cooldown_signals": n_pre, "signals": n_sig, "metrics": cand_m},
            "dataset": {k: d.get(k) for k in ("dataset_id", "parent_dataset_id", "provider", "instrument",
                                              "timeframe", "start", "end")},
            "cost_profile": a["costs"]["profile"], "cost_status": a["cost_status"],
            "sample_status": sample_status, "control_config": config, "labels": labels,
            "comparison": rc.summarize(cand_m, reals),
            "realizations": reals,
            "stored_as_runs": False,
        })
        out["stored_as_control_record"] = rc.save_control_record(self.data_root, out)
        return out

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

    # ============================================================ RESEARCH PROTOCOL (ADR-56)
    @staticmethod
    def _scope_key(instrument: str, provider: str) -> str:
        return f"{instrument}@{provider}"

    def _governing_protocol(self, instrument: str, provider: str) -> dict | None:
        """The ACTIVE protocol of an instrument/provider (verified against its identity), or None.
        Protocol storage is SQLite-only like search storage; other backends have no protocols."""
        if getattr(self.store, "backend", None) != "sqlite":
            return None
        from edgelab.research.protocol import verify_record
        act = self.store.list_protocols(self._scope_key(instrument, provider), "ACTIVE")
        if not act:
            return None
        verify_record(act[0])
        return act[0]

    def create_protocol(self, dataset_id: str, discovery: tuple, holdout: tuple, *, name: str = "",
                        pre_protocol_exposure: list | tuple = (), exposure_statement: str = "",
                        trial_budget: int | None = None, holdout_looks: int | None = None,
                        supersedes: str | None = None, replaces: str | None = None) -> dict:
        """Create and ACTIVATE a research protocol (immutable; see research/protocol.py). Windows are
        trading dates on the dataset's calendar. `pre_protocol_exposure` lists run ids made before
        activation (recorded by identity only - never metrics, never re-attributed as trials).
        `supersedes` (ADR-67): the ACTIVE protocol of the same scope is replaced by this new one - allowed ONLY
        while it is unused (zero trial events, zero holdout accesses); it is retired (never edited) and the new
        material records its id. The new record is fully built and validated before anything is retired.
        `replaces` (ADR-86): the ACTIVE protocol of the same scope is retired (never edited) and this one activated even
        though it HAS been used; the history must be documented in `exposure_statement` / `pre_protocol_exposure` (the
        material itself is built exactly as without this argument). Used by the strategy-pool-2 switch."""
        from edgelab.core.identity import code_version, hash_obj
        from edgelab.engine.costs import cost_model_from_config
        from edgelab.research import protocol as rp
        self.store._require_search_storage()
        ds = self.load_dataset(dataset_id)                  # re-validates + re-checks the content hash
        m = ds.manifest
        if m.quality_status == "FAIL":
            raise rp.ProtocolRefusal("PROTOCOL_DATASET_INELIGIBLE", "dataset failed validation", dataset_id=dataset_id)
        cm = cost_model_from_config(self.cfg, m.instrument, provider=m.provider)   # unconfigured costs refuse
        windows = rp.resolve_windows(ds, discovery, holdout)
        exposure = []
        for item in pre_protocol_exposure:
            rid = item if isinstance(item, str) else item["run_id"]
            rec, _ = self.store.load_run(rid)               # KeyError: an unverifiable exposure is refused
            d = rec.get("dataset") or {}
            exposure.append({"run_id": rid, "strategy_id": (rec.get("strategy") or {}).get("strategy_id"),
                             "run_status": rec.get("status"), "dataset_id": d.get("dataset_id"),
                             "parent_dataset_id": d.get("parent_dataset_id"), "start": str(d.get("start")),
                             "end": str(d.get("end")), "trades_hash": rec.get("trades_hash"),
                             "note": "" if isinstance(item, str) else str(item.get("note", ""))})
        material = rp.build_material(
            dataset_manifest=m, windows=windows, cost_model=cm.to_dict(), backtest_config_hash=hash_obj(self.cfg["backtest"]),
            config_hash=self._config_hash(), pre_protocol_exposure=exposure, exposure_statement=exposure_statement,
            name=name, trial_budget=trial_budget or rp.DEFAULT_TRIAL_BUDGET,
            holdout_looks=holdout_looks or rp.DEFAULT_HOLDOUT_LOOKS, supersedes=supersedes)
        rec = rp.make_record(material, {"code_version": code_version()})
        key = self._scope_key(m.instrument, m.provider)
        active = self.store.list_protocols(key, "ACTIVE")
        if supersedes is not None:
            old = self.store.get_protocol(supersedes)                     # KeyError: unknown protocol
            rp.verify_record(old)
            if not active or active[0]["protocol_id"] != supersedes:
                raise rp.ProtocolRefusal("PROTOCOL_SUPERSEDE_INVALID", "only the ACTIVE protocol of this scope can be "
                                         "superseded", protocol_id=supersedes, scope=key)
            events = self.store.list_trial_events(supersedes)
            looks = self.store.list_holdout_access(supersedes)
            if events or looks:
                raise rp.ProtocolRefusal("PROTOCOL_IN_USE", "a protocol with recorded trials or holdout accesses is never "
                                         "superseded; retire it and document the history instead",
                                         protocol_id=supersedes, trial_events=len(events), holdout_accesses=len(looks))
            if rec["protocol_id"] == supersedes:
                raise rp.ProtocolRefusal("PROTOCOL_SUPERSEDE_INVALID", "the new material equals the old one")
            active = []                                  # retired below, only after every check has passed
        if replaces is not None:
            if supersedes is not None:
                raise rp.ProtocolRefusal("PROTOCOL_SUPERSEDE_INVALID", "use either supersedes or replaces")
            old = self.store.get_protocol(replaces)                       # KeyError: unknown protocol
            rp.verify_record(old)
            if not active or active[0]["protocol_id"] != replaces:
                raise rp.ProtocolRefusal("PROTOCOL_REPLACE_INVALID", "only the ACTIVE protocol of this scope can be "
                                         "replaced", protocol_id=replaces, scope=key)
            if rec["protocol_id"] == replaces:
                raise rp.ProtocolRefusal("PROTOCOL_REPLACE_INVALID", "the new material equals the old one")
            if not str(exposure_statement).strip():
                raise rp.ProtocolRefusal("PROTOCOL_REPLACE_INVALID", "replacing a used protocol needs an exposure "
                                         "statement documenting its history")
            active = []                                  # retired below, only after every check has passed
        if active and active[0]["protocol_id"] != rec["protocol_id"]:
            raise rp.ProtocolRefusal("PROTOCOL_ALREADY_ACTIVE", f"{active[0]['protocol_id']} is ACTIVE for {key}; "
                                     "retire it explicitly before activating another protocol",
                                     active_protocol_id=active[0]["protocol_id"])
        try:
            if self.store.get_protocol(rec["protocol_id"])["status"] == "RETIRED":
                raise rp.ProtocolRefusal("PROTOCOL_RETIRED", "a retired protocol is never re-activated",
                                         protocol_id=rec["protocol_id"])
        except KeyError:
            pass
        if supersedes is not None:
            self.store.retire_protocol(supersedes)
        if replaces is not None:
            self.store.retire_protocol(replaces)
        created = self.store.save_protocol(rec, key)
        return _jsonable({**self.store.get_protocol(rec["protocol_id"]), "created": created})

    def get_protocol(self, protocol_id: str) -> dict:
        from edgelab.research.protocol import verify_record
        rec = self.store.get_protocol(protocol_id)
        verify_record(rec)
        return _jsonable(rec)

    def list_protocols(self) -> list[dict]:
        return _jsonable([{k: r[k] for k in ("protocol_id", "status", "created_at")} | {
            "scope": r["material"]["scope"], "name": r["material"]["name"],
            "protocol_version": r["material"].get("protocol_version"),
            "source_dataset_id": r["material"]["source_dataset"]["dataset_id"],
            "discovery_trading_dates": r["material"]["windows"]["discovery"]["trading_dates"],
            "holdout_trading_dates": r["material"]["windows"]["holdout"]["trading_dates"],
            "discovery_period": self.protocol_discovery_period(r)} for r in self.store.list_protocols()])

    def retire_protocol(self, protocol_id: str) -> dict:
        """ACTIVE -> RETIRED (the only lifecycle change; material and ledgers are kept unchanged)."""
        self.store.get_protocol(protocol_id)
        self.store.retire_protocol(protocol_id)
        return self.get_protocol(protocol_id)

    def protocol_status(self, protocol_id: str) -> dict:
        """Program-wide counters of one protocol (no metrics, no run results)."""
        from collections import Counter
        from edgelab.research.protocol import bonferroni, verify_record
        p = self.store.get_protocol(protocol_id)
        verify_record(p)
        mat = p["material"]
        ev = self.store.list_trial_events(protocol_id)
        counted = [e for e in ev if e["counted"]]
        props = self.store.list_proposal_attempts(protocol_id)
        ho = self.store.list_holdout_access(protocol_id)
        looks = [h for h in ho if h["status"] != "refused"]
        n, budget = len(counted), mat["trial_budget"]["max_unique_trials"]
        hb = mat["holdout_budget"]["max_unique_candidate_evaluations"]
        from edgelab.research.protocol import family_size
        mt = bonferroni(mat["multiple_testing"], family_size(mat, n))
        declared = mat["multiple_testing"].get("family_size_rule") == "declared_max_unique_trials"
        return _jsonable({
            "protocol_id": protocol_id, "status": p["status"], "scope": mat["scope"],
            "source_dataset": mat["source_dataset"],
            "windows": {k: {x: mat["windows"][k][x] for x in ("trading_dates", "first_bar", "last_bar", "n_bars")}
                        for k in ("discovery", "holdout")},
            "trials": {"unique_numerical_trials": n, "budget": budget, "remaining": max(0, budget - n),
                       "unique_logic_hashes": len({e["logic_hash"] for e in counted}),
                       "evaluation_events": len(ev),
                       "duplicate_events": sum(1 for e in ev if e["status"] == "completed" and not e["counted"]),
                       "failed_events": sum(1 for e in ev if e["status"] == "failed"),
                       "by_entry_point": dict(Counter(e["entry_point"] for e in counted)),
                       "by_family": dict(Counter(e["family"] for e in counted)),
                       "by_dataset_window": dict(Counter(f"{e['dataset_id']} [{e['window_start']} .. {e['window_end']}]"
                                                         for e in counted))},
            "proposal_attempts": {"total": len(props), "by_source": dict(Counter(a["source"] for a in props)),
                                  "by_gate_status": dict(Counter(a["gate_status"] for a in props)),
                                  "distinct_logic_hashes": len({a["logic_hash"] for a in props if a["logic_hash"]})},
            "holdout": {"looks_used": len(looks), "budget": hb, "remaining": max(0, hb - len(looks)),
                        "refusals": sum(1 for h in ho if h["status"] == "refused"),
                        "evaluated_strategy_ids": [h["strategy_id"] for h in looks]},
            "multiple_testing": {**mt, "effect": (
                f"a holdout candidate passes oos_confidence only if its OOS expectancy_r lower bound at one-sided "
                f"alpha {mt['per_test_alpha']:.3g} exceeds {mat['acceptance_criteria']['oos_confidence']['must_exceed']}: "
                + ("mean - max(z, q_boot) * se with z = "
                   f"{mt['z_one_sided']:.4f} and q_boot the bootstrap-t quantile at the same alpha"
                   if mat['acceptance_criteria']['oos_confidence'].get('method_id') == 'min_normal_bootstrap_t_v1'
                   else f"mean - {mt['z_one_sided']:.4f} * se")
                + ("; the family is the DECLARED trial budget, fixed before discovery (it does not change with the "
                   "counted trials)" if declared else
                   "; alpha shrinks with every counted unique trial (family_size) and is recomputed at evaluation time"))},
            "supersedes": mat.get("supersedes"),
            "pre_protocol_exposure": mat["pre_protocol_exposure"]})

    def _protocol_gate(self, ds, strat, entry_point: str, holdout_access_id: str | None) -> dict | None:
        """Classify one evaluation under the governing protocol and refuse what the protocol forbids.
        Returns the attribution context (None when no protocol governs the dataset)."""
        from edgelab.research import protocol as rp
        m = ds.manifest
        p = self._governing_protocol(m.instrument, m.provider)
        if p is None:
            if holdout_access_id:
                raise rp.ProtocolRefusal("PROTOCOL_NOT_ACTIVE", "no ACTIVE protocol governs this dataset",
                                         dataset_id=m.dataset_id)
            return None
        pid, mat = p["protocol_id"], p["material"]
        if self._config_hash() != mat["config_hash"]:
            raise rp.ProtocolRefusal("PROTOCOL_CONFIG_CHANGED", "the research config (costs, fills, sessions, backtest "
                                     "rules) differs from the protocol's; create a new protocol",
                                     protocol_id=pid, protocol_config_hash=mat["config_hash"],
                                     current_config_hash=self._config_hash())
        first, last = int(ds.bars.ts_ns[0]), int(ds.bars.ts_ns[-1])
        # ADR-85: the exact holdout window may also be read on a timeframe derived from the protocol source, only
        # through a granted access (the discovery test is unchanged)
        tf_ns = int(ds.bars.tf_minutes) * 60_000_000_000 if holdout_access_id else None
        stage = rp.stage_of(p, first, last, tf_ns)
        ident = strat.compiled.identity
        ctx = {"protocol_id": pid, "stage": stage, "entry_point": entry_point,
               "window": [rp._iso(first), rp._iso(last)], "strategy_id": ident.strategy_id,
               "logic_hash": ident.logic_hash, "definition_hash": ident.definition_hash,
               "family": strat.compiled.family_id, "config_hash": mat["config_hash"],
               "cost_scenario": mat["execution"]["cost_scenario"]}
        if stage == "discovery":
            if holdout_access_id:
                raise rp.ProtocolRefusal("HOLDOUT_ACCESS_INVALID", "a holdout access applies only to the exact holdout "
                                         "window", protocol_id=pid, access_id=holdout_access_id, window=ctx["window"])
            key = rp.trial_key(pid, ident.logic_hash, m.content_hash, mat["config_hash"])
            ctx.update(trial_key=key, trial_id="TR_" + key[:12].upper())
            budget = mat["trial_budget"]["max_unique_trials"]
            if not self.store.trial_counted(pid, key) and self.store.count_trials(pid) >= budget:
                raise rp.ProtocolRefusal("PROTOCOL_TRIAL_BUDGET_EXHAUSTED", f"the protocol's {budget} unique numerical "
                                         "trials are used", protocol_id=pid, budget=budget)
            return ctx
        if stage == "holdout" and holdout_access_id and entry_point in ("holdout_evaluation", "random_control_candidate"):
            acc = [a for a in self.store.list_holdout_access(pid) if a["access_id"] == holdout_access_id]
            if acc and acc[0]["status"] == "granted" and acc[0]["logic_hash"] == ident.logic_hash:
                ctx["holdout_access_id"] = holdout_access_id
                return ctx
            raise rp.ProtocolRefusal("HOLDOUT_ACCESS_INVALID", "the holdout access is not an open grant for this "
                                     "strategy", protocol_id=pid, access_id=holdout_access_id)
        raise rp.ProtocolRefusal(
            "HOLDOUT_LOCKED", "the evaluated bars touch the locked holdout (or lie outside the discovery window); "
            "only Services.evaluate_holdout may read the holdout", protocol_id=pid, entry_point=entry_point,
            window=ctx["window"], discovery_trading_dates=mat["windows"]["discovery"]["trading_dates"],
            holdout_trading_dates=mat["windows"]["holdout"]["trading_dates"])

    def _proposal_of(self, strategy_id: str) -> str | None:
        try:
            for rec in self.library.load(strategy_id).get("lineage") or []:
                pid = (rec.get("generation_parameters") or {}).get("proposal_id")
                if pid:
                    return pid
        except (KeyError, FileNotFoundError):
            pass
        return None

    def _protocol_record(self, ctx: Mapping, dataset: Mapping, run_id: str | None, entry_point: str,
                         search_id: str | None = None, status: str = "completed", error: str | None = None) -> bool:
        from datetime import datetime, timezone
        return self.store.add_trial_event({
            "protocol_id": ctx["protocol_id"], "trial_id": ctx.get("trial_id"), "trial_key": ctx.get("trial_key"),
            "status": status, "entry_point": entry_point, "strategy_id": ctx["strategy_id"],
            "logic_hash": ctx["logic_hash"], "definition_hash": ctx["definition_hash"], "family": ctx.get("family"),
            "dataset_id": dataset.get("dataset_id"), "source_dataset_id": dataset.get("parent_dataset_id") or dataset.get("dataset_id"),
            "evaluated_content_hash": dataset.get("content_hash"), "window_start": str(dataset.get("start")),
            "window_end": str(dataset.get("end")), "config_hash": ctx["config_hash"], "cost_scenario": ctx.get("cost_scenario"),
            "proposal_id": self._proposal_of(ctx["strategy_id"]), "search_id": search_id, "run_id": run_id,
            "error": error, "created_at": datetime.now(timezone.utc).isoformat()})

    def _protocol_record_search_cell(self, protocol_id: str, search_id: str, cell: Mapping, outcome: Mapping,
                                     run_id: str | None) -> None:
        """Search runner hook (parent process, sequential and parallel alike): one ledger event per
        executed cell of a governed dataset; failures are recorded, never counted."""
        from edgelab.research import protocol as rp
        p = self.store.get_protocol(protocol_id)
        m = self.store.get_manifest(cell["dataset_id"])
        if self._scope_key(m.instrument, m.provider) != self._scope_key(p["material"]["scope"]["instrument"],
                                                                          p["material"]["scope"]["provider"]):
            return
        doc = self.library.load(cell["strategy_id"])
        ctx = {"protocol_id": protocol_id, "strategy_id": cell["strategy_id"], "logic_hash": doc["logic_hash"],
               "definition_hash": doc["definition_hash"], "config_hash": p["material"]["config_hash"],
               "cost_scenario": p["material"]["execution"]["cost_scenario"]}
        if "error" in outcome:
            self._protocol_record({**ctx, "family": None, "trial_key": None}, {"dataset_id": cell["dataset_id"]},
                                  None, "search_cell", search_id, status="failed", error=outcome["error"])
            return
        res = outcome["result"]
        key = rp.trial_key(protocol_id, doc["logic_hash"], res.dataset["content_hash"], ctx["config_hash"])
        self._protocol_record({**ctx, "family": (res.strategy_spec or {}).get("family"), "trial_key": key,
                               "trial_id": "TR_" + key[:12].upper()}, res.dataset, run_id, "search_cell", search_id)

    @staticmethod
    def protocol_discovery_period(protocol: Mapping) -> dict:
        """THE discovery window a client should request under a protocol (ADR-70): [discovery session open,
        holdout session open - 1 s], the same window the frozen campaign uses (research.campaign.discovery_period).
        Served as data on protocol rows so the UI never computes or guesses a window; an explicit window that reaches
        the holdout is still refused by _protocol_interval_check (never clipped), and a windowless request on a
        governed dataset stays refused."""
        from edgelab.research.campaign import discovery_period
        return discovery_period(protocol["material"])

    def _protocol_interval_check(self, manifest, start: Any, end: Any, entry_point: str) -> str | None:
        """Refuse a requested window of a governed dataset that is not inside the discovery window."""
        from edgelab.research import protocol as rp
        p = self._governing_protocol(manifest.instrument, manifest.provider)
        if p is None:
            return None
        s = max(pd.Timestamp(start), pd.Timestamp(manifest.start)) if start is not None else pd.Timestamp(manifest.start)
        e = min(pd.Timestamp(end), pd.Timestamp(manifest.end)) if end is not None else pd.Timestamp(manifest.end)
        if rp.interval_stage(p, s, e) != "discovery":
            mat = p["material"]
            raise rp.ProtocolRefusal(
                "HOLDOUT_LOCKED", "the requested window touches the locked holdout; restrict it to the discovery "
                "window", protocol_id=p["protocol_id"], entry_point=entry_point, dataset_id=manifest.dataset_id,
                requested=[str(s), str(e)], discovery_trading_dates=mat["windows"]["discovery"]["trading_dates"],
                discovery_last_bar=mat["windows"]["discovery"]["last_bar"],
                holdout_trading_dates=mat["windows"]["holdout"]["trading_dates"])
        return p["protocol_id"]

    def _protocol_precheck(self, manifest, windows: list, entry_point: str) -> None:
        for s, e in windows:                               # all windows before any window runs (no side effects)
            self._protocol_interval_check(manifest, s, e, entry_point)

    def _protocol_plan_check(self, manifests: list, period: Mapping | None) -> str | None:
        """plan_search hook: every governed dataset must be searched inside the discovery window."""
        pids = set()
        for m in manifests:
            pid = self._protocol_interval_check(m, None if period is None else period["start"],
                                                None if period is None else period["end"], "search_cell")
            if pid:
                pids.add(pid)
        if len(pids) > 1:
            from edgelab.research.protocol import ProtocolRefusal
            raise ProtocolRefusal("PROTOCOL_MISMATCH", "one search cannot span several protocols",
                                  protocol_ids=sorted(pids))
        return next(iter(pids), None)

    def _protocol_budget_check(self, protocol_id: str, cells_to_run: list) -> None:
        """A search may start only if every cell it will run fits the remaining trial budget
        (conservative: each pending cell is assumed to be a new unique trial)."""
        from edgelab.research.protocol import ProtocolRefusal
        mat = self.store.get_protocol(protocol_id)["material"]
        budget = mat["trial_budget"]["max_unique_trials"]
        used = self.store.count_trials(protocol_id)
        if used + len(cells_to_run) > budget:
            raise ProtocolRefusal("PROTOCOL_TRIAL_BUDGET_EXHAUSTED", f"{len(cells_to_run)} cells would exceed the "
                                  f"remaining trial budget ({budget - used} of {budget})", protocol_id=protocol_id,
                                  budget=budget, used=used, requested=len(cells_to_run))

    def _protocol_record_mode_b(self, rep) -> None:
        """Mode B batches (human/AI proposal ingestion) are proposal attempts of every ACTIVE protocol."""
        if getattr(self.store, "backend", None) != "sqlite":
            return
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        for p in self.store.list_protocols(status="ACTIVE"):
            for i, a in enumerate(rep.accepted):
                sid = a.get("strategy_id") if isinstance(a, Mapping) else a
                self.store.add_proposal_attempt({"protocol_id": p["protocol_id"],
                                                 "proposal_id": f"{rep.batch_id}:{i}", "source": "mode_b_ingest",
                                                 "generation_id": rep.batch_id, "gate_status": "valid",
                                                 "logic_hash": None, "strategy_id": sid, "created_at": now})
            for i, r in enumerate(rep.rejected):
                self.store.add_proposal_attempt({"protocol_id": p["protocol_id"],
                                                 "proposal_id": f"{rep.batch_id}:rejected:{i}", "source": "mode_b_ingest",
                                                 "generation_id": rep.batch_id, "gate_status": "rejected",
                                                 "logic_hash": None, "strategy_id": None, "created_at": now})

    # ------------------------------------------------------------ holdout backtests of survivors (ADR-85)
    def holdout_candidates(self) -> dict:
        """Current survivors ranked for prop trading (discovery numbers only), whether each can take a holdout test,
        and the protocol's looks left."""
        from edgelab.research import holdout
        return _jsonable(holdout.candidates(self))

    def holdout_history(self) -> list[dict]:
        from edgelab.research import holdout
        return _jsonable(holdout.history(self))

    def start_holdout_job(self, strategy_ids: list) -> dict:
        """Holdout backtests of the chosen survivors in the background (one research job at a time)."""
        return _jsonable(self.jobs.start_holdout(strategy_ids))

    def holdout_job(self, job_id: str) -> dict:
        return _jsonable(self.jobs.holdout_status(job_id))

    # ------------------------------------------------------------ strategy pool 2 (ADR-86)
    def pool2_status(self) -> dict:
        """Read-only: pool 1 / pool 2 manifests, the active protocol and what the protocol switch would do."""
        from edgelab.research import pool2
        return _jsonable(pool2.status(self))

    def start_pool2_job(self, action: str, confirm: str | None = None) -> dict:
        """Background job: ``generate`` pool 2 (safe) or ``switch`` to the 20,000-strategy protocol (typed confirmation)."""
        from edgelab.research import pool2
        if action == "switch" and confirm != pool2.CONFIRM_WORD:
            raise ValueError(f"type {pool2.CONFIRM_WORD} to confirm the protocol switch")
        return _jsonable(self.jobs.start_pool(action, confirm))

    def pool2_job(self, job_id: str) -> dict:
        return _jsonable(self.jobs.pool_status(job_id))

    def _holdout_dataset(self, protocol: Mapping, frozen: Mapping, refuse) -> tuple[str, tuple]:
        """(dataset id, period) for a holdout evaluation, checked without spending a look (ADR-85): the dataset of
        the strategy's own timeframe from the protocol source import, the period = the holdout session open .. the
        last holdout source bar; the cut must classify as exactly the holdout window, and costs and the instrument
        identity must allow a backtest. Any problem calls ``refuse`` (recorded as a refused access)."""
        from edgelab.engine.costs import cost_model_from_config
        from edgelab.instruments import check_identity
        from edgelab.research import campaign
        from edgelab.research import protocol as rp
        mat = protocol["material"]
        tf = str(frozen.get("timeframe") or (frozen.get("logic") or {}).get("timeframe") or "")
        by_tf, problems = campaign.resolve_datasets(self, protocol, {tf})
        if tf not in by_tf:
            refuse("HOLDOUT_DATASET_UNRESOLVED", f"no {tf} dataset of the protocol source import to test on",
                   timeframe=tf, problems=problems)
        h = mat["windows"]["holdout"]
        period = (pd.Timestamp(h["boundary_open"]), pd.Timestamp(h["last_bar"]))
        did = by_tf[tf]["dataset_id"]
        try:
            ds = self._cell_dataset(did, period)
            check_identity(ds.instrument)
            cost_model_from_config(self.cfg, ds.instrument.symbol, provider=ds.manifest.provider)
        except Exception as exc:                                   # refused: no look spent
            refuse("HOLDOUT_DATASET_UNRESOLVED", f"the {tf} holdout data cannot be backtested: {exc}", dataset_id=did)
        tf_ns = int(ds.bars.tf_minutes) * 60_000_000_000
        if rp.stage_of(protocol, int(ds.bars.ts_ns[0]), int(ds.bars.ts_ns[-1]), tf_ns) != "holdout":
            refuse("HOLDOUT_WINDOW_MISMATCH", f"the {tf} data does not cover exactly the holdout window",
                   dataset_id=did, first_bar=rp._iso(int(ds.bars.ts_ns[0])), last_bar=rp._iso(int(ds.bars.ts_ns[-1])))
        return did, period

    def evaluate_holdout(self, protocol_id: str, search_id: str, strategy_id: str) -> dict:
        """THE holdout stage (ADR-56). One look per frozen, shortlisted candidate, within the protocol's
        holdout-look budget: runs the candidate on exactly the locked holdout bars (run status
        OUT_OF_SAMPLE), the protocol's random-entry control and cost stress, and applies the
        pre-registered criteria with the protocol's Bonferroni family (declared budget for version 3,
        counted unique trials for version <= 2).
        Every attempt (granted or refused) is written to the holdout ledger first. Never 'accepted'."""
        import json as _json
        from datetime import datetime, timezone
        from edgelab.analytics.metrics import cost_sensitivity
        from edgelab.core.identity import hash_obj
        from edgelab.research import protocol as rp
        from edgelab.research.validation import copy_frozen, freeze_definition
        now = lambda: datetime.now(timezone.utc).isoformat()                          # noqa: E731
        p = self.store.get_protocol(protocol_id)                                    # KeyError: unknown protocol
        rp.verify_record(p)
        mat = p["material"]
        doc = self.library.load(strategy_id)
        frozen, fh = freeze_definition(doc["definition"])
        aid = "HA_" + hash_obj({"protocol_id": protocol_id, "strategy_id": strategy_id, "search_id": search_id,
                                "at": now()}, 12).upper()
        base = {"access_id": aid, "protocol_id": protocol_id, "strategy_id": strategy_id,
                "logic_hash": doc["logic_hash"], "definition_hash": doc["definition_hash"], "frozen_hash": fh,
                "search_id": search_id, "created_at": now()}

        def refuse(code: str, msg: str, **detail):
            self.store.add_holdout_access({**base, "status": "refused", "reason_code": code, "reason": msg})
            raise rp.ProtocolRefusal(code, msg, protocol_id=protocol_id, strategy_id=strategy_id, access_id=aid, **detail)

        if p["status"] != "ACTIVE":
            refuse("PROTOCOL_NOT_ACTIVE", f"protocol is {p['status']}")
        if self._config_hash() != mat["config_hash"]:
            refuse("PROTOCOL_CONFIG_CHANGED", "the research config differs from the protocol's")
        b = self.store.get_search_batch(search_id)
        if b is None or b.get("protocol_id") != protocol_id:
            refuse("HOLDOUT_SEARCH_NOT_IN_PROTOCOL", "the search is unknown or not attributed to this protocol")
        tag = _json.loads(b["shortlist_json"]) if b.get("shortlist_json") else {}
        if strategy_id not in (tag.get("strategy_ids") or []):
            refuse("HOLDOUT_NOT_SHORTLISTED", "the candidate is not on the search's shortlist")
        if not any(e["counted"] and e["logic_hash"] == doc["logic_hash"]
                   for e in self.store.list_trial_events(protocol_id)):
            refuse("HOLDOUT_NOT_DISCOVERY_EVALUATED", "the candidate has no counted discovery trial in this protocol")
        prior = self.store.list_holdout_access(protocol_id)
        if any(a["logic_hash"] == doc["logic_hash"] and a["status"] != "refused" for a in prior):
            refuse("HOLDOUT_ALREADY_EVALUATED", "this candidate's logic was already evaluated on the holdout")
        if strategy_id in rp.holdout_exposed(p):                                  # ADR-86: looked at under an earlier protocol
            refuse("HOLDOUT_ALREADY_EVALUATED", "this candidate was already evaluated on the holdout under an earlier "
                   "protocol (listed in this protocol's prior exposure)")
        used = sum(1 for a in prior if a["status"] != "refused")
        if used >= mat["holdout_budget"]["max_unique_candidate_evaluations"]:
            refuse("HOLDOUT_BUDGET_EXHAUSTED", f"all {used} holdout looks are used")
        from edgelab.strategy.compiler import compile_definition
        ident = compile_definition(copy_frozen(frozen), self.sessions, self._config_hash()).identity
        if (ident.strategy_id, ident.logic_hash) != (strategy_id, doc["logic_hash"]):             # before the look is spent
            refuse("HOLDOUT_DEFINITION_CHANGED", "the stored definition no longer compiles to the shortlisted "
                   "candidate's identity", compiled_strategy_id=ident.strategy_id)
        # ADR-85: everything that could fail is checked BEFORE the look is spent: the dataset of the strategy's own
        # timeframe (the protocol source import or a dataset derived from it), cut to exactly the holdout trading dates
        src, period = self._holdout_dataset(p, frozen, refuse)
        n_trials = self.store.count_trials(protocol_id)
        self.store.add_holdout_access({**base, "status": "granted"})              # the look is spent from here on
        h = mat["windows"]["holdout"]
        try:
            cell = self._run_cell(copy_frozen(frozen), src, True, period=period, status="OUT_OF_SAMPLE",
                                  notes=f"protocol {protocol_id} holdout evaluation {aid}",
                                  entry_point="holdout_evaluation", holdout_access_id=aid)
            rc = mat["acceptance_criteria"]["random_control"]
            ctrl = self.random_entry_control(copy_frozen(frozen), src, n_controls=rc["n_controls"], seed=rc["seed"],
                                             period=period, sample_status="OUT_OF_SAMPLE", holdout_access_id=aid)
            if freeze_definition(frozen)[1] != fh:
                raise RuntimeError("frozen strategy definition changed during the holdout evaluation")
            trades = cell["result"].trades
            costs = (cost_sensitivity(trades, mat["acceptance_criteria"]["cost_stress"]["multipliers"]).to_dict("records")
                     if len(trades) else [])
            verdict = rp.assess_holdout(mat, cell["metrics"], costs,
                                        [r.get("expectancy_r") for r in ctrl["realizations"]], n_trials,
                                        trade_r=trades["net_r"].to_numpy(float) if len(trades) else [],
                                        seed=rp.derive_seed(protocol_id, doc["logic_hash"]))
        except Exception as exc:
            self.store.update_holdout_access(aid, status="failed", completed_at=now(),
                                             reason=f"{type(exc).__name__}: {exc}")
            raise
        out = {"access_id": aid, "protocol_id": protocol_id, "strategy_id": strategy_id, "search_id": search_id,
               "frozen_hash": fh, "run_id": cell["run_id"], "run_status": "OUT_OF_SAMPLE",
               "window": {"trading_dates": h["trading_dates"], "first_bar": h["first_bar"], "last_bar": h["last_bar"]},
               "trade_count": cell["metrics"].get("trade_count"), **verdict,
               "holdout_looks_used": used + 1,
               "holdout_looks_budget": mat["holdout_budget"]["max_unique_candidate_evaluations"]}
        self.store.update_holdout_access(aid, status="completed", run_id=cell["run_id"], completed_at=now(),
                                         result_json=_json.dumps(_jsonable(out), sort_keys=True))
        return _jsonable(out)

    # ============================================================ PROP SIMULATION (Phase 6)
    def prop_configs(self) -> list[dict]:
        """Prop rule sets in configs/prop (outside the research config hash), each validated."""
        from edgelab.prop import service as ps
        return _jsonable(ps.list_configs(self.root))

    def validate_prop_config(self, src: Any) -> dict:
        from edgelab.prop import service as ps
        return _jsonable(ps.validate_config(self.root, src))

    def prop_simulate(self, run_id: str, accounts: list[Mapping], record: bool = False) -> dict:
        """Replay a STORED run's trades through one or more prop accounts. The run is read, its
        trades hash re-verified, and it is proven unchanged afterwards; the simulation is recorded
        (record=True) as its own document under <data>/prop_simulations, never in the run table."""
        from edgelab.prop import service as ps
        return _jsonable(ps.simulate(self, run_id, accounts, record))

    def list_prop_simulations(self) -> list[dict]:
        from edgelab.prop import service as ps
        return _jsonable(ps.list_simulations(self.data_root))

    def get_prop_simulation(self, simulation_id: str) -> dict:
        from edgelab.prop import service as ps
        return _jsonable(ps.get_simulation(self.data_root, simulation_id))

    # ============================================================ STRATEGY LAB (Phase 8)
    def strategy_research(self, strategy_id: str) -> dict:
        """Machine-readable provenance of one strategy version + its stored runs (read-only)."""
        from edgelab.research import lab
        return _jsonable(lab.strategy_research(self, strategy_id))

    def compare_runs(self, run_ids: list[str] | None = None, strategy_id: str | None = None,
                     lineage_of: str | None = None, batch_id: str | None = None,
                     search_id: str | None = None) -> dict:
        """Transparent side-by-side metrics of stored runs from ONE source; never ranked or scored."""
        from edgelab.research import lab
        return _jsonable(lab.compare_runs(self, run_ids, strategy_id, lineage_of, batch_id, search_id))

    def run_curve(self, run_id: str) -> dict:
        from edgelab.research import lab
        return _jsonable(lab.run_curve(self, run_id))

    # ------------------------------------------------------------ research terminal read models (UI)
    # All read-only (research/overview.py): no evaluation, no trial, no ledger or protocol change.
    def research_overview(self) -> dict:
        from edgelab.research import overview as ov
        return _jsonable(ov.overview(self))

    # ------------------------------------------------------------ backtest results views (ADR-73, read-only)
    def results_overview(self, params: Mapping) -> dict:
        from edgelab.research import results_view as rv
        return _jsonable(rv.results_overview(self, params))

    def strategy_panel(self, strategy_id: str, params: Mapping) -> dict:
        from edgelab.research import results_view as rv
        return _jsonable(rv.strategy_panel(self, strategy_id, params))

    def control_panel(self, control_id: str) -> dict:
        from edgelab.research import results_view as rv
        return _jsonable(rv.control_panel(self, control_id))

    def prop_bootstrap(self, run_id: str, profile_id: str, params: Mapping) -> dict:
        """Bootstrapped evaluations of one stored run under one rule profile: cached result, or a background job's
        progress (prop/bootstrap.py; simulated, downstream, changes nothing)."""
        from edgelab.prop import bootstrap as bs
        return _jsonable(bs.request(self, run_id, profile_id, params))

    def research_runs(self) -> list[dict]:
        from edgelab.research import results_view as rv
        return _jsonable(rv.research_runs(self))

    def explore_strategies(self, params: Mapping) -> dict:
        from edgelab.research import overview as ov
        return _jsonable(ov.explorer(self, params))

    def research_dashboard(self, params: Mapping) -> dict:
        from edgelab.research import overview as ov
        return _jsonable(ov.research_dashboard(self, params))

    def run_analytics(self, run_id: str) -> dict:
        from edgelab.research import overview as ov
        return _jsonable(ov.run_analytics(self, run_id))

    def strategy_pipeline(self, strategy_id: str) -> dict:
        from edgelab.research import overview as ov
        return _jsonable(ov.strategy_pipeline(self, strategy_id))

    def pipeline_board(self) -> dict:
        from edgelab.research import overview as ov
        return _jsonable(ov.pipeline_board(self))

    def oos_random_control(self, src: Any, dataset_id: str, split_at: Any, n_controls: int = 20,
                           seed: int = 0) -> dict:
        """random_entry_control (unchanged method) on the OOS window of an evaluate_oos split."""
        from edgelab.research import lab
        return _jsonable(lab.oos_control(self, src, dataset_id, split_at, n_controls, seed))
