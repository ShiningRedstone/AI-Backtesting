"""Strategy combinations (ADR-92): up to five surviving strategies traded in ONE prop account, one position at a time.

A combination is built from the members' STORED discovery trades; nothing is re-run and nothing is resized:

* Merge rule (``MERGE_RULE``): all member trades in order of entry (then signal time, then member = sorted strategy id,
  then trade number); a trade is taken only when no taken trade is still open (its entry is at or after the last taken
  exit, the same no-overlap rule the prop simulator enforces). Overlapping trades of other members are SKIPPED and
  counted. A skipped member might in reality have re-entered earlier (its own later signals assumed it traded): the
  merged stream is an approximation and says so.
* Money: every trade keeps its recorded size and net P&L (each member's own sizing).
* Prop: the merged trades go through the UNCHANGED prop lifecycle (``prop/lifecycle.py``) under the Settings pass-criteria
  account: once from the first day (the survivor rule: evaluation passed with a payout, net R per trade > 0) and once from
  every month start of the window ("rolling starts": % of starts that pass, median trading days to pass and to the
  first payout).
* Score: weighted average rank like the Holdout ranking (lower is better); trades per week only counts up to a cap.
* Search: greedy forward selection from EVERY survivor: add the survivor that improves the score most while the result
  stays a survivor, stop when nothing improves or at five members. Deterministic; a memo on the member set.

Read-only: a search or an evaluation writes no runs, trials or ledger rows to the research store. What it writes lives
under ``<data>/combinations/`` (search results, saved combinations, and the evaluated-combination ledger the honest
holdout family is counted from).
"""
from __future__ import annotations

import json
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_obj

MERGE_RULE = "one_position_first_entry_v1"
MAX_MEMBERS = 5
PROP_TOP_K = 10                       # candidates per step that reach the (slower) rolling-start prop stage
DEFAULT_TPW_CAP = 5.0
LOCAL_TZ = "America/New_York"

# (key, label, higher_is_better, weight)
CHEAP_CRITERIA = (
    ("negative_months", "Losing months", False, 2.0),
    ("max_drawdown_usd", "Max drawdown ($)", False, 2.0),
    ("expectancy_r", "Net R per trade", True, 1.0),
    ("tpw_scored", "Trades per week (up to the limit)", True, 1.0),
)
SPEED_CRITERIA = (
    ("pass_pct", "Evaluations passed (rolling starts)", True, 1.0),
    ("median_days_to_pass", "Trading days to pass", False, 1.0),
    ("median_days_to_payout", "Trading days to first payout", False, 1.0),
)
CRITERIA = CHEAP_CRITERIA + SPEED_CRITERIA
RANKING_RULE = ("Ranked on 7 criteria (1 = best): losing months (fewer), max drawdown in $ (smaller), net R per trade "
                "(higher), trades per week up to the limit (more), evaluations passed from rolling monthly starts "
                "(more), trading days to pass (fewer), trading days to the first payout (fewer). Losing months and "
                "drawdown count double; the score is the weighted average rank (lower is better). Discovery trades "
                "only: the holdout is never read.")
MERGE_NOTE = ("One prop account, one position at a time: when trades overlap, the one that started first is taken and "
              "the other member's trade is skipped. A skipped member might in reality have re-entered earlier, so the "
              "combined result is an approximation built from the recorded trades (nothing was re-run).")


class ComboError(ValueError):
    def __init__(self, code: str, message: str, **details):
        super().__init__(message)
        self.code, self.details = code, details


# ------------------------------------------------------------------------------------------------ members
@dataclass
class Member:
    strategy_id: str
    run_id: str
    display_name: str | None
    logic_hash: str | None
    timeframe: str | None
    dataset_id: str | None
    protocol_id: str | None
    start: pd.Timestamp | None
    end: pd.Timestamp | None
    contract: str | None
    trades: pd.DataFrame = field(repr=False)
    entry: np.ndarray = field(repr=False)
    exit: np.ndarray = field(repr=False)
    signal: np.ndarray = field(repr=False)
    trade_no: np.ndarray = field(repr=False)
    net_r: np.ndarray = field(repr=False)
    net_usd: np.ndarray = field(repr=False)
    month: np.ndarray = field(repr=False)          # New York (year * 12 + month - 1) of each exit
    info: dict = field(default_factory=dict, repr=False)


def _ns(s) -> np.ndarray:
    """UTC nanoseconds (whatever unit the column is stored in)."""
    return pd.DatetimeIndex(pd.to_datetime(s, utc=True)).as_unit("ns").asi8.astype(np.int64)


def member_from(row: Mapping, rec: Mapping, trades: pd.DataFrame) -> Member:
    """A member from a survivor row (strategy_id, run_id, …), its run record and its stored trades."""
    if trades is None or not len(trades):                   # no trades (an empty frame may lack columns)
        trades = pd.DataFrame({c: pd.Series(dtype="datetime64[ns, UTC]") for c in ("signal_ts", "entry_ts", "exit_ts")}
                              | {c: pd.Series(dtype=float) for c in ("net_r", "net_usd")}
                              | {"trade_no": pd.Series(dtype=np.int64)})
    t = trades.sort_values(["entry_ts", "trade_no"], kind="mergesort").reset_index(drop=True) if len(trades) else trades
    ds = rec.get("dataset") or {}
    sig = t["signal_ts"] if "signal_ts" in t.columns else t["entry_ts"]
    local = pd.DatetimeIndex(pd.to_datetime(t["exit_ts"], utc=True)).tz_convert(LOCAL_TZ)
    contract = ((rec.get("assumptions") or {}).get("execution_contract") or {}).get("contract")
    return Member(strategy_id=row["strategy_id"], run_id=row["run_id"], display_name=row.get("display_name"),
                  logic_hash=row.get("logic_hash"), timeframe=row.get("timeframe"), dataset_id=row.get("dataset_id"),
                  protocol_id=row.get("protocol_id"),
                  start=pd.Timestamp(ds["start"]) if ds.get("start") else None,
                  end=pd.Timestamp(ds["end"]) if ds.get("end") else None, contract=contract, trades=t,
                  entry=_ns(t["entry_ts"]), exit=_ns(t["exit_ts"]), signal=_ns(sig),
                  trade_no=t["trade_no"].to_numpy(np.int64), net_r=t["net_r"].to_numpy(float),
                  net_usd=t["net_usd"].to_numpy(float),
                  month=(local.year.to_numpy() * 12 + local.month.to_numpy() - 1).astype(np.int64),
                  info=dict(row))


