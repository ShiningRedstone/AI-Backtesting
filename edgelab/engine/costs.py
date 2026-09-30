"""Transaction costs.

Fills are triggered by *theoretical* prices (the level touched); slippage is then
applied to the fill price. Because costs never change WHICH bar a stop or target
triggers on, cost sensitivity is exact post hoc:

    net_R(k x costs) = gross_R - k * cost_R_at_1x

The engine stores ``cost_usd_base`` (1x) on every trade so analytics can
produce the 0.5x/1x/1.5x/2x/3x table without re-running the backtest.

Phase 2 (CFD readiness):
  * ``status`` records where the numbers came from: ``assumed`` (placeholder you
    chose), ``broker_verified`` (checked against a real schedule), or
    ``unconfigured``. Unconfigured profiles REFUSE to build a cost model - the
    engine never invents broker numbers.
  * ``slippage_unit`` = ticks | points (CFD feeds often have tiny ticks).
  * ``spread_source`` = fixed (``spread_points``) | dataset (per-bar ``spread``
    column: the average of the entry-bar and exit-bar spread is charged once
    per round trip, which is the correct total whether prices are bid-, ask- or
    mid-based).
  * ``spread_source: quotes`` (ADR-55, opt-in): directional BID/ASK execution - buys fill on the
    dataset's observed ASK OHLC, sells on BID, so the spread is already inside the fill prices and
    the separate spread charge is exactly 0 (never charged twice). Requires ASK OHLC in the dataset;
    ``spread_points`` must be 0/unset.
  * Overnight financing: ``financing_mode: annual_rate`` charges
    notional x rate / day_count for every rollover instant the position is held
    through (``triple_rollover_weekday`` counts 3). Rates may be negative (credit).

After Phase 9 (Dukascopy-style CFD economics, both opt-in, defaults unchanged):
  * ``commission_mode: notional`` charges ``commission_per_million`` USD per USD 1,000,000 of
    traded notional per side (notional = |theoretical fill price| x point_value x size), instead
    of a fixed amount per unit (``per_unit``, the default).
  * ``financing_mode: not_modeled`` charges nothing but records that overnight holding costs
    exist and are NOT modelled (distinct from ``none`` = there are none). A single constant
    ``annual_rate`` cannot follow a broker's changing benchmark rate over years.
"""
from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import timedelta
from typing import Mapping

import pandas as pd

from edgelab.instruments import Instrument

ORDER_TYPES = ("market", "stop", "limit")
COST_STATUSES = ("assumed", "broker_verified", "unconfigured", "zero_for_testing")
_NON_NEGATIVE = ("commission_per_side", "commission_per_million", "fees_per_side", "slippage_ticks_market",
                 "slippage_ticks_stop", "slippage_ticks_limit", "spread_points", "multiplier",
                 "financing_day_count")


class CostConfigError(ValueError):
    """A cost model was requested for a profile that has no real numbers."""


class CostScenarioIncomplete(CostConfigError):
    """A named research-cost scenario (status assumed/broker_verified) lacks required fields."""

    def __init__(self, message: str, scenario, status: str, missing: list[str]):
        super().__init__(message)
        self.scenario, self.status, self.missing = scenario, status, missing


