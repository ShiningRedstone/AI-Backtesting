"""The Fair price strategy as a ``mystrategy.kind.Kind`` (ADR-114): its folder, companion protocols, news input, the
two-phase engine runs and its autotuner tweaks."""
from __future__ import annotations

import pandas as pd

from edgelab.core.identity import hash_obj
from edgelab.fairprice import params as P
from edgelab.fairprice.logic import PHASES, RULES_VERSION, News


def news_for(svc, s: dict, ds=None, win=None) -> News | None:
    """The high-impact USD releases of the Market simulator's news calendar inside the dataset's span (None when the
    settings do not trade news). No calendar = an empty input: every day then trades the normal rules (the days are
    counted as 'news unknown'). The content hash goes into the strategy id and the trial key."""
    if not P.news_used(s):
        return None
    from edgelab.market import news as NW
    cal = NW.calendar(svc.data_root)
    ev = [] if not cal or cal.get("refused") else cal.get("events") or []
    first = int(ev[0]["ts"]) if ev else None
    last = int(ev[-1]["ts"]) if ev else None
    if ds is not None and len(ds.bars):
        lo, hi = int(ds.bars.ts_ns[0]), int(ds.bars.ts_ns[-1])
        ev = [e for e in ev if lo <= int(e["ts"]) <= hi]
        first = None if first is None else max(first, lo)
        last = None if last is None else min(last, hi)
    ev = [e for e in ev if int(e.get("impact") or 0) >= 3]
    rows = [[int(e["ts"]), str(e.get("name") or ""), e.get("surprise_z")] for e in ev]
    h = hash_obj({"events": rows, "first": first, "last": last}, 16)
    return News(ev, first, last, h)


def evaluated_hash(ds, s: dict, news) -> str:
    if news is None or not P.news_used(s):
        return ds.manifest.content_hash
    return hash_obj({"bars": ds.manifest.content_hash, "news": news.content_hash})