def window_of(members: Sequence[Member]) -> tuple[pd.Timestamp | None, pd.Timestamp | None]:
    """The tested window of a combination: from the earliest member start to the latest member end."""
    starts = [m.start for m in members if m.start is not None]
    ends = [m.end for m in members if m.end is not None]
    return (min(starts) if starts else None, max(ends) if ends else None)


# ------------------------------------------------------------------------------------------------ merge
def _merge(members: Sequence[Member]) -> tuple[dict, np.ndarray]:
    """The members' trades concatenated (``cat``) and the positions in ``cat`` of every TAKEN trade, in time order."""
    cat = {k: np.concatenate([getattr(m, k) for m in members]) if members else np.zeros(0)
           for k in ("entry", "exit", "signal", "trade_no", "net_r", "net_usd", "month")}
    cat["mid"] = (np.concatenate([np.full(len(m.entry), i, np.int64) for i, m in enumerate(members)])
                  if members else np.zeros(0, np.int64))
    cat["row"] = (np.concatenate([np.arange(len(m.entry), dtype=np.int64) for m in members])
                  if members else np.zeros(0, np.int64))
    if not len(cat["entry"]):
        return cat, np.zeros(0, np.int64)
    order = np.lexsort((cat["trade_no"], cat["mid"], cat["signal"], cat["entry"]))
    ent_s, ext_s = cat["entry"][order], cat["exit"][order]
    taken = []
    i, n = 0, len(order)
    while i < n:
        taken.append(i)
        # the next trade that starts at or after this exit (and after this one in the order)
        i = max(i + 1, int(np.searchsorted(ent_s, ext_s[i], side="left")))
    return cat, order[np.asarray(taken, np.int64)]


def merge_select(members: Sequence[Member]) -> tuple[np.ndarray, np.ndarray]:
    """(member index, row index) of every TAKEN trade, in time order. Members are used in the given order as the last
    tie-break; callers pass them sorted by strategy id so a combination has one answer whatever order it was built in."""
    cat, sel = _merge(members)
    return cat["mid"][sel], cat["row"][sel]


def merged_frame(members: Sequence[Member], mids: np.ndarray, rows: np.ndarray) -> pd.DataFrame:
    """The taken trades as one DataFrame in time order: every recorded column, plus ``member`` (strategy id) and
    ``member_trade_no`` (the member's own trade number); ``trade_no`` is renumbered 1..n."""
    if not len(mids):
        cols = list(members[0].trades.columns) if members else []
        return pd.DataFrame(columns=cols + ["member", "member_trade_no"])
    parts = []
    for i, m in enumerate(members):
        pick = np.sort(rows[mids == i])
        if len(pick):
            p = m.trades.iloc[pick].copy()
            p["member"] = m.strategy_id
            parts.append(p)
    out = pd.concat(parts, ignore_index=True)
    out = out.sort_values(["entry_ts", "exit_ts"], kind="mergesort").reset_index(drop=True)
    out["member_trade_no"] = out["trade_no"]
    out["trade_no"] = np.arange(1, len(out) + 1)
    return out


def skipped_by_member(members: Sequence[Member], mids: np.ndarray) -> dict[str, int]:
    kept = np.bincount(mids, minlength=len(members)) if len(mids) else np.zeros(len(members), np.int64)
    return {m.strategy_id: int(len(m.entry) - kept[i]) for i, m in enumerate(members)}


# ------------------------------------------------------------------------------------------------ stats
def negative_months_of(exit_ts: Any, net_r: Any) -> int:
    """New York calendar months (of each trade's EXIT, like Results by year) whose total net R is negative."""
    if not len(net_r):
        return 0
    local = pd.DatetimeIndex(pd.to_datetime(exit_ts, utc=True)).tz_convert(LOCAL_TZ)
    per = pd.Series(np.asarray(net_r, float)).groupby([local.year, local.month]).sum()
    return int((per < 0).sum())


def _max_dd(x: np.ndarray) -> float:
    if not len(x):
        return 0.0
    eq = np.concatenate([[0.0], np.cumsum(x)])
    return float((np.maximum.accumulate(eq) - eq).max())


def _tpw(n: int, window) -> float | None:
    from edgelab.analytics.metrics import trades_per_week
    if window[0] is None or window[1] is None:
        return None
    return trades_per_week(n, window[0], window[1])


def cheap_stats(members: Sequence[Member], window, cap: float) -> dict:
    """The fast criteria from the merged arrays (no DataFrame): trades, net R, net R per trade, net $ and its max
    drawdown on the closed-trade path, losing months, trades per week (and its value capped for the score)."""
    cat, sel = _merge(members)
    n = int(len(sel))
    r, usd, mon = cat["net_r"][sel].astype(float), cat["net_usd"][sel].astype(float), cat["month"][sel].astype(np.int64)
    neg = 0
    if n:
        u, inv = np.unique(mon, return_inverse=True)       # per-month sums, accumulated in time order
        per = np.zeros(len(u))
        np.add.at(per, inv, r)
        neg = int((per < 0).sum())
    tpw = _tpw(n, window)
    return {"trades": n, "net_r": float(r.sum()) if n else 0.0,
            "expectancy_r": float(r.mean()) if n else None,
            "net_usd": float(usd.sum()) if n else 0.0, "max_drawdown_usd": _max_dd(usd),
            "max_drawdown_r": _max_dd(r), "negative_months": neg, "trades_per_week": tpw,
            "tpw_scored": None if tpw is None else min(tpw, float(cap))}


# ------------------------------------------------------------------------------------------------ prop
def profile_unit(profile: Mapping) -> str | None:
    if profile.get("schema_version") == 3:
        return profile["rules"]["account.quantity_unit"]["value"]
    return profile.get("quantity_unit")


