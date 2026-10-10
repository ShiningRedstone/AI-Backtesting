"""Evaluation / funded phases of a Fair price backtest (ADR-114).

A Fair price backtest runs the ONE engine twice on the same bars: once with the evaluation rules on every day, once with
the funded rules on every day (each with its own lookahead check). The prop challenge chain then decides which phase's
trades an account actually takes:

* an evaluation attempt takes the EVALUATION trades from its start;
* when the evaluation passes, the funded account starts at the end of the pass day (the lifecycle's own activation
  rule) and takes the FUNDED trades from then on (a funded trade starting before the last evaluation trade closed is
  left out: one position at a time);
* a failed evaluation, a lost funded account, its live-transition point or payout limit start a new evaluation
  (``paper.engine.run_attempts``, the unchanged lifecycle).

The pass day is found by running the unchanged lifecycle on the evaluation trades; the combined list gives the same
evaluation result (identical trades up to the end of the pass day). "As traded" = the trades the chain used, in order:
the headline numbers and the autotuner's goals of a Fair price backtest use them (the user's choice), under the
pass-criteria account; the evaluation-only and funded-only results are reported too.
"""
from __future__ import annotations

from collections import Counter
from typing import Mapping

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_obj

CHAIN_VERSION = 2          # 2: trades bigger than the account allows are cut to its limit (the chain no longer stops)
# Dollar columns of a trade record that scale with the contract count (every cost is per contract: commission, fees,
# slippage, financing; the spread is in the fill prices), so a trade cut from n to m contracts keeps its R exactly.
USD_COLS = ("gross_usd", "cost_usd", "cost_usd_base", "net_usd", "risk_usd", "commission_usd", "fees_usd", "slippage_usd",
            "spread_usd", "financing_usd", "planned_risk_usd")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _window(tr: pd.DataFrame, a: pd.Timestamp, until: pd.Timestamp | None) -> pd.DataFrame:
    if not len(tr):
        return tr
    e = pd.to_datetime(tr["entry_ts"], utc=True)
    keep = (e >= a) if until is None else ((e >= a) & (e < until))
    return tr[keep.to_numpy()]


def fit_to_limits(tr: pd.DataFrame, profile: Mapping) -> pd.DataFrame:
    """The user's rule (ADR-114 follow-up): a trade that asks for more micros than the account allows at that moment
    (``max_micros``, or the funded scaling tier of that day) is taken with the allowed number, rounded down; its dollar
    columns scale in proportion (same R); one that cannot keep a single micro is not taken. The UNCHANGED lifecycle finds
    each violation (and the permitted quantity); earlier trades never change, so the loop ends. Column ``cut_from`` =
    the engine's contracts of a cut trade. ``trade_no`` must be unique."""
    from edgelab.prop.lifecycle import INCOMPATIBLE, simulate_lifecycle
    if not len(tr):
        return tr
    tr = tr.copy()
    if "cut_from" not in tr.columns:
        tr["cut_from"] = np.nan
    for _ in range(len(tr) + 1):
        r = simulate_lifecycle(tr, profile)
        br = None
        for stage in ("evaluation", "funded"):
            b = (r.get(stage) or {}).get("breach")
            if b and b.get("reason") in INCOMPATIBLE:
                br = b
                break
        if br is None:
            return tr
        k = int(np.flatnonzero(tr["trade_no"].to_numpy() == int(br["trade_no"]))[0])
        n = float(tr.iloc[k]["contracts"])
        m = float(np.floor(float(br["permitted_quantity"]) + 1e-9))
        if m < 1:
            tr = tr.drop(index=tr.index[k])
            continue
        f = m / n
        idx = tr.index[k]
        for c in USD_COLS:
            if c in tr.columns:
                tr.loc[idx, c] = float(tr.loc[idx, c]) * f
        if pd.isna(tr.loc[idx, "cut_from"]):
            tr.loc[idx, "cut_from"] = n
        tr.loc[idx, "contracts"] = m
    raise RuntimeError("could not fit the trades to the account's contract limits")


