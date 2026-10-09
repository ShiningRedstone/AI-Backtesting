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


TRAIL_MODES = ("none", "distance", "level", "breakeven")          # "none": only a no-progress exit
TRAIL_TRIGGER_KINDS = ("points", "r", "atr")


@dataclass(frozen=True)
class TrailSpec:
    """Trailing / breakeven stop management (ADR-61). Semantics are documented in engine/fills.py
    (``simulate_exit_trailing``). Everything is decided at a bar CLOSE and takes effect from the next bar.

    mode "distance": candidate stop = favorable extreme since entry -/+ distance (points, or ``distance`` x ATR).
    mode "level":    candidate stop = the strategy's per-bar level (SignalSet.trail_level_long/short).
    mode "breakeven": only the breakeven rule below.
    The stop only ever moves in the favorable direction (ratchet); it is never loosened."""
    mode: str = "distance"
    distance_kind: str = "points"          # distance mode: points | atr
    distance: float = 0.0                  # points, or an ATR multiple
    activation_kind: str = "immediate"     # immediate | points | r | atr (favorable excursion from the fill)
    activation: float = 0.0
    be_kind: str | None = None             # optional breakeven trigger: points | r | atr
    be_trigger: float = 0.0
    be_offset: float = 0.0                 # points locked beyond the fill once breakeven triggers (>= 0)
    every_bars: int = 1                    # update only on bars whose count since entry (entry bar = 1) % n == 0
    only_new_extreme: bool = False         # update only on bars that set a new favorable extreme
    min_step: float = 0.0                  # a trailing candidate must improve the stop by >= this many points
    np_bars: int = 0                       # no-progress exit: 0 = off; else check at the close of bar N since entry
    np_kind: str = "points"                # points | r | atr : the favorable excursion required by then
    np_value: float = 0.0

    def __post_init__(self):
        if self.mode not in TRAIL_MODES:
            raise ValueError(f"trail mode must be one of {TRAIL_MODES}")
        if self.distance_kind not in ("points", "atr"):
            raise ValueError("distance_kind must be points or atr")
        if self.activation_kind not in ("immediate",) + TRAIL_TRIGGER_KINDS:
            raise ValueError("invalid activation_kind")
        if self.be_kind is not None and self.be_kind not in TRAIL_TRIGGER_KINDS:
            raise ValueError("invalid be_kind")
        if self.mode == "distance" and not self.distance > 0:
            raise ValueError("distance trailing needs distance > 0")
        if self.mode == "breakeven" and self.be_kind is None:
            raise ValueError("breakeven mode needs a breakeven trigger")
        if self.np_bars < 0 or self.np_kind not in TRAIL_TRIGGER_KINDS or (self.np_bars and not self.np_value > 0):
            raise ValueError("no-progress exit needs np_bars >= 0 and, when on, np_kind points|r|atr and np_value > 0")
        if self.mode == "none" and not self.np_bars:
            raise ValueError("mode 'none' is only valid together with a no-progress exit")
        if self.every_bars < 1 or self.min_step < 0 or self.be_offset < 0:
            raise ValueError("every_bars >= 1, min_step >= 0 and be_offset >= 0 are required")
        if self.activation_kind != "immediate" and not self.activation > 0:
            raise ValueError("a triggered activation needs activation > 0")
        if self.be_kind is not None and not self.be_trigger > 0:
            raise ValueError("breakeven needs be_trigger > 0")


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
    # ADR-61 (optional): trailing management. All arrays are per-bar values KNOWN AT THAT BAR'S CLOSE.
    trail: "TrailSpec | None" = None
    trail_atr: np.ndarray | None = None          # ATR at each bar (atr distance / atr activation)
    trail_level_long: np.ndarray | None = None   # level mode: candidate stop for a long held after this bar
    trail_level_short: np.ndarray | None = None
    # ADR-62 (optional, strategy-level trade management; all scalars, fixed per strategy)
    max_trades_per_day: int | None = None        # executed trades per trading date (min with the config cap)
    exit_cooldown_bars: int = 0                  # no new signal for N bars after a trade EXIT (signal bar - exit bar < N)
    block_after: str | None = None               # "stop" | "target" | "any": no further entry that trading date
    # after an exit of that class (stop: STOP/STOP_GAP/TRAIL_STOP*; target: TARGET/TARGET_GAP)
    # ADR-114 (optional): a per-signal dollar risk budget for ``risk`` sizing (KNOWN AT THE SIGNAL BAR'S CLOSE); NaN =
    # the sizing's own risk_usd. None = Phase 1 behaviour, unchanged.
    risk_usd: np.ndarray | None = None

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

    def trail_arrays(self) -> dict[str, np.ndarray]:
        return {k: v for k, v in (("trail_atr", self.trail_atr), ("trail_level_long", self.trail_level_long),
                                  ("trail_level_short", self.trail_level_short)) if v is not None}


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
    for name, a in sig.trail_arrays().items():
        if len(a) != n or a.dtype.kind != "f":
            raise ValueError(f"{name} must be a float array of length {n}")
    if sig.trail is not None:
        if sig.trail.mode == "level" and (sig.trail_level_long is None and sig.trail_level_short is None):
            raise ValueError("level trailing needs trail_level arrays")
        if "atr" in (sig.trail.distance_kind if sig.trail.mode == "distance" else None,
                     sig.trail.activation_kind if sig.trail.mode not in ("none", "breakeven") else None,
                     sig.trail.be_kind, sig.trail.np_kind if sig.trail.np_bars else None) and sig.trail_atr is None:
            raise ValueError("ATR-based trailing needs the trail_atr array")
    elif sig.trail_arrays():
        raise ValueError("trail arrays without a TrailSpec")
    if sig.risk_usd is not None and (len(sig.risk_usd) != n or sig.risk_usd.dtype.kind != "f"):
        raise ValueError(f"risk_usd must be a float array of length {n}")
    if sig.block_after not in (None, "stop", "target", "any"):
        raise ValueError("block_after must be None, stop, target or any")
    if sig.exit_cooldown_bars < 0 or (sig.max_trades_per_day is not None and sig.max_trades_per_day < 1):
        raise ValueError("exit_cooldown_bars >= 0 and max_trades_per_day >= 1 are required")
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
        if (full.max_trades_per_day, full.exit_cooldown_bars, full.block_after) != \
                (part.max_trades_per_day, part.exit_cooldown_bars, part.block_after):
            return CausalityReport(False, cuts, None, k, "trade-management parameters differ on truncated history")
        if full.trail != part.trail:
            return CausalityReport(False, cuts, None, k, "trail spec differs on truncated history")
        ft, pt = full.trail_arrays(), part.trail_arrays()
        if set(ft) != set(pt):
            return CausalityReport(False, cuts, None, k, "trail arrays present/absent inconsistently")
        for name in ft:
            a, b = ft[name][:k], pt[name]
            if len(b) != k or not np.all((a == b) | (np.isnan(a) & np.isnan(b))):
                bad = 0 if len(b) != k else int(np.flatnonzero(~((a == b) | (np.isnan(a) & np.isnan(b))))[0])
                return CausalityReport(False, cuts, bad, k,
                                       f"'{name}' at bar {bad} changes when history is cut at {k}: "
                                       "the trailing rule uses information from after the decision bar")
        if (full.risk_usd is None) != (part.risk_usd is None):
            return CausalityReport(False, cuts, None, k, "risk_usd present/absent inconsistently")
        if full.risk_usd is not None:
            a, b = full.risk_usd[:k], part.risk_usd
            same = (a == b) | (np.isnan(a) & np.isnan(b)) if len(b) == k else np.zeros(k, bool)
            if not np.all(same):
                bad = int(np.flatnonzero(~same)[0])
                return CausalityReport(False, cuts, bad, k,
                                       f"'risk_usd' at bar {bad} changes when history is cut at {k}: "
                                       "the sizing uses information from after the decision bar")
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