class PropRunner:
    """The unchanged prop lifecycle on one merged trade stream, prepared ONCE and replayed from several start days.
    ``prepare`` labels each trade on its own (trading day, MAE in $), so the trades from a start day on are exactly what
    ``simulate_lifecycle`` would prepare from that subset (checked by tests).

    Only the evaluation outcome and the FIRST payout are read, so a replay runs on a growing time window (the
    lifecycle is chronological: nothing on a later day changes an earlier decision). A decision is accepted only when
    it fell on a day before the window's last (possibly cut) trading day, or when the window already holds every
    trade; otherwise the window doubles. The answer is identical to a replay over all the data (checked by tests)."""

    FIRST_WINDOW_DAYS = 120

    def __init__(self, frame: pd.DataFrame, profile: Mapping):
        from edgelab.prop.lifecycle import prepare
        from edgelab.prop.profiles import resolve, rule_basis
        self.profile = profile
        self.R, self.rb = resolve(profile), rule_basis(profile)
        self.t = prepare(frame, profile)
        self._entry, self._exit = _ns(self.t["entry_ts"]), _ns(self.t["exit_ts"])

    def run_from(self, start: pd.Timestamp | None = None) -> dict:
        """The full lifecycle result over every trade entered at or after ``start``."""
        from edgelab.prop.lifecycle import _Run
        t = self.t if start is None else self.t[self.t["entry_ts"] >= start].reset_index(drop=True)
        return _Run(self.R, self.rb, t).run()

    def first_payout(self, start: pd.Timestamp | None = None) -> dict:
        """Evaluation status, trading days to pass and trading days to the first payout, from ``start``."""
        from edgelab.prop.lifecycle import _Run
        keep = np.ones(len(self.t), bool) if start is None else self._entry >= pd.Timestamp(start).value
        base_exit = self._exit[keep]
        if not len(base_exit):
            return _outcome(_Run(self.R, self.rb, self.t.iloc[0:0].reset_index(drop=True)).run())
        idx = np.flatnonzero(keep)
        t0 = int(self._entry[idx].min())
        days = self.FIRST_WINDOW_DAYS
        while True:
            cut = t0 + days * 86_400_000_000_000
            part = idx[base_exit < cut]
            whole = len(part) == len(idx)
            sub = self.t.iloc[part].reset_index(drop=True)
            res = _Run(self.R, self.rb, sub).run()
            if whole or (len(sub) and _settled(res, sub)):
                return _outcome(res)
            days *= 2


def _settled(res: Mapping, sub: pd.DataFrame) -> bool:
    """Whether the evaluation outcome and the first payout (or the funded breach) of a replay on a CUT window are final:
    each decision fell on a day before the window's last trading day (days before it are complete)."""
    ev = res["evaluation"]
    last_eval = str(sub["evaluation_day"].max())
    if ev["status"] != "PASS":
        br = ev.get("breach")
        return bool(br) and str(br["date"]) < last_eval
    if str(ev["pass"]["date"]) >= last_eval:
        return False
    last_funded = str(sub["funded_day"].max())
    if res["payouts"]:
        return str(res["payouts"][0]["date"]) < last_funded
    br = (res.get("funded") or {}).get("breach")
    return bool(br) and str(br["date"]) < last_funded


def _outcome(res: Mapping) -> dict:
    ev = res["evaluation"]
    return {"evaluation": ev["status"], "failure_reason": ev.get("failure_reason"),
            "pass_days": (ev.get("pass") or {}).get("trading_days"), "payout": bool(res["payouts"]),
            "days_to_payout": _days_to_first_payout(res)}


def _days_to_first_payout(res: Mapping) -> int | None:
    if not res.get("payouts"):
        return None
    ev = res["evaluation"]
    pay_date = res["payouts"][0]["date"]
    funded_days = sum(1 for d in (res.get("funded") or {}).get("days") or ()
                      if d["n_trades"] > 0 and str(d["date"]) <= str(pay_date))
    return int((ev.get("pass") or {}).get("trading_days") or 0) + funded_days


def month_starts(window) -> list[pd.Timestamp]:
    """The window start and every New York month start after it (at least a week later), before the window end."""
    start, end = window
    if start is None or end is None:
        return []
    s = pd.Timestamp(start)
    s = s.tz_localize("UTC") if s.tzinfo is None else s.tz_convert("UTC")
    e = pd.Timestamp(end)
    e = e.tz_localize("UTC") if e.tzinfo is None else e.tz_convert("UTC")
    local = s.tz_convert(LOCAL_TZ)
    first = pd.Timestamp(year=local.year, month=local.month, day=1, tz=LOCAL_TZ)
    out = [s]
    m = first + pd.DateOffset(months=1)
    while m.tz_convert("UTC") < e:
        if m.tz_convert("UTC") >= s + pd.Timedelta(days=7):           # no near-duplicate of the window start
            out.append(m.tz_convert("UTC"))
        m = m + pd.DateOffset(months=1)
    return out


def survivor_check(runner: PropRunner | None, expectancy_r: float | None) -> dict:
    """The survivor rule on the merged trades: net R per trade > 0 AND the evaluation passes with at least one payout."""
    if runner is None:
        return {"survivor": False, "evaluation": "NOT_APPLICABLE", "payout": False, "pass_days": None,
                "days_to_payout": None, "failure_reason": None}
    o = runner.first_payout(None)
    return {**o, "survivor": bool(o["evaluation"] == "PASS" and o["payout"] and (expectancy_r or 0) > 0)}


def rolling_starts(runner: PropRunner | None, window) -> dict:
    """One evaluation from every month start: % that pass (of the starts decided before the data ends), median trading
    days to pass, median trading days to the first payout (of the starts that got one)."""
    starts = month_starts(window)
    out = {"starts": len(starts), "passed": 0, "failed": 0, "undecided": 0, "paid": 0, "pass_pct": None,
           "median_days_to_pass": None, "median_days_to_payout": None}
    if runner is None or not starts:
        return out
    pass_days, pay_days = [], []
    for s in starts:
        o = runner.first_payout(s)
        if o["evaluation"] == "PASS":
            out["passed"] += 1
            pass_days.append(int(o["pass_days"]))
            if o["days_to_payout"] is not None:
                out["paid"] += 1
                pay_days.append(o["days_to_payout"])
        elif o["failure_reason"] == "NOT_PASSED_BY_END_OF_DATA":
            out["undecided"] += 1
        else:
            out["failed"] += 1
    decided = out["passed"] + out["failed"]
    out["pass_pct"] = round(100.0 * out["passed"] / decided, 6) if decided else None
    out["median_days_to_pass"] = float(np.median(pass_days)) if pass_days else None
    out["median_days_to_payout"] = float(np.median(pay_days)) if pay_days else None
    return out


# ------------------------------------------------------------------------------------------------ ranking
def rank(rows: list[dict], criteria=CRITERIA) -> list[dict]:
    """Weighted average rank (lower is better; ties share the average rank; a missing value ranks last); best first.
    Ties on the score break on net R per trade, then the members' ids."""
    if not rows:
        return []
    df = pd.DataFrame([{k: r.get(k) for k, *_ in criteria} for r in rows], dtype=float)
    total_w = sum(w for *_, w in criteria)
    score = np.zeros(len(df))
    ranks = {}
    for key, _, higher, w in criteria:
        rk = df[key].rank(ascending=not higher, method="average", na_option="bottom").to_numpy()
        ranks[key] = rk
        score += w * rk
    out = []
    for i, r in enumerate(rows):
        out.append({**r, "ranks": {k: float(ranks[k][i]) for k in ranks}, "score": round(float(score[i] / total_w), 6)})
    out.sort(key=lambda r: (r["score"], -(r.get("expectancy_r") if r.get("expectancy_r") is not None else -1e9),
                            "|".join(r["members"])))
    for i, r in enumerate(out, 1):
        r["position"] = i
    return out