@dataclass(frozen=True)
class CostModel:
    commission_per_side: float = 0.0       # $ per contract/unit per side
    fees_per_side: float = 0.0             # exchange + clearing + NFA, $ per contract per side
    slippage_ticks_market: float = 0.0     # adverse slippage on market fills (see slippage_unit)
    slippage_ticks_stop: float = 0.0       # adverse slippage on stop fills (entries and stop-losses)
    slippage_ticks_limit: float = 0.0      # usually 0: limits fill at price or not at all
    spread_points: float = 0.0             # full bid/ask spread in points (CFDs); half per side
    multiplier: float = 1.0                # scenario scaling of ALL costs
    # ---- Phase 2 ----
    slippage_unit: str = "ticks"           # ticks | points
    spread_source: str = "fixed"           # fixed | dataset
    financing_mode: str = "none"           # none | annual_rate
    financing_long_rate: float = 0.0       # annual; + = cost, - = credit
    financing_short_rate: float = 0.0
    financing_day_count: float = 365.0
    rollover_time: str = "17:00"
    rollover_timezone: str = "America/New_York"
    triple_rollover_weekday: int | None = None   # 0=Mon .. 6=Sun; e.g. 2 for Wednesday
    # ---- after Phase 9 ----
    commission_mode: str = "per_unit"      # per_unit | notional
    commission_per_million: float = 0.0    # USD per USD 1,000,000 traded notional per side (notional mode)
    scenario: str = ""                     # named research-cost scenario ("" = none declared)
    basis: str = ""                        # the user's statement of where the numbers come from
    status: str = "assumed"
    profile: str = ""

    def __post_init__(self):
        for k in _NON_NEGATIVE:
            if getattr(self, k) < 0:
                raise ValueError(f"cost field {k} must be >= 0")
        if self.slippage_unit not in ("ticks", "points"):
            raise ValueError("slippage_unit must be ticks|points")
        if self.spread_source not in ("fixed", "dataset", "quotes"):
            raise ValueError("spread_source must be fixed|dataset|quotes")
        if self.spread_source == "quotes" and self.spread_points != 0:
            raise ValueError("spread_source quotes embeds the spread in BID/ASK fills; spread_points must be 0")
        if self.financing_mode not in ("none", "annual_rate", "not_modeled"):
            raise ValueError("financing_mode must be none|annual_rate|not_modeled")
        if self.commission_mode not in ("per_unit", "notional"):
            raise ValueError("commission_mode must be per_unit|notional")
        if self.status not in COST_STATUSES:
            raise ValueError(f"status must be one of {COST_STATUSES}")

    # ---- slippage ----------------------------------------------------------------------
    def slippage_ticks(self, order_type: str) -> float:
        """Raw configured slippage for an order type (in ``slippage_unit``)."""
        return {"market": self.slippage_ticks_market, "stop": self.slippage_ticks_stop,
                "limit": self.slippage_ticks_limit}[order_type]

    def slippage_points(self, order_type: str, inst: Instrument) -> float:
        raw = self.slippage_ticks(order_type)
        return raw if self.slippage_unit == "points" else raw * inst.tick_size

    # ---- round trip --------------------------------------------------------------------
    def round_trip_base(self, entry_type: str, exit_type: str, contracts: float,
                        inst: Instrument, spread_points: float | None = None,
                        entry_price: float | None = None, exit_price: float | None = None) -> dict:
        """Costs at 1x for one round trip, in dollars, by component.
        ``spread_points`` overrides the fixed spread (dataset spread mode). ``entry_price`` /
        ``exit_price`` (theoretical fills) are required in notional commission mode."""
        slip_pts = self.slippage_points(entry_type, inst) + self.slippage_points(exit_type, inst)
        spread = self.spread_points if spread_points is None else spread_points
        if self.spread_source == "quotes":
            if spread_points:
                raise ValueError("spread_source quotes: the spread is in the fill prices, never a separate cost")
            spread = 0.0
        if self.commission_mode == "notional":
            if entry_price is None or exit_price is None:
                raise ValueError("notional commission needs the entry and exit prices")
            notional = (abs(entry_price) + abs(exit_price)) * inst.point_value * contracts
            commission = self.commission_per_million / 1_000_000 * notional
        else:
            commission = 2 * self.commission_per_side * contracts
        return {
            "commission_usd": commission,
            "fees_usd": 2 * self.fees_per_side * contracts,
            "slippage_usd": slip_pts * inst.point_value * contracts,
            "spread_usd": spread * inst.point_value * contracts,
            "slippage_ticks": slip_pts / inst.tick_size,
        }

    # ---- financing ---------------------------------------------------------------------
    def rollovers_held(self, entry_ts_ns: int, exit_ts_ns: int) -> int:
        """Weighted count of rollover instants r with entry < r <= exit."""
        if self.financing_mode in ("none", "not_modeled") or exit_ts_ns <= entry_ts_ns:
            return 0
        tz = self.rollover_timezone
        entry = pd.Timestamp(int(entry_ts_ns), tz="UTC").tz_convert(tz)
        exit_ = pd.Timestamp(int(exit_ts_ns), tz="UTC").tz_convert(tz)
        hh, mm = (int(x) for x in self.rollover_time.split(":"))
        d = entry.normalize().tz_localize(None).date() - timedelta(days=1)
        last = exit_.normalize().tz_localize(None).date()
        count = 0
        while d <= last:
            r = pd.Timestamp(d).replace(hour=hh, minute=mm).tz_localize(tz, nonexistent="shift_forward",
                                                                        ambiguous=True)
            if entry < r <= exit_ and d.weekday() < 5:
                count += 3 if self.triple_rollover_weekday == d.weekday() else 1
            d += timedelta(days=1)
        return count

    def financing_usd(self, direction: int, entry_price: float, contracts: float,
                      inst: Instrument, entry_ts_ns: int, exit_ts_ns: int) -> float:
        n = self.rollovers_held(entry_ts_ns, exit_ts_ns)
        if n == 0:
            return 0.0
        rate = self.financing_long_rate if direction > 0 else self.financing_short_rate
        notional = abs(entry_price) * inst.point_value * contracts
        return notional * rate / self.financing_day_count * n

    def to_dict(self) -> dict:
        return asdict(self)


_REQUIRED_WHEN_CONFIGURED = ("commission_per_side", "slippage_ticks_market", "slippage_ticks_stop")


SCENARIO_NAME = r"^[A-Za-z0-9][A-Za-z0-9_\-.]{0,63}$"


