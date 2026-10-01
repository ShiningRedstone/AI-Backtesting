"""Empirical no-lookahead test for features (same philosophy as Phase 1's strategy check).

For random cut points k, features are recomputed on bars[:k] with a fresh, UNCACHED
engine and compared with the full-history values on the same bars, exactly (NaN == NaN).
Any difference means a value at some bar depends on bars after it: the feature fails.
Higher-timeframe specs are tested end-to-end (resample -> compute -> map), which is
where 'unfinished candle' leaks live.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from edgelab.features.engine import FeatureEngine
from edgelab.features.spec import FeatureSpec


@dataclass
class FeatureCausalityReport:
    spec_label: str
    passed: bool
    cuts: list[int] = field(default_factory=list)
    output: str | None = None
    bar: int | None = None
    cut: int | None = None
    detail: str = ""


def _same(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    return (a == b) | (np.isnan(a) & np.isnan(b))


def check_feature_causality(engine: FeatureEngine, spec: FeatureSpec, n_cuts: int = 12,
                            seed: int = 7, min_bars: int = 50, cuts: list[int] | None = None
                            ) -> FeatureCausalityReport:
    n = len(engine.bars)
    full = FeatureEngine(engine.bars, engine.calendar, engine.sessions, engine.volume_type,
                         engine.tick_size, engine.dataset_id, engine.dataset_hash, cache=None,
                         volume_refusal=engine.volume_refusal).compute(spec)
    if cuts is None:
        rng = np.random.default_rng(seed)
        lo = min(max(min_bars, n // 20), n - 1)
        cuts = sorted(set(rng.integers(lo, n, size=n_cuts).tolist()) | {n - 1})
    for k in cuts:
        part_eng = FeatureEngine(engine.bars.head(k), engine.calendar, engine.sessions,
                                 engine.volume_type, engine.tick_size, f"{engine.dataset_id}#cut{k}",
                                 cache=None, volume_refusal=engine.volume_refusal)
        part = part_eng.compute(spec)
        for name, a_full in full.arrays.items():
            same = _same(a_full[:k], part.arrays[name])
            if not same.all():
                bar = int(np.flatnonzero(~same)[0])
                return FeatureCausalityReport(spec.label, False, cuts, name, bar, k,
                                              f"{spec.label}.{name} at bar {bar} changes when history is "
                                              f"cut at {k}: uses information from after the bar")
    return FeatureCausalityReport(spec.label, True, cuts)
