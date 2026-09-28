"""Position sizing.

Contracts are always rounded DOWN. If the stop is too wide for even one contract
within the risk budget, the answer is 0 contracts (trade rejected) - never
"round up to 1", which would silently exceed the risk limit. This matters for
prop accounts: a 40-point NQ stop is $800/contract, which may exceed a small
account's per-trade budget.

Sizing is decided at SIGNAL time from information available then (planned
entry, planned stop). It does not peek at the actual fill.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from edgelab.instruments import Instrument


@dataclass(frozen=True)
class SizingDecision:
    contracts: int
    risk_per_contract_usd: float
    total_risk_usd: float
    reason: str


def contracts_for_risk(risk_usd: float, stop_distance_points: float, inst: Instrument,
                       max_contracts: int | None = None,
                       cost_per_contract_usd: float = 0.0) -> SizingDecision:
    if not (stop_distance_points > 0 and math.isfinite(stop_distance_points)):
        return SizingDecision(0, math.nan, 0.0, "invalid stop distance")
    if risk_usd <= 0:
        return SizingDecision(0, math.nan, 0.0, "non-positive risk budget")
    per = stop_distance_points * inst.point_value + cost_per_contract_usd
    n = int(math.floor(risk_usd / per + 1e-9))
    reason = "ok"
    if max_contracts is not None and n > max_contracts:
        n, reason = max_contracts, f"capped at max_contracts={max_contracts}"
    if n == 0:
        reason = f"1 contract risks ${per:,.2f} > budget ${risk_usd:,.2f}"
    return SizingDecision(n, per, n * per, reason)


def size_trade(sizing_cfg: dict, stop_distance_points: float, inst: Instrument) -> SizingDecision:
    mode = sizing_cfg.get("mode", "fixed")
    if mode == "fixed":
        n = int(sizing_cfg.get("contracts", 1))
        per = stop_distance_points * inst.point_value
        return SizingDecision(n, per, n * per, "fixed")
    if mode == "risk":
        return contracts_for_risk(float(sizing_cfg["risk_usd"]), stop_distance_points, inst,
                                  sizing_cfg.get("max_contracts"))
    raise ValueError(f"unknown sizing mode {mode!r}")