def _check_scenario(merged: dict, status: str, symbol: str, provider: str | None) -> None:
    """A profile that declares ``scenario`` (even as null) may only be used as a NAMED research-cost
    scenario: a name, a non-empty ``basis`` (the user's source statement), status assumed or
    broker_verified, notional commission with ``commission_per_million``, and market + stop slippage
    in points. Nothing is defaulted; a missing item refuses with the exact field."""
    import re
    where = f"costs.symbols.{symbol}{'.providers.' + provider if provider else ''}"
    errs, missing = [], []
    if not (isinstance(merged.get("scenario"), str) and re.match(SCENARIO_NAME, merged["scenario"])):
        errs.append("scenario: a name for this research-cost scenario (letters, digits, _ - .)")
    if not (isinstance(merged.get("basis"), str) and merged["basis"].strip()):
        errs.append("basis: your statement of the source of every number (not broker-verified unless it is)")
    if status not in ("assumed", "broker_verified"):
        errs.append("status: assumed or broker_verified")
    if merged.get("commission_mode") != "notional":
        errs.append("commission_mode: notional")
    if merged.get("commission_per_million") is None:
        errs.append("commission_per_million: USD per USD 1,000,000 traded notional per side")
    if merged.get("slippage_unit") != "points":
        errs.append("slippage_unit: points")
    for k in ("slippage_ticks_market", "slippage_ticks_stop"):
        if merged.get(k) is None:
            errs.append(f"{k}: slippage in points")
    if errs:
        missing = [e.split(":", 1)[0] for e in errs]
        raise CostScenarioIncomplete(f"cost scenario at {where} is incomplete - set: " + "; ".join(errs),
                                     merged.get("scenario"), status, missing)


def cost_model_from_config(cfg: Mapping, symbol: str, multiplier: float | None = None,
                           provider: str | None = None, allow_unconfigured: bool = False) -> CostModel:
    """Resolve costs: ``costs.default`` <- ``costs.symbols.<SYMBOL>`` <-
    ``costs.symbols.<SYMBOL>.providers.<PROVIDER>``.

    A resolved profile with ``status: unconfigured`` raises CostConfigError listing what
    to fill in - unless ``allow_unconfigured`` (plumbing tests only), which returns an
    all-zero model labelled ``zero_for_testing`` so no result can be mistaken for real.
    """
    costs = cfg["costs"]
    sym = dict((costs.get("symbols", {}) or {}).get(symbol, {}) or {})
    prov_blocks = sym.pop("providers", {}) or {}
    merged = {**costs["default"], **sym}
    profile = symbol
    if provider and provider in prov_blocks:
        merged.update(prov_blocks[provider])
        profile = f"{symbol}@{provider}"
    status = merged.pop("status", "assumed")
    notes = merged.pop("notes", None)  # documentation only
    if "scenario" in merged and status != "unconfigured":
        _check_scenario(merged, status, symbol, provider)
    required = _REQUIRED_WHEN_CONFIGURED
    if merged.get("commission_mode") == "notional":             # the per-million rate replaces the per-unit amount
        required = ("commission_per_million",) + tuple(k for k in required if k != "commission_per_side")
        if merged.get("commission_per_side") is None:
            merged["commission_per_side"] = 0.0
    if status == "unconfigured" or any(merged.get(k) is None for k in required):
        if allow_unconfigured:
            return CostModel(status="zero_for_testing", profile=profile,
                             multiplier=float(1.0 if multiplier is None else multiplier))
        missing = [k for k, v in merged.items() if v is None]
        raise CostConfigError(
            f"cost profile '{profile}' is unconfigured{f' ({notes})' if notes else ''}. "
            f"Set real broker values in configs/costs.yaml (costs.symbols.{symbol}"
            f"{'.providers.' + provider if provider else ''}): {missing or 'status: assumed|broker_verified'}")
    if merged.get("spread_source") == "quotes" and merged.get("spread_points") not in (None, 0, 0.0):
        raise CostConfigError(f"cost profile '{profile}': spread_source quotes embeds the spread in BID/ASK "
                              "fills; remove spread_points (it would be charged twice)")
    if merged.get("spread_points") is None:
        if merged.get("spread_source", "fixed") not in ("dataset", "quotes"):
            raise CostConfigError(f"cost profile '{profile}': set spread_points or spread_source: dataset|quotes")
        merged["spread_points"] = 0.0
    merged = {k: v for k, v in merged.items() if v is not None}
    if multiplier is None:
        multiplier = cfg.get("backtest", {}).get("cost_multiplier", 1.0)
    merged["multiplier"] = float(multiplier)
    str_fields = {"slippage_unit", "spread_source", "financing_mode", "rollover_time", "rollover_timezone",
                  "commission_mode", "scenario", "basis"}
    kw = {}
    for k, v in merged.items():
        if k in str_fields:
            kw[k] = str(v)
        elif k == "triple_rollover_weekday":
            kw[k] = None if v is None else int(v)
        else:
            kw[k] = float(v)
    return CostModel(status=status, profile=profile, **kw)
