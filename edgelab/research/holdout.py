"""Holdout backtests of chosen survivors (ADR-85): the candidate list, its prop-trading ranking, eligibility, the
background job and the history. Read models + orchestration only: every test goes through the unchanged gate
``Services.evaluate_holdout`` (one look per strategy, within the protocol's holdout-look budget).

Ranking (display order only; never a status, never written anywhere): every survivor is ranked on eight criteria
from its DISCOVERY reference run (rank 1 = best, ties share the average rank, a missing value ranks last), and the
default order is the weighted mean of those ranks. Max drawdown and negative months weigh double (the two that most
often end prop accounts). The holdout is never read for the ranking.
"""
from __future__ import annotations

import json
import threading
from typing import Any, Mapping

import numpy as np
import pandas as pd

from edgelab.research import overview as ov

# (key, label, higher_is_better, weight)
CRITERIA = (
    ("trades_per_week", "Trades per week", True, 1.0),
    ("negative_months", "Negative months", False, 2.0),
    ("avg_rr", "Reward to risk", True, 1.0),
    ("profit_factor", "Profit factor", True, 1.0),
    ("max_drawdown_r", "Max drawdown (R)", False, 2.0),
    ("expectancy_r", "Net R per trade", True, 1.0),
    ("net_r", "Total net R", True, 1.0),
    ("max_loss_streak", "Longest losing streak", False, 1.0),
)
RANKING_RULE = ("Ranked on 8 discovery criteria (1 = best): trades per week (more), negative months (fewer), reward to "
                "risk (higher), profit factor (higher), max drawdown (smaller), net R per trade (higher), total net R "
                "(higher), longest losing streak (shorter). Max drawdown and negative months count double; the score is "
                "the weighted average rank (lower is better). Ties share the average rank; a missing value ranks last. "
                "Discovery numbers only: the holdout is never read for this order.")
_MONTHS: dict[tuple, int | None] = {}                  # (store, run id) -> negative months (stored trades never change)


def negative_months(svc, run_id: str | None) -> int | None:
    """Calendar months (New York date of each trade's exit, like Results by year) with negative total net R."""
    if not run_id:
        return None
    key = (str(getattr(getattr(svc, "writer_store", svc.store), "path", "")), run_id)
    if key not in _MONTHS:
        try:
            _, t = svc.store.load_run(run_id)
        except KeyError:
            return None
        if t is None or not len(t):
            _MONTHS[key] = 0
        else:
            local = pd.DatetimeIndex(pd.to_datetime(t["exit_ts"], utc=True)).tz_convert(ov.LOCAL_TZ)
            per = pd.Series(t["net_r"].to_numpy(float)).groupby([local.year, local.month]).sum()
            _MONTHS[key] = int((per < 0).sum())
    return _MONTHS[key]


def rank_rows(rows: list[dict]) -> list[dict]:
    """Add per-criterion ranks, the weighted score and the overall position; return rows best first."""
    if not rows:
        return []
    df = pd.DataFrame([{k: r.get(k) for k, *_ in CRITERIA} for r in rows], dtype=float)
    df["max_drawdown_r"] = df["max_drawdown_r"].abs()        # a drawdown's size, whatever its sign convention
    total_w = sum(w for *_, w in CRITERIA)
    score = np.zeros(len(df))
    for key, _, higher, w in CRITERIA:
        rk = df[key].rank(ascending=not higher, method="average", na_option="bottom").to_numpy()
        for i, r in enumerate(rows):
            r.setdefault("ranks", {})[key] = float(rk[i])
        score += w * rk
    for i, r in enumerate(rows):
        r["score"] = round(float(score[i] / total_w), 6)
    out = sorted(rows, key=lambda r: (r["score"], -(r.get("expectancy_r") or -1e9), r["strategy_id"]))
    for i, r in enumerate(out, 1):
        r["position"] = i
    return out


def protocol_summary(p: Mapping | None, used: int) -> dict | None:
    if p is None:
        return None
    mat = p["material"]
    budget = int(mat["holdout_budget"]["max_unique_candidate_evaluations"])
    h = mat["windows"]["holdout"]
    return {"protocol_id": p["protocol_id"], "name": mat.get("name"), "looks_used": used, "looks_budget": budget,
            "looks_left": max(0, budget - used), "holdout_trading_dates": h["trading_dates"],
            "holdout_first_bar": h["first_bar"], "holdout_last_bar": h["last_bar"]}


def _protocol_of(svc, ref: Mapping) -> dict | None:
    try:
        return svc._governing_protocol(ref.get("instrument"), ref.get("provider"))
    except Exception:                                        # noqa: BLE001 - a store without protocol tables
        return None


