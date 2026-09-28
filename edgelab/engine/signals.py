"""Strategy -> signal interface, and lookahead detection.

Timing contract:
  * A signal at bar i is decided at the CLOSE of bar i, using bars[0..i] only.
  * The earliest possible fill is bar i+1 (market: its open; stop/limit: during it).

The engine enforces the second rule structurally. The first rule cannot be
enforced by construction for arbitrary Python, so it is TESTED empirically by
``check_causality``: signals computed on a truncated history must equal the
full-history signals over the same bars. Any feature that peeks forward (shift(-1),
centred windows, full-sample normalisation, future-aware resampling) changes
the tail when the future is removed, and is caught.

Phase 3 replaces hand-written strategies with a validated DSL; this protocol
stays as the engine-facing contract.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from edgelab.core.identity import hash_obj
from edgelab.data.schema import BarArrays

ENTRY_TYPES = ("market", "stop", "limit")


@dataclass(frozen=True)
class OrderSpec:
    """How each signal is turned into orders. Distances are in price points."""
    entry_type: str = "market"
    stop_points: float | None = None       # used when a signal has no absolute stop
    target_points: float | None = None     # None -> no target (time/session exits only)
    time_exit_bars: int | None = None      # exit at close of the Nth bar (entry bar = 1)
    max_hold_bars: int | None = None
    entry_expiry_bars: int = 1             # bars a stop/limit entry stays working

    def __post_init__(self):
        if self.entry_type not in ENTRY_TYPES:
            raise ValueError(f"entry_type must be one of {ENTRY_TYPES}")
        for k in ("stop_points", "target_points"):
            v = getattr(self, k)
            if v is not None and not v > 0:
                raise ValueError(f"{k} must be > 0")
        for k in ("time_exit_bars", "max_hold_bars", "entry_expiry_bars"):
            v = getattr(self, k)
            if v is not None and v < 1:
                raise ValueError(f"{k} must be >= 1")


@dataclass
class SignalSet:
    """Per-bar arrays (length = number of bars). direction: +1 long, -1 short, 0 none.
    Price arrays use NaN for 'not specified'."""
    direction: np.ndarray
    entry_price: np.ndarray       # required for stop/limit entries
    stop_price: np.ndarray        # absolute protective stop (else OrderSpec.stop_points)
    target_price: np.ndarray      # absolute target (else OrderSpec.target_points)
    # Phase 3 (optional): signal exits. True at bar k = "at the close of bar k, exit an open
    # long/short"; the exit is a market order at the OPEN of bar k+1 (same timing rule as
    # entries). None = no signal exits (Phase 1 behaviour, unchanged).
    exit_long: np.ndarray | None = None
    exit_short: np.ndarray | None = None

    @classmethod
    def empty(cls, n: int) -> "SignalSet":
        nan = np.full(n, np.nan)
        return cls(np.zeros(n, np.int8), nan.copy(), nan.copy(), nan.copy())

    def __len__(self) -> int:
        return len(self.direction)

    def arrays(self) -> tuple[np.ndarray, ...]:
        return (self.direction, self.entry_price, self.stop_price, self.target_price)

    def exit_arrays(self) -> dict[str, np.ndarray]:
        return {k: v for k, v in (("exit_long", self.exit_long), ("exit_short", self.exit_short))
                if v is not None}


class Strategy(ABC):
    """Base class. Subclasses set ``family``, ``params`` and ``order`` and implement
    generate_signals using only past/current bar data."""
    family: str = "base"

    def __init__(self, order: OrderSpec, **params: Any):
        self.order = order
        self.params = params

    @property
    def spec(self) -> dict:
        from dataclasses import asdict
        return {"family": self.family, "params": self.params, "order": asdict(self.order)}

    @property
    def strategy_id(self) -> str:
        """Deterministic ID derived from the full configuration."""
        return f"{self.family.upper()}_{hash_obj(self.spec, 10)}"

    @abstractmethod
    def generate_signals(self, bars: BarArrays) -> SignalSet: ...


def validate_signals(sig: SignalSet, n: int, order: OrderSpec) -> None:
    for a in sig.arrays():
        if len(a) != n:
            raise ValueError(f"signal array length {len(a)} != bars {n}")
    for name, a in sig.exit_arrays().items():
        if len(a) != n or a.dtype != np.bool_:
            raise ValueError(f"{name} must be a bool array of length {n}")
    if not np.isin(sig.direction, (-1, 0, 1)).all():
        raise ValueError("direction must be in {-1, 0, 1}")
    active = sig.direction != 0
    if order.entry_type in ("stop", "limit") and np.isnan(sig.entry_price[active]).any():
        raise ValueError(f"{order.entry_type} entries need entry_price on every signal")
    if order.stop_points is None and np.isnan(sig.stop_price[active]).any():
        raise ValueError("every signal needs a protective stop (stop_price or stop_points)")


@dataclass
class CausalityReport:
    passed: bool
    cuts_tested: list[int] = field(default_factory=list)
    violation_at_bar: int | None = None
    violation_cut: int | None = None
    detail: str = ""


def check_causality(strategy: Strategy, bars: BarArrays, n_cuts: int = 20,
                    seed: int = 12345, min_bars: int = 50) -> CausalityReport:
    """Empirical lookahead test (see module docstring)."""
    n = len(bars)
    full = strategy.generate_signals(bars)
    lo = min(max(min_bars, n // 20), n - 1)
    rng = np.random.default_rng(seed)
    cuts = sorted(set(rng.integers(lo, n, size=n_cuts).tolist()) | {n - 1})
    for k in cuts:
        part = strategy.generate_signals(bars.head(k))
        if any(len(a) != k for a in part.arrays()):
            return CausalityReport(False, cuts, None, k, f"signal length != {k} on truncated history")
        for name, a_full, a_part in zip(("direction", "entry_price", "stop_price", "target_price"),
                                        full.arrays(), part.arrays()):
            a, b = a_full[:k], a_part
            same = (a == b) | (np.isnan(a) & np.isnan(b)) if a.dtype.kind == "f" else (a == b)
            if not np.all(same):
                bad = int(np.flatnonzero(~same)[0])
                return CausalityReport(False, cuts, bad, k,
                                       f"'{name}' at bar {bad} changes when history is cut at {k}: "
                                       "the strategy uses information from after the decision bar")
        fx, px = full.exit_arrays(), part.exit_arrays()
        if set(fx) != set(px):
            return CausalityReport(False, cuts, None, k, "exit arrays present/absent inconsistently")
        for name in fx:
            if len(px[name]) != k:
                return CausalityReport(False, cuts, None, k, f"{name} length != {k} on truncated history")
            diff = fx[name][:k] != px[name]
            if diff.any():
                bad = int(np.flatnonzero(diff)[0])
                return CausalityReport(False, cuts, bad, k,
                                       f"'{name}' at bar {bad} changes when history is cut at {k}: "
                                       "the strategy uses information from after the decision bar")
    return CausalityReport(True, cuts)
