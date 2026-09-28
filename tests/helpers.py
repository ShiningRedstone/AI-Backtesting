"""Shared fixtures for the test suite (stdlib unittest; also collected by pytest)."""
from __future__ import annotations

import copy

import numpy as np

from edgelab.core.config import load_config
from edgelab.data.calendar import SessionCalendar, load_calendars
from edgelab.data.schema import BarArrays
from edgelab.data.synthetic import bars_from_ohlc
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.costs import CostModel
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy
from edgelab.instruments import load_instruments

CFG = load_config(environ={})
INSTRUMENTS = load_instruments(CFG)
CALENDARS = load_calendars(CFG)
NQ = INSTRUMENTS["NQ"]
CME = CALENDARS["CME_EQUITY"]
UTC247 = SessionCalendar("TEST_24_7", "UTC", "00:00", "24:00", tuple(range(7)))
ZERO_COSTS = CostModel()
NQ_COSTS = CostModel(commission_per_side=1.5, fees_per_side=0.7, slippage_ticks_market=1,
                     slippage_ticks_stop=1, slippage_ticks_limit=0)


def bt_cfg(**over):
    """Backtest config for scripted scenarios: no session flattening, no causality check."""
    c = copy.deepcopy(CFG["backtest"])
    c["session"] = {"flatten_daily": False, "hold_overnight": True}
    c["same_bar_policy"] = "conservative"
    c["require_causality_check"] = False
    for k, v in over.items():
        c[k] = v
    return c


def dataset(rows, tf=1, cal=UTC247, start="2024-01-02 00:00", ds_id="TEST", inst=NQ, df=None):
    df = bars_from_ohlc(rows, start=start, tf_minutes=tf) if df is None else df
    return validate_and_freeze(df, inst, cal, f"{tf}m", tf, "test", ds_id)


class Scripted(Strategy):
    """Fixed signals: {bar_index: direction} plus optional price arrays. Causal by construction."""
    family = "scripted"

    def __init__(self, order: OrderSpec, signals: dict, entry=None, stop=None, target=None):
        super().__init__(order, signals={str(k): v for k, v in signals.items()},
                         entry=entry, stop=stop, target=target)
        self._sig, self._e, self._s, self._t = signals, entry or {}, stop or {}, target or {}

    def generate_signals(self, bars: BarArrays) -> SignalSet:
        s = SignalSet.empty(len(bars))
        for i, d in self._sig.items():
            if i < len(bars):
                s.direction[i] = d
        for src, dst in ((self._e, s.entry_price), (self._s, s.stop_price), (self._t, s.target_price)):
            for i, v in src.items():
                if i < len(bars):
                    dst[i] = v
        return s


def flat_bar(p):
    return (p, p + 0.5, p - 0.5, p)
