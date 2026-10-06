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
    mine = svc.store.get_protocol(st["protocol_id"]) if st.get("protocol_id") else None
    if mine is None:
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
    st = open_review(svc)
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


# ============================================================================================== ADR-102: 2-look allowance
# One strategy, two holdout looks: first AUTOMATIC (the engine trades every signal; the result is shown at once), then
# MANUAL (the trader's take / skip on each setup, with the automatic result already known - the user's choice). The looks
# belong to a companion protocol of their own (role ``my_holdout``) created when the user reset the holdout limit; the
# looks spent earlier under the My strategy / autotuner protocols stay recorded there. The strategy can be any settings
# combination counted as a discovery try of My strategy or of the Strategy autotuner.
LOOKS = ("automatic", "manual")


def _allowance_path(svc) -> Path:
    return R.home(svc) / "holdout_allowance.json"


def _hp_scope(parent: dict) -> str:
    from edgelab.research.protocol import MY_HOLDOUT_SCOPE_SUFFIX
    sc = parent["material"]["scope"]
    return R.svc_scope(sc["instrument"], sc["provider"]) + MY_HOLDOUT_SCOPE_SUFFIX


def _source_protocols(svc, parent: dict) -> list[dict]:
    """The protocols whose discovery tries may be tested: My strategy's and the Strategy autotuner's (active, same parent)."""
    from edgelab.mystrategy import optimizer as O
    out = []
    for scope in (R._scope(parent), O._scope(parent)):
        out += [p for p in svc.store.list_protocols(scope, "ACTIVE")
                if (p["material"].get("parent") or {}).get("protocol_id") == parent["protocol_id"]]
    return out


def holdout_material(svc, parent: dict) -> dict:
    from edgelab.research import protocol as rp
    srcs = _source_protocols(svc, parent)
    family = sum(int(p["material"]["trial_budget"]["max_unique_trials"]) for p in srcs) or R.TRIAL_BUDGET
    earlier = sum(len([a for a in svc.store.list_holdout_access(p["protocol_id"]) if a["status"] != "refused"])
                  for p in srcs)
    mat = R.build_material(parent, budget=family, looks=len(LOOKS))
    mat.update({
        "role": rp.MY_HOLDOUT_ROLE, "name": "My strategy holdout (automatic + manual)",
        "search_constraints": {
            "strategies": "one settings combination of 'My strategy' that was counted as a discovery try of the My "
                          "strategy or Strategy autotuner protocol; no discovery tries are counted here",
            "evaluation_windows": "the holdout window only",
            "stages": {"discovery": "none (the tries belong to the source protocols)",
                       "holdout": "one AUTOMATIC look (every signal), then one MANUAL look (the trader's take / skip) "
                                  "of the same settings"}},
        "source_protocols": [{"protocol_id": p["protocol_id"], "role": p["material"].get("role"),
                              "trial_budget": int(p["material"]["trial_budget"]["max_unique_trials"])} for p in srcs],
    })
    mat["holdout_budget"] = {"max_unique_candidate_evaluations": len(LOOKS), "per_candidate": len(LOOKS),
                             "looks": list(LOOKS)}
    mat["trial_budget"]["unit"] += "; the declared budget is the source protocols' budgets together (the family the " \
                                   "tested strategy was chosen from); no trial is counted under this protocol"
    mat["pre_protocol_exposure"] = {
        "statement": "Created when the user reset the My strategy holdout limit to one automatic and one manual look. "
                     f"Holdout looks spent before under the source protocols: {earlier}. Strategies were chosen after "
                     "discovery-period backtests and autotuner runs.",
        "runs": []}
    return mat


