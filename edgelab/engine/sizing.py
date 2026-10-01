"""Position sizing.

Contracts are always rounded DOWN. If the stop is too wide for even one contract
within the risk budget, the answer is 0 contracts (trade rejected) - never
"round up to 1", which would silently exceed the risk limit. This matters for
prop accounts: a 40-point NQ stop is $800/contract, which may exceed a small
account's per-trade budget.

Sizing is decided at SIGNAL time from information available then (planned
entry, planned stop). It does not peek at the actual fill.

``equity_risk`` (ADR-62): risk budget = risk_pct % of the account equity AT THE SIGNAL, where equity =
the run's ACCOUNT starting equity (default $50,000; not a strategy property) + the net P&L (after costs) of trades
that have already EXITED. The backtester runs one
position at a time, so every earlier trade has exited by the time a later signal is decided; no open or
future trade can enter the equity figure. The contracts then follow the same rule as ``risk`` (rounded down,
0 = rejected, optional hard cap), from the planned initial stop.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import ROUND_FLOOR, Decimal

from edgelab.instruments import Instrument

# The research ACCOUNT scenario: a run/evaluation environment, never part of a strategy's identity. The same strategy is
# simulated under any other account size by passing ``account=`` to the backtester.
DEFAULT_RESEARCH_ACCOUNT = {"name": "research_50k", "starting_equity": 50_000.0, "currency": "USD"}

DOLLAR_DIGITS = 9          # dollars are compared at 1e-9: far above float noise (~1e-11 at 1e5), below any real amount


@dataclass(frozen=True)
class SizingDecision:
    contracts: float          # whole contracts for futures (int-valued), units/lots for CFDs
    risk_per_contract_usd: float
    total_risk_usd: float
    reason: str


def _d(x: float) -> Decimal:
    """float -> Decimal at dollar precision (removes float noise such as 499.99999999999994 -> 500)."""
    return Decimal(repr(round(float(x), DOLLAR_DIGITS)))


def contracts_for_risk(risk_usd: float, stop_distance_points: float, inst: Instrument,
                       max_contracts: int | None = None,
                       cost_per_contract_usd: float = 0.0) -> SizingDecision:
    """THE contract conversion used by every risk-based sizing mode (``risk`` and ``equity_risk``):

        contracts = floor( risk_budget_usd / (stop_points x point_value + cost) / size_step ) x size_step

    Exact decimal arithmetic: the result never rounds UP, so contracts x risk-per-contract <= budget always
    holds (at 1e-9 dollars), and it is maximal (one more step would exceed the budget). Whole contracts when
    the instrument's size step is 1. 0 (trade rejected) when the budget cannot buy the minimum size.
    ``max_contracts`` is a hard deterministic cap."""
    if not (stop_distance_points > 0 and math.isfinite(stop_distance_points)):
        return SizingDecision(0, math.nan, 0.0, "invalid stop distance")
    if not (risk_usd > 0 and math.isfinite(risk_usd)):
        return SizingDecision(0, math.nan, 0.0, "non-positive risk budget")
    per_f = stop_distance_points * inst.point_value + cost_per_contract_usd
    per, budget, step = _d(per_f), _d(risk_usd), Decimal(repr(inst.size_step))
    if per <= 0:
        return SizingDecision(0, math.nan, 0.0, "invalid stop distance")
    steps = (budget / per / step).to_integral_value(rounding=ROUND_FLOOR)
    n_d = steps * step
    reason = "ok"
    if max_contracts is not None and n_d > Decimal(repr(max_contracts)):
        n_d, reason = Decimal(repr(max_contracts)), f"capped at max_contracts={max_contracts}"
    n = float(n_d)
    if inst.size_step == 1.0:
        n = int(n_d)
    if n < inst.min_size:
        n = 0
        reason = f"{inst.min_size:g} unit(s) risk ${per_f * inst.min_size:,.2f} > budget ${risk_usd:,.2f}"
    return SizingDecision(n, per_f, n * per_f, reason)


def check_quantity(n: float, inst: Instrument, what: str = "quantity") -> float:
    """Defense in depth at the engine boundary: a size that is not a multiple of the instrument's size step
    (whole contracts for step 1) or is below its minimum never reaches execution."""
    steps = n / inst.size_step
    if n < inst.min_size or abs(steps - round(steps)) > 1e-9:
        raise ValueError(f"{what} {n!r} is not a valid size for {inst.symbol} (min {inst.min_size:g}, "
                         f"step {inst.size_step:g}): fractional contracts are never executed")
    return int(round(steps * inst.size_step)) if inst.size_step == 1.0 else n


def size_trade(sizing_cfg: dict, stop_distance_points: float, inst: Instrument,
               equity: float | None = None) -> SizingDecision:
    mode = sizing_cfg.get("mode", "fixed")
    if mode == "fixed":
        n = sizing_cfg.get("contracts", 1)
        n = int(n) if float(n).is_integer() else float(n)
        if sizing_cfg.get("contract"):                 # a named contract trades whole contract units only
            n = check_quantity(n, inst, "fixed contracts")
        per = stop_distance_points * inst.point_value
        return SizingDecision(n, per, n * per, "fixed")
    if mode == "risk":
        return contracts_for_risk(float(sizing_cfg["risk_usd"]), stop_distance_points, inst,
                                  sizing_cfg.get("max_contracts"))
    if mode == "equity_risk":
        if equity is None:
            raise ValueError("equity_risk sizing needs the running equity (the backtester supplies it)")
        if not equity > 0:
            return SizingDecision(0, math.nan, 0.0, f"non-positive equity {equity:,.2f}")
        return contracts_for_risk(float(sizing_cfg["risk_pct"]) / 100.0 * equity, stop_distance_points, inst,
                                  sizing_cfg.get("max_contracts"))
    raise ValueError(f"unknown sizing mode {mode!r}")
