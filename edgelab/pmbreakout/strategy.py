"""Engine-facing strategy class of Premarket breakout (ADR-115): settings -> signals (``logic.Rules``), run by the ONE
engine with the lookahead check like every other strategy."""
from __future__ import annotations

from collections import Counter

from edgelab.engine.signals import OrderSpec, SignalSet, Strategy
from edgelab.pmbreakout import params as P
from edgelab.pmbreakout.logic import RULES_VERSION, Rules


def sizing_of(s: dict) -> dict:
    if s["risk.mode"] == "equity_pct":
        return {"mode": "equity_risk", "risk_pct": float(s["risk.pct"]), "max_contracts": int(s["risk.max_contracts"]),
                "contract": "MNQ"}
    return {"mode": "risk", "risk_usd": float(s["risk.usd"]), "max_contracts": int(s["risk.max_contracts"]),
            "contract": "MNQ"}


class PremarketBreakout(Strategy):
    family = "pm_breakout"

    def __init__(self, settings: dict | None, calendar, skip: set | None = None, trade_from_td: int | None = None):
        s = P.resolve(settings)
        super().__init__(OrderSpec(entry_type="market"), params_version=P.PARAMS_VERSION, rules_version=RULES_VERSION,
                         settings_hash=P.settings_hash(s))
        self.settings, self.calendar = s, calendar
        self.skip = set(skip or ())
        self.trade_from_td = trade_from_td
        self.sizing = sizing_of(s)
        self.es = None
        self.explanations: dict[int, dict] = {}
        self.stats: Counter = Counter()
        self.days: list = []
        self.last_signals: SignalSet | None = None

    def generate_signals(self, bars) -> SignalSet:
        r = Rules(bars, self.calendar, self.settings, self.skip, self.trade_from_td)
        sig, expl, stats = r.run()
        self.explanations, self.stats, self.days, self.last_signals = expl, stats, r.days, sig
        return sig
