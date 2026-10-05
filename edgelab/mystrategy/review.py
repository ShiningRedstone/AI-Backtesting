"""Holdout review of My strategy (ADR-93): the protocol's ONE holdout look, then the trader's take / skip decisions.

1. ``start``: frozen settings that were backtested on the discovery period; the look is spent (holdout_access
   'granted'), the mechanical holdout result is computed by the engine (causality check on), recorded as a run
   (OUT_OF_SAMPLE, labelled Holdout) and saved as a report.
2. The review then walks the setups in time order. For each one the trader sees ONLY data up to the signal bar (the
   chart stops at the entry candle) and decides take or skip. Declined signals are removed and the day continues as if
   the trader had stayed flat (``MyStrategy(skip=...)``); the engine then decides, as always, which signals become
   trades (one position at a time, daily limits). The outcome of a taken trade is shown right after the decision.
3. When no undecided setup is left, the "with your decisions" result is saved next to the mechanical one. It is never
   a run or a trial: human discretion stays separate from automated results.

The signals of a review step are produced by the same rules; the engine replays them without repeating the causality
check (it ran on the mechanical holdout run). Declining a signal is a decision at its own bar.
"""
from __future__ import annotations

import json
import re
import threading
from pathlib import Path
from typing import Callable

import pandas as pd

from edgelab.mystrategy import params as P
from edgelab.mystrategy import runner as R
from edgelab.mystrategy.records import Charts, charts_used, jsonable, report_numbers, trade_rows
from edgelab.mystrategy.strategy import MyStrategy, ReplayStrategy

_MEMO_LOCK = threading.Lock()


def _dir(svc) -> Path:
    p = R.home(svc) / "reviews"
    p.mkdir(parents=True, exist_ok=True)
    return p


def current(svc) -> dict | None:
    rows = sorted(_dir(svc).glob("RV_*/state.json"))
    return R._read_json(rows[-1]) if rows else None


def _save(svc, st: dict) -> None:
    R._write_json(_dir(svc) / st["id"] / "state.json", st)


def _decisions(st: dict) -> dict[int, dict]:
    return {int(k): v for k, v in (st.get("decisions") or {}).items()}


def start(svc, overrides: dict | None, *, lock=None, progress: Callable | None = None) -> dict:
    from datetime import datetime, timezone
    s = P.resolve(overrides if overrides is not None else R.load_overrides(svc))
    h = P.settings_hash(s)
    parent, mine = R.ensure_protocol(svc, lock)
    pid, mat = mine["protocol_id"], mine["material"]
    cur = current(svc)
    if cur and cur["status"] == "in_progress":
        raise R.MyStrategyError("REVIEW_OPEN", "A holdout review is already in progress.")
    guard = lock if lock is not None else R.nullcontext()
    with guard:
        ev = [e for e in svc.store.list_trial_events(pid) if e["logic_hash"] == h and e["counted"]]
        if not ev:
            raise R.MyStrategyError("NOT_BACKTESTED", "Backtest these exact settings on the discovery period first: the "
                                                      "holdout tests frozen settings only.")
        used = [a for a in svc.store.list_holdout_access(pid) if a["status"] != "refused"]
        budget = mat["holdout_budget"]["max_unique_candidate_evaluations"]
        if len(used) >= budget:
            raise R.MyStrategyError("HOLDOUT_LOOKS_USED", f"The My strategy protocol's {budget} holdout look(s) are used.")
        if svc._config_hash() != mat["config_hash"]:
            raise R.MyStrategyError("PROTOCOL_CONFIG_CHANGED", "The research settings differ from the protocol's.")
        rv_id = R.new_id("RV")
        access_id = "HA_" + rv_id[3:]
        svc.store.add_holdout_access({"access_id": access_id, "protocol_id": pid, "strategy_id": "my_strategy:" + h,
                                      "logic_hash": h, "definition_hash": h, "frozen_hash": h, "search_id": None,
                                      "status": "granted", "reason_code": None, "reason": None, "run_id": None,
                                      "result_json": None, "created_at": R._now(), "completed_at": None})
    try:
        strat, res, ds, win, _ = R.run_window(svc, s, None, None, stage="holdout", lock=lock, progress=progress)
        if progress:
            progress("Recording the mechanical holdout result")
        rec = R._record(svc, s, strat, res, ds, win, mine, status="OUT_OF_SAMPLE",
                        notes=f"My strategy holdout (mechanical) {rv_id}", lock=lock, count=False)
        ho_id = "HO_" + rv_id[3:]
        summary = R.build_report(R.home(svc) / "holdout" / ho_id, s, strat, res, ds, win, rec, kind="holdout_mechanical",
                                 label="Holdout - mechanical (every signal)", extra={"review_id": rv_id,
                                                                                    "protocol_id": pid},
                                 challenge=R.challenge_of(svc, ds, strat, res, win))
        with guard:
            svc.store.update_holdout_access(access_id, status="completed", run_id=rec["run_id"],
                                            completed_at=datetime.now(timezone.utc).isoformat(),
                                            result_json=json.dumps(jsonable({"outcome": "MY_STRATEGY_HOLDOUT",
                                                                             "trade_count": summary["trade_count"],
                                                                             "metrics": summary["metrics"]})))
    except Exception as e:
        with guard:
            svc.store.update_holdout_access(access_id, status="failed", reason=str(e)[:500],
                                            completed_at=datetime.now(timezone.utc).isoformat())
        raise
    st = {"id": rv_id, "created_at": R._now(), "status": "in_progress", "settings": s, "settings_hash": h,
          "protocol_id": pid, "access_id": access_id, "window": summary["window"],
          "dataset_id": summary["dataset"]["dataset_id"], "mechanical_report": ho_id, "decisions": {},
          "es_content_hash": None if strat.es is None else strat.es.content_hash,
          "final_report": None}
    _save(svc, st)
    return st