def holdout_protocol(svc, lock=None, create: bool = True) -> tuple[dict, dict | None]:
    from edgelab.research import protocol as rp
    guard = lock if lock is not None else R.nullcontext()
    with guard:
        parent = R._parent(svc)
        if parent is None:
            raise R.MyStrategyError("NO_PROTOCOL", "The holdout needs the workspace's active research protocol.")
        rows = [p for p in svc.store.list_protocols(_hp_scope(parent), "ACTIVE")
                if (p["material"].get("parent") or {}).get("protocol_id") == parent["protocol_id"]]
        hp = rows[0] if rows else None
        if hp is None and create:
            rec = rp.make_record(holdout_material(svc, parent), {"code_version": R._code_version()})
            svc.store.save_protocol(rec, _hp_scope(parent))
            hp = svc.store.get_protocol(rec["protocol_id"])
        if hp is not None:
            rp.verify_record(hp)
        return parent, hp


def allowance(svc) -> dict:
    return R._read_json(_allowance_path(svc)) or {}


def _save_allowance(svc, a: dict) -> None:
    R._write_json(_allowance_path(svc), a)


def candidates(svc) -> list[dict]:
    """Strategies that may be tested: My strategy discovery backtests (favourites first) and the autotuner's best results
    that passed the lookahead check. One row per settings combination (a backtest wins over the same autotuner result)."""
    from edgelab.mystrategy import optimizer as O
    rows, seen = [], set()
    bts = [b for b in R.list_backtests(svc) if b.get("kind") == "discovery_backtest"]
    for b in [b for b in bts if b.get("favorite")] + [b for b in bts if not b.get("favorite")]:
        if b["settings_hash"] in seen:
            continue
        seen.add(b["settings_hash"])
        rows.append({"ref": f"bt:{b['id']}", "source": "backtest", "label": b.get("label") or "", "favorite": bool(b.get("favorite")),
                     "created_at": b["created_at"], "settings_hash": b["settings_hash"], "trade_count": b.get("trade_count"),
                     "metrics": b.get("metrics"), "settings_changed": b.get("settings_changed")})
    for f in sorted((O.home(svc) / "runs").glob("OPT_*.json"), reverse=True):
        rec = R._read_json(f) or {}
        for b in rec.get("bests") or []:
            if b.get("status") != "best" or b.get("lookahead") != "passed" or b["settings_hash"] in seen or not b.get("full"):
                continue
            seen.add(b["settings_hash"])
            m = b["full"]["metrics"]
            rows.append({"ref": f"opt:{rec['id']}:{b['n']}", "source": "autotuner",
                         "label": f"Autotuner {str(rec['created_at'])[:16].replace('T', ' ')} best #{b['n']}"
                                  + (" (final)" if rec.get("final") == b["n"] else ""),
                         "favorite": False, "created_at": b.get("found_at"), "settings_hash": b["settings_hash"],
                         "trade_count": m.get("trade_count"), "metrics": m, "settings_changed": b["overrides"]})
    return rows


def _resolve_ref(svc, ref: str) -> tuple[dict, str]:
    """(resolved settings, label) of a candidate reference."""
    if not isinstance(ref, str):
        raise R.MyStrategyError("NO_STRATEGY", "Pick a strategy to test.")
    if ref.startswith("bt:"):
        try:
            sm = R._read_json(R._bt_folder(svc, ref[3:]) / "summary.json") or {}
        except KeyError:
            sm = {}
        if sm.get("kind") != "discovery_backtest":
            raise R.MyStrategyError("NO_STRATEGY", "That backtest does not exist (or is not a discovery backtest).")
        return P.resolve(sm.get("settings_changed") or {}), sm.get("label") or sm["id"]
    m = re.fullmatch(r"opt:(OPT_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}):([0-9]+)", ref)
    if m:
        from edgelab.mystrategy import optimizer as O
        rec = R._read_json(O._run_path(svc, m.group(1))) or {}
        bests = rec.get("bests") or []
        n = int(m.group(2))
        if n < len(bests) and bests[n].get("status") == "best" and bests[n].get("lookahead") == "passed":
            return P.resolve(bests[n]["overrides"]), f"Autotuner {m.group(1)[4:19]} best #{n}"
    raise R.MyStrategyError("NO_STRATEGY", "That autotuner result does not exist or did not pass the lookahead check.")