def candidates(svc) -> dict:
    """Every current survivor (latest discovery run, Settings pass-criteria account), ranked for prop trading, with
    whether it can take a holdout test now (and why not)."""
    from edgelab.research.results_view import _latest_scoped
    crit = ov.criteria_profile(svc)
    rows, _ = _latest_scoped(svc, "in_sample", None)
    protos: dict[str, dict | None] = {}
    ledgers: dict[str, dict] = {}
    out = []
    for x in rows:
        ref, f = x["ref"], x["facets"]
        if not ref or not ref.get("trade_count") or not ref.get("survivor"):
            continue
        p = _protocol_of(svc, ref)
        pid = None if p is None else p["protocol_id"]
        if pid is not None and pid not in ledgers:
            protos[pid] = p
            ledgers[pid] = _ledger(svc, pid)
        row = {"strategy_id": f["strategy_id"], "display_name": f.get("display_name") or f.get("name"),
               "family_id": f.get("family_id"), "family_name": f.get("family_name"), "timeframe": f.get("timeframe"),
               "session": f.get("session"), "run_id": ref["run_id"], "dataset_id": ref.get("dataset_id"),
               "synthetic": ref.get("synthetic"), "trade_count": ref.get("trade_count"),
               "trades_per_week": ref.get("trades_per_week"), "avg_rr": ref.get("avg_rr"),
               "profit_factor": ref.get("profit_factor"), "max_drawdown_r": ref.get("max_drawdown_r"),
               "expectancy_r": ref.get("expectancy_r"), "net_r": ref.get("net_r"),
               "max_loss_streak": ref.get("max_loss_streak"), "win_rate": ref.get("win_rate"),
               "negative_months": negative_months(svc, ref["run_id"]), "protocol_id": pid}
        row.update(_eligibility(svc, row, f, ledgers.get(pid)))
        out.append(row)
    ranked = rank_rows(out)
    used = {pid: sum(1 for a in led["access"] if a["status"] != "refused") for pid, led in ledgers.items()}
    active = [protocol_summary(protos[pid], used[pid]) for pid in sorted(protos)]
    return {"criteria_profile": crit, "ranking_rule": RANKING_RULE,
            "criteria": [{"key": k, "label": lab, "higher_is_better": hi, "weight": w} for k, lab, hi, w in CRITERIA],
            "protocols": active, "protocol": active[0] if len(active) == 1 else None,
            "rows": ranked, "n_survivors": len(ranked), "n_eligible": sum(1 for r in ranked if r["eligible"])}


def _ledger(svc, pid: str) -> dict:
    from edgelab.research.protocol import holdout_exposed
    return {"access": svc.store.list_holdout_access(pid), "trials": svc.store.list_trial_events(pid),
            "batches": {b["search_id"]: b for b in svc.store.list_search_batches()},
            "exposed": holdout_exposed(svc.store.get_protocol(pid))}


def _logic_hash(svc, sid: str) -> str | None:
    try:
        return svc.library.load(sid).get("logic_hash")
    except (KeyError, FileNotFoundError):
        return None


def _eligibility(svc, row: Mapping, facets: Mapping, led: Mapping | None) -> dict:
    if led is None:
        return {"eligible": False, "reason": "no active research protocol governs this data", "search_id": None,
                "tested": None}
    lh = facets.get("logic_hash") or _logic_hash(svc, row["strategy_id"])
    done = [a for a in led["access"] if a["logic_hash"] == lh and a["status"] != "refused"]
    if done:
        a = done[-1]
        res = json.loads(a["result_json"]) if a.get("result_json") else {}
        return {"eligible": False, "reason": "already holdout-tested (one test per strategy)", "search_id": a["search_id"],
                "tested": {"access_id": a["access_id"], "status": a["status"], "run_id": a.get("run_id"),
                           "outcome": res.get("outcome"), "created_at": a.get("created_at")}}
    if row["strategy_id"] in led.get("exposed", ()):
        return {"eligible": False, "search_id": None, "tested": None,
                "reason": "already holdout-tested under an earlier protocol (one test per strategy)"}
    searches = [e["search_id"] for e in led["trials"] if e["counted"] and e["logic_hash"] == lh and e.get("search_id")
                and (led["batches"].get(e["search_id"]) or {}).get("protocol_id") == row["protocol_id"]]
    if not searches:
        return {"eligible": False, "search_id": None, "tested": None,
                "reason": "only tested outside a research run (the holdout gate needs the research run that tested it)"}
    return {"eligible": True, "reason": None, "search_id": searches[-1], "tested": None}


