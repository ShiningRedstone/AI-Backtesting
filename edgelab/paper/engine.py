"""Paper accounts (ADR-81): one strategy, one prop rule profile, forward from a start date, attempt after attempt.

Everything is recomputed from the stored inputs on every update (deterministic, nothing hand-maintained):
1. the strategy's FROZEN definition is compiled and run by the existing engine (``run_backtest``) on the validated
   forward feed (warm-up history before the start feeds the indicators only); only trades ENTERED on or after the
   account start count;
2. attempts through the unchanged prop lifecycle (``prop.lifecycle.simulate_lifecycle``):
   * evaluation breach -> attempt failed; the next attempt starts with the first trade entered after the breach and
     costs the reset fee (or, when none is set, a new evaluation);
   * evaluation passed -> activation fee (if set); the funded account trades on with every payout;
   * funded breach, live-transition point or payout limit -> the cycle ends; a new evaluation is bought;
   * not finished at the end of the data -> the attempt is in progress;
   * INCOMPATIBLE (the strategy cannot trade this account, e.g. too many micros) -> the account stops.
3. equity-% sizing uses the attempt's own balance: each attempt re-runs the engine with ``account`` = the profile's
   starting balance and ``equity_from_ts`` = the attempt start (trades before it never change equity). Payout
   withdrawals do not lower that sizing balance inside a funded cycle (a stated simplification).
Net = trader payouts - fees. No order is ever sent anywhere.
"""
from __future__ import annotations

from typing import Callable, Mapping

import pandas as pd

MAX_ATTEMPTS = 500
LABEL = ("PAPER: simulated forward trading on Dukascopy USATECH data (an index CFD standing in for NQ), MNQ contract "
         "spec, assumed costs, prop rules under default assumptions. Not a forecast; no real orders.")


def profile_version(root, profile_id: str, version: int) -> dict:
    """The exact registered rule-profile version an account was started with (hash-checked against the registry)."""
    from edgelab.prop.profiles import load_profile, profile_hash, profiles_dir, read_registry
    d = profiles_dir(root)
    for e in read_registry(d)["profiles"]:
        if (e["profile_id"], int(e["version"])) == (profile_id, int(version)):
            doc = load_profile(d / e["file"])
            if profile_hash(doc) != e["profile_hash"]:
                raise ValueError(f"{e['file']} changed since registration")
            return doc
    raise KeyError(f"rule profile {profile_id} v{version} is not registered")


def fee_schedule(fees: Mapping | None) -> dict:
    f = dict(fees or {})
    ev = f.get("eval_price")
    return {"eval_price": float(ev) if ev not in (None, "") else None,
            "reset_fee": float(f["reset_fee"]) if f.get("reset_fee") not in (None, "") else None,
            "activation_fee": float(f["activation_fee"]) if f.get("activation_fee") not in (None, "") else None}