def _counted_somewhere(svc, parent: dict, h: str) -> bool:
    return any(e["logic_hash"] == h and e["counted"] for p in _source_protocols(svc, parent)
               for e in svc.store.list_trial_events(p["protocol_id"]))


def _spend(svc, hp: dict, kind: str, h: str, lock) -> str:
    """Record one look (granted). Refuses a look of a kind already used or beyond the budget."""
    guard = lock if lock is not None else R.nullcontext()
    with guard:
        used = [a for a in svc.store.list_holdout_access(hp["protocol_id"]) if a["status"] != "refused"]
        if any(a["access_id"].startswith(f"HA_{kind.upper()}_") for a in used):
            raise R.MyStrategyError("HOLDOUT_LOOK_USED", f"The {kind} holdout look is already used.")
        if len(used) >= hp["material"]["holdout_budget"]["max_unique_candidate_evaluations"]:
            raise R.MyStrategyError("HOLDOUT_LOOKS_USED", "Both holdout looks are used.")
        if svc._config_hash() != hp["material"]["config_hash"]:
            raise R.MyStrategyError("PROTOCOL_CONFIG_CHANGED", "The research settings differ from the protocol's.")
        access_id = f"HA_{kind.upper()}_" + R.new_id("X")[2:]
        svc.store.add_holdout_access({"access_id": access_id, "protocol_id": hp["protocol_id"],
                                      "strategy_id": "my_strategy:" + h, "logic_hash": h, "definition_hash": h,
                                      "frozen_hash": h, "search_id": None, "status": "granted", "reason_code": None,
                                      "reason": None, "run_id": None, "result_json": None, "created_at": R._now(),
                                      "completed_at": None})
    return access_id


def start_automatic(svc, ref: str, *, lock=None, progress: Callable | None = None) -> dict:
    """Look 1: the engine trades every signal of the chosen settings on the holdout (lookahead check on), recorded as a
    run (OUT_OF_SAMPLE, labelled Holdout) and saved as a report. Shown at once."""
    from datetime import datetime, timezone
    parent, hp = holdout_protocol(svc, lock)
    a = allowance(svc)
    if a.get("protocol_id") == hp["protocol_id"] and a.get("automatic"):
        raise R.MyStrategyError("HOLDOUT_LOOK_USED", "The automatic holdout look is already used.")
    s, label = _resolve_ref(svc, ref)
    h = P.settings_hash(s)
    if not _counted_somewhere(svc, parent, h):
        raise R.MyStrategyError("NOT_BACKTESTED", "Only settings backtested on the discovery period (My strategy or the "
                                                  "autotuner) can be tested on the holdout.")
    R.es_for(svc, s)                                     # refuse SMT settings without ES data before a look is spent
    access_id = _spend(svc, hp, "automatic", h, lock)
    guard = lock if lock is not None else R.nullcontext()
    try:
        strat, res, ds, win, _ = R.run_window(svc, s, None, None, stage="holdout", lock=lock, progress=progress,
                                              protocol=hp)
        if progress:
            progress("Recording the automatic holdout result")
        rec = R._record(svc, s, strat, res, ds, win, hp, status="OUT_OF_SAMPLE",
                        notes=f"My strategy holdout (automatic) {access_id}", lock=lock, count=False)
        ho_id = R.new_id("HO")
        summary = R.build_report(R.home(svc) / "holdout" / ho_id, s, strat, res, ds, win, rec, kind="holdout_mechanical",
                                 label=f"Holdout - automatic ({label})"[:160],
                                 extra={"protocol_id": hp["protocol_id"], "holdout_look": "automatic",
                                        "source_ref": ref, "source_label": label},
                                 challenge=R.challenge_of(svc, ds, strat, res, win))
        with guard:
            svc.store.update_holdout_access(access_id, status="completed", run_id=rec["run_id"],
                                            completed_at=datetime.now(timezone.utc).isoformat(),
                                            result_json=json.dumps(jsonable({"outcome": "MY_STRATEGY_HOLDOUT",
                                                                             "look": "automatic",
                                                                             "trade_count": summary["trade_count"],
                                                                             "metrics": summary["metrics"]})))
    except Exception as e:
        with guard:
            svc.store.update_holdout_access(access_id, status="failed", reason=str(e)[:500],
                                            completed_at=datetime.now(timezone.utc).isoformat())
        raise
    a = {"protocol_id": hp["protocol_id"], "created_at": a.get("created_at") or R._now(), "settings": s,
         "settings_hash": h, "source_ref": ref, "source_label": label,
         "automatic": {"access_id": access_id, "report": ho_id, "at": R._now(), "run_id": rec["run_id"],
                       "window": summary["window"], "dataset_id": summary["dataset"]["dataset_id"],
                       "es_content_hash": None if strat.es is None else strat.es.content_hash},
         "manual": None}
    _save_allowance(svc, a)
    return a


