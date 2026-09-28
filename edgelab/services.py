"""Backend service contracts for the future web application.

Every public method returns JSON-serializable dicts/lists (no numpy, no DataFrames),
so the same functions back the CLI today and HTTP endpoints later:

  DATA CENTER            list_datasets, dataset_detail, inspect_file, import_file, compare_feeds
  FEATURE LAB            feature_catalog, feature_detail, feature_cache_status, build_features,
                         feature_values (for charts)
  RESEARCH CONFIGURATION research_config_options

Nothing here fabricates data: when something is unavailable the response says so.
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
