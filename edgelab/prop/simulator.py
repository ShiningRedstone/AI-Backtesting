"""Prop-account simulation over a COMPLETED backtest trade stream (Phase 6).

The simulator sits above the engine: it reads the stored trades of one run (never the market
data, never the strategy) and replays them, in chronological order, through the account rules of
one or more accounts. Trades are taken exactly as recorded (size, P&L, costs); nothing is resized,
re-filled or re-costed. It never mutates its input.

Exact semantics (also in PROP_SIMULATION.md):

* Order: trades sorted by (exit_ts, entry_ts, trade_no); positions must not overlap (the engine
  holds one position at a time; overlapping positions would need a joint equity path -> refused).
* Trading day: runs from ``trading_day.reset_time`` to the next reset in ``trading_day.timezone``
  and is labelled by the local date on which it ENDS (reset 00:00 -> the local calendar date).
  Entries are dated at ``entry_ts`` (bar open); exits at ``exit_ts - 1 ns`` (exit_ts is the exit
  bar's close, an exclusive bound), so an exit on the bar ending exactly at the reset belongs to
  the day that ends then.
* Booking: realized P&L (``net_usd``, i.e. after the backtest's stated costs) is booked at the exit
  on the exit's trading day. A trade crossing the reset is booked entirely on the exit day and
  counted in ``trades_crossing_reset``. A trading day counts toward ``min_trading_days`` when at
  least one trade is booked on it.
* Daily loss: day P&L = closed balance - balance at the day's start. Breach when day P&L <=
  -daily_loss.max (reaching the limit is a breach).
* Drawdown floor: static -> starting_balance - max; trailing/closed_balance -> (highest closed
  balance) - max, updated after each exit; trailing/end_of_day_balance -> (highest end-of-day
  closed balance) - max, updated at each day end. ``lock_floor_at`` caps the trailing floor.
  Breach when equity <= floor.
* Detection ``end_of_trade``: rules checked on the closed balance after each exit.
  ``intratrade_bound``: additionally, before the exit, a conservative worst-equity bound
  ``balance_before - mae_points * point_value * contracts - cost_usd`` is checked (MAE is recorded
  at bar resolution with the entry bar included in full, so it bounds, and may overstate, the
  true adverse excursion). The trailing peak never moves inside a trade. A trade crossing the
  daily reset is REFUSED in this mode: the records do not say on which day the MAE occurred.
* Same trade: an intra-trade breach precedes the exit (the MAE occurs at or before the exit), so
  a breach and a target on the same trade resolve as the breach. A single closed trade cannot both
  reach the target (a gain) and breach a loss rule (a loss). When daily-loss and max-drawdown
  breach together both are recorded; status precedence is MAX_DRAWDOWN_VIOLATION first.
* Target: closed balance >= starting_balance + target.profit, AND trading days >=
  min_trading_days, AND (if configured) best-day profit / total profit <= max_best_day_share.
  Unrealized equity never counts toward the target.
* Position size and session rules are checked at entry (size, entry window) and over the holding
  interval (flat_by). ``terminate`` ends the account at the entry (that trade's P&L is not booked:
  the records cannot say where a firm would have closed it); ``record`` logs the violation and
  continues. Sizes are never clipped.
* End of period: an account still ACTIVE after the last trade is INCOMPLETE. A trade the backtest
  force-closed at END_OF_DATA is counted in ``trades_end_of_data`` (its P&L is the forced exit).
* Multiple accounts: each account replays the same stream independently from its own ``start``.
"""
from __future__ import annotations

import datetime as dt
from enum import Enum
from typing import Any, Mapping, Sequence

import pandas as pd

from edgelab.core.identity import hash_obj
from edgelab.prop.rules import PropRules, validate_rules

SIMULATOR_VERSION = "prop_sim_v1"
ORDERING = "exit_ts, then entry_ts, then trade_no (stable); non-overlapping positions required"
BASE_COLUMNS = ("trade_no", "entry_ts", "exit_ts", "contracts", "net_usd", "cost_usd", "net_r", "exit_reason")
INTRATRADE_COLUMNS = ("mae_points", "risk_points", "risk_usd")
EPS = 1e-9


