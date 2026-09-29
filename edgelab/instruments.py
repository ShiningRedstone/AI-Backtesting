"""Instrument metadata.

Contract specs are loaded from ``configs/instruments.yaml``. The engine never
hard-codes a symbol; NQ is simply the first configured instrument.

point_value is DERIVED from tick_value / tick_size and, if also given in the
config, must agree - a mismatch is the kind of silent error that mis-sizes every
position, so it raises.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping


class InstrumentError(ValueError):
    pass


@dataclass(frozen=True)
class Instrument:
    symbol: str
    tick_size: float
    tick_value: float
    calendar: str
    exchange: str = ""
    asset_class: str = "future"
    currency: str = "USD"
    description: str = ""
    contract_months: str = ""
    roll_methodology: str = "unspecified"
    extra: Mapping[str, Any] = field(default_factory=dict)
    # Phase 2: position-size granularity. Futures: whole contracts (1, 1). CFDs: broker
    # units/lots, e.g. min 0.1 in steps of 0.1. tick_value is always per 1 size unit.
    min_size: float = 1.0
    size_step: float = 1.0
    underlying: str = ""        # e.g. NDX. Informational grouping only - never used to merge data.

    def __post_init__(self):
        if not (self.tick_size > 0 and math.isfinite(self.tick_size)):
            raise InstrumentError(f"{self.symbol}: tick_size must be > 0")
        if not (self.tick_value > 0 and math.isfinite(self.tick_value)):
            raise InstrumentError(f"{self.symbol}: tick_value must be > 0")
        if not (self.size_step > 0 and self.min_size > 0):
            raise InstrumentError(f"{self.symbol}: min_size and size_step must be > 0")

    @property
    def point_value(self) -> float:
        """Dollar value of a 1.0 price move for one contract (contract multiplier)."""
        return self.tick_value / self.tick_size

    def round_to_tick(self, price: float) -> float:
        return round(round(price / self.tick_size) * self.tick_size, 10)

    def points_to_dollars(self, points: float, contracts: float = 1.0) -> float:
        return points * self.point_value * contracts

    def ticks_to_points(self, ticks: float) -> float:
        return ticks * self.tick_size


def instrument_from_config(symbol: str, meta: Mapping[str, Any]) -> Instrument:
    known = {"tick_size", "tick_value", "calendar", "exchange", "asset_class", "currency",
             "description", "contract_months", "roll_methodology", "point_value",
             "min_size", "size_step", "underlying"}
    inst = Instrument(
        symbol=symbol,
        tick_size=float(meta["tick_size"]),
        tick_value=float(meta["tick_value"]),
        calendar=meta["calendar"],
        exchange=meta.get("exchange", ""),
        asset_class=meta.get("asset_class", "future"),
        currency=meta.get("currency", "USD"),
        description=meta.get("description", ""),
        contract_months=meta.get("contract_months", ""),
        roll_methodology=meta.get("roll_methodology", "unspecified"),
        extra={k: v for k, v in meta.items() if k not in known},
        min_size=float(meta.get("min_size", 1.0)),
        size_step=float(meta.get("size_step", 1.0)),
        underlying=meta.get("underlying", ""),
    )
    if "point_value" in meta:
        declared = float(meta["point_value"])
        if not math.isclose(declared, inst.point_value, rel_tol=1e-9):
            raise InstrumentError(
                f"{symbol}: point_value {declared} != tick_value/tick_size {inst.point_value}")
    return inst


def load_instruments(cfg: Mapping[str, Any]) -> dict[str, Instrument]:
    return {sym: instrument_from_config(sym, meta) for sym, meta in cfg["instruments"].items()}


# ---- source identity (Phase 9) -------------------------------------------------------------
# An instrument entry may describe a price SOURCE whose identity is not yet established
# (``identity_status: provisional``). Such an instrument can be imported, validated and inspected,
# but nothing that interprets point value, tick value, sizing or costs may run on it until the
# user states the missing metadata (``user_specified``) or verifies it (``source_verified``).
# Entries without ``identity_status`` are ordinary configured instruments (unchanged behaviour).
IDENTITY_STATUSES = ("provisional", "user_specified", "source_verified")


class InstrumentIdentityError(InstrumentError):
    pass


def identity_info(inst: Instrument) -> dict:
    """The identity fields the UI shows for an instrument (nothing invented: absent = None)."""
    ex = dict(inst.extra or {})
    status = ex.get("identity_status")
    required = list(ex.get("required_metadata") or [])
    missing = []                          # fields still visibly unset (numbers are research units until confirmed)
    for k in required:
        if k == "asset_class":
            if inst.asset_class in ("", "unverified", "unknown"):
                missing.append(k)
        elif k in ("source_symbol", "identity_evidence", "price_basis") and not ex.get(k):
            missing.append(k)
    return {"identity_status": status or "configured", "research_proxy": bool(ex.get("research_proxy")),
            "source_provider": ex.get("source_provider"), "source_symbol": ex.get("source_symbol"),
            "asset_class": inst.asset_class, "exchange": inst.exchange, "price_source": ex.get("price_source"),
            "identity_evidence": ex.get("identity_evidence"), "required_metadata": required,
            "missing_metadata": missing if status == "provisional" else [],
            "point_value": inst.point_value, "tick_size": inst.tick_size, "description": inst.description}


def identity_problem(inst: Instrument) -> str | None:
    """None when research may interpret this instrument's economics, else the exact reason."""
    status = (inst.extra or {}).get("identity_status")
    if status is None or status in ("user_specified", "source_verified"):
        if status == "source_verified" and not (inst.extra or {}).get("identity_evidence"):
            return (f"instrument {inst.symbol}: identity_status source_verified needs identity_evidence "
                    "(where it was verified) in configs/instruments.yaml")
        return None
    if status not in IDENTITY_STATUSES:
        return f"instrument {inst.symbol}: unknown identity_status {status!r} (one of {IDENTITY_STATUSES})"
    need = identity_info(inst)["required_metadata"]
    return (f"instrument {inst.symbol} has a PROVISIONAL source identity: its symbol, asset class and "
            f"contract economics are not established, so point value / tick value / sizing / costs "
            f"cannot be interpreted. State {need} in configs/instruments.yaml and set identity_status "
            "to user_specified (or source_verified with identity_evidence). See DATA_IMPORT.md.")


def check_identity(inst: Instrument) -> None:
    p = identity_problem(inst)
    if p:
        raise InstrumentIdentityError(p)
