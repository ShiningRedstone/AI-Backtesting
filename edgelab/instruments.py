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


# ---- execution contract (ADR-63) ------------------------------------------------------------
# The historical price series and the contract that is TRADED are different things. The canonical research
# series is Dukascopy's USA 100 Technical Index CFD (a proxy, never CME MNQ); the execution target is whole
# Micro E-mini Nasdaq-100 contracts. The ONLY authoritative contract specification is the instrument's entry
# in configs/instruments.yaml (for MNQ: tick 0.25, tick value $0.50 -> point value $2, whole contracts). An
# "execution view" keeps the DATA instrument's price grid and identity and takes the economics (point value,
# minimum size, size step) from the contract.
class ExecutionContractError(InstrumentError):
    pass


def execution_view(data: Instrument, contract: Instrument) -> Instrument:
    """Instrument used for sizing, P&L and cost USD conversion when a strategy trades ``contract`` on a ``data`` series.
    Price grid (tick_size), calendar, asset class and underlying stay the DATA series'; point value, min size and
    size step are the CONTRACT's. Index points are translated 1:1 (1 index point of the series = 1 point of the
    contract = ``contract.point_value`` dollars per contract); prices are NOT snapped to the contract's tick."""
    if data.asset_class == "future" and data.symbol != contract.symbol:
        raise ExecutionContractError(
            f"{contract.symbol} contracts cannot be traded on the {data.symbol} futures series: its cost profile "
            f"describes {data.symbol}, not {contract.symbol} (use a proxy/CFD index series, or the {contract.symbol} series)")
    pv = contract.point_value
    view = Instrument(symbol=contract.symbol, tick_size=data.tick_size, tick_value=data.tick_size * pv,
                      calendar=data.calendar, exchange=contract.exchange, asset_class=data.asset_class,
                      currency=contract.currency, description=f"{contract.symbol} contracts on the {data.symbol} series",
                      min_size=contract.min_size, size_step=contract.size_step, underlying=data.underlying,
                      extra={"execution_contract": contract.symbol, "data_instrument": data.symbol,
                             "contract_tick_size": contract.tick_size, "contract_tick_value": contract.tick_value})
    if abs(view.point_value - pv) > 1e-9 * max(1.0, abs(pv)):
        raise ExecutionContractError(f"point value {view.point_value} != {pv} after adaptation")
    return view


def contract_for(cfg: Mapping[str, Any], sizing: Mapping | None) -> Instrument | None:
    """The execution contract named by a strategy's sizing, from the ONE authoritative specification
    (``cfg["instruments"]``, i.e. configs/instruments.yaml). None when the sizing names no contract; an unknown
    name is a refusal, never a silent fallback to the data series."""
    name = (sizing or {}).get("contract")
    if not name:
        return None
    insts = load_instruments(cfg)
    if name not in insts:
        raise ExecutionContractError(f"unknown execution contract {name!r}; configured: {sorted(insts)}")
    return insts[name]


def contract_summary(contract: Instrument, data: Instrument) -> dict:
    """What a run discloses about its execution contract (recorded in the run assumptions)."""
    return {"contract": contract.symbol, "contract_point_value": contract.point_value,
            "contract_tick_size": contract.tick_size, "contract_tick_value": contract.tick_value,
            "min_contracts": contract.min_size, "contract_step": contract.size_step,
            "data_instrument": data.symbol, "data_tick_size": data.tick_size,
            "translation": f"1 index point of the {data.symbol} research series = 1 point of the contract = "
                           f"${contract.point_value:g} per contract; prices are not snapped to the contract tick; "
                           "sizing and USD P&L/costs use the contract's point value; R-multiples do not depend on it",
            "source": "configs/instruments.yaml (single authoritative contract specification)"}


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
            "source_feed_code": ex.get("source_feed_code"), "price_basis": ex.get("price_basis"),
            "volume_semantics": ex.get("volume_semantics"), "economics": ex.get("economics"),
            "economics_evidence": ex.get("economics_evidence"), "economics_assumption": ex.get("economics_assumption"),
            "calendar": inst.calendar, "calendar_status": ex.get("calendar_status"),
            "calendar_unverified_scope": ex.get("calendar_unverified_scope"), "calendar_caveat": calendar_caveat(inst),
            "missing_metadata": missing if status == "provisional" else [],
            "point_value": inst.point_value, "tick_size": inst.tick_size, "description": inst.description}


# provisional_unverified: session hours not checked against the source -> research refused.
# regular_hours_verified: the REGULAR session hours are verified (calendar_evidence), but holidays /
#   early closes are not (calendar_unverified_scope says exactly what is not) -> research allowed,
#   with that caveat surfaced as a dataset limitation. Nothing in the data or its validation changes:
#   unlisted special dates stay visible as missing bars / missing trading days.
# verified: regular hours AND special dates verified (calendar_evidence).
CALENDAR_STATUSES = ("provisional_unverified", "regular_hours_verified", "verified")


def identity_problem(inst: Instrument) -> str | None:
    """None when research may interpret this instrument's economics and sessions, else the exact reason."""
    cal_status = (inst.extra or {}).get("calendar_status")
    if cal_status is not None and cal_status not in CALENDAR_STATUSES:
        return f"instrument {inst.symbol}: unknown calendar_status {cal_status!r} (one of {CALENDAR_STATUSES})"
    if cal_status == "provisional_unverified":
        return (f"instrument {inst.symbol}: its session calendar {inst.calendar} is PROVISIONAL and has not been "
                "verified against the real source file. Run scripts/dukascopy_inspect.py on the file, confirm or "
                "replace the calendar, then set calendar_status: verified (with calendar_evidence) in "
                "configs/instruments.yaml. See DATA_IMPORT.md.")
    if cal_status in ("verified", "regular_hours_verified") and not (inst.extra or {}).get("calendar_evidence"):
        return (f"instrument {inst.symbol}: calendar_status {cal_status} needs calendar_evidence in "
                "configs/instruments.yaml")
    if cal_status == "regular_hours_verified" and not (inst.extra or {}).get("calendar_unverified_scope"):
        return (f"instrument {inst.symbol}: calendar_status regular_hours_verified needs calendar_unverified_scope "
                "(what is NOT verified, e.g. holidays / early closes) in configs/instruments.yaml")
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


def calendar_caveat(inst: Instrument) -> str | None:
    """The research caveat of a partly verified calendar (None when fully verified or not applicable)."""
    ex = inst.extra or {}
    if ex.get("calendar_status") != "regular_hours_verified":
        return None
    return (f"calendar {inst.calendar}: regular session hours verified; NOT verified: "
            f"{ex.get('calendar_unverified_scope')}. Unlisted closures stay as data gaps, and a position can be held "
            "across an unannounced early close or holiday until the next available bar")


def check_identity(inst: Instrument) -> None:
    p = identity_problem(inst)
    if p:
        raise InstrumentIdentityError(p)