def start_manual(svc, *, lock=None) -> dict:
    """Look 2: the trader's take / skip on each setup of the SAME settings, after the automatic result."""
    _, hp = holdout_protocol(svc, lock, create=False)
    a = allowance(svc)
    if hp is None or a.get("protocol_id") != hp["protocol_id"] or not a.get("automatic"):
        raise R.MyStrategyError("AUTOMATIC_FIRST", "Run the automatic holdout first; the manual one tests the same settings.")
    if a.get("manual"):
        raise R.MyStrategyError("HOLDOUT_LOOK_USED", "The manual holdout look is already used.")
    access_id = _spend(svc, hp, "manual", a["settings_hash"], lock)
    rv_id = R.new_id("RV")
    au = a["automatic"]
    st = {"id": rv_id, "created_at": R._now(), "status": "in_progress", "settings": a["settings"],
          "settings_hash": a["settings_hash"], "protocol_id": hp["protocol_id"], "access_id": access_id,
          "window": au["window"], "dataset_id": au["dataset_id"], "mechanical_report": au["report"], "decisions": {},
          "es_content_hash": au.get("es_content_hash"), "final_report": None, "look": "manual"}
    _save(svc, st)
    a["manual"] = {"access_id": access_id, "review": rv_id, "at": R._now()}
    _save_allowance(svc, a)
    return st


def allowance_view(svc, lock=None) -> dict:
    """The holdout page: the 2-look allowance, the strategy picker, the automatic report and the manual review."""
    try:
        parent, hp = holdout_protocol(svc, create=False)
        ready = {"ready": True}
    except R.MyStrategyError as e:
        parent, hp, ready = None, None, {"ready": False, "problem": e.message}
    a = allowance(svc)
    if hp is None or a.get("protocol_id") != hp["protocol_id"]:
        a = {}
    out: dict = {**ready, "looks": list(LOOKS), "protocol_id": hp["protocol_id"] if hp else None,
                 "automatic": a.get("automatic"), "manual": a.get("manual"), "source_label": a.get("source_label"),
                 "settings_hash": a.get("settings_hash"),
                 "settings_changed": P.changed(a["settings"]) if a.get("settings") else None,
                 "candidates": candidates(svc) if not a.get("automatic") else []}
    if parent is not None:
        w = R.windows(parent["material"])
        out["holdout"], out["discovery"] = w["holdout"], w["discovery"]
        out["config_ok"] = svc._config_hash() == parent["material"]["config_hash"]
    if a.get("manual"):
        st = R._read_json(_dir(svc) / a["manual"]["review"] / "state.json")
        if st is not None:
            out.update(_review_payload(svc, st, lock))
    return R.jsonable(out)


def _review_payload(svc, st: dict, lock=None) -> dict:
    out = {"review": {k: st.get(k) for k in ("id", "created_at", "status", "settings_hash", "window", "mechanical_report",
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


def open_review(svc) -> dict | None:
    """The review that decisions go to: the allowance's manual review, else the latest one (earlier reviews)."""
    a = allowance(svc)
    if a.get("manual"):
        st = R._read_json(_dir(svc) / a["manual"]["review"] / "state.json")
        if st is not None:
            return st
    return current(svc)
