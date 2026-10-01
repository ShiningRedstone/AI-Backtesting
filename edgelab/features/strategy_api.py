"""How strategies consume features (the contract Phase 3's DSL will compile to).

A FeatureStrategy declares the FeatureSpecs it needs and turns a FeatureFrame into
signals. It is dataset-agnostic: the same object (same strategy_id) can be BOUND to a
futures dataset or to any CFD dataset, each with its own calendar, volume semantics and
feature cache - strategy logic never changes and data is never mixed.

Lookahead: the Phase 1 causality check calls generate_signals on truncated bars; the
bound context then computes features on those truncated bars with a fresh engine that sees
ONLY the truncated bars, so a leaky feature is caught end-to-end at the strategy level too.
ADR-77: those truncated-history features are kept in a process-wide in-memory cache whose keys
include the content hash of the truncated bars themselves (never the full history), so the
same cut of the same data is computed once instead of once per strategy; a reused entry is
exactly what a fresh computation on those truncated bars returns. EDGELAB_CAUSALITY_CACHE_MB=0
switches the cache off.
"""
from __future__ import annotations

import copy
import os
import threading
from abc import abstractmethod
from typing import Mapping

from edgelab.data.schema import BarArrays
from edgelab.data.validation import ValidatedDataset
from edgelab.engine.signals import SignalSet, Strategy
from edgelab.features.cache import FeatureCache, MemoryFeatureCache
from edgelab.features.engine import FeatureEngine, FeatureFrame
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import FeatureSpec


_TRUNC_CACHE: list = []
_TRUNC_LOCK = threading.Lock()


def truncation_budget_mb() -> int:
    """Memory budget of the truncated-history feature cache (MB; 0 = off). Parallel searches split it over workers."""
    try:
        return max(0, int(os.environ.get("EDGELAB_CAUSALITY_CACHE_MB", "1024")))
    except ValueError:
        return 1024


def truncation_cache() -> MemoryFeatureCache | None:
    """The process-wide cache for features of truncated histories (None when switched off)."""
    with _TRUNC_LOCK:
        if not _TRUNC_CACHE:
            mb = truncation_budget_mb()
            _TRUNC_CACHE.append(MemoryFeatureCache(mb * 1024 * 1024) if mb > 0 else None)
        return _TRUNC_CACHE[0]


class FeatureContext:
    def __init__(self, ds: ValidatedDataset, sessions: Mapping[str, SessionWindow],
                 cache: FeatureCache | None = None):
        self.dataset = ds
        self.sessions = dict(sessions)
        self.engine = FeatureEngine.for_dataset(ds, sessions, cache)

    def engine_for(self, bars: BarArrays) -> FeatureEngine:
        if bars is self.dataset.bars:
            return self.engine
        # Any other bars (e.g. causality truncations): a fresh engine over THOSE bars only, never mixed with the
        # dataset's own features or disk cache; its in-memory cache keys include these bars' own content hash.
        return FeatureEngine(bars, self.dataset.calendar, self.sessions,
                             self.dataset.manifest.volume_type, self.dataset.instrument.tick_size,
                             f"{self.dataset.manifest.dataset_id}#view", cache=truncation_cache(),
                             volume_refusal=self.engine.volume_refusal)


class FeatureStrategy(Strategy):
    _context: FeatureContext | None = None

    @abstractmethod
    def feature_specs(self) -> list[FeatureSpec]: ...

    @abstractmethod
    def signals_from_features(self, bars: BarArrays, f: FeatureFrame) -> SignalSet: ...

    @property
    def spec(self) -> dict:
        s = super().spec
        s["features"] = [fs.to_dict() for fs in self.feature_specs()]
        return s

    def bind(self, context: FeatureContext) -> "FeatureStrategy":
        bound = copy.copy(self)
        bound._context = context
        return bound

    def generate_signals(self, bars: BarArrays) -> SignalSet:
        if self._context is None:
            raise RuntimeError(f"{self.family}: bind(FeatureContext) before running")
        frame = self._context.engine_for(bars).frame(self.feature_specs())
        return self.signals_from_features(bars, frame)