class AccountStatus(str, Enum):
    ACTIVE = "ACTIVE"
    TARGET_REACHED = "TARGET_REACHED"
    DAILY_LOSS_VIOLATION = "DAILY_LOSS_VIOLATION"
    MAX_DRAWDOWN_VIOLATION = "MAX_DRAWDOWN_VIOLATION"
    RULE_VIOLATION = "RULE_VIOLATION"
    INCOMPLETE = "INCOMPLETE"


FAILED = {AccountStatus.DAILY_LOSS_VIOLATION, AccountStatus.MAX_DRAWDOWN_VIOLATION, AccountStatus.RULE_VIOLATION}


class PropDataError(ValueError):
    """The trade records lack information needed to model a rule honestly."""


# ------------------------------------------------------------------------------------ time helpers
def _reset_minutes(rules: PropRules) -> int:
    h, m = map(int, rules["trading_day"]["reset_time"].split(":"))
    return h * 60 + m


def trading_days(ts: pd.Series, rules: PropRules) -> pd.Series:
    """Trading-day label (local date on which the day ends) of UTC timestamps."""
    local = pd.DatetimeIndex(ts).tz_convert(rules["trading_day"]["timezone"])
    off = _reset_minutes(rules)
    shifted = local - pd.Timedelta(minutes=off)
    days = shifted.tz_localize(None).normalize()
    if off:
        days = days + pd.Timedelta(days=1)
    return pd.Series(days.date, index=ts.index)


def _local_minutes(ts: pd.Timestamp, tz: str) -> int:
    t = ts.tz_convert(tz)
    return t.hour * 60 + t.minute


def _hm(s: str) -> int:
    h, m = map(int, s.split(":"))
    return h * 60 + m


def _in_window(minute: int, start: int, end: int) -> bool:
    return start <= minute < end if start < end else (minute >= start or minute < end)


def _open_at_flat_by(entry: pd.Timestamp, exit_: pd.Timestamp, tz: str, flat_by: str) -> bool:
    """True when a local ``flat_by`` instant lies strictly inside (entry, exit)."""
    e = entry.tz_convert(tz)
    h, m = map(int, flat_by.split(":"))
    for add in (0, 1, 2):
        naive = dt.datetime.combine(e.date() + dt.timedelta(days=add), dt.time(h, m))
        cand = pd.Timestamp(naive).tz_localize(tz, nonexistent="shift_forward", ambiguous=False)
        if cand > e:
            return bool(cand < exit_.tz_convert(tz))
    return False


# ------------------------------------------------------------------------------------ input prep
def prepare_trades(trades: pd.DataFrame, detection_modes: Sequence[str]) -> pd.DataFrame:
    """A sorted, validated COPY of the trade stream; refuses missing or ambiguous data."""
    need = list(BASE_COLUMNS) + (list(INTRATRADE_COLUMNS) if "intratrade_bound" in detection_modes else [])
    missing = [c for c in need if c not in trades.columns]
    if missing:
        raise PropDataError(f"trade records lack required columns {missing}; cannot simulate these rules")
    t = trades[need].copy()
    for c in ("entry_ts", "exit_ts"):
        t[c] = pd.to_datetime(t[c], utc=True)
    if t[["entry_ts", "exit_ts", "net_usd", "contracts"]].isna().any().any():
        raise PropDataError("trade records contain missing timestamps, sizes or P&L")
    if (t["exit_ts"] < t["entry_ts"]).any():
        raise PropDataError("a trade exits before it enters")
    t = t.sort_values(["exit_ts", "entry_ts", "trade_no"], kind="mergesort").reset_index(drop=True)
    if len(t) > 1 and (t["entry_ts"].to_numpy()[1:] < t["exit_ts"].to_numpy()[:-1]).any():
        raise PropDataError("positions overlap in time; the account equity of concurrent positions needs a "
                            "joint intra-trade path that trade records do not contain")
    if "intratrade_bound" in detection_modes:
        if not (t["risk_points"] > 0).all():
            raise PropDataError("risk_points must be > 0 to convert MAE points to USD")
        t["point_value_x_units"] = t["risk_usd"] / t["risk_points"]
        t["mae_usd"] = t["mae_points"] * t["point_value_x_units"]
    return t