def combined_from(ev_all: pd.DataFrame, fu_all: pd.DataFrame, profile: Mapping, t: pd.Timestamp,
                  until: pd.Timestamp | None = None) -> pd.DataFrame:
    """The trades an attempt starting at ``t`` takes: evaluation trades until its evaluation passes, funded trades from
    the funded start, each cut to the account's contract limit when bigger (``fit_to_limits``). Column ``phase`` says
    which; ``phase_trade_no`` = the engine's trade number; ``trade_no`` = 1.. in this attempt's order."""
    from edgelab.prop.lifecycle import simulate_lifecycle
    ev = _window(ev_all, t, until)
    if not len(ev):
        return ev.assign(phase=pd.Series(dtype=object)) if len(ev.columns) else ev
    ev = ev.assign(phase_trade_no=ev["trade_no"].to_numpy())
    ev = fit_to_limits(ev, profile)
    r = simulate_lifecycle(ev, profile)
    p = (r.get("evaluation") or {}).get("pass") if r.get("evaluation", {}).get("status") == "PASS" else None
    ev = ev.assign(phase="eval")
    if not p:
        return ev
    cut = _ts(p["day_end_ts"])
    head = ev[(pd.to_datetime(ev["entry_ts"], utc=True) < cut).to_numpy()]
    last_exit = pd.to_datetime(head["exit_ts"], utc=True).max() if len(head) else cut
    fu = _window(fu_all, max(cut, last_exit), until)
    if len(fu):
        fu = fu[(pd.to_datetime(fu["entry_ts"], utc=True) >= last_exit).to_numpy()]
    if not len(fu):
        return head
    fu = fu.assign(phase="funded", phase_trade_no=fu["trade_no"].to_numpy(), cut_from=np.nan)
    out = pd.concat([head, fu], ignore_index=True).sort_values("entry_ts", kind="mergesort").reset_index(drop=True)
    out["trade_no"] = np.arange(1, len(out) + 1)               # unique numbers for the lifecycle's records
    return fit_to_limits(out, profile)


def phase_chain(ev_all: pd.DataFrame, fu_all: pd.DataFrame, profile: Mapping, start, until=None) -> tuple[dict, pd.DataFrame]:
    """(raw chain in ``mystrategy.challenge`` format, the trades the chain took). Pure."""
    from edgelab.paper.engine import run_attempts
    from edgelab.prop.simulator import PropDataError
    start = _ts(start)
    until = None if until is None else _ts(until)
    frames: dict[str, pd.DataFrame] = {}

    def fetch(t):
        f = combined_from(ev_all, fu_all, profile, _ts(t), until)
        f = f.assign(trade_no=np.arange(1, len(f) + 1)) if len(f) else f
        frames[_ts(t).isoformat()] = f
        return f
    try:
        r = run_attempts(fetch, profile, {}, start)
    except PropDataError as exc:
        return {"v": CHAIN_VERSION, "error": str(exc)}, ev_all.iloc[0:0]
    atts = list(r["attempts"])
    while atts and atts[-1]["n_trades"] == 0:
        atts.pop()
    used, rows = [], []
    for a in atts:
        f = frames.get(_ts(a["start"]).isoformat())
        if f is None or not len(f):
            continue
        e = pd.to_datetime(f["entry_ts"], utc=True)
        keep = e >= _ts(a["start"])
        if a.get("end"):
            keep &= e <= _ts(a["end"])
        u = f[keep.to_numpy()]
        used.append(u.assign(attempt=len(rows) + 1))
        rows.append({"status": a["status"], "passed": bool(a.get("passed_at")), "payouts": int(a.get("payouts") or 0),
                     "trader": round(float(a.get("trader_payout") or 0.0), 2), "trades": int(a["n_trades"]),
                     "start": a["start"], "end": a["end"], "reason": a.get("reason"),
                     "eval_trades": int((u["phase"] == "eval").sum()), "funded_trades": int((u["phase"] == "funded").sum()),
                     "cut_to_limit": int(u["cut_from"].notna().sum()) if "cut_from" in u.columns else 0})
    raw = {"v": CHAIN_VERSION, "attempts": rows, "stopped": r["stopped"], "dropped": 0}
    if used:
        tr = pd.concat(used, ignore_index=True).sort_values("entry_ts", kind="mergesort").reset_index(drop=True)
        tr = tr.rename(columns={"trade_no": "chain_trade_no"})
    else:
        tr = ev_all.iloc[0:0].assign(phase=pd.Series(dtype=object), attempt=pd.Series(dtype="int64"))
    return raw, tr


