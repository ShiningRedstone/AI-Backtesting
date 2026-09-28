"""Transaction costs.

Fills are triggered by *theoretical* prices (the level touched); slippage is then
applied to the fill price. Because costs never change WHICH bar a stop or target
triggers on, cost sensitivity is exact post hoc:

    net_R(k x costs) = gross_R - k * cost_R_at_1x

The engine stores ``cost_usd_base`` (1x) on every trade so analytics can
produce the 0.5x/1x/1.5x/2x/3x table without re-running the backtest.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Mapping

from edgelab.instruments import Instrument

ORDER_TYPES = ("market", "stop", "limit")


@dataclass(frozen=True)
class CostModel:
    commission_per_side: float = 0.0       # $ per contract per side
    fees_per_side: float = 0.0             # exchange + clearing + NFA, $ per contract per side
    slippage_ticks_market: float = 0.0     # adverse ticks on market fills
    slippage_ticks_stop: float = 0.0       # adverse ticks on stop fills (entries and stop-losses)
    slippage_ticks_limit: float = 0.0      # usually 0: limits fill at price or not at all
    spread_points: float = 0.0             # full bid/ask spread in points (CFDs); half per side
    multiplier: float = 1.0                # scenario scaling of ALL costs

    def __post_init__(self):
        for k, v in asdict(self).items():
            if v < 0:
                raise ValueError(f"cost field {k} must be >= 0")

    def slippage_ticks(self, order_type: str) -> float:
        return {"market": self.slippage_ticks_market, "stop": self.slippage_ticks_stop,
                "limit": self.slippage_ticks_limit}[order_type]

    def round_trip_base(self, entry_type: str, exit_type: str, contracts: float,
                        inst: Instrument) -> dict:
        """Costs at 1x for one round trip, in dollars, by component."""
        slip_ticks = self.slippage_ticks(entry_type) + self.slippage_ticks(exit_type)
        return {
            "commission_usd": 2 * self.commission_per_side * contracts,
            "fees_usd": 2 * self.fees_per_side * contracts,
            "slippage_usd": slip_ticks * inst.tick_value * contracts,
            "spread_usd": self.spread_points * inst.point_value * contracts,
            "slippage_ticks": slip_ticks,
        }

    def to_dict(self) -> dict:
        return asdict(self)


def cost_model_from_config(cfg: Mapping, symbol: str, multiplier: float | None = None) -> CostModel:
    costs = cfg["costs"]
    merged = {**costs["default"], **(costs.get("symbols", {}) or {}).get(symbol, {})}
    if multiplier is None:
        multiplier = cfg.get("backtest", {}).get("cost_multiplier", 1.0)
    merged["multiplier"] = float(multiplier)
    return CostModel(**{k: float(v) for k, v in merged.items()})
