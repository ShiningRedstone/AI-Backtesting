"""Strategy kinds of the My strategy machinery (ADR-114).

The backtests, reports, test plans, Strategy autotuner, holdout allowance and setup review of My strategy (ADR-93 ...
ADR-103) serve more than one hand-built strategy. A ``Kind`` names what differs: the settings schema, the folder under
the data root, the companion protocols (roles, scopes, texts), how a settings combination is run through the engine and
what extra input it reads (ES for BP Blake's SMT, the news calendar for Fair price). ``BP`` (My strategy) is the default
everywhere, so every existing call keeps exactly its behaviour; ``kinds.get("fair")`` is the Fair price strategy.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any, Callable


@dataclass(frozen=True)
class Kind:
    id: str                      # "my" | "fair" (API prefix /api/<id>)
    label: str                   # shown in messages
    folder: str                  # <data>/<folder>/
    family: str                  # trial ledger family
    P: ModuleType                # settings schema module
    strategy_role: str
    strategy_suffix: str
    optimizer_role: str
    optimizer_suffix: str
    holdout_role: str
    holdout_suffix: str
    texts: dict
    rules_version: int
    inputs: Callable             # (svc, s, ds=None, win=None) -> extra input (ES / news) or None
    extra_of: Callable           # (strategy) -> the extra input it ran with
    evaluated_hash: Callable     # (ds, s, extra) -> content hash of what was evaluated
    execute: Callable            # (cfg, root, ds, s, extra, *, td_from, skip, check, profile, start, until) -> (strat, res)
    load_extra: Callable         # (svc, protocol, ds, start, end) -> extra for the autotuner (None allowed)
    setup_reasons: dict = field(default_factory=dict)
    optimizer: dict = field(default_factory=dict)    # steps / fixed / inert_unless / contradicts / adjust
    phased: bool = False         # True: results are evaluation + funded phases (Fair price)

    def strategy_ref(self, h: str) -> str:
        return f"{self.family}:{h}"


_CACHE: dict[str, Kind] = {}


def get(kid: str | None = None) -> Kind:
    kid = kid or "my"
    k = _CACHE.get(kid)
    if k is None:
        if kid == "my":
            k = _bp()
        elif kid == "fair":
            from edgelab.fairprice.kind import fair_kind
            k = fair_kind()
        else:
            raise KeyError(kid)
        _CACHE[kid] = k
    return k


def of(K: Kind | None) -> Kind:
    return K if K is not None else get("my")


# =============================================================================================== BP Blake (My strategy)
def _bp() -> Kind:
    from edgelab.mystrategy import params as P
    from edgelab.mystrategy.logic import RULES_VERSION
    from edgelab.research import protocol as rp

    def inputs(svc, s, ds=None, win=None):
        from edgelab.mystrategy import runner as R
        return R.es_for(svc, s, ds, win)

    def evaluated_hash(ds, s, es):
        from edgelab.mystrategy import runner as R
        return R.evaluated_hash(ds, s, es)

    def execute(cfg, root, ds, s, es, *, td_from=None, skip=None, check=True, profile=None, start=None, until=None):
        from edgelab.engine.backtester import run_backtest
        from edgelab.engine.costs import cost_model_from_config
        from edgelab.instruments import check_identity, contract_for
        from edgelab.mystrategy.strategy import MyStrategy
        strat = MyStrategy(s, ds.calendar, skip=skip, trade_from_td=td_from, es=es)
        check_identity(ds.instrument)
        costs = cost_model_from_config(cfg, ds.instrument.symbol, provider=ds.manifest.provider)
        bt = dict(cfg["backtest"])
        if not check:
            bt["require_causality_check"] = False
        return strat, run_backtest(ds, strat, costs, bt, sizing=strat.sizing, contract=contract_for(cfg, strat.sizing))

    def load_extra(svc, protocol, ds, start, end):
        from edgelab.mystrategy import es as ES
        try:
            es = ES.load(svc.data_root)
        except ES.EsError:
            return None
        if es is not None and not es.covers(max(int(start.value), int(ds.bars.ts_ns[0])),
                                            min(int(end.value), int(ds.bars.ts_ns[-1])) - 3 * 86_400_000_000_000):
            return None
        return es

    return Kind(
        id="my", label="My strategy", folder="my_strategy", family="my_strategy", P=P,
        strategy_role=rp.MY_STRATEGY_ROLE, strategy_suffix=rp.MY_STRATEGY_SCOPE_SUFFIX,
        optimizer_role=rp.OPTIMIZER_ROLE, optimizer_suffix=rp.OPTIMIZER_SCOPE_SUFFIX,
        holdout_role=rp.MY_HOLDOUT_ROLE, holdout_suffix=rp.MY_HOLDOUT_SCOPE_SUFFIX,
        texts={"model": "the hand-built 'My strategy' (BP Blake's model)",
               "source": "BP Blake's public videos (transcripts supplied by the user)",
               "dates_note": "The videos show trades from May-August 2026, which may lie inside the holdout window."},
        rules_version=RULES_VERSION, inputs=inputs, extra_of=lambda strat: getattr(strat, "es", None),
        evaluated_hash=evaluated_hash, execute=execute, load_extra=load_extra)


def any_of(x: Any) -> Kind:
    """A Kind from a Kind, an id or None."""
    if isinstance(x, Kind):
        return x
    return get(x)