# ------------------------------------------------------------------------------------ one account
def simulate_account(trades: pd.DataFrame, rules: PropRules | Mapping, account_id: str = "A1",
                     start: Any = None) -> dict:
    """Replay a prepared (see ``prepare_trades``) trade stream through one account's rules."""
    rules = rules if isinstance(rules, PropRules) else validate_rules(rules)
    R = rules.doc
    start_bal = R["account"]["starting_balance"]
    tgt, ddr, dlr, pos, ses = R["target"], R["drawdown"], R["daily_loss"], R["position"], R["session"]
    intratrade = R["detection"] == "intratrade_bound"
    start_ts = None if start is None else pd.Timestamp(start)
    if start_ts is not None and start_ts.tzinfo is None:
        start_ts = start_ts.tz_localize("UTC")
    t = trades if start_ts is None else trades[trades["entry_ts"] >= start_ts]
    entry_day = trading_days(t["entry_ts"], rules) if len(t) else pd.Series(dtype=object)
    exit_day = trading_days(t["exit_ts"] - pd.Timedelta(1, "ns"), rules) if len(t) else pd.Series(dtype=object)
    first_day = (trading_days(pd.Series([start_ts]), rules).iloc[0] if start_ts is not None
                 else (entry_day.iloc[0] if len(t) else None))
    deadline = (first_day + dt.timedelta(days=R["max_calendar_days"] - 1)
                if R["max_calendar_days"] and first_day is not None else None)

    balance, peak_closed, peak_eod = start_bal, start_bal, start_bal
    status, violations, progression, days = AccountStatus.ACTIVE, [], [], {}
    cur_day, day_start_bal, paused_day = None, start_bal, None
    target_hit_ts = target_ts = breach_ts = None
    max_dd_closed = max_dd_bound = 0.0
    n_cross = n_eod = n_booked = 0
    net_r = 0.0
    not_processed = n_paused = 0

    def floor() -> float | None:
        if ddr is None:
            return None
        if ddr["mode"] == "static":
            return start_bal - ddr["max"]
        ref = peak_closed if ddr["trailing_reference"] == "closed_balance" else peak_eod
        f = ref - ddr["max"]
        return min(f, ddr["lock_floor_at"]) if ddr["lock_floor_at"] is not None else f

    def close_day():
        nonlocal peak_eod
        if cur_day is not None:
            peak_eod = max(peak_eod, balance)
            days[cur_day]["end_balance"] = balance

    def violate(rule: str, at, trade_no, detail: str, detection: str):
        violations.append({"rule": rule, "at": str(at), "trade_no": int(trade_no), "detail": detail,
                           "detection": detection})

    def max_units_now() -> float | None:
        if pos is None:
            return None
        mu = pos["max_units"]
        for tier in pos["scaling"]:
            if balance - start_bal >= tier["min_profit"] - EPS:
                mu = tier["max_units"]
        return mu

    def consistency() -> tuple[float | None, bool]:
        profit = balance - start_bal
        if R["consistency"] is None:
            return None, True
        best = max((d["pnl"] for d in days.values()), default=0.0)
        if profit <= 0:
            return None, False
        share = best / profit
        return share, share <= R["consistency"]["max_best_day_share"] + EPS

    for k in range(len(t)):
        row = t.iloc[k]
        no, eday, xday = int(row["trade_no"]), entry_day.iloc[k], exit_day.iloc[k]
        if status is not AccountStatus.ACTIVE:
            not_processed = len(t) - k
            break
        if deadline is not None and xday > deadline:
            status = AccountStatus.RULE_VIOLATION
            breach_ts = breach_ts or row["entry_ts"]
            violate("EVALUATION_DEADLINE", row["entry_ts"], no,
                    f"max_calendar_days {R['max_calendar_days']} ended on {deadline} without reaching the target",
                    "calendar")
            not_processed = len(t) - k
            break
        if xday != cur_day:
            close_day()
            cur_day, day_start_bal = xday, balance
            days[cur_day] = {"day": str(cur_day), "start_balance": balance, "pnl": 0.0, "trades": 0,
                             "worst_pnl_bound": 0.0, "end_balance": balance}
        if paused_day == xday:
            progression.append({"trade_no": no, "skipped": "DAILY_LOSS_PAUSE", "day": str(xday)})
            n_paused += 1
            continue
        crosses = eday != xday
        n_cross += crosses
        if intratrade and crosses:
            raise PropDataError(
                f"trade {no} crosses the daily reset ({eday} -> {xday}); intratrade_bound detection needs the "
                "time of its maximum adverse excursion, which trade records do not contain. Use "
                "detection: end_of_trade or a reset that the backtest's session flattening respects.")

        # ---- entry checks (size, session) ------------------------------------------------
        entry_breach = None
        mu = max_units_now()
        if mu is not None and row["contracts"] > mu + EPS:
            violate("MAX_POSITION_SIZE", row["entry_ts"], no,
                    f"trade size {row['contracts']:g} units > allowed {mu:g} (not clipped)", "entry")
            entry_breach = entry_breach or (pos["action"] == "terminate")
        if ses is not None:
            if ses["entry_start"] is not None and not _in_window(_local_minutes(row["entry_ts"], ses["timezone"]),
                                                                 _hm(ses["entry_start"]), _hm(ses["entry_end"])):
                violate("SESSION_ENTRY_WINDOW", row["entry_ts"], no,
                        f"entry outside {ses['entry_start']}-{ses['entry_end']} {ses['timezone']}", "entry")
                entry_breach = entry_breach or (ses["action"] == "terminate")
            if ses["flat_by"] is not None and _open_at_flat_by(row["entry_ts"], row["exit_ts"], ses["timezone"],
                                                               ses["flat_by"]):
                violate("SESSION_FLAT_BY", row["entry_ts"], no,
                        f"position open at flat_by {ses['flat_by']} {ses['timezone']}", "holding interval")
                entry_breach = entry_breach or (ses["action"] == "terminate")
        if entry_breach:
            status, breach_ts = AccountStatus.RULE_VIOLATION, row["entry_ts"]
            progression.append({"trade_no": no, "skipped": "TERMINATED_AT_ENTRY", "day": str(xday)})
            not_processed = len(t) - k - 1
            break

        # ---- intra-trade bound (before the exit) -----------------------------------------
        fl = floor()
        bal_before = balance
        low = None
        breaches = []
        if intratrade:
            low = bal_before - float(row["mae_usd"]) - float(row["cost_usd"])
            max_dd_bound = max(max_dd_bound, peak_closed - low)
            days[cur_day]["worst_pnl_bound"] = min(days[cur_day]["worst_pnl_bound"], low - day_start_bal)
            if fl is not None and low <= fl + EPS:
                breaches.append(("MAX_DRAWDOWN", f"intra-trade equity bound {low:.2f} <= floor {fl:.2f}",
                                 "intratrade_bound"))
            if dlr is not None and low - day_start_bal <= -dlr["max"] + EPS:
                breaches.append(("DAILY_LOSS", f"intra-trade day P&L bound {low - day_start_bal:.2f} <= "
                                 f"-{dlr['max']:.2f}", "intratrade_bound"))

        # ---- exit: book realized P&L ------------------------------------------------------
        balance = bal_before + float(row["net_usd"])
        net_r += float(row["net_r"])
        n_booked += 1
        n_eod += row["exit_reason"] == "END_OF_DATA"
        d = days[cur_day]
        d["pnl"] = balance - day_start_bal
        d["trades"] += 1
        d["worst_pnl_bound"] = min(d["worst_pnl_bound"], d["pnl"])
        d["end_balance"] = balance
        if not breaches:
            if fl is not None and balance <= fl + EPS:
                breaches.append(("MAX_DRAWDOWN", f"closed balance {balance:.2f} <= floor {fl:.2f}", "end_of_trade"))
            if dlr is not None and d["pnl"] <= -dlr["max"] + EPS:
                breaches.append(("DAILY_LOSS", f"day P&L {d['pnl']:.2f} <= -{dlr['max']:.2f}", "end_of_trade"))
        max_dd_closed = max(max_dd_closed, peak_closed - balance)
        if low is None:
            max_dd_bound = max(max_dd_bound, peak_closed - balance)
        peak_closed = max(peak_closed, balance)
        n_days = sum(1 for x in days.values() if x["trades"])

        for rule, detail, how in breaches:
            violate(rule, row["exit_ts"] if how == "end_of_trade" else f"within [{row['entry_ts']}, {row['exit_ts']}]",
                    no, detail, how)
        rules_hit = {b[0] for b in breaches}
        if "MAX_DRAWDOWN" in rules_hit:
            status, breach_ts = AccountStatus.MAX_DRAWDOWN_VIOLATION, row["exit_ts"]
        elif "DAILY_LOSS" in rules_hit:
            if dlr["action"] == "terminate":
                status, breach_ts = AccountStatus.DAILY_LOSS_VIOLATION, row["exit_ts"]
            else:
                paused_day = xday
                breach_ts = breach_ts or row["exit_ts"]
        elif tgt is not None:
            if balance >= start_bal + tgt["profit"] - EPS:
                target_hit_ts = target_hit_ts or row["exit_ts"]
                share, ok = consistency()
                if n_days >= R["min_trading_days"] and ok:
                    target_ts = target_ts or row["exit_ts"]
                    if tgt["stop_on_target"]:
                        status = AccountStatus.TARGET_REACHED
        fl_after = floor()
        progression.append({
            "trade_no": no, "entry_ts": str(row["entry_ts"]), "exit_ts": str(row["exit_ts"]),
            "entry_day": str(eday), "day": str(xday), "crosses_reset": bool(crosses),
            "contracts": float(row["contracts"]), "net_usd": float(row["net_usd"]), "net_r": float(row["net_r"]),
            "balance": balance, "equity": balance, "peak_balance": peak_closed,
            "drawdown": peak_closed - balance, "intratrade_low_bound": low,
            "day_pnl": d["pnl"],
            "daily_loss_headroom": None if dlr is None else dlr["max"] + d["pnl"],
            "drawdown_floor": fl_after,
            "drawdown_headroom": None if fl_after is None else balance - fl_after,
            "target_progress": None if tgt is None else (balance - start_bal) / tgt["profit"],
            "trading_days": n_days, "status": status.value})
        if status is not AccountStatus.ACTIVE:
            not_processed = len(t) - k - 1
            break
    close_day()

    n_days = sum(1 for x in days.values() if x["trades"])
    share, cons_ok = consistency()
    incomplete_reasons = []
    if status is AccountStatus.ACTIVE:
        if target_ts is not None:            # stop_on_target false: target met, kept trading, no breach
            status = AccountStatus.TARGET_REACHED
        else:
            status = AccountStatus.INCOMPLETE
            if tgt is None:
                incomplete_reasons.append("no profit target configured; period ended without a breach")
            elif balance < start_bal + tgt["profit"] - EPS:
                incomplete_reasons.append("profit target not reached by the end of the period")
            else:
                if n_days < R["min_trading_days"]:
                    incomplete_reasons.append(f"trading days {n_days} < min_trading_days {R['min_trading_days']}")
                if not cons_ok:
                    incomplete_reasons.append("consistency rule not met")
    day_rows = list(days.values())
    first_ts = t["entry_ts"].iloc[0] if len(t) else None

    def elapsed(ts):
        if ts is None or first_ts is None:
            return None
        return {"at": str(ts), "calendar_days": round((pd.Timestamp(ts) - first_ts).total_seconds() / 86400, 4),
                "trading_days": sum(1 for x in day_rows if x["trades"] and x["day"] <= str(trading_days(
                    pd.Series([pd.Timestamp(ts) - pd.Timedelta(1, "ns")]), rules).iloc[0]))}

    pe = R["payout_eligibility"]
    rules_set = {v["rule"] for v in violations}
    summary = {
        "account_id": account_id, "prop_config_id": R["id"], "prop_config_hash": rules.config_hash,
        "synthetic_test_only_rules": R["synthetic_test_only"], "account_start": None if start_ts is None else str(start_ts),
        "status": status.value, "survived": status not in FAILED,
        "starting_balance": start_bal, "ending_balance": balance, "net_pnl_usd": balance - start_bal,
        "net_r": net_r, "trade_count": n_booked, "trades_not_processed": not_processed,
        "trades_skipped_daily_loss_pause": n_paused,
        "target_usd": None if tgt is None else tgt["profit"],
        "profit_target_reached": status is AccountStatus.TARGET_REACHED,
        "profit_target_balance_touched": target_hit_ts is not None,
        "drawdown_breach": "MAX_DRAWDOWN" in rules_set, "daily_loss_breach": "DAILY_LOSS" in rules_set,
        "rule_violation": bool(rules_set - {"MAX_DRAWDOWN", "DAILY_LOSS"}),
        "trading_days": n_days,
        "max_drawdown_usd_closed": max_dd_closed,
        "max_drawdown_usd_intratrade_bound": max_dd_bound if intratrade else None,
        "max_daily_loss_usd_closed": max(0.0, -min((x["pnl"] for x in day_rows), default=0.0)),
        "max_daily_loss_usd_intratrade_bound": (max(0.0, -min((x["worst_pnl_bound"] for x in day_rows), default=0.0))
                                                if intratrade else None),
        "time_to_target": elapsed(target_ts), "time_to_breach": elapsed(breach_ts),
        "violation_reason": "; ".join(f"{v['rule']}: {v['detail']}" for v in violations) or None,
        "violations": violations, "incomplete_reasons": incomplete_reasons,
        "best_day_share_of_profit": share,
        "payout_eligible": (None if pe is None else bool(status not in FAILED and n_days >= pe["min_trading_days"]
                                                         and balance - start_bal >= pe["min_profit"] - EPS)),
        "trades_crossing_reset": n_cross, "trades_end_of_data": n_eod, "detection": R["detection"],
    }
    return {"summary": summary, "progression": progression, "days": day_rows}