# ------------------------------------------------------------------------------------------------ evaluator
def combo_id(logic_hashes: Iterable[str]) -> str:
    return "CMB_" + hash_obj({"members": sorted(logic_hashes), "merge_rule": MERGE_RULE}, 16).upper()


class Evaluator:
    """Scores member sets of one pool of survivors, memoised on the (sorted) member set."""

    def __init__(self, members: Sequence[Member], profile: Mapping | None, cap: float = DEFAULT_TPW_CAP):
        self.members = {m.strategy_id: m for m in members}
        self.profile, self.cap = profile, float(cap)
        unit = profile_unit(profile) if profile else None
        self.prop_ok = bool(profile and profile.get("schema_version") == 3)
        self.unit = unit
        self._cheap: dict[tuple, dict] = {}
        self._full: dict[tuple, dict] = {}
        self._runner: dict[tuple, Any] = {}

    def key(self, ids: Iterable[str]) -> tuple:
        return tuple(sorted(set(ids)))

    def _ms(self, key: tuple) -> list[Member]:
        return [self.members[s] for s in key]

    def evaluated(self) -> list[tuple]:
        return sorted(self._cheap)

    def cheap(self, key: tuple) -> dict:
        hit = self._cheap.get(key)
        if hit is None:
            ms = self._ms(key)
            hit = {"members": list(key), **cheap_stats(ms, window_of(ms), self.cap)}
            self._cheap[key] = hit
        return hit

    def runner(self, key: tuple) -> PropRunner | None:
        if key not in self._runner:
            ms = self._ms(key)
            if not self.prop_ok or (self.unit and any(m.contract != self.unit for m in ms)):
                self._runner[key] = None
            else:
                mids, rows = merge_select(ms)
                self._runner[key] = PropRunner(merged_frame(ms, mids, rows), self.profile)
        return self._runner[key]

    def survivor(self, key: tuple) -> dict:
        c = self.cheap(key)
        if "survivor" not in c:
            c.update({k: v for k, v in survivor_check(self.runner(key), c["expectancy_r"]).items()
                      if k in ("survivor", "evaluation", "payout", "pass_days", "days_to_payout")})
        return c

    def full(self, key: tuple) -> dict:
        hit = self._full.get(key)
        if hit is None:
            c = self.survivor(key)
            hit = {**c, **rolling_starts(self.runner(key), window_of(self._ms(key)))}
            self._full[key] = hit
            self._runner.pop(key, None)          # the prepared trades are not needed any more
        return hit


def greedy_from(ev: Evaluator, seed: str, pool: Sequence[str], max_members: int = MAX_MEMBERS,
                top_k: int = PROP_TOP_K) -> tuple[tuple, list[str]]:
    """Forward selection from one survivor: (final member set, the order members were added)."""
    cur, path = ev.key([seed]), [seed]
    while len(cur) < max_members:
        cands = [ev.key(cur + (o,)) for o in pool if o not in cur]
        if not cands:
            break
        cheap_ranked = rank([ev.cheap(c) for c in cands], CHEAP_CRITERIA)
        picked = []
        for r in cheap_ranked:
            k = tuple(r["members"])
            if ev.survivor(k)["survivor"]:
                picked.append(k)
                if len(picked) >= top_k:
                    break
        if not picked:
            break
        best = rank([ev.full(cur)] + [ev.full(k) for k in picked], CRITERIA)[0]
        nxt = tuple(best["members"])
        if nxt == cur:
            break
        path.append(next(s for s in nxt if s not in cur))
        cur = nxt
    return cur, path


def search(members: Sequence[Member], profile: Mapping | None, cap: float = DEFAULT_TPW_CAP,
           progress: Callable[[int, int], None] | None = None, cancel: threading.Event | None = None,
           max_members: int = MAX_MEMBERS, top_k: int = PROP_TOP_K) -> dict:
    """Greedy forward selection from every survivor; the distinct results (two or more members) ranked best first."""
    ev = Evaluator(members, profile, cap)
    pool = sorted(ev.members)
    finals: dict[tuple, list[str]] = {}
    alone = 0
    for i, seed in enumerate(pool):
        if cancel is not None and cancel.is_set():
            break
        if not ev.survivor(ev.key([seed]))["survivor"]:
            continue
        final, path = greedy_from(ev, seed, pool, max_members, top_k)
        if len(final) < 2:
            alone += 1
        else:
            finals.setdefault(final, path)
        if progress:
            progress(i + 1, len(pool))
    rows = []
    for key, path in finals.items():
        rows.append({**ev.full(key), "path": path})
    ranked = rank(rows, CRITERIA)
    multi = [list(k) for k in ev.evaluated() if len(k) >= 2]          # single strategies are not combinations
    return {"rows": ranked, "evaluated": multi, "n_evaluated": len(multi),
            "n_seeds": len(pool), "n_alone": alone, "cancelled": bool(cancel is not None and cancel.is_set())}