def execute(cfg, root, ds, s, news, *, td_from=None, skip=None, check=True, profile=None, start=None, until=None):
    """Both phases through the ONE engine (each with its lookahead check unless ``check`` is False), combined by the
    prop challenge chain of ``profile``. ``skip`` = {phase: signal bars the trader declined}."""
    from edgelab.engine.backtester import run_backtest
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.fairprice.chain import PhaseRun
    from edgelab.fairprice.strategy import FairStrategy
    from edgelab.instruments import check_identity, contract_for
    check_identity(ds.instrument)
    costs = cost_model_from_config(cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    bt = dict(cfg["backtest"])
    if not check:
        bt["require_causality_check"] = False
    strats, results = {}, {}
    for ph in PHASES:
        contract = contract_for(cfg, {"contract": "MNQ"})
        st = FairStrategy(s, ds.calendar, ph, news=news, skip=(skip or {}).get(ph), trade_from_td=td_from,
                          point_value=contract.point_value)
        results[ph] = run_backtest(ds, st, costs, bt, sizing=st.sizing, contract=contract_for(cfg, st.sizing))
        strats[ph] = st
    a = pd.Timestamp(start) if start is not None else pd.Timestamp(int(ds.bars.ts_ns[0]), tz="UTC")
    pr = PhaseRun(strats, results, profile, a, until)
    return pr.strategy, pr


def load_extra(svc, protocol, ds, start, end):
    return news_for(svc, P.resolve(), ds)


SETUP_REASONS = {
    "trend_day": "Trending day, price not reverting",
    "fair_price_unclear": "Fair price unclear (moved / consolidation elsewhere)",
    "weak_entry": "Weak displacement / structure",
    "too_close": "Too close to the fair price",
    "news_unexpected": "News outcome unexpected",
    "against_htf": "Against the higher timeframe",
    "other": "Other",
}

_AFTER_NEWS = lambda s: s["news.enabled"] and s["session.ny_am"]      # noqa: E731
_CONT = lambda s: s["cont.eval"] or s["cont.funded"]                   # noqa: E731
_FUNDED_REV = lambda s: s["funded.displacement"] or s["funded.bos"]    # noqa: E731
OPTIMIZER = {
    # the session opening times and the news time define WHICH sessions are traded; the cap is a safety limit
    "fixed": ("session.ny_am_open", "session.ny_pm_open", "session.asia_open", "session.london_open", "news.time",
              "risk.max_contracts", "models.flip", "models.flip_levels"),
    "steps": {
        "session.window_minutes": 15, "session.exit_after_minutes": 30, "fair.cons_minutes": 10,
        "fair.cons_max_points": 10.0, "news.max_surprise_z": 0.5, "cont.max_minutes": 1, "cont.bias_hours": 2,
        "cont.target_points": 4.0, "cont.stop_points": 2.5, "cont.big_candle_points": 5.0, "entry.swing_strength": 1,
        "entry.bos_lookback": 15, "entry.min_distance_share": 0.05, "entry.min_gap_points": 2.5,
        "eval.target_points": 4.0, "eval.stop_points": 2.5, "eval.risk_usd": 100.0, "funded.win_usd": 250.0,
        "funded.stop_points": 12.5, "funded.target_step": 5.0, "funded.min_target": 25.0, "funded.max_target": 25.0,
        "day.max_losses_in_row": 1, "day.max_trades_per_session": 1, "day.cooldown": 5,
    },
    "inert_unless": {
        "fair.cons_minutes": lambda s: s["fair.adapt"], "fair.cons_max_points": lambda s: s["fair.adapt"],
        "fair.pm_target": lambda s: s["session.ny_pm"],
        "news.until": _AFTER_NEWS, "news.max_surprise_z": _AFTER_NEWS,
        "cont.max_minutes": _CONT, "cont.structure": _CONT, "cont.bias_hours": _CONT, "cont.target_points": _CONT,
        "cont.stop_points": _CONT, "cont.big_candle_points": _CONT,
        "entry.disp_opposite": lambda s: s["eval.displacement"] or s["funded.displacement"],
        "entry.bos_lookback": lambda s: s["eval.bos"] or s["funded.bos"] or (_CONT(s) and s["cont.structure"] == "swing"),
        "entry.swing_strength": lambda s: s["eval.bos"] or s["funded.bos"] or (_CONT(s) and s["cont.structure"] == "swing"),
        "eval.target_points": lambda s: s["eval.displacement"] or s["eval.bos"],
        "eval.stop_points": lambda s: s["eval.displacement"] or s["eval.bos"],
        "funded.stop_points": _FUNDED_REV, "funded.target_step": _FUNDED_REV, "funded.min_target": _FUNDED_REV,
        "funded.max_target": _FUNDED_REV,
        "funded.win_usd": lambda s: _FUNDED_REV(s) or s["cont.funded"],
        "eval.risk_usd": lambda s: True,
        **{f"session.{k}_open": (lambda key: (lambda s: s[f"session.{key}"]))(k)
           for k in ("ny_am", "ny_pm", "asia", "london")},
    },
    "contradicts": [],
}


def fair_kind():
    from edgelab.mystrategy.kind import Kind
    from edgelab.research import protocol as rp
    return Kind(
        id="fair", label="Fair price", folder="fair_price", family="fair_price", P=P,
        strategy_role=rp.FAIR_STRATEGY_ROLE, strategy_suffix=rp.FAIR_STRATEGY_SCOPE_SUFFIX,
        optimizer_role=rp.FAIR_OPTIMIZER_ROLE, optimizer_suffix=rp.FAIR_OPTIMIZER_SCOPE_SUFFIX,
        holdout_role=rp.FAIR_HOLDOUT_ROLE, holdout_suffix=rp.FAIR_HOLDOUT_SCOPE_SUFFIX,
        texts={"model": "the hand-built 'Fair price' strategy (the fair pricing theory from a video transcript)",
               "source": "a public video interview about the fair pricing theory (transcript supplied by the user)",
               "dates_note": "The video's chart examples are from July-August of an unstated year, which may lie inside "
                             "the holdout window."},
        rules_version=RULES_VERSION, inputs=news_for, extra_of=lambda strat: getattr(strat, "news", None),
        evaluated_hash=evaluated_hash, execute=execute, load_extra=load_extra, setup_reasons=SETUP_REASONS,
        optimizer=OPTIMIZER, phased=True)