def strategy_trades(defn: Mapping, feed, cfg: Mapping, sessions, *, equity_from=None, starting_equity=None) -> pd.DataFrame:
    """The engine's trades for one frozen strategy definition on the feed (same compile -> bind -> run_backtest path
    as research, Dukascopy directional cost scenario, the strategy's own sizing and execution contract)."""
    from edgelab.core.config import config_hash
    from edgelab.engine.backtester import run_backtest
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.features.cache import FeatureCache
    from edgelab.features.strategy_api import FeatureContext
    from edgelab.instruments import contract_for
    from edgelab.strategy.compiler import compile_strategy
    strat = compile_strategy(dict(defn), sessions, config_hash(cfg))
    tf = f"{strat.compiled.tf_minutes}m"
    if tf not in feed.datasets:
        raise ValueError(f"the forward feed has no {tf} bars (available: {sorted(feed.datasets)})")
    ds = feed.datasets[tf]
    costs = cost_model_from_config(cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    bound = strat.bind(FeatureContext(ds, sessions, FeatureCache(None)))
    account = None
    if strat.sizing.get("mode") == "equity_risk":
        account = {"name": "paper attempt", "starting_equity": float(starting_equity or 50_000.0),
                   **({"equity_from_ts": pd.Timestamp(equity_from).isoformat()} if equity_from is not None else {})}
    res = run_backtest(ds, bound, costs, cfg["backtest"], sizing=strat.sizing,
                       contract=contract_for(cfg, strat.sizing), account=account)
    return res.trades


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def run_attempts(trades_from: Callable[[pd.Timestamp], pd.DataFrame], profile: Mapping, fees: Mapping,
                 start: pd.Timestamp) -> dict:
    """The attempt sequence from ``start``. ``trades_from(t)`` returns the trades entered at or after ``t`` (sized for
    an attempt starting at ``t``). Pure: the same inputs always give the same record."""
    from edgelab.prop.lifecycle import compact, simulate_lifecycle
    fs = fee_schedule(fees)
    attempts, payouts, fee_log = [], [], []
    t, kind, stop_reason = _ts(start), "new_evaluation", None
    for n in range(1, MAX_ATTEMPTS + 1):
        fee_kind = "eval_price" if kind == "new_evaluation" else "reset_fee"
        amount = fs["eval_price"] if fee_kind == "eval_price" else (fs["reset_fee"] if fs["reset_fee"] is not None
                                                                     else fs["eval_price"])
        fee_log.append({"attempt": n, "kind": fee_kind if (fee_kind == "eval_price" or fs["reset_fee"] is not None)
                        else "eval_price", "amount": amount, "at": t.isoformat()})
        tr = trades_from(t)
        tr = tr[pd.to_datetime(tr["entry_ts"], utc=True) >= t] if len(tr) else tr
        rec = {"n": n, "start": t.isoformat(), "fee": amount, "n_trades": int(len(tr)), "status": "in_progress",
               "stage": "evaluation", "end": None, "reason": None, "detail": None, "trades": tr}
        if not len(tr):
            attempts.append(rec)
            break
        r = simulate_lifecycle(tr, profile)
        ev, fu = r["evaluation"], r["funded"]
        c = compact(r)
        rec.update(evaluation=c.get("evaluation"), funded=c.get("funded"), eval_profit=ev["profit"],
                   eval_trading_days=ev["trading_days"],
                   balance=(fu.get("balance") if fu.get("started") else ev["balance"]))
        if ev["status"] == "INCOMPATIBLE" or fu.get("status") == "INCOMPATIBLE":
            b = (ev["breach"] if ev["status"] == "INCOMPATIBLE" else fu.get("breach")) or {}
            rec.update(status="incompatible", end=b.get("ts"), reason=b.get("reason"), detail=b.get("detail"))
            _close(rec)
            attempts.append(rec)
            stop_reason = f"the strategy cannot trade this account: {b.get('detail') or b.get('reason')}"
            break
        if ev["status"] == "FAIL":
            if ev["failure_reason"] == "NOT_PASSED_BY_END_OF_DATA":
                attempts.append(rec)                          # still trading the evaluation
                break
            b = ev["breach"]
            rec.update(status="failed", end=b["ts"], reason=b["reason"], detail=b["detail"])
            _close(rec)
            attempts.append(rec)
            t, kind = _ts(b["ts"]), "reset"
            t = t + pd.Timedelta(microseconds=1)              # the next attempt: trades entered after the breach
            continue
        # passed the evaluation
        rec["passed_at"] = (ev["pass"] or {}).get("ts")
        if fs["activation_fee"] is not None:
            fee_log.append({"attempt": n, "kind": "activation_fee", "amount": fs["activation_fee"],
                            "at": (ev["pass"] or {}).get("day_end_ts")})
        for p in r["payouts"]:
            payouts.append({"attempt": n, **{k: p[k] for k in ("n", "date", "gross", "trader_share", "firm_share",
                                                               "balance_after")}})
        rec["payouts"] = len(r["payouts"])
        rec["trader_payout"] = r["totals"]["trader_payout"]
        if fu.get("outcome") == "ACTIVE_AT_END_OF_DATA":
            rec.update(status="funded", stage="funded")
            attempts.append(rec)
            break
        if fu.get("outcome") == "BREACH":
            b = fu["breach"] or {}
            rec.update(status="funded_lost", stage="funded", end=b.get("ts"), reason=b.get("reason"),
                       detail=b.get("detail"))
        else:                                                 # live-transition point or payout limit reached
            last = r["payouts"][-1]["eligibility_ts"] if r["payouts"] else (ev["pass"] or {}).get("ts")
            rec.update(status="funded_completed", stage="funded", end=last, reason=fu.get("outcome"))
        _close(rec)
        attempts.append(rec)
        t, kind = _ts(rec["end"]) + pd.Timedelta(microseconds=1), "new_evaluation"
    fees_total = sum(f["amount"] for f in fee_log if f["amount"] is not None)
    trader = sum(p["trader_share"] for p in payouts)
    missing_fee = any(f["amount"] is None for f in fee_log)
    return {"attempts": attempts, "payouts": payouts, "fees": fee_log, "fees_total": round(fees_total, 2),
            "fees_complete": not missing_fee, "trader_payouts": round(trader, 2),
            "net": round(trader - fees_total, 2), "passes": sum(1 for a in attempts if a.get("passed_at")),
            "stopped": stop_reason}


def _close(rec: dict) -> None:
    """A finished attempt keeps only its own trades (entered up to its end); later trades belong to the next one."""
    if rec["end"] is not None and len(rec["trades"]):
        tr = rec["trades"]
        rec["trades"] = tr[pd.to_datetime(tr["entry_ts"], utc=True) <= _ts(rec["end"])]
        rec["n_trades"] = int(len(rec["trades"]))


def simulate_account(account: Mapping, feed, cfg: Mapping, sessions, profile: Mapping) -> dict:
    """The full, recomputed state of one paper account on the current feed."""
    defn = account["definition"]
    start = _ts(account["start_ts"])
    cache: dict = {}

    def base():
        if "base" not in cache:
            cache["base"] = strategy_trades(defn, feed, cfg, sessions)
        return cache["base"]

    equity_mode = (defn.get("sizing") or {}).get("mode") == "equity_risk"
    from edgelab.prop.profiles import resolve
    start_balance = float(resolve(profile)["evaluation"]["starting_balance"])

    def trades_from(t: pd.Timestamp) -> pd.DataFrame:
        if equity_mode:
            return strategy_trades(defn, feed, cfg, sessions, equity_from=t, starting_equity=start_balance)
        return base()
    res = run_attempts(trades_from, profile, account.get("fees") or {}, start)
    cur = res["attempts"][-1] if res["attempts"] else None
    all_trades = pd.concat([a["trades"] for a in res["attempts"] if len(a["trades"])], ignore_index=True) \
        if any(len(a["trades"]) for a in res["attempts"]) else pd.DataFrame()
    state = ("stopped" if res["stopped"] else "waiting" if cur is None or not cur["n_trades"] else
             "funded" if cur["status"] == "funded" else "evaluation")
    return {**{k: v for k, v in res.items() if k != "attempts"},
            "attempts": [{k: v for k, v in a.items() if k != "trades"} for a in res["attempts"]],
            "state": state, "current_attempt": cur["n"] if cur else 0,
            "trades": _jsonable_trades(all_trades), "n_trades": int(len(all_trades)),
            "feed": {"first_day": feed.first_day, "last_day": feed.last_day, "content_hash": feed.content_hash},
            "label": LABEL}


def _jsonable_trades(t: pd.DataFrame) -> list[dict]:
    if t is None or not len(t):
        return []
    keep = [c for c in ("trade_no", "entry_ts", "exit_ts", "direction", "contracts", "entry_price_eff", "exit_price_eff",
                        "exit_reason", "net_usd", "net_r", "cost_usd") if c in t.columns]
    out = t[keep].copy()
    for c in ("entry_ts", "exit_ts"):
        if c in out:
            out[c] = pd.to_datetime(out[c], utc=True).map(lambda x: x.isoformat())
    return out.to_dict("records")
