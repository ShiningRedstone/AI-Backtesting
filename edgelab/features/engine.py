"""Feature engine: the single entry point strategies (Phase 3), the research engine
(Phase 4) and the Feature Lab UI use to obtain features.

    engine = FeatureEngine.for_dataset(ds, sessions, cache)
    res = engine.compute(FeatureSpec.make("atr", {"period": 14}, timeframe="15m"))
    res.arrays["atr"]          # aligned to ds bars, float64, read-only

Guarantees
  * outputs are aligned to the dataset's bars and only use information available at
    each bar's close (native features) or at the close of the last COMPLETED higher-
    timeframe bar (``timeframe`` > native, see features.mtf);
  * identical inputs -> identical cache key -> bit-identical arrays;
  * requirements are checked: a volume feature on a no-volume dataset raises
    FeatureUnavailable (with the reason) instead of producing garbage.
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Iterable, Mapping

import numpy as np

from edgelab.core.identity import hash_obj
from edgelab.data.calendar import SessionCalendar
from edgelab.data.schema import BarArrays, timeframe_minutes
from edgelab.data.validation import ValidatedDataset
from edgelab.features.cache import CACHE_SCHEMA_VERSION, FeatureCache
from edgelab.features.context import FeatureInput
from edgelab.features.mtf import build_htf, map_to_base
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import FeatureError, FeatureSpec, FeatureUnavailable

VOLUME_OK = ("exchange", "tick", "synthetic", "unknown")


@dataclass
class FeatureResult:
    spec: FeatureSpec
    arrays: dict[str, np.ndarray]
    cache_key: str
    cache_source: str | None          # "memory" | "disk" | None (computed now)
    seconds: float
    base_timeframe_minutes: int
    computed_timeframe_minutes: int
    mapped_from_higher_timeframe: bool
    meta: dict = field(default_factory=dict)

    @property
    def known_at(self) -> str:
        return ("close of the last completed higher-timeframe bar"
                if self.mapped_from_higher_timeframe else self.spec.definition.known_at)


class FeatureFrame(Mapping):
    """Column view over several FeatureResults: frame['atr(period=14).atr'] or frame.get(spec, 'atr')."""

    def __init__(self, results: Iterable[FeatureResult]):
        self.results = {r.spec: r for r in results}
        self._cols = {f"{r.spec.label}.{k}": v for r in self.results.values() for k, v in r.arrays.items()}

    def __getitem__(self, k):
        return self._cols[k]

    def __iter__(self):
        return iter(self._cols)

    def __len__(self):
        return len(self._cols)

    def get_output(self, spec: FeatureSpec, output: str) -> np.ndarray:
        return self.results[spec].arrays[output]


class FeatureEngine:
    def __init__(self, bars: BarArrays, calendar: SessionCalendar,
                 sessions: Mapping[str, SessionWindow], volume_type: str, tick_size: float,
                 dataset_id: str, dataset_hash: str | None = None, cache: FeatureCache | None = None,
                 volume_refusal: str | None = None):
        self.bars = bars
        self.calendar = calendar
        self.sessions = dict(sessions)
        self.volume_type = volume_type
        # ADR-62: an instrument whose volume is provider-defined (e.g. Dukascopy decimal volume, NOT CME
        # exchange-traded volume) never feeds volume-weighted features. Not part of any cache key.
        self.volume_refusal = volume_refusal
        self.tick_size = tick_size
        self.dataset_id = dataset_id
        self.dataset_hash = dataset_hash or bars.content_hash()
        self.cache = cache
        self._ctx: dict[int, tuple[BarArrays, np.ndarray]] = {}
        self._known: dict[int, np.ndarray] = {}
        self._keys: dict[tuple, str] = {}
        self._results: dict[FeatureSpec, FeatureResult] = {}

    @classmethod
    def for_dataset(cls, ds: ValidatedDataset, sessions: Mapping[str, SessionWindow],
                    cache: FeatureCache | None = None) -> "FeatureEngine":
        ds.verify_unchanged()
        return cls(ds.bars, ds.calendar, sessions, ds.manifest.volume_type, ds.instrument.tick_size,
                   ds.manifest.dataset_id, ds.manifest.content_hash, cache,
                   volume_refusal=(ds.instrument.extra or {}).get("volume_semantics"))

    # ------------------------------------------------------------------ contexts
    def _context(self, tf: int) -> tuple[BarArrays, np.ndarray]:
        if tf not in self._ctx:
            if tf == self.bars.tf_minutes:
                self._ctx[tf] = (self.bars, self.bars.ts_close_ns)
            else:
                self._ctx[tf] = build_htf(self.bars, self.calendar, tf)
        return self._ctx[tf]

    def _known_ns(self, tf: int) -> np.ndarray:
        """When each HTF bar becomes known (effective close). Persisted in the feature cache,
        so mapping cached HTF features never needs to re-run the resample."""
        if tf in self._known:
            return self._known[tf]
        if tf in self._ctx:
            self._known[tf] = self._ctx[tf][1]
            return self._known[tf]
        key = hash_obj({"schema": CACHE_SCHEMA_VERSION, "htf_known": True, "dataset_id": self.dataset_id,
                        "dataset_hash": self.dataset_hash, "base_tf": self.bars.tf_minutes, "tf": tf,
                        "calendar": self.calendar.fingerprint()})
        hit = self.cache.get(self.dataset_id, key)[0] if self.cache is not None else None
        if hit is not None:
            known = hit["known"]
        else:
            known = self._context(tf)[1]
            if self.cache is not None:
                self.cache.put(self.dataset_id, key, {"known": np.asarray(known, dtype=np.int64)},
                               {"label": f"__htf_known__@{tf}m", "tf": tf, "dataset_hash": self.dataset_hash})
        self._known[tf] = known
        return known

    def _tf_of(self, spec: FeatureSpec) -> int:
        tf = timeframe_minutes(spec.timeframe) if spec.timeframe else self.bars.tf_minutes
        if tf < self.bars.tf_minutes or tf % self.bars.tf_minutes:
            raise FeatureError(f"{spec.label}: timeframe must be a multiple of the dataset's "
                               f"{self.bars.tf_minutes}m")
        return tf

    # ------------------------------------------------------------------ identity
    def _session_defs(self, spec: FeatureSpec) -> dict:
        out = {}
        for pname in spec.definition.session_params:
            name = spec.param_dict[pname]
            if name == "trading_day":
                out[pname] = {"trading_day": self.calendar.fingerprint()}
            elif name in self.sessions:
                out[pname] = self.sessions[name].definition()
            else:
                raise FeatureError(f"{spec.label}: unknown session {name!r}; configured: {sorted(self.sessions)}")
        return out

    def cache_key(self, spec: FeatureSpec, tf: int | None = None) -> str:
        tf = self._tf_of(spec) if tf is None else tf
        memo = (spec, tf)
        if memo in self._keys:
            return self._keys[memo]
        fd = spec.definition
        native = spec.at(None)
        deps = [self.cache_key(d, tf) for d in (fd.depends(native.param_dict) if fd.depends else [])]
        key = hash_obj({
            "schema": CACHE_SCHEMA_VERSION, "dataset_id": self.dataset_id,
            "dataset_hash": self.dataset_hash, "base_tf": self.bars.tf_minutes, "tf": tf,
            "calendar": self.calendar.fingerprint(), "volume_type": self.volume_type,
            "feature": native.to_dict(), "impl": fd.impl_hash, "sessions": self._session_defs(spec),
            "deps": deps})
        self._keys[memo] = key
        return key

    # ------------------------------------------------------------------ availability
    def availability(self, spec: FeatureSpec) -> tuple[bool, str]:
        try:
            self._tf_of(spec)
            self._session_defs(spec)
            self._check_requirements(spec)
        except (FeatureError, FeatureUnavailable) as exc:
            return False, str(exc)
        return True, "ok"

    def _check_requirements(self, spec: FeatureSpec) -> None:
        fd = spec.definition
        if "volume" in fd.requires:
            if self.volume_refusal:
                raise FeatureUnavailable(f"{spec.label} needs exchange volume; refused for this instrument: "
                                         f"{self.volume_refusal}")
            if self.volume_type not in VOLUME_OK:
                raise FeatureUnavailable(f"{spec.label} needs volume; dataset volume_type="
                                         f"{self.volume_type!r}")
            if not self.bars.has_volume:
                raise FeatureUnavailable(f"{spec.label} needs volume on every bar; this dataset "
                                         "has missing volume values")
        for d in (fd.depends(spec.at(None).param_dict) if fd.depends else []):
            self._check_requirements(d)

    # ------------------------------------------------------------------ compute
    def _native(self, spec: FeatureSpec, tf: int) -> tuple[dict, str, str | None]:
        """Arrays on the tf context bars (not yet mapped). Returns (arrays, key, cache_source)."""
        key = self.cache_key(spec, tf)
        if self.cache is not None:
            hit, src = self.cache.get(self.dataset_id, key)
            if hit is not None:
                return hit, key, src
        bars, known = self._context(tf)
        fd = spec.definition
        inp = FeatureInput(bars=bars, ts_close_ns=known, calendar=self.calendar,
                           sessions=self.sessions, volume_type=self.volume_type,
                           tick_size=self.tick_size,
                           dep=lambda dspec: self._native(dspec.at(None), tf)[0])
        raw = fd.compute(inp, spec.param_dict)
        missing = set(fd.output_names) - set(raw)
        extra = set(raw) - set(fd.output_names)
        if missing or extra:
            raise AssertionError(f"{spec.feature_id}: outputs mismatch (missing {missing}, extra {extra})")
        arrays = {}
        for k, v in raw.items():
            a = np.ascontiguousarray(v, dtype=np.float64)
            if a.shape != (len(bars),):
                raise AssertionError(f"{spec.feature_id}.{k}: shape {a.shape} != ({len(bars)},)")
            a.flags.writeable = False
            arrays[k] = a
        if self.cache is not None:
            self.cache.put(self.dataset_id, key, arrays, {
                "feature": spec.at(None).to_dict(), "label": spec.at(None).label, "tf": tf,
                "base_tf": self.bars.tf_minutes, "dataset_hash": self.dataset_hash,
                "impl_hash": fd.impl_hash, "volume_type": self.volume_type})
        return arrays, key, None

    def compute(self, spec: FeatureSpec) -> FeatureResult:
        t0 = time.perf_counter()
        memo = self._results.get(spec)
        if memo is not None:              # same engine, same spec: identical read-only arrays
            return FeatureResult(memo.spec, memo.arrays, memo.cache_key, "memory",
                                 time.perf_counter() - t0, memo.base_timeframe_minutes,
                                 memo.computed_timeframe_minutes, memo.mapped_from_higher_timeframe,
                                 memo.meta)
        tf = self._tf_of(spec)
        self._session_defs(spec)
        self._check_requirements(spec)
        arrays, key, src = self._native(spec.at(None), tf)
        mapped = tf != self.bars.tf_minutes
        if mapped:
            known = self._known_ns(tf)
            base_close = self.bars.ts_close_ns
            arrays = {k: map_to_base(v, known, base_close) for k, v in arrays.items()}
            for a in arrays.values():
                a.flags.writeable = False
        meta = {"volume_type": self.volume_type if "volume" in spec.definition.requires else None,
                "sessions": self._session_defs(spec), "impl_hash": spec.definition.impl_hash,
                "causal": spec.definition.causal, "dataset_id": self.dataset_id,
                "dataset_hash": self.dataset_hash}
        res = FeatureResult(spec, arrays, key, src, time.perf_counter() - t0, self.bars.tf_minutes,
                            tf, mapped, meta)
        self._results[spec] = res
        return res

    def frame(self, specs: Iterable[FeatureSpec]) -> FeatureFrame:
        return FeatureFrame(self.compute(s) for s in specs)
