"""How strategies consume features (the contract Phase 3's DSL will compile to).

A FeatureStrategy declares the FeatureSpecs it needs and turns a FeatureFrame into
signals. It is dataset-agnostic: the same object (same strategy_id) can be BOUND to a
futures dataset or to any CFD dataset, each with its own calendar, volume semantics and
feature cache - strategy logic never changes and data is never mixed.

Lookahead: the Phase 1 causality check calls generate_signals on truncated bars; the
bound context then computes features on those truncated bars with an uncached engine,
so a leaky feature is caught end-to-end at the strategy level too.
"""
from __future__ import annotations

import copy
from abc import abstractmethod
from typing import Mapping

from edgelab.data.schema import BarArrays
from edgelab.data.validation import ValidatedDataset
from edgelab.engine.signals import SignalSet, Strategy
from edgelab.features.cache import FeatureCache
from edgelab.features.engine import FeatureEngine, FeatureFrame
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import FeatureSpec


class FeatureContext:
    def __init__(self, ds: ValidatedDataset, sessions: Mapping[str, SessionWindow],
                 cache: FeatureCache | None = None):
        self.dataset = ds
        self.sessions = dict(sessions)
        self.engine = FeatureEngine.for_dataset(ds, sessions, cache)

    def engine_for(self, bars: BarArrays) -> FeatureEngine:
        if bars is self.dataset.bars:
            return self.engine
        # Any other bars (e.g. causality truncations): fresh engine, never cached, never mixed.
        return FeatureEngine(bars, self.dataset.calendar, self.sessions,
                             self.dataset.manifest.volume_type, self.dataset.instrument.tick_size,
                             f"{self.dataset.manifest.dataset_id}#view", cache=None)


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
