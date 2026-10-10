"""Engine-facing strategy class of Fair price (ADR-114).

``FairStrategy`` turns the settings into the signals of ONE phase ("eval" or "funded") through ``logic.Rules`` and is
run by the ONE engine like every other strategy (fills, costs, BID/ASK execution, MNQ sizing, the lookahead check).
A backtest runs it twice (both phases); the prop challenge chain decides which phase's trades an account takes
(``fairprice.chain``). ``mystrategy.strategy.ReplayStrategy`` replays its signals in the holdout review.
"""
from __future__ import annotations

from collections import Counter

from edgelab.engine.signals import OrderSpec, SignalSet, Strategy
from edgelab.fairprice import params as P
from edgelab.fairprice.logic import RULES_VERSION, News, Rules


def sizing_of(s: dict, phase: str) -> dict:
    """Evaluations: a fixed dollar risk. Funded: every signal carries its own budget (win $ x stop / target), so a
    winning trade pays about ``funded.win_usd``; the sizing's risk_usd is only the fallback."""
    usd = float(s["eval.risk_usd"]) if phase == "eval" else float(s["funded.win_usd"])
    return {"mode": "risk", "risk_usd": usd, "max_contracts": int(s["risk.max_contracts"]), "contract": "MNQ"}


class FairStrategy(Strategy):
    family = "fair_price"

    def __init__(self, settings: dict | None, calendar, phase: str, news: News | None = None, skip: set | None = None,
                 trade_from_td: int | None = None, point_value: float | None = None):
        s = P.resolve(settings)
        if P.news_used(s) and news is None:
            raise ValueError("these settings trade scheduled news, which needs the news calendar input (it may be empty)")
        super().__init__(OrderSpec(entry_type="market"), params_version=P.PARAMS_VERSION, rules_version=RULES_VERSION,
                         settings_hash=P.settings_hash(s), phase=phase,
                         **({"news_hash": news.content_hash} if P.news_used(s) else {}))
        self.settings, self.calendar, self.phase = s, calendar, phase
        self.news = news if P.news_used(s) else None
        self.skip = set(skip or ())
        self.trade_from_td = trade_from_td
        self.point_value = point_value          # the execution contract's (flipped trades keep the setup's contracts)
        if s["models.flip"] and not point_value:
            raise ValueError("flipped trades need the execution contract's point value")
        self.sizing = sizing_of(s, phase)
        self.es = None
        self.explanations: dict[int, dict] = {}
        self.stats: Counter = Counter()
        self.days: list = []
        self.last_signals: SignalSet | None = None

    def generate_signals(self, bars) -> SignalSet:
        r = Rules(bars, self.calendar, self.settings, self.phase, news=self.news, skip=self.skip,
                  trade_from_td=self.trade_from_td, point_value=self.point_value)
        sig, expl, stats = r.run()
        self.explanations, self.stats, self.days, self.last_signals = expl, stats, r.days, sig
        return sig