# ------------------------------------------------------------------------------------ many accounts
def simulate_accounts(trades: pd.DataFrame, accounts: Sequence[Mapping]) -> dict:
    """``accounts``: [{account_id, rules (PropRules or doc), start?}]. Accounts are independent."""
    if not accounts:
        raise ValueError("at least one account is required")
    ids = [a.get("account_id") for a in accounts]
    if len(set(ids)) != len(ids) or not all(isinstance(i, str) and i for i in ids):
        raise ValueError("account ids must be unique non-empty strings")
    specs = [(a["account_id"], a["rules"] if isinstance(a["rules"], PropRules) else validate_rules(a["rules"]),
              a.get("start")) for a in accounts]
    prepared = prepare_trades(trades, {r["detection"] for _, r, _ in specs})
    out = [simulate_account(prepared, r, aid, s) for aid, r, s in specs]
    return {"simulator_version": SIMULATOR_VERSION, "ordering": ORDERING, "n_source_trades": int(len(prepared)),
            "accounts": out,
            "account_configs": [{"account_id": aid, "prop_config_id": r.id, "prop_config_hash": r.config_hash,
                                 "start": None if s is None else str(s), "rules": r.to_dict()} for aid, r, s in specs]}


def simulation_id(source: Mapping, account_configs: Sequence[Mapping]) -> str:
    """Deterministic: same source run + trades + account configs + simulator -> same id."""
    key = {"run_id": source.get("run_id"), "trades_hash": source.get("trades_hash"),
           "accounts": [(a["account_id"], a["prop_config_hash"], a["start"]) for a in account_configs],
           "simulator": SIMULATOR_VERSION}
    return "PROP_" + hash_obj(key)[:16].upper()


def trades_fingerprint(trades: pd.DataFrame) -> str:
    """Byte-level hash of a trade table (used to prove the source was not mutated)."""
    return hash_obj({c: [str(x) for x in trades[c].tolist()] for c in trades.columns}) if len(trades) else hash_obj([])


__all__ = ["AccountStatus", "PropDataError", "SIMULATOR_VERSION", "ORDERING", "prepare_trades",
           "simulate_account", "simulate_accounts", "simulation_id", "trading_days", "trades_fingerprint"]