# ------------------------------------------------------------------------------------------------ detail
def detail(members: Sequence[Member], profile: Mapping | None, cap: float, risk_usd: float) -> dict:
    """Everything the combination panel shows, from the merged trades (display only)."""
    from edgelab.analytics.metrics import compute_metrics
    from edgelab.research.lab import MAX_CURVE_POINTS
    from edgelab.research.results_view import calendar_years
    ms = sorted(members, key=lambda m: m.strategy_id)
    ev = Evaluator(ms, profile, cap)
    key = ev.key(m.strategy_id for m in ms)
    full = ev.full(key)
    mids, rows = merge_select(ms)
    frame = merged_frame(ms, mids, rows)
    win = window_of(ms)
    span = (win[0], win[1]) if win[0] is not None and win[1] is not None else None
    met = compute_metrics(frame, span=span) if len(frame) else {"trade_count": 0}
    skipped = skipped_by_member(ms, mids)
    member_rows = []
    for m in ms:
        own = Evaluator([m], profile, cap).full((m.strategy_id,))
        kept = frame[frame["member"] == m.strategy_id] if len(frame) else frame
        member_rows.append({"strategy_id": m.strategy_id, "display_name": m.display_name, "run_id": m.run_id,
                            "timeframe": m.timeframe, "trades": int(len(m.entry)), "kept": int(len(kept)),
                            "skipped": skipped[m.strategy_id],
                            "net_r_kept": float(kept["net_r"].sum()) if len(kept) else 0.0,
                            "own": {k: own.get(k) for k, *_ in CRITERIA} | {"trades": own.get("trades"),
                                                                             "net_r": own.get("net_r"),
                                                                             "net_usd": own.get("net_usd")}})
    best_single = rank([{**r["own"], "members": [r["strategy_id"]]} for r in member_rows], CRITERIA)[0]["members"][0]
    curve = []
    if len(frame):
        eq = np.cumsum(frame["net_r"].to_numpy(float))
        idx = np.arange(len(eq))
        if len(eq) > MAX_CURVE_POINTS:
            idx = np.unique(np.linspace(0, len(eq) - 1, MAX_CURVE_POINTS).round().astype(int))
        ts = pd.to_datetime(frame["exit_ts"], utc=True)
        curve = [{"t": ts.iloc[i].isoformat(), "v": round(float(eq[i]), 6), "n": int(i + 1)} for i in idx]
    pick = ("expectancy_r", "trades_per_week", "win_rate", "profit_factor", "max_drawdown_r", "max_drawdown_usd",
            "net_r", "net_usd", "max_loss_streak", "avg_hold_minutes", "sample_label", "trade_count")
    kpis = {k: _clean(met.get(k)) for k in pick}
    kpis["net_usd_at_risk"] = None if met.get("net_r") is None else float(met["net_r"]) * risk_usd
    return {"members": member_rows, "member_ids": list(key), "stats": full, "kpis": kpis, "best_single": best_single,
            "curve": curve, "years": calendar_years(frame, risk_usd) if len(frame) else [],
            "window": {"start": _iso(win[0]), "end": _iso(win[1])}, "skipped_total": int(sum(skipped.values())),
            "merge_rule": MERGE_RULE, "merge_note": MERGE_NOTE, "frame": frame}


def _iso(x) -> str | None:
    return None if x is None else pd.Timestamp(x).isoformat()


def _clean(v):
    if isinstance(v, (float, np.floating)):
        return None if not math.isfinite(float(v)) else float(v)
    if isinstance(v, np.integer):
        return int(v)
    return v


# ------------------------------------------------------------------------------------------------ storage
def combos_dir(data_root: Path | str) -> Path:
    return Path(data_root) / "combinations"


def _read_json(p: Path, default):
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_json(p: Path, obj) -> None:
    from edgelab.core.fsutil import atomic_write_text
    p.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(p, json.dumps(obj, indent=1, default=str))


def ledger_add(data_root, protocol_id: str | None, member_sets: Iterable[Sequence[str]], lock=threading.Lock()) -> int:
    """Remember every distinct combination evaluated under a protocol (honest counting); returns the total."""
    with lock:
        p = combos_dir(data_root) / "ledger.json"
        doc = _read_json(p, {"version": 1, "protocols": {}})
        key = protocol_id or "none"
        have = set(doc["protocols"].get(key, []))
        before = len(have)
        have.update("|".join(sorted(s)) for s in member_sets if len(s) >= 2)
        if len(have) != before:
            doc["protocols"][key] = sorted(have)
            _write_json(p, doc)
        return len(have)


def ledger_count(data_root, protocol_id: str | None) -> int:
    doc = _read_json(combos_dir(data_root) / "ledger.json", {"protocols": {}})
    return len(doc["protocols"].get(protocol_id or "none", []))


def save_search(data_root, doc: Mapping) -> str:
    sid = doc["search_id"]
    _write_json(combos_dir(data_root) / "searches" / f"{sid}.json", doc)
    return sid


def latest_search(data_root, scope_ref: str | None = None) -> dict | None:
    d = combos_dir(data_root) / "searches"
    best = None
    for p in sorted(d.glob("CS_*.json")) if d.is_dir() else ():
        doc = _read_json(p, None)
        if not doc or (scope_ref is not None and (doc.get("scope_ref") or "") != (scope_ref or "")):
            continue
        if best is None or str(doc.get("created_at")) > str(best.get("created_at")):
            best = doc
    return best


def saved(data_root) -> list[dict]:
    return _read_json(combos_dir(data_root) / "saved.json", {"rows": []}).get("rows", [])


def save_named(data_root, name: str, member_ids: Sequence[str]) -> list[dict]:
    rows = [r for r in saved(data_root) if sorted(r["members"]) != sorted(member_ids)]
    rows.append({"name": name, "members": sorted(member_ids), "saved_at": datetime.now(timezone.utc).isoformat()})
    _write_json(combos_dir(data_root) / "saved.json", {"version": 1, "rows": rows})
    return rows


def delete_named(data_root, member_ids: Sequence[str]) -> list[dict]:
    rows = [r for r in saved(data_root) if sorted(r["members"]) != sorted(member_ids)]
    _write_json(combos_dir(data_root) / "saved.json", {"version": 1, "rows": rows})
    return rows


# ================================================================================================ service layer
_MEMBERS: dict[tuple, Member] = {}          # (store path, run id, strategy id) -> member (stored trades never change)
_MEMBERS_LOCK = threading.Lock()
_MEMBERS_MAX = 2000


def _store_key(svc) -> str:
    return str(getattr(getattr(svc, "writer_store", svc.store), "path", ""))


def criteria_profile(svc) -> dict | None:
    """The Settings pass-criteria rule profile (the survivor rule's account), latest registered version."""
    from edgelab.prop.service import default_profiles
    from edgelab.research import overview as ov
    pid = ov.criteria_profile(svc)
    return next((p for p in default_profiles(svc.root) if p["profile_id"] == pid), None)


def tpw_cap(svc) -> float:
    try:
        v = svc.ui_preferences().get("combo_tpw_cap")
    except Exception:                                        # noqa: BLE001
        v = None
    return float(v) if isinstance(v, (int, float)) and not isinstance(v, bool) and v > 0 else DEFAULT_TPW_CAP


