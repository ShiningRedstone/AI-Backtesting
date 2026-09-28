"""Feature specifications, definitions and the registry.

A FeatureDef is the *implementation* (math + documentation + metadata).
A FeatureSpec is a *request*: feature id + normalized parameters + timeframe.

Identity rules
  * FeatureSpec.make() fills defaults, validates and canonicalizes parameters, so
    ``ema(period=20)`` and ``ema()`` with default 20 are the same spec, same id.
  * ``spec_hash`` = sha256 of the canonical dict {id, version, timeframe, params}.
  * Changing a feature's MEANING requires bumping ``FeatureDef.version`` -> new identity.
  * Changing only its CODE (a bug fix, refactor) changes ``impl_hash`` -> the cache
    invalidates automatically even if someone forgets to bump the version.

All outputs are float64 arrays aligned to the bars (NaN = not yet defined /
unavailable; booleans/events are 1.0 / 0.0). One dtype keeps caching, multi-timeframe
mapping and strategy consumption uniform.
"""
from __future__ import annotations

import hashlib
import inspect
from dataclasses import dataclass, field
from functools import cached_property
from typing import Any, Callable, Mapping

from edgelab.core.identity import hash_obj
from edgelab.data.schema import timeframe_minutes

KNOWN_AT = ("bar_open", "bar_close")


class FeatureError(ValueError):
    """Invalid feature request (unknown id, bad parameter, unsupported timeframe)."""


class FeatureUnavailable(RuntimeError):
    """The dataset cannot support this feature (e.g. volume feature on a no-volume CFD feed)."""


@dataclass(frozen=True)
class Param:
    name: str
    default: Any
    kind: type
    doc: str = ""
    choices: tuple | None = None
    min: float | None = None

    def check(self, value: Any) -> Any:
        if self.kind is bool:
            if not isinstance(value, bool):
                raise FeatureError(f"param {self.name} must be bool")
            return value
        if self.kind is int:
            if isinstance(value, bool) or not float(value).is_integer():
                raise FeatureError(f"param {self.name} must be an integer")
            value = int(value)
        elif self.kind is float:
            value = float(value)
        elif self.kind is str:
            value = str(value)
        if self.choices is not None and value not in self.choices:
            raise FeatureError(f"param {self.name}={value!r} not in {self.choices}")
        if self.min is not None and value < self.min:
            raise FeatureError(f"param {self.name}={value} must be >= {self.min}")
        return value

    def describe(self) -> dict:
        d = {"name": self.name, "type": self.kind.__name__, "default": self.default, "doc": self.doc}
        if self.choices:
            d["choices"] = list(self.choices)
        if self.min is not None:
            d["min"] = self.min
        return d


@dataclass(frozen=True)
class FeatureDef:
    feature_id: str
    version: int
    category: str
    params: tuple[Param, ...]
    outputs: tuple[tuple[str, str], ...]          # (name, description)
    compute: Callable                              # (FeatureInput, params: dict) -> dict[str, ndarray]
    summary: str
    calculation: str
    edge_cases: str
    warmup: str
    known_at: str = "bar_close"
    causal: bool = True
    requires: tuple[str, ...] = ()                 # data requirements, e.g. ("volume",)
    depends: Callable[[dict], list] | None = None  # params -> [FeatureSpec] on the same bars
    session_params: tuple[str, ...] = ()           # params that name a session window

    def __post_init__(self):
        if self.known_at not in KNOWN_AT:
            raise ValueError(f"{self.feature_id}: known_at must be one of {KNOWN_AT}")

    @cached_property
    def impl_hash(self) -> str:
        """sha256 of the compute function's source (computed once per definition object)."""
        src = inspect.getsource(self.compute)
        return hashlib.sha256(src.encode()).hexdigest()

    @property
    def output_names(self) -> tuple[str, ...]:
        return tuple(o[0] for o in self.outputs)

    def describe(self) -> dict:
        return {"id": self.feature_id, "version": self.version, "category": self.category,
                "summary": self.summary, "calculation": self.calculation,
                "edge_cases": self.edge_cases, "warmup": self.warmup, "known_at": self.known_at,
                "causal": self.causal, "requires": list(self.requires),
                "params": [p.describe() for p in self.params],
                "outputs": [{"name": n, "doc": d} for n, d in self.outputs],
                "depends_on": sorted({s.feature_id for s in (self.depends({p.name: p.default for p in self.params})
                                                            if self.depends else [])}),
                "impl_hash": self.impl_hash[:16]}


REGISTRY: dict[str, FeatureDef] = {}


def register(fd: FeatureDef) -> FeatureDef:
    if fd.feature_id in REGISTRY and REGISTRY[fd.feature_id] is not fd:
        raise ValueError(f"feature {fd.feature_id} registered twice")
    REGISTRY[fd.feature_id] = fd
    return fd


def get_def(feature_id: str) -> FeatureDef:
    _ensure_library_loaded()
    if feature_id not in REGISTRY:
        raise FeatureError(f"unknown feature {feature_id!r}; known: {sorted(REGISTRY)}")
    return REGISTRY[feature_id]


def all_defs() -> list[FeatureDef]:
    _ensure_library_loaded()
    return [REGISTRY[k] for k in sorted(REGISTRY)]


def _ensure_library_loaded() -> None:
    import edgelab.features.library  # noqa: F401  (registers on import)


def _norm_tf(tf: str | int | None) -> str | None:
    if tf is None:
        return None
    m = tf if isinstance(tf, int) else timeframe_minutes(str(tf))
    return f"{m}m"


@dataclass(frozen=True)
class FeatureSpec:
    feature_id: str
    version: int
    params: tuple[tuple[str, Any], ...]
    timeframe: str | None = None    # None = the dataset's native timeframe

    @classmethod
    def make(cls, feature_id: str, params: Mapping[str, Any] | None = None,
             timeframe: str | int | None = None, version: int | None = None) -> "FeatureSpec":
        fd = get_def(feature_id)
        if version is not None and version != fd.version:
            raise FeatureError(f"{feature_id} v{version} requested; only v{fd.version} is implemented")
        params = dict(params or {})
        unknown = set(params) - {p.name for p in fd.params}
        if unknown:
            raise FeatureError(f"{feature_id}: unknown params {sorted(unknown)}")
        norm = tuple(sorted((p.name, p.check(params.get(p.name, p.default))) for p in fd.params))
        return cls(feature_id, fd.version, norm, _norm_tf(timeframe))

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> "FeatureSpec":
        return cls.make(d["id"], d.get("params"), d.get("timeframe"), d.get("version"))

    @property
    def definition(self) -> FeatureDef:
        return get_def(self.feature_id)

    @property
    def param_dict(self) -> dict[str, Any]:
        return dict(self.params)

    def to_dict(self) -> dict:
        return {"id": self.feature_id, "version": self.version, "timeframe": self.timeframe,
                "params": self.param_dict}

    @property
    def spec_hash(self) -> str:
        return hash_obj(self.to_dict())

    @property
    def label(self) -> str:
        """Human/column label, stable: e.g. ``atr(period=14)@15m``."""
        p = ",".join(f"{k}={v}" for k, v in self.params)
        return f"{self.feature_id}({p})" + (f"@{self.timeframe}" if self.timeframe else "")

    @property
    def spec_id(self) -> str:
        return f"{self.feature_id}.v{self.version}.{self.spec_hash[:12]}"

    def at(self, timeframe: str | int | None) -> "FeatureSpec":
        return FeatureSpec(self.feature_id, self.version, self.params, _norm_tf(timeframe))