# ---------------------------------------------------------------------------------------------- one step
def _run(svc, st: dict, lock=None):
    """(strategy, result, ds) with the trader's declined signals removed. Remembered per decision set."""
    from edgelab.engine.backtester import run_backtest
    skip = frozenset(k for k, v in _decisions(st).items() if not v["take"])
    key = (st["id"], skip)
    memo = svc.__dict__.setdefault("_my_review_memo", {})
    with _MEMO_LOCK:
        hit = memo.get(key)
    if hit is not None:
        return hit
    s = st["settings"]
    w = st["window"]
    start, end = R._ts(w["start"]), R._ts(w["end"])
    _, mine = R.ensure_protocol(svc, lock, create=False)
    mat = mine["material"]
    data_start = max(R._ts(R.windows(mat)["discovery"]["start"]), start - pd.Timedelta(days=R.WARMUP_DAYS))
    ds = svc._cell_dataset(R.dataset_1m(svc, mine), (data_start, end), lock)
    es = R.es_for(svc, P.resolve(s), ds, (start, end))
    if P.smt_used(P.resolve(s)) and (es is None or es.content_hash != st.get("es_content_hash")):
        raise R.MyStrategyError("ES_DATA_CHANGED", "The ES data changed since this holdout review started; the review "
                                                   "needs the ES data it started with.")
    strat = MyStrategy(s, ds.calendar, skip=set(skip), trade_from_td=R._trading_date_ord(ds.calendar, start),
                       es=es if es is not None and es.content_hash == st.get("es_content_hash", es.content_hash) else None)
    sig = strat.generate_signals(ds.bars)
    replay = ReplayStrategy(strat, sig, set(), len(ds.bars))
    costs, contract = R._engine_inputs(svc, ds, strat)
    bt = {**svc.cfg["backtest"], "require_causality_check": False}
    res = run_backtest(ds, replay, costs, bt, sizing=strat.sizing, contract=contract)
    out = (strat, res, ds)
    with _MEMO_LOCK:
        if len(memo) > 6:
            memo.pop(next(iter(memo)))
        memo[key] = out
    return out


def _candidate(svc, st: dict, lock=None) -> dict | None:
    strat, res, ds = _run(svc, st, lock)
    dec = _decisions(st)
    if res.trades.empty:
        return None
    for r in res.trades.sort_values("signal_bar").to_dict("records"):
        sb = int(r["signal_bar"])
        if sb not in dec:
            expl = strat.explanations.get(sb, {})
            tfs = charts_used(expl)
            charts = Charts(ds.bars, ds.calendar, st["settings"]["models.price_series"])
            return {"signal_bar": sb, "signal_ts": pd.Timestamp(int(ds.bars.ts_ns[sb]), tz="UTC").isoformat(),
                    "explanation": jsonable(expl), "charts": tfs,
                    "candles": charts.for_trade(tfs, sb, sb, until=sb)}
    return None