def survivors(svc, campaign_run: Any = None) -> list[dict]:
    """The current survivors (latest discovery run per strategy, Settings pass-criteria account) that may be combined:
    flipped strategies excluded (they have their own protocol). Each row says which ACTIVE protocol governs it."""
    from edgelab.research import holdout as H
    from edgelab.research import overview as ov
    from edgelab.research.results_view import _latest_scoped
    rows, _ = _latest_scoped(svc, "in_sample", campaign_run)
    out, protos, ledgers = [], {}, {}
    for x in rows:
        ref, f = x["ref"], x["facets"]
        if not ref or not ref.get("trade_count") or not ref.get("survivor") or f.get("mirror_of"):
            continue
        lh = f.get("logic_hash") or H._logic_hash(svc, f["strategy_id"])
        p = H._protocol_of(svc, ref, lh)
        pid = p["protocol_id"] if p is not None and p["status"] == "ACTIVE" else None
        if pid and pid not in ledgers:
            protos[pid], ledgers[pid] = p, H._ledger(svc, pid)
        row = {"strategy_id": f["strategy_id"], "display_name": f.get("display_name") or f.get("name"),
               "family_id": f.get("family_id"), "family_name": f.get("family_name"), "timeframe": f.get("timeframe"),
               "session": f.get("session"), "run_id": ref["run_id"], "dataset_id": ref.get("dataset_id"),
               "logic_hash": lh, "protocol_id": pid, "trade_count": ref.get("trade_count"),
               "trades_per_week": ref.get("trades_per_week"), "expectancy_r": ref.get("expectancy_r"),
               "net_r": ref.get("net_r"), "max_drawdown_usd": ref.get("max_drawdown_usd"),
               "negative_months": H.negative_months(svc, ref["run_id"]), "synthetic": ref.get("synthetic")}
        el = H._eligibility(svc, {**row, "protocol_id": pid}, f, ledgers.get(pid))
        row.update(search_id=el.get("search_id"), own_holdout=bool(el.get("tested")) or "earlier protocol" in str(el.get("reason")),
                   registrable=bool(pid) and (el["eligible"] or bool(el.get("tested")) or "earlier protocol" in str(el.get("reason"))),
                   not_registrable_reason=None if pid else "no active research protocol governs this data")
        if pid and not row["registrable"]:
            row["not_registrable_reason"] = el.get("reason")
        out.append(row)
    return out


def load_members(svc, rows: Sequence[Mapping]) -> list[Member]:
    """Members from survivor rows (stored trades are read once per process; they never change)."""
    out = []
    sk = _store_key(svc)
    for r in rows:
        key = (sk, r["run_id"], r["strategy_id"])
        with _MEMBERS_LOCK:
            m = _MEMBERS.get(key)
        if m is None:
            rec, t = svc.store.load_run(r["run_id"])
            m = member_from(r, rec, t)
            with _MEMBERS_LOCK:
                if len(_MEMBERS) >= _MEMBERS_MAX:
                    _MEMBERS.pop(next(iter(_MEMBERS)))
                _MEMBERS[key] = m
        out.append(m)
    return out


def pick(svc, strategy_ids: Any, campaign_run: Any = None) -> tuple[list[dict], str | None]:
    """Validate a hand-picked set (2..5 distinct current survivors under one protocol) -> (survivor rows, protocol)."""
    if not isinstance(strategy_ids, list) or not all(isinstance(x, str) and x for x in strategy_ids):
        raise ComboError("COMBO_INVALID", "choose 2 to 5 survivors")
    if len(set(strategy_ids)) != len(strategy_ids):
        raise ComboError("COMBO_INVALID", "a strategy is listed twice")
    if not 2 <= len(strategy_ids) <= MAX_MEMBERS:
        raise ComboError("COMBO_INVALID", f"a combination has 2 to {MAX_MEMBERS} strategies")
    by = {r["strategy_id"]: r for r in survivors(svc, None)}
    missing = [s for s in strategy_ids if s not in by]
    if missing:
        raise ComboError("COMBO_NOT_SURVIVOR", f"{len(missing)} of the chosen strategies "
                         f"{'is' if len(missing) == 1 else 'are'} not a current survivor", strategy_ids=missing)
    rows = [by[s] for s in sorted(strategy_ids)]
    pids = {r["protocol_id"] for r in rows}
    if len(pids) != 1:
        raise ComboError("COMBO_MIXED_PROTOCOLS", "the chosen strategies are governed by different research protocols")
    return rows, rows[0]["protocol_id"]


def evaluate(svc, strategy_ids: Any) -> dict:
    """The combination panel of a hand-picked (or listed) set; the set is added to the evaluated-combination ledger."""
    rows, pid = pick(svc, strategy_ids)
    ms = load_members(svc, rows)
    d = detail(ms, criteria_profile(svc), tpw_cap(svc), float(svc.risk_per_trade()["risk_per_trade_usd"]))
    d.pop("frame", None)
    n = ledger_add(svc.data_root, pid, [d["member_ids"]])
    names = {r["strategy_id"]: r["display_name"] for r in rows}
    for m in d["members"]:
        m["display_name"] = names.get(m["strategy_id"]) or m["display_name"]
    lhs = [r["logic_hash"] for r in rows]
    d.update(protocol_id=pid, combo_id=combo_id(lhs) if all(lhs) else None, evaluated_in_protocol=n,
             criteria=_criteria_rows(), ranking_rule=RANKING_RULE, tpw_cap=tpw_cap(svc),
             criteria_profile=(criteria_profile(svc) or {}).get("profile_id"))
    return d


def _criteria_rows() -> list[dict]:
    return [{"key": k, "label": lab, "higher_is_better": hi, "weight": w} for k, lab, hi, w in CRITERIA]


def _search_parallel(members: list[Member], profile, cap, processes: int, progress, cancel) -> dict:
    """``search`` with the seeds split over worker processes: each seed's greedy path is computed the same way on any
    core; the parent combines in seed order, so the answer equals a one-core search (tested)."""
    from concurrent.futures import ProcessPoolExecutor
    pool = sorted(members, key=lambda m: m.strategy_id)
    ids = [m.strategy_id for m in pool]
    chunks = [ids[i::processes] for i in range(processes)]
    finals: dict[str, tuple] = {}
    evaluated: set[tuple] = set()
    alone, done = 0, 0
    with ProcessPoolExecutor(max_workers=processes) as ex:
        futs = [ex.submit(_seed_worker, pool, profile, cap, ch) for ch in chunks if ch]
        for f in futs:
            out = f.result()
            for seed, final, path, row in out["seeds"]:
                finals[seed] = (final, path, row)
            evaluated.update(tuple(k) for k in out["evaluated"] if len(k) >= 2)
            done += out["n"]
            if progress:
                progress(done, len(ids))
            if cancel is not None and cancel.is_set():
                break
    rows, seen = [], set()
    for seed in ids:
        if seed not in finals:
            continue
        final, path, row = finals[seed]
        if final is None:
            alone += 1
        elif final not in seen:
            seen.add(final)
            rows.append({**row, "path": path})
    return {"rows": rank(rows, CRITERIA), "evaluated": [list(k) for k in sorted(evaluated)],
            "n_evaluated": len(evaluated), "n_seeds": len(ids), "n_alone": alone,
            "cancelled": bool(cancel is not None and cancel.is_set())}


