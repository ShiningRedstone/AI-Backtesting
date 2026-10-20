"""Premarket breakout as a ``mystrategy.kind.Kind`` (ADR-115): its folder, companion protocols, execution and autotuner
tweaks. One engine run per backtest (not phased); the prop challenge chain cuts trades bigger than the account allows to
its limit (the user's rule from ADR-114), so a chain never stops on a size limit."""
from __future__ import annotations

from edgelab.pmbreakout import params as P
from edgelab.pmbreakout.logic import RULES_VERSION


def inputs(svc, s, ds=None, win=None):
    return None


def evaluated_hash(ds, s, extra) -> str:
    return ds.manifest.content_hash


def execute(cfg, root, ds, s, extra, *, td_from=None, skip=None, check=True, profile=None, start=None, until=None):
    from edgelab.engine.backtester import run_backtest
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.instruments import check_identity, contract_for
    from edgelab.pmbreakout.strategy import PremarketBreakout
    strat = PremarketBreakout(s, ds.calendar, skip=skip, trade_from_td=td_from)
    check_identity(ds.instrument)
    costs = cost_model_from_config(cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    bt = dict(cfg["backtest"])
    if not check:
        bt["require_causality_check"] = False
    return strat, run_backtest(ds, strat, costs, bt, sizing=strat.sizing, contract=contract_for(cfg, strat.sizing))


def load_extra(svc, protocol, ds, start, end):
    return None


SETUP_REASONS = {
    "fake_breakout": "Looks like a fake breakout",
    "range_too_small": "Premarket range too small / choppy",
    "range_too_big": "Premarket range too big",
    "stop_too_wide": "Stop too wide",
    "stop_too_tight": "Stop too tight",
    "against_htf": "Against the higher timeframe",
    "news": "News / event risk",
    "other": "Other",
}

OPTIMIZER = {
    "fixed": ("risk.mode", "risk.usd", "risk.pct", "risk.max_contracts", "entry.tf"),
    "steps": {"pm.min_minutes": 30, "pm.min_range_points": 10.0, "pm.max_range_points": 25.0,
              "entry.buffer_points": 1.0, "stop.buffer_points": 1.0, "stop.min_points": 2.5, "stop.max_points": 10.0,
              "target.rr": 0.5, "day.max_trades": 1},
    "inert_unless": {},
    "contradicts": [],
}


def pm_kind():
    from edgelab.mystrategy.kind import Kind
    from edgelab.research import protocol as rp
    return Kind(
        id="pmb", label="Premarket breakout", folder="pm_breakout", family="pm_breakout", P=P,
        strategy_role=rp.PMB_STRATEGY_ROLE, strategy_suffix=rp.PMB_STRATEGY_SCOPE_SUFFIX,
        optimizer_role=rp.PMB_OPTIMIZER_ROLE, optimizer_suffix=rp.PMB_OPTIMIZER_SCOPE_SUFFIX,
        holdout_role=rp.PMB_HOLDOUT_ROLE, holdout_suffix=rp.PMB_HOLDOUT_SCOPE_SUFFIX,
        texts={"model": "the user's 'Premarket breakout' strategy (premarket high / low, a 5-minute close beyond it, "
                        "stop at the candle's wick, 1 : 8 target)",
               "source": "the user's short description typed in the chat (a widely shared public setup)",
               "dates_note": "The rules came with no chart examples or dates; the user may have seen or traded this "
                             "setup on any day, including days inside the holdout window."},
        rules_version=RULES_VERSION, inputs=inputs, extra_of=lambda strat: None, evaluated_hash=evaluated_hash,
        execute=execute, load_extra=load_extra, setup_reasons=SETUP_REASONS, optimizer=OPTIMIZER, phased=False,
        fit_limits=True)