# =============================================================================================== combined run
class PhaseRun:
    """Both phases of one Fair price run, shaped like a ``BacktestResult`` (``trades`` = as traded under the
    pass-criteria account, renumbered; ``phase_trade_no`` / ``phase`` point back to the engine run) and, through
    ``strategy``, like a ``MyStrategy`` (explanations keyed by signal bar of the as-traded trades)."""

    def __init__(self, strats: dict, results: dict, profile: Mapping | None, start, until=None):
        self.strats, self.results = strats, results
        ev, fu = results["eval"], results["funded"]
        self.profile_id = None if profile is None else profile["profile_id"]
        if profile is not None:
            raw, tr = phase_chain(ev.trades, fu.trades, profile, start, until)
        else:
            raw, tr = None, ev.trades.iloc[0:0].assign(phase=pd.Series(dtype=object))
        self.criteria_chain = raw
        tr = tr.drop(columns=[c for c in ("trade_no", "chain_trade_no") if c in tr.columns])
        if "phase_trade_no" not in tr.columns:
            tr = tr.assign(phase_trade_no=pd.Series(dtype="int64"))
        tr.insert(0, "trade_no", np.arange(1, len(tr) + 1))
        self.trades = tr
        self.dataset = ev.dataset
        self.assumptions = {**ev.assumptions, "phases": {
            "rule": "two engine runs (evaluation rules, funded rules); the prop challenge chain takes evaluation trades "
                    "until an evaluation passes and funded trades from the funded start (fairprice.chain)",
            "as_traded_profile": self.profile_id}}
        self.n_signals = int(ev.n_signals + fu.n_signals)
        sk: Counter = Counter()
        for ph, r in results.items():
            for k, v in (r.skipped or {}).items():
                sk[f"{ph}:{k}"] += v
        self.skipped = dict(sk)
        cz = [r.causality for r in results.values()]
        self.causality = None if any(c is None for c in cz) else _Causality(all(c.passed for c in cz))
        self.strategy_id = "FAIR_PRICE_" + hash_obj({"eval": ev.strategy_id, "funded": fu.strategy_id}, 10)
        self.trades_hash = hash_obj({"eval": ev.trades_hash, "funded": fu.trades_hash, "profile": self.profile_id,
                                     "as_traded": [list(map(str, x)) for x in tr[["phase", "phase_trade_no"]].itertuples(
                                         index=False)] if len(tr) else []}, 16)
        self.strategy = _Combined(strats, tr)


class _Causality:
    def __init__(self, passed: bool):
        self.passed = passed


class _Combined:
    """Strategy-shaped view of both phases for the report writer."""

    def __init__(self, strats: dict, tr: pd.DataFrame):
        ev = strats["eval"]
        self.settings, self.sizing, self.order, self.params = ev.settings, ev.sizing, ev.order, ev.params
        self.es = None
        self.news = ev.news
        self.explanations = {}
        for r in tr.to_dict("records") if len(tr) else []:
            e = strats[r["phase"]].explanations.get(int(r["signal_bar"]))
            if e is not None:
                self.explanations[int(r["signal_bar"])] = e
        st: Counter = Counter()
        for ph, s in strats.items():
            for k, v in s.stats.items():
                st[f"{ph}: {k}"] += v
        self.stats = st
        self.days = {"eval": strats["eval"].days, "funded": strats["funded"].days}
        self.last_signals = None


def phase_numbers(res, start, end, sample) -> dict:
    """Metrics of each engine run on its own (every day traded with that phase's rules)."""
    from edgelab.mystrategy.records import report_numbers
    out = {}
    for ph, r in res.results.items():
        nums = report_numbers(r.trades, start, end, sample)
        out[ph] = {"trade_count": int(len(r.trades)), "metrics": nums["metrics"], "monthly": nums["monthly"],
                   "strategy_id": r.strategy_id, "trades_hash": r.trades_hash, "skipped": r.skipped,
                   "n_signals": r.n_signals}
    return out


def all_chains(root, res, start, until=None, profile_ids=None) -> dict:
    """{profile_id: raw chain} of every registered rule profile for one combined run."""
    from edgelab.prop.service import default_profiles
    out = {}
    ev, fu = res.results["eval"].trades, res.results["funded"].trades
    for p in default_profiles(root):
        pid = p["profile_id"]
        if profile_ids is not None and pid not in profile_ids:
            continue
        unit = p["rules"]["account.quantity_unit"]["value"] if p.get("schema_version") == 3 else p.get("quantity_unit")
        if unit and unit != "MNQ":
            out[pid] = {"v": CHAIN_VERSION, "error": f"the profile limits {unit} contracts; these trades are MNQ"}
            continue
        if pid == res.profile_id and until is None and res.criteria_chain is not None:
            out[pid] = res.criteria_chain
        else:
            out[pid] = phase_chain(ev, fu, p, start, until)[0]
    return out
