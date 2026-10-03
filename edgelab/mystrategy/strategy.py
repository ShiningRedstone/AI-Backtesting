"""Engine-facing strategy classes of My strategy (ADR-93).

``MyStrategy`` turns the settings into signals through ``logic.Rules`` and is run by the ONE engine
(``engine.backtester.run_backtest``) exactly like every other strategy: same fills, costs, BID/ASK execution, MNQ
sizing and the empirical causality (lookahead) check on every run.

``ReplayStrategy`` re-plays a subset of signals already produced (and causality-checked) by a ``MyStrategy`` on the
same bars. It is used by the holdout review, where the trader's take / skip decisions remove signals; the engine
then decides, as always, which of the remaining signals become trades.
"""
from __future__ import annotations

from collections import Counter

import numpy as np

from edgelab.engine.signals import OrderSpec, SignalSet, Strategy
from edgelab.mystrategy import params as P
from edgelab.mystrategy.logic import Rules


def order_spec(s: dict) -> OrderSpec:
    if s["entry.type"] == "market":
        return OrderSpec(entry_type="market")
    return OrderSpec(entry_type="limit", entry_expiry_bars=int(s["entry.limit_minutes"]))


def sizing_of(s: dict) -> dict:
    if s["risk.mode"] == "equity_pct":
        return {"mode": "equity_risk", "risk_pct": float(s["risk.pct"]), "max_contracts": int(s["risk.max_contracts"]),
                "contract": "MNQ"}
    return {"mode": "risk", "risk_usd": float(s["risk.usd"]), "max_contracts": int(s["risk.max_contracts"]),
            "contract": "MNQ"}


class MyStrategy(Strategy):
    family = "my_strategy"

    def __init__(self, settings: dict | None, calendar, skip: set | None = None, trade_from_td: int | None = None,
                 es=None):
        s = P.resolve(settings)
        if es is None and P.smt_used(s):
            raise ValueError("these settings use SMT divergence, which needs the ES data")
        super().__init__(order_spec(s), params_version=P.PARAMS_VERSION, settings_hash=P.settings_hash(s),
                         settings=P.changed(P.identity(s)), **({"es_content_hash": es.content_hash} if es is not None and
                                                   P.smt_used(s) else {}))
        self.settings = s
        self.calendar = calendar
        self.skip = set(skip or ())
        self.trade_from_td = trade_from_td      # run window (warm-up before it), never part of the identity
        self.es = es                            # ES reference prices for SMT (ADR-95); None = SMT unknown
        self.sizing = sizing_of(s)
        self.explanations: dict[int, dict] = {}
        self.stats: Counter = Counter()
        self.days: list = []
        self.last_signals: SignalSet | None = None

    def generate_signals(self, bars) -> SignalSet:
        r = Rules(bars, self.calendar, self.settings, self.skip, self.trade_from_td, es=self.es)
        sig, expl, stats = r.run()
        self.explanations, self.stats, self.days, self.last_signals = expl, stats, r.days, sig
        return sig


class ReplayStrategy(Strategy):
    """Signals of an earlier causality-checked ``MyStrategy`` run on the SAME bars, minus the removed signal bars."""
    family = "my_strategy_replay"

    def __init__(self, base: MyStrategy, signals: SignalSet, removed: set[int], n_bars: int):
        super().__init__(base.order, **base.params, removed=sorted(int(x) for x in removed))
        self.sizing = base.sizing
        self._sig, self._removed, self._n = signals, set(removed), n_bars

    def generate_signals(self, bars) -> SignalSet:
        n = len(bars)
        if n > self._n:
            raise ValueError("replay bars are longer than the recorded signals")
        src = self._sig
        out = SignalSet(src.direction[:n].copy(), src.entry_price[:n].copy(), src.stop_price[:n].copy(),
                        src.target_price[:n].copy(),
                        exit_long=None if src.exit_long is None else src.exit_long[:n].copy(),
                        exit_short=None if src.exit_short is None else src.exit_short[:n].copy(),
                        trail=src.trail,
                        trail_level_long=None if src.trail_level_long is None else src.trail_level_long[:n].copy(),
                        trail_level_short=None if src.trail_level_short is None else src.trail_level_short[:n].copy(),
                        max_trades_per_day=src.max_trades_per_day, exit_cooldown_bars=src.exit_cooldown_bars,
                        block_after=src.block_after)
        for i in self._removed:
            if i < n:
                out.direction[i] = 0
                out.entry_price[i] = out.stop_price[i] = out.target_price[i] = np.nan
        return out