def _seed_worker(pool: list[Member], profile, cap: float, seeds: list[str]) -> dict:
    ev = Evaluator(pool, profile, cap)
    ids = sorted(ev.members)
    out = []
    for s in seeds:
        if not ev.survivor(ev.key([s]))["survivor"]:
            continue
        final, path = greedy_from(ev, s, ids)
        out.append((s, final if len(final) >= 2 else None, path, ev.full(final) if len(final) >= 2 else None))
    return {"seeds": out, "evaluated": ev.evaluated(), "n": len(seeds)}


def run_search(svc, campaign_run: Any = None, processes: int = 1, progress=None, cancel=None) -> dict:
    """Find the best combinations among the survivors of one protocol (the active research protocol when there is one)
    and save the result (``<data>/combinations/searches``); every evaluated set goes into the ledger."""
    rows = survivors(svc, campaign_run)
    groups: dict[str | None, list[dict]] = {}
    for r in rows:
        groups.setdefault(r["protocol_id"], []).append(r)
    pid = max(groups, key=lambda k: (k is not None, len(groups[k]))) if groups else None
    chosen = groups.get(pid, [])
    profile, cap = criteria_profile(svc), tpw_cap(svc)
    started = datetime.now(timezone.utc)
    ms = load_members(svc, chosen)
    if processes > 1 and len(ms) > 2:
        res = _search_parallel(ms, profile, cap, processes, progress, cancel)
    else:
        res = search(ms, profile, cap, progress=progress, cancel=cancel)
    names = {r["strategy_id"]: r["display_name"] for r in chosen}
    lh = {r["strategy_id"]: r["logic_hash"] for r in chosen}
    for r in res["rows"]:
        r["names"] = [names.get(s) for s in r["members"]]
        r["combo_id"] = combo_id(lh[s] for s in r["members"]) if all(lh.get(s) for s in r["members"]) else None
    n_ledger = ledger_add(svc.data_root, pid, res["evaluated"]) if not res["cancelled"] else ledger_count(svc.data_root, pid)
    doc = {"search_id": "CS_" + hash_obj({"at": started.isoformat(), "scope": campaign_run, "pid": pid}, 12).upper(),
           "created_at": started.isoformat(), "finished_at": datetime.now(timezone.utc).isoformat(),
           "scope_ref": campaign_run or "", "protocol_id": pid,
           "criteria_profile": (profile or {}).get("profile_id"), "tpw_cap": cap, "merge_rule": MERGE_RULE,
           "ranking_rule": RANKING_RULE, "criteria": _criteria_rows(), "processes": processes,
           "n_survivors": len(chosen), "n_survivors_other_protocols": len(rows) - len(chosen),
           "survivors": [{k: r[k] for k in ("strategy_id", "run_id", "logic_hash")} for r in chosen],
           "n_evaluated": res["n_evaluated"], "evaluated_in_protocol": n_ledger, "n_alone": res["n_alone"],
           "cancelled": res["cancelled"], "rows": res["rows"]}
    save_search(svc.data_root, doc)
    return doc


# ------------------------------------------------------------------------------------------------ holdout registration
CONFIRM_WORD = "REGISTER"
MAX_REGISTERED = 50


def parent_protocol(svc) -> dict | None:
    """The ACTIVE research protocol (never a companion) that combinations are registered under."""
    from edgelab.research.protocol import is_companion
    try:
        act = [p for p in svc.store.list_protocols(status="ACTIVE") if not is_companion(p)]
    except Exception:                                        # noqa: BLE001 - a store without protocol tables
        return None
    return act[0] if len(act) == 1 else None


def combo_protocol(svc, parent: Mapping | None) -> dict | None:
    if parent is None:
        return None
    ps = svc._combo_protocols(parent)
    return ps[-1] if ps else None


def register(svc, sets: Any, confirm: str | None, holdout_looks: int | None = None) -> dict:
    """Freeze a list of combinations for holdout tests: ONE combination protocol per research protocol (typed
    confirmation). Every combination: 2..5 current survivors of that protocol, each with its counted discovery trial
    there, and the merged trades still a survivor. Nothing is evaluated on the holdout here."""
    from edgelab.core.identity import code_version
    from edgelab.research import protocol as rp
    if confirm != CONFIRM_WORD:
        raise ComboError("COMBO_CONFIRM", f"type {CONFIRM_WORD} to register the combinations")
    if not isinstance(sets, list) or not sets:
        raise ComboError("COMBO_INVALID", "choose at least one combination to register")
    if len(sets) > MAX_REGISTERED:
        raise ComboError("COMBO_INVALID", f"at most {MAX_REGISTERED} combinations can be registered")
    parent = parent_protocol(svc)
    if parent is None:
        raise ComboError("PROTOCOL_NOT_ACTIVE", "no single active research protocol to register combinations under")
    pm = parent["material"]
    if combo_protocol(svc, parent) is not None:
        raise ComboError("COMBO_EXISTS", "this research protocol already has its registered combinations (one "
                         "registration per protocol)", protocol_id=parent["protocol_id"])
    if svc._config_hash() != pm["config_hash"]:
        raise ComboError("PROTOCOL_CONFIG_CHANGED", "the research settings differ from the protocol's")
    by = {r["strategy_id"]: r for r in survivors(svc, None)}
    profile, cap = criteria_profile(svc), tpw_cap(svc)
    entries, seen_keys, seen_members = [], set(), {}
    for ids in sets:
        rows, pid = pick(svc, ids)
        if pid != parent["protocol_id"]:
            raise ComboError("COMBO_MIXED_PROTOCOLS", "a combination's strategies are not governed by the active "
                             "research protocol")
        bad = [r for r in rows if not by[r["strategy_id"]]["registrable"]]
        if bad:
            raise ComboError("COMBO_NOT_REGISTRABLE", f"{bad[0]['display_name'] or bad[0]['strategy_id']}: "
                             f"{by[bad[0]['strategy_id']]['not_registrable_reason']}")
        key = tuple(r["strategy_id"] for r in rows)
        if key in seen_keys:
            raise ComboError("COMBO_INVALID", "a combination is listed twice")
        seen_keys.add(key)
        ms = load_members(svc, rows)
        ev = Evaluator(ms, profile, cap)
        st = ev.full(key)
        if not st["survivor"]:
            raise ComboError("COMBO_NOT_SURVIVOR", "a combination's merged trades are not a survivor (they must pass an "
                             "evaluation with a payout and be net positive)", strategy_ids=list(key))
        doc_of = {r["strategy_id"]: svc.library.load(r["strategy_id"]) for r in rows}
        members = [{"strategy_id": r["strategy_id"], "logic_hash": r["logic_hash"],
                    "definition_hash": doc_of[r["strategy_id"]]["definition_hash"], "timeframe": r["timeframe"],
                    "dataset_id": r["dataset_id"], "run_id": r["run_id"], "search_id": r["search_id"]} for r in rows]
        for r in rows:
            if by[r["strategy_id"]]["own_holdout"]:
                seen_members[r["strategy_id"]] = {"strategy_id": r["strategy_id"]}
        entries.append({"combo_id": combo_id(m["logic_hash"] for m in members), "members": members,
                        "discovery": {k: _clean(st.get(k)) for k in ("trades", "net_r", "expectancy_r", "net_usd",
                                                                    "max_drawdown_usd", "negative_months",
                                                                    "trades_per_week", "pass_pct", "median_days_to_pass",
                                                                    "median_days_to_payout")}})
    n_eval = ledger_add(svc.data_root, parent["protocol_id"], [list(k) for k in seen_keys])
    looks_used = sum(1 for a in svc.store.list_holdout_access(parent["protocol_id"]) if a["status"] != "refused")
    last = latest_search(svc.data_root)
    selection = {"rule": "chosen by the user from the combination search's ranked list and hand-built sets; every "
                         "combination = 2..5 survivors merged one position at a time, still a survivor",
                 "n_registered": len(entries), "n_evaluated": n_eval, "criteria_profile": (profile or {}).get("profile_id"),
                 "tpw_cap": cap, "ranking_rule": RANKING_RULE,
                 "search_id": (last or {}).get("search_id") if (last or {}).get("protocol_id") == parent["protocol_id"] else None}
    material = rp.build_combo_material(parent, combo_set=entries, n_evaluated=n_eval, selection=selection,
                                       parent_looks_used=looks_used, members_seen=list(seen_members.values()),
                                       holdout_looks=holdout_looks or rp.DEFAULT_HOLDOUT_LOOKS,
                                       name=f"Combinations of {pm.get('name') or parent['protocol_id']}",
                                       merge_rule=MERGE_RULE)
    rec = rp.make_record(material, {"code_version": code_version()})
    key = svc._scope_key(pm["scope"]["instrument"], pm["scope"]["provider"]) + rp.COMBO_SCOPE_SUFFIX
    svc.store.save_protocol(rec, key)
    return {"combo_protocol_id": rec["protocol_id"], "parent_protocol_id": parent["protocol_id"],
            "n_registered": len(entries), "family_size": rp.family_size(material, 0),
            "holdout_tests": material["holdout_budget"]["max_unique_candidate_evaluations"]}