def history(svc) -> list[dict]:
    """Every holdout attempt of every protocol (granted, completed, failed or refused), newest first."""
    names = {}
    try:
        names = {f["strategy_id"]: f.get("display_name") or f.get("name") for f in ov.library_facets(svc)}
    except Exception:                                        # noqa: BLE001
        pass
    out = []
    try:
        protos = svc.store.list_protocols()
    except Exception:                                        # noqa: BLE001
        return out
    for p in protos:
        for a in svc.store.list_holdout_access(p["protocol_id"]):
            res = json.loads(a["result_json"]) if a.get("result_json") else {}
            out.append({"access_id": a["access_id"], "protocol_id": p["protocol_id"], "strategy_id": a["strategy_id"],
                        "display_name": names.get(a["strategy_id"]), "status": a["status"],
                        "reason_code": a.get("reason_code"), "reason": a.get("reason"), "run_id": a.get("run_id"),
                        "outcome": res.get("outcome"), "trade_count": res.get("trade_count"),
                        "created_at": a.get("created_at"), "completed_at": a.get("completed_at")})
    return sorted(out, key=lambda r: str(r["created_at"]), reverse=True)


# ------------------------------------------------------------------------------------------- the job
def plan(svc, strategy_ids: Any) -> dict:
    """Check a selection before anything runs: survivors only, eligible, one protocol, within the looks left."""
    if not isinstance(strategy_ids, list) or not strategy_ids or not all(isinstance(x, str) and x for x in strategy_ids):
        raise ValueError("choose at least one survivor")
    if len(set(strategy_ids)) != len(strategy_ids):
        raise ValueError("a strategy is listed twice")
    c = candidates(svc)
    by = {r["strategy_id"]: r for r in c["rows"]}
    missing = [s for s in strategy_ids if s not in by]
    if missing:
        raise ValueError(f"{len(missing)} selected strateg{'y is' if len(missing) == 1 else 'ies are'} not a current "
                         "survivor; only survivors can take a holdout backtest")
    bad = [by[s] for s in strategy_ids if not by[s]["eligible"]]
    if bad:
        raise ValueError(f"{bad[0]['display_name']}: {bad[0]['reason']}")
    pids = {by[s]["protocol_id"] for s in strategy_ids}
    if len(pids) != 1:
        raise ValueError("the selection spans several research protocols; test one protocol's survivors at a time")
    proto = next(p for p in c["protocols"] if p["protocol_id"] in pids)
    if len(strategy_ids) > proto["looks_left"]:
        raise ValueError(f"{len(strategy_ids)} selected but only {proto['looks_left']} holdout test"
                         f"{'' if proto['looks_left'] == 1 else 's'} left in this protocol")
    return {"protocol": proto, "items": [{"strategy_id": s, "search_id": by[s]["search_id"],
                                          "display_name": by[s]["display_name"]} for s in strategy_ids]}


def shortlist(svc, search_id: str, strategy_id: str) -> None:
    """Add one strategy to its research run's shortlist tag (kept: the strategies already on it)."""
    b = svc.store.get_search_batch(search_id)
    tag = json.loads(b["shortlist_json"]) if b and b.get("shortlist_json") else {}
    ids = list(tag.get("strategy_ids") or [])
    if strategy_id not in ids:
        svc.select_shortlist(search_id, ids + [strategy_id])


def run_items(svc, job, lock) -> None:
    """The background work: one strategy at a time through the gate; cancel is honoured BETWEEN strategies (a test
    whose look is granted always runs to the end)."""
    for it in job.items:
        if job.cancel_requested.is_set():
            it["state"] = "cancelled"
            continue
        it["state"] = "running"
        job.live = {"current": it["display_name"], "done": sum(1 for x in job.items if x["state"] in ("completed", "failed",
                                                                                                      "refused"))}
        try:
            with lock:                                       # store writes and the ledger; pages read lock-free
                shortlist(svc, it["search_id"], it["strategy_id"])
                out = svc.evaluate_holdout(job.protocol_id, it["search_id"], it["strategy_id"])
            it.update(state="completed", run_id=out.get("run_id"), outcome=out.get("outcome"),
                      access_id=out.get("access_id"))
        except Exception as exc:                             # noqa: BLE001 - reported per strategy, the job continues
            code = getattr(exc, "code", None)
            it.update(state="refused" if code else "failed", error=f"{type(exc).__name__}: {exc}", code=code)
    job.live = {"current": None, "done": sum(1 for x in job.items if x["state"] in ("completed", "failed", "refused"))}