def progress_of(svc, st: dict, lock=None) -> dict:
    strat, res, ds = _run(svc, st, lock)
    dec = _decisions(st)
    taken = res.trades[res.trades["signal_bar"].isin([k for k, v in dec.items() if v["take"]])] \
        if not res.trades.empty else res.trades
    return {"decided": len(dec), "taken": int(sum(1 for v in dec.values() if v["take"])),
            "skipped": int(sum(1 for v in dec.values() if not v["take"])),
            "taken_net_r": round(float(taken["net_r"].sum()), 4) if len(taken) else 0.0,
            "taken_wins": int((taken["net_r"] > 0).sum()) if len(taken) else 0}


def view(svc, lock=None) -> dict:
    st = current(svc)
    if st is None:
        return {"review": None}
    out = {"review": {k: st[k] for k in ("id", "created_at", "status", "settings_hash", "window", "mechanical_report",
                                         "final_report")}}
    out["review"]["settings_changed"] = P.changed(st["settings"])
    if st["status"] == "in_progress":
        out["progress"] = progress_of(svc, st, lock)
        out["candidate"] = _candidate(svc, st, lock)
        if out["candidate"] is None:
            out["finished"] = finish(svc, st, lock)
            out["review"]["status"] = "complete"
            out["review"]["final_report"] = out["finished"]["id"]
    return out


def decide(svc, signal_bar: int, take: bool, lock=None) -> dict:
    st = current(svc)
    if st is None or st["status"] != "in_progress":
        raise R.MyStrategyError("NO_REVIEW", "No holdout review is in progress.")
    cand = _candidate(svc, st, lock)
    if cand is None or cand["signal_bar"] != int(signal_bar):
        raise R.MyStrategyError("NOT_CURRENT", "That setup is not the one waiting for a decision (refresh the page).")
    st["decisions"][str(int(signal_bar))] = {"take": bool(take), "decided_at": R._now(), "signal_ts": cand["signal_ts"]}
    _save(svc, st)
    out: dict = {"signal_bar": int(signal_bar), "take": bool(take)}
    if take:
        strat, res, ds = _run(svc, st, lock)
        row = res.trades[res.trades["signal_bar"] == int(signal_bar)]
        if len(row):
            r = trade_rows(row)[0]
            charts = Charts(ds.bars, ds.calendar, st["settings"]["models.price_series"])
            out["outcome"] = {**r, "candles": charts.for_trade(cand["charts"], int(signal_bar), int(r["exit_bar"]))}
    return out


def finish(svc, st: dict, lock=None) -> dict:
    strat, res, ds = _run(svc, st, lock)
    dec = _decisions(st)
    w = st["window"]
    win = (R._ts(w["start"]), R._ts(w["end"]))
    nums = report_numbers(res.trades, win[0], win[1], svc.cfg.get("sample_size"))
    from edgelab.prop.service import outcomes
    prop = outcomes(svc.root, res.trades, assumptions=res.assumptions)
    hd_id = "HD_" + st["id"][3:]
    rec = {"run_id": None, "trial_id": None, "trial_counted": None, "prop": prop, **nums}
    summary = R.build_report(R.home(svc) / "holdout" / hd_id, st["settings"], strat, res, ds, win, rec,
                             kind="holdout_with_decisions", label="Holdout - with your decisions",
                             extra={"review_id": st["id"], "decisions": {str(k): v for k, v in dec.items()},
                                    "note": "Human decisions applied; not a run and not a trial (kept separate from "
                                            "automated results)."},
                             challenge=R.challenge_of(svc, ds, strat, res, win))
    st["status"], st["final_report"], st["finished_at"] = "complete", hd_id, R._now()
    _save(svc, st)
    return summary


def valid_id(x: str) -> bool:
    return bool(re.fullmatch(r"RV_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", x))