def registration(svc) -> dict:
    """The combination protocol of the active research protocol: its registered combinations, tests used / left and
    each combination's holdout result (read only)."""
    import json as _json
    parent = parent_protocol(svc)
    cp = combo_protocol(svc, parent)
    out = {"parent_protocol_id": (parent or {}).get("protocol_id"), "protocol": None, "combos": [],
           "can_register": parent is not None and cp is None, "confirm_word": CONFIRM_WORD,
           "evaluated_in_protocol": ledger_count(svc.data_root, (parent or {}).get("protocol_id"))}
    if cp is None:
        return out
    from edgelab.research.protocol import family_size
    mat = cp["material"]
    acc = svc.store.list_holdout_access(cp["protocol_id"])
    used = sum(1 for a in acc if a["status"] != "refused")
    budget = int(mat["holdout_budget"]["max_unique_candidate_evaluations"])
    out["protocol"] = {"protocol_id": cp["protocol_id"], "status": cp["status"], "name": mat.get("name"),
                       "tests_used": used, "tests_budget": budget, "tests_left": max(0, budget - used),
                       "family_size": family_size(mat, 0), "created_at": cp.get("created_at"),
                       "holdout_trading_dates": mat["windows"]["holdout"]["trading_dates"]}
    for c in mat["combo_set"]:
        tried = [a for a in acc if a["strategy_id"] == c["combo_id"] and a["status"] != "refused"]
        res = _json.loads(tried[-1]["result_json"]) if tried and tried[-1].get("result_json") else {}
        out["combos"].append({"combo_id": c["combo_id"], "members": [m["strategy_id"] for m in c["members"]],
                              "discovery": c["discovery"],
                              "holdout": None if not tried else {"status": tried[-1]["status"],
                                                                 "outcome": res.get("outcome"),
                                                                 "access_id": tried[-1]["access_id"],
                                                                 "trade_count": res.get("trade_count"),
                                                                 "net_r": res.get("net_r"),
                                                                 "criteria": res.get("criteria"),
                                                                 "members": res.get("members"),
                                                                 "completed_at": tried[-1].get("completed_at")}})
    return out


def holdout_view(svc, result: Mapping, risk_usd: float) -> dict | None:
    """Curve and years of a combination's holdout test (labelled Holdout), from its member runs merged again."""
    runs = [m for m in (result.get("members") or ()) if m.get("run_id")]
    if not runs:
        return None
    ms = []
    for m in runs:
        try:
            rec, t = svc.store.load_run(m["run_id"])
        except KeyError:
            return None
        ms.append(member_from({"strategy_id": m["strategy_id"], "run_id": m["run_id"]}, rec, t))
    ms.sort(key=lambda m: m.strategy_id)
    mids, rows = merge_select(ms)
    frame = merged_frame(ms, mids, rows)
    from edgelab.research.results_view import calendar_years
    eq = np.cumsum(frame["net_r"].to_numpy(float)) if len(frame) else np.zeros(0)
    ts = pd.to_datetime(frame["exit_ts"], utc=True) if len(frame) else []
    win = window_of(ms)
    return {"curve": [{"t": ts.iloc[i].isoformat(), "v": round(float(eq[i]), 6), "n": i + 1} for i in range(len(eq))],
            "years": calendar_years(frame, risk_usd) if len(frame) else [],
            "window": {"start": _iso(win[0]), "end": _iso(win[1])}, "trades": int(len(frame))}


def panel(svc, strategy_ids: Any) -> dict:
    """The combination panel: discovery detail + its registration / holdout state (and the holdout curve, if tested)."""
    d = evaluate(svc, strategy_ids)
    reg = registration(svc)
    mine = next((c for c in reg["combos"] if sorted(c["members"]) == sorted(d["member_ids"])), None)
    d["registered"] = mine is not None
    d["holdout"] = (mine or {}).get("holdout")
    d["registration"] = {k: reg[k] for k in ("protocol", "can_register", "confirm_word")}
    if d["holdout"] and d["holdout"].get("status") == "completed":
        d["holdout_view"] = holdout_view(svc, d["holdout"], float(svc.risk_per_trade()["risk_per_trade_usd"]))
    return d
