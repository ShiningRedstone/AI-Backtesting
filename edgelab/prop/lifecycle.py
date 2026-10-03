"""Prop-account LIFECYCLE simulation over a completed backtest trade stream (ADR-64..66): evaluation -> funded -> payouts ->
live-transition eligibility.

A DOWNSTREAM AUDIT: a pure function of (trades, rule profile). It never changes the strategy, its trades or the base
backtest; it states what the configured account rules would have done to that exact trade sequence. Every number comes from
the versioned schema-3 profile (``edgelab.prop.profiles``: each rule = value + VERIFIED / ASSUMED_DEFAULT / CUSTOM + basis);
this module contains NO provider values. An ASSUMED_DEFAULT rule is an ACTIVE rule: it is checked like any other, and the
result says the outcome holds UNDER DEFAULT ASSUMED RULES (never a confirmed provider outcome).

The trade stream is processed CHRONOLOGICALLY (a breach earlier in the sequence ends the account even if the final profit
exceeds the target). Every rule looks only at trades already booked; an end-of-day figure is used only at that day's end.

Semantics (all parameters are profile rules; EdgeLab conventions are recorded on each result):

* Trades are taken exactly as recorded (size, net P&L after costs). Nothing is resized, clipped or re-costed. Positions must not
  overlap. A trade is booked at its exit, on its exit trading day (each stage has its own day boundary).
* Quantity: permitted micros for a day = ``max_micros`` or, with scaling, the tier fixed at the END of the previous session
  (never intraday). A trade requesting more is recorded (requested, permitted, rule) and the account is INCOMPATIBLE.
* Drawdown: ``eod_trailing`` floor = highest end-of-day balance - max_loss, raised only at a day's end, never lowered; with a
  lock, once the highest EOD balance reaches start + lock_trigger_offset the floor locks at start + locked_floor_offset.
  ``static`` floor = start - max_loss. Measurement ``intratrade_worst_price`` also compares the worst equity inside each trade
  (balance - MAE$, the bar/quote model's worst executable price: an EdgeLab execution-model bound, not a provider-certified
  intrabar rule); ``closed_balance`` compares closed balances only.
* DLL (if enabled): day P&L <= -amount, measured on closed trades (``closed_trades_only``) or also on the worst price inside a
  trade (``unrealized_worst_price`` / ``intraday_worst_price``, same bar-resolution bound). ``soft``: no further trades that day
  (the base backtest keeps them), the account continues next day. ``hard``: the account fails.
* Consistency: best single trading day / profit of the window <= percent (plus the configured cushion, reported separately).
  Evaluation: checked for the pass (if unmet the account keeps trading). Funded: checked at each payout request.
* Evaluation pass (end of day): profit >= target AND consistency AND minimum trading days AND no breach so far.
* Funded: a SEPARATE account from the end of the pass day (trades entered after that moment), funded starting balance.
* Payouts (end of day): frequency (N winning days >= threshold, or daily), positive cycle profit, balance requirement,
  consistency. Maximum = min(formula, cap, balance - (start + buffer)); paid if >= minimum. The gross leaves the balance;
  the trader receives split_trader of it; the cycle resets. Scaling is recalculated from the post-payout balance at session end.

Result states (per stage and overall): PASS, FAIL, INCOMPATIBLE, NOT_APPLICABLE; ``rule_basis_state`` RULE_ASSUMED when any
active rule is an ASSUMED_DEFAULT. IN_PROGRESS exists only inside the simulation: an evaluation not passed when the data ends is
FAIL (reason NOT_PASSED_BY_END_OF_DATA); a funded account not breached when the data ends is PASS (ACTIVE_AT_END_OF_DATA).
"""
from __future__ import annotations

from typing import Any, Mapping

import pandas as pd

from edgelab.prop.profiles import profile_identity, resolve, rule_basis
from edgelab.prop.simulator import PropDataError, trading_days

LIFECYCLE_VERSION = "prop_lifecycle_v3"
EPS = 1e-9
STATES = ("PASS", "FAIL", "INCOMPATIBLE", "NOT_APPLICABLE")
REASON_TEXT = {"MAX_LOSS_LIMIT": "Max Loss Limit", "DAILY_LOSS_LIMIT": "Daily Loss Limit (hard)",
               "SIZE_LIMIT_VIOLATION": "Contract limit exceeded", "SCALING_LIMIT_VIOLATION": "Funded scaling limit exceeded",
               "NOT_PASSED_BY_END_OF_DATA": "Evaluation not passed by the end of the data"}
INCOMPATIBLE = ("SIZE_LIMIT_VIOLATION", "SCALING_LIMIT_VIOLATION")


def _iso(ts) -> str | None:
    return None if ts is None else pd.Timestamp(ts).isoformat()


def _money(x: float) -> str:
    return f"${x:,.0f}" if abs(x - round(x)) < 1e-9 else f"${x:,.2f}"


def _r(x: float) -> float:
    return round(float(x), 9)


def _rules(profile: Mapping) -> Mapping:
    return resolve(profile) if profile.get("schema_version") == 3 else profile


# ------------------------------------------------------------------------------------ input
def _needs_intratrade(R: Mapping) -> bool:
    return any(R[s]["drawdown"]["measurement"] == "intratrade_worst_price"
               or (R[s]["dll"]["enabled"] and R[s]["dll"]["measurement"] != "closed_trades_only")
               for s in ("evaluation", "funded"))


def prepare(trades: pd.DataFrame, profile: Mapping) -> pd.DataFrame:
    R = _rules(profile)
    need = ["entry_ts", "exit_ts", "contracts", "net_usd"]
    if "trade_no" not in trades.columns:
        trades = trades.assign(trade_no=range(1, len(trades) + 1))
    need.append("trade_no")
    intratrade = _needs_intratrade(R)
    if intratrade:
        need += ["mae_points", "risk_points", "risk_usd"]
    miss = [c for c in need if c not in trades.columns]
    if miss:
        raise PropDataError(f"trade records lack required columns {miss}; cannot simulate these rules")
    t = trades[need].copy()
    for c in ("entry_ts", "exit_ts"):
        t[c] = pd.to_datetime(t[c], utc=True)
    if t[["entry_ts", "exit_ts", "net_usd", "contracts"]].isna().any().any():
        raise PropDataError("trade records contain missing timestamps, sizes or P&L")
    if (t["exit_ts"] < t["entry_ts"]).any():
        raise PropDataError("a trade exits before it enters")
    t = t.sort_values(["exit_ts", "entry_ts", "trade_no"], kind="mergesort").reset_index(drop=True)
    if len(t) > 1 and (t["entry_ts"].to_numpy()[1:] < t["exit_ts"].to_numpy()[:-1]).any():
        raise PropDataError("positions overlap in time; a joint equity path is not recorded")
    for s in ("evaluation", "funded"):
        rules = {"trading_day": R[s]["day_boundary"]}
        t[f"{s}_day"] = trading_days(t["exit_ts"] - pd.Timedelta(nanoseconds=1), rules).to_numpy() if len(t) else []
    if intratrade:
        if not (t["risk_points"] > 0).all():
            raise PropDataError("risk_points must be > 0 to convert MAE points to USD")
        t["mae_usd"] = t["mae_points"] * t["risk_usd"] / t["risk_points"]     # MAE points x point value x contracts
    else:
        t["mae_usd"] = 0.0
    return t


def quantity_stats(trades: pd.DataFrame) -> dict:
    if trades is None or len(trades) == 0:
        return {"n_trades": 0, "min": None, "max": None, "mean": None, "median": None, "distribution": {}}
    c = pd.to_numeric(trades["contracts"])
    dist = {f"{k:g}": int(v) for k, v in c.value_counts().sort_index().items()}
    return {"n_trades": int(len(c)), "min": float(c.min()), "max": float(c.max()), "mean": float(c.mean()),
            "median": float(c.median()), "distribution": dist}


def payout_cap(profile: Mapping, n: int) -> float | None:
    """The configured cap for payout number ``n`` (None = no cap)."""
    po = _rules(profile)["payout"]
    mode = po["cap_mode"]
    if mode == "none":
        return None
    if mode == "fixed":
        return float(po["cap_schedule"][0])
    if mode == "by_payout_number":
        sch = po["cap_schedule"]
        return float(sch[min(n, len(sch)) - 1])
    pdate = str(_rules(profile)["purchase_date"])
    rows = sorted(po["cap_table"], key=lambda r: str(r["purchased_on_or_after"]))
    chosen = [r for r in rows if str(r["purchased_on_or_after"]) <= pdate]
    if not chosen:
        raise PropDataError(f"no payout cap is configured for purchase_date {pdate}")
    return float(chosen[-1]["amount"])


# ------------------------------------------------------------------------------------ one stage account
class _Account:
    """Balance / drawdown-floor state of one stage (evaluation or funded)."""

    def __init__(self, stage: str, cfg: Mapping, start: float | None = None):
        self.stage, self.cfg = stage, cfg
        self.start = float(cfg["starting_balance"]) if start is None else float(start)
        self.balance = self.peak = self.start
        d = cfg["drawdown"]
        self.max_loss, self.mode = float(d["max_loss"]), d["mode"]
        self.lock_trigger = None if d["lock_trigger_offset"] is None else self.start + float(d["lock_trigger_offset"])
        self.locked_floor = None if d["locked_floor_offset"] is None else self.start + float(d["locked_floor_offset"])
        self.locked = False
        self.inclusive = d["breach_comparison"] == "at_or_below"
        self.intratrade = d["measurement"] == "intratrade_worst_price"
        self.floor = self.start - self.max_loss

    def eod_update(self) -> None:
        self.peak = max(self.peak, self.balance)
        if self.mode != "eod_trailing" or self.locked:
            return
        if self.lock_trigger is not None and self.peak >= self.lock_trigger - EPS:
            self.floor, self.locked = max(self.floor, self.locked_floor), True
        else:
            self.floor = max(self.floor, self.peak - self.max_loss)          # the floor never moves down

    def breached(self, equity: float) -> bool:
        return equity <= self.floor + EPS if self.inclusive else equity < self.floor - EPS

    def state(self, **kw) -> dict:
        return {"balance": _r(self.balance), "floor": _r(self.floor), "highest_eod_balance": _r(self.peak),
                "floor_locked": self.locked, **kw}


def _tier(sc: Mapping, level: float) -> int:
    chosen = sc["start_micros"]
    for t in sc["tiers"]:
        if level >= float(t["min_profit"]) - EPS:
            chosen = t["micros"]
    return chosen


def _consistency(c: Mapping, day_pnls: list[float], profit: float) -> dict:
    share = float(c["percent"]) / 100.0
    best = max([x for x in day_pnls if x > 0], default=0.0)
    allowance = float(c["cushion"]["amount_usd"]) + float(c["cushion"]["percent_points"]) / 100.0 * max(profit, 0.0)
    normal = profit > EPS and best <= share * profit + EPS
    cushioned = profit > EPS and best <= share * profit + allowance + EPS
    return {"percent": float(c["percent"]), "window": c["window"], "best_day": _r(best), "profit": _r(profit),
            "ratio": _r(best / profit) if profit > EPS else None, "normal_rule_met": normal,
            "cushion_allowance": _r(allowance), "met_with_cushion": cushioned, "met": cushioned,
            "profit_needed_normal_rule": _r(best / share) if share else None}


# ------------------------------------------------------------------------------------ simulation
def simulate_lifecycle(trades: pd.DataFrame, profile: Mapping) -> dict:
    ident = profile_identity(profile)
    base = {"profile": ident, "simulator_version": LIFECYCLE_VERSION, "conventions": dict(profile["conventions"])}
    if profile.get("schema_version") != 3:
        msg = "NOT_APPLICABLE - superseded profile schema (no per-rule value/status/basis); no outcome is claimed"
        return {**base, "status": "NOT_APPLICABLE", "final_status": msg, "headline": [msg]}
    rb = rule_basis(profile)
    R = resolve(profile)
    t = prepare(trades, profile)
    out = _Run(R, rb, t).run()
    basis = {k: rb[k] for k in ("verified_rule_count", "assumed_rule_count", "custom_rule_count", "inactive_rule_count",
                                "basis_state", "label", "assumed_rules", "custom_rules")}
    return {**base, "rule_basis": basis, "rule_basis_state": rb["basis_state"], **out}


class _Run:
    def __init__(self, R: Mapping, rb: Mapping, t: pd.DataFrame):
        self.p, self.rb, self.t = R, rb, t
        self.events: list[dict] = []
        self.payouts: list[dict] = []
        self.days_eval: list[dict] = []
        self.days_funded: list[dict] = []

    def ev(self, stage: str, typ: str, day, ts=None, **kw) -> None:
        self.events.append({"stage": stage, "type": typ, "date": str(day), "ts": _iso(ts), **kw})

    # ---- one trading day inside one stage; returns (day_record, breach | None)
    def _day(self, acct: _Account, day, rows: pd.DataFrame, limit: float, limit_src: str) -> tuple[dict, dict | None]:
        stage = acct.stage
        day_start = acct.balance
        rec = {"date": str(day), "stage": stage, "n_trades": 0, "n_skipped_dll": 0, "day_pnl": 0.0,
               "max_contracts_requested": 0.0, "permitted_micros": limit, "permitted_source": limit_src,
               "balance_start": _r(day_start), "floor_start": _r(acct.floor)}
        dll = acct.cfg["dll"]
        dll_intra = dll["enabled"] and dll["measurement"] != "closed_trades_only"
        paused = False

        def breach(reason, ts, r, detail, **kw):
            rec["balance_end"] = _r(acct.balance)
            return rec, {"reason": reason, "stage": stage, "date": str(day), "ts": _iso(ts), "trade_no": int(r.trade_no),
                         "detail": detail, "state_at_breach": acct.state(day_pnl=_r(acct.balance - day_start),
                                                                         permitted_micros=limit), **kw}

        for r in rows.itertuples():
            q = float(r.contracts)
            rec["max_contracts_requested"] = max(rec["max_contracts_requested"], q)
            if paused:
                rec["n_skipped_dll"] += 1
                self.ev(stage, "TRADE_NOT_TAKEN_DLL_SOFT", day, r.entry_ts, trade_no=int(r.trade_no))
                continue
            if q > float(limit) + EPS:
                reason = "SCALING_LIMIT_VIOLATION" if limit_src == "scaling" else "SIZE_LIMIT_VIOLATION"
                return breach(reason, r.entry_ts, r, f"requested {q:g} micros, permitted {float(limit):g} micros ({limit_src})",
                              requested_quantity=q, permitted_quantity=float(limit),
                              rule=f"{stage}.{'scaling' if limit_src == 'scaling' else 'max_micros'}")
            worst = acct.balance - float(r.mae_usd)
            if acct.intratrade and acct.breached(worst):
                return breach("MAX_LOSS_LIMIT", r.exit_ts, r,
                              f"worst equity inside the trade {worst:,.2f} reached the floor {acct.floor:,.2f} "
                              "(EdgeLab bar/quote worst-price bound; breach time lies within the trade)",
                              entry_ts=_iso(r.entry_ts))
            dll_hit_intra = dll_intra and (worst - day_start) <= -float(dll["amount"]) + EPS
            if dll_hit_intra and dll["behavior"] == "hard":
                return breach("DAILY_LOSS_LIMIT", r.exit_ts, r,
                              f"worst day P&L inside the trade {worst - day_start:,.2f} <= -{float(dll['amount']):,.2f}")
            acct.balance += float(r.net_usd)
            rec["n_trades"] += 1
            rec["day_pnl"] = _r(acct.balance - day_start)
            if acct.breached(acct.balance):
                return breach("MAX_LOSS_LIMIT", r.exit_ts, r, f"closed balance {acct.balance:,.2f} reached the floor {acct.floor:,.2f}")
            hit = dll["enabled"] and (dll_hit_intra or rec["day_pnl"] <= -float(dll["amount"]) + EPS)
            if hit:
                if dll["behavior"] == "hard":
                    return breach("DAILY_LOSS_LIMIT", r.exit_ts, r, f"day P&L {rec['day_pnl']:,.2f} <= -{float(dll['amount']):,.2f}")
                paused = True
                rec["dll_soft_breach"] = True
                self.ev(stage, "DLL_SOFT_BREACH", day, r.exit_ts, day_pnl=rec["day_pnl"], amount=float(dll["amount"]),
                        measurement=dll["measurement"])
        rec["last_exit_ts"] = _iso(rows["exit_ts"].max()) if len(rows) else None
        return rec, None

    def _limit(self, cfg: Mapping, tier: int | None) -> tuple[float, str]:
        lim, src = float(cfg["max_micros"]), "max_micros"
        if cfg["scaling"]["enabled"] and tier is not None and tier < lim:
            lim, src = float(tier), "scaling"
        return lim, src

    def _day_end_ts(self, stage: str, day) -> pd.Timestamp:
        td = self.p[stage]["day_boundary"]
        return pd.Timestamp(f"{day} {td['reset_time']}", tz=td["timezone"])

    def _next_tier(self, sc: Mapping, acct: _Account, paid_out: float, prev: int | None) -> int:
        level = acct.balance - acct.start + (paid_out if sc["basis"] == "cumulative_profit_incl_payouts" else 0.0)
        t = _tier(sc, level)
        return max(t, prev) if sc["persist"] and prev is not None else t

    def run(self) -> dict:
        ev_cfg = self.p["evaluation"]
        groups = [(d, g) for d, g in self.t.groupby("evaluation_day", sort=True)]
        acct = _Account("evaluation", ev_cfg)
        sc = ev_cfg["scaling"]
        tier = sc["start_micros"] if sc["enabled"] else None
        cons = ev_cfg["consistency"]
        passed, fail, target_noted = None, None, False
        for day, rows in groups:
            lim, src = self._limit(ev_cfg, tier)
            rec, br = self._day(acct, day, rows, lim, src)
            self.days_eval.append(rec)
            if br:
                fail = br
                self.ev("evaluation", "BREACH", day, pd.Timestamp(br["ts"]), reason=br["reason"], detail=br["detail"])
                break
            acct.eod_update()
            rec.update(balance_end=_r(acct.balance), highest_eod_balance=_r(acct.peak), floor=_r(acct.floor))
            profit = acct.balance - acct.start
            n_days = sum(1 for d in self.days_eval if d["n_trades"] > 0)
            c = _consistency(cons, [d["day_pnl"] for d in self.days_eval], profit) if cons["enabled"] else None
            rec["consistency"] = c
            ok_days = n_days >= float(ev_cfg["minimum_trading_days"])
            if profit >= float(ev_cfg["profit_target"]) - EPS:
                if (c is None or c["met"]) and ok_days:
                    end = self._day_end_ts("evaluation", day)
                    passed = {"date": str(day), "ts": rec["last_exit_ts"], "day_end_ts": end.isoformat(), "day_end": end,
                              "profit": _r(profit), "consistency": c, "trading_days": n_days}
                    self.ev("evaluation", "PASS", day, pd.Timestamp(rec["last_exit_ts"]), profit=_r(profit))
                    break
                unmet = [w for w, bad in (("CONSISTENCY", c is not None and not c["met"]),
                                          ("MINIMUM_TRADING_DAYS", not ok_days)) if bad]
                self.ev("evaluation", "TARGET_REACHED_NOT_PASSED", day, None, unmet=unmet, profit=_r(profit),
                        consistency=c, trading_days=n_days)
                target_noted = True
            if sc["enabled"]:
                tier = self._next_tier(sc, acct, 0.0, tier)
        if passed:
            ev_status, reason = "PASS", None
        elif fail:
            ev_status, reason = ("INCOMPATIBLE" if fail["reason"] in INCOMPATIBLE else "FAIL"), fail["reason"]
        else:
            ev_status, reason = "FAIL", "NOT_PASSED_BY_END_OF_DATA"
        result: dict[str, Any] = {}
        result["evaluation"] = {
            "status": ev_status, "failure_reason": reason, "breach": fail,
            "pass": {k: v for k, v in passed.items() if k != "day_end"} if passed else None,
            "profit": _r(acct.balance - acct.start), "target": float(ev_cfg["profit_target"]),
            "target_reached_but_not_passed": target_noted and not passed,
            "balance": _r(acct.balance), "highest_eod_balance": _r(acct.peak), "floor": _r(acct.floor),
            "trading_days": sum(1 for d in self.days_eval if d["n_trades"] > 0),
            "consistency": (self.days_eval[-1].get("consistency") if self.days_eval else None),
            "dll_soft_breaches": sum(1 for d in self.days_eval if d.get("dll_soft_breach")),
            "max_contracts_requested": max([d["max_contracts_requested"] for d in self.days_eval], default=0.0),
            "days": self.days_eval}
        result["funded"] = self._funded(passed) if passed else {"started": False, "status": "NOT_APPLICABLE"}
        result["payouts"] = self.payouts
        gross = sum(x["gross"] for x in self.payouts)
        trader = sum(x["trader_share"] for x in self.payouts)
        result["totals"] = {"n_payouts": len(self.payouts), "gross_payouts": _r(gross), "trader_payout": _r(trader),
                            "firm_share": _r(gross - trader)}
        result["events"] = self.events
        result["quantity"] = quantity_stats(self.t)
        f = result["funded"]
        result["status"] = ("INCOMPATIBLE" if "INCOMPATIBLE" in (ev_status, f["status"]) else
                            "FAIL" if "FAIL" in (ev_status, f["status"]) else "PASS")
        result["final_status"], result["headline"] = self._final(result)
        result["summary"] = self._summary(result)
        return result

    # ---------------- funded stage + payouts
    def _funded(self, passed: Mapping) -> dict:
        fu, po, lt = self.p["funded"], self.p["payout"], self.p["live_transition"]
        start = float(fu["starting_balance"]) + (passed["profit"] if fu["carry_evaluation_profit"] else 0.0)
        acct = _Account("funded", fu, start)
        act = passed["day_end"]
        rest = self.t[self.t["entry_ts"] >= act.tz_convert("UTC")]
        groups = [(d, g) for d, g in rest.groupby("funded_day", sort=True)]
        sc = fu["scaling"]
        tier = sc["start_micros"] if sc["enabled"] else None
        paid_out = 0.0
        cycle = {"start": None, "start_balance": acct.balance, "day_pnls": [], "winning": []}
        res: dict[str, Any] = {"started": True, "start_ts": passed["day_end_ts"],
                               "activation": {"rule": "next_trading_day", "after_pass_date": passed["date"],
                                              "activation_ts": passed["day_end_ts"]},
                               "starting_balance": _r(acct.balance), "status": "PASS", "outcome": "ACTIVE_AT_END_OF_DATA",
                               "breach": None, "live_transition_eligible": False, "payout_checks": []}
        split = float(po["split_trader"])
        for day, rows in groups:
            if cycle["start"] is None:
                cycle["start"] = str(day)
            lim, src = self._limit(fu, tier)
            rec, br = self._day(acct, day, rows, lim, src)
            self.days_funded.append(rec)
            if br:
                res.update(status="INCOMPATIBLE" if br["reason"] in INCOMPATIBLE else "FAIL", outcome="BREACH", breach=br)
                self.ev("funded", "BREACH", day, pd.Timestamp(br["ts"]), reason=br["reason"], detail=br["detail"])
                break
            acct.eod_update()
            rec.update(balance_end=_r(acct.balance), highest_eod_balance=_r(acct.peak), floor=_r(acct.floor))
            if rec["n_trades"] > 0:
                cycle["day_pnls"].append(rec["day_pnl"])
                if rec["day_pnl"] >= float(po["winning_day_threshold"]) - EPS:
                    cycle["winning"].append(str(day))
            limit_reached = po["count_limit"] is not None and len(self.payouts) >= int(po["count_limit"])
            if po["enabled"] and not limit_reached and rec["n_trades"] > 0:
                chk = self._payout_check(acct, cycle, day, paid_out)
                rec["payout_check"] = chk
                if chk["frequency_met"]:
                    res["payout_checks"].append({k: chk[k] for k in ("date", "eligible", "unmet", "max_permitted")})
                if chk["eligible"]:
                    gross = chk["gross"]
                    acct.balance -= gross
                    paid_out += gross
                    if po["after_payout_drawdown"] == "reset_to_balance_minus_max_loss":
                        acct.peak, acct.floor, acct.locked = acct.balance, acct.balance - acct.max_loss, False
                    elif po["after_payout_drawdown"] == "floor_at_starting_balance":
                        acct.floor = max(acct.floor, acct.start)
                    n = len(self.payouts) + 1
                    trader = _r(gross * split)
                    self.payouts.append({
                        "n": n, "date": str(day), "eligibility_ts": rec["last_exit_ts"], "cycle_start": cycle["start"],
                        "winning_days": list(cycle["winning"]), "cycle_profit": chk["cycle_profit"],
                        "formula_amount": chk["formula_amount"], "cap": chk["cap"], "withdrawable": chk["withdrawable"],
                        "max_permitted": chk["max_permitted"], "minimum": float(po["minimum"]), "request": po["request"],
                        "gross": _r(gross), "trader_share": trader, "firm_share": _r(gross - trader),
                        "balance_before": _r(acct.balance + gross), "balance_after": _r(acct.balance),
                        "floor_after": _r(acct.floor), "consistency": chk["consistency"]})
                    self.ev("funded", "PAYOUT", day, pd.Timestamp(rec["last_exit_ts"]), n=n, gross=_r(gross), trader=trader)
                    cycle = {"start": None, "start_balance": acct.balance, "day_pnls": [], "winning": []}
                    if acct.breached(acct.balance):
                        br = {"reason": "MAX_LOSS_LIMIT", "stage": "funded", "date": str(day), "ts": rec["last_exit_ts"],
                              "trade_no": None, "detail": f"balance after payout {acct.balance:,.2f} reached the floor "
                                                          f"{acct.floor:,.2f}", "state_at_breach": acct.state()}
                        res.update(status="FAIL", outcome="BREACH", breach=br)
                        break
                    if lt["rule"] == "after_payout_count" and n >= int(lt["payout_count"]):
                        res.update(outcome="LIVE_TRANSITION_ELIGIBLE", live_transition_eligible=True)
                        break
                    if po["count_limit"] is not None and n >= int(po["count_limit"]):
                        res["outcome"] = "PAYOUT_COUNT_LIMIT_REACHED"
                        break
            if sc["enabled"]:                        # the tier for the NEXT session, from the end of THIS one
                tier = self._next_tier(sc, acct, paid_out, tier)
            rec["next_session_permitted_micros"] = self._limit(fu, tier)[0]
        res.update({"balance": _r(acct.balance), "highest_eod_balance": _r(acct.peak), "floor": _r(acct.floor),
                    "floor_locked": acct.locked, "trading_days": sum(1 for d in self.days_funded if d["n_trades"] > 0),
                    "dll_soft_breaches": sum(1 for d in self.days_funded if d.get("dll_soft_breach")),
                    "next_session_permitted_micros": self._limit(fu, tier)[0], "days": self.days_funded,
                    "max_contracts_requested": max([d["max_contracts_requested"] for d in self.days_funded], default=0.0)})
        return res

    def _payout_check(self, acct: _Account, cycle: Mapping, day, paid_out: float) -> dict:
        po, fu = self.p["payout"], self.p["funded"]
        cyc_profit = acct.balance - cycle["start_balance"]
        freq_met = po["frequency_mode"] == "daily" or len(cycle["winning"]) >= int(po["winning_days_required"])
        unmet = []
        if not freq_met:
            unmet.append("WINNING_DAYS")
        if po["positive_cycle_profit_required"] and cyc_profit <= EPS:
            unmet.append("POSITIVE_CYCLE_PROFIT")
        if po["balance_requirement"] is not None and acct.balance < float(po["balance_requirement"]) - EPS:
            unmet.append("BALANCE_REQUIREMENT")
        cons = None
        if fu["consistency"]["enabled"]:
            c = fu["consistency"]
            if c["window"] == "payout_cycle":
                cons = _consistency(c, cycle["day_pnls"], cyc_profit)
            else:
                cons = _consistency(c, [d["day_pnl"] for d in self.days_funded if d["n_trades"] > 0],
                                    acct.balance - acct.start + paid_out)
            if not cons["met"]:
                unmet.append("CONSISTENCY")
        basis = cyc_profit if po["formula_profit_basis"] == "cycle_net_profit" else acct.balance - acct.start
        formula = float(po["formula_factor"]) * max(basis, 0.0)
        cap = payout_cap(self.p, len(self.payouts) + 1)
        withdrawable = None if po["buffer_offset"] is None else acct.balance - (acct.start + float(po["buffer_offset"]))
        max_perm = max(min([x for x in (formula, cap, withdrawable) if x is not None]), 0.0)
        eligible = not unmet
        if eligible and max_perm < float(po["minimum"]) - EPS:
            unmet.append("BELOW_MINIMUM_PAYOUT")
            eligible = False
        gross = (max_perm if po["request"] == "max_allowed" else float(po["minimum"])) if eligible else 0.0
        if freq_met and not eligible:
            self.ev("funded", "PAYOUT_NOT_ELIGIBLE", day, None, unmet=unmet, max_permitted=_r(max_perm))
        return {"date": str(day), "frequency_met": freq_met, "eligible": eligible, "unmet": unmet,
                "cycle_profit": _r(cyc_profit), "formula_amount": _r(formula), "cap": cap,
                "withdrawable": None if withdrawable is None else _r(withdrawable), "max_permitted": _r(max_perm),
                "gross": _r(gross), "consistency": cons}

    # ---- readable result
    def _final(self, r: Mapping) -> tuple[str, list[str]]:
        p, label = self.p, self.rb["label"]
        lines = [f"{p['product']} {int(p['account_size']) // 1000}K ({p['provider']}):"]
        ev, f = r["evaluation"], r["funded"]
        if ev["status"] != "PASS":
            detail = (ev["breach"] or {}).get("detail") if ev["status"] == "INCOMPATIBLE" else REASON_TEXT[ev["failure_reason"]]
            if ev["target_reached_but_not_passed"] and ev["failure_reason"] == "NOT_PASSED_BY_END_OF_DATA":
                detail += " (target reached, other pass conditions unmet)"
            core = f"EVAL {ev['status']} {label} — {detail}"
            lines.append(core)
        else:
            lines.append(f"EVAL PASS {label}")
            for x in r["payouts"]:
                lines.append(f"PAYOUT {x['n']} — gross {_money(x['gross'])}, trader {_money(x['trader_share'])}, "
                             f"firm {_money(x['firm_share'])}, balance after {_money(x['balance_after'])}")
            if f["status"] == "PASS":
                extra = {"ACTIVE_AT_END_OF_DATA": "no breach through the end of the data",
                         "LIVE_TRANSITION_ELIGIBLE": "live transition ELIGIBLE per the configured rule (not guaranteed; "
                                                     "live rules not simulated)",
                         "PAYOUT_COUNT_LIMIT_REACHED": "payout count limit reached"}[f["outcome"]]
                lines.append(f"FUNDED PASS {label} — {extra}")
            else:
                b = f["breach"]
                why = b["detail"] if f["status"] == "INCOMPATIBLE" else REASON_TEXT[b["reason"]]
                lines.append(f"FUNDED {f['status']} {label} — {why}")
            lines.append(f"TOTAL TRADER PAYOUT — {_money(r['totals']['trader_payout'])} "
                         f"(gross {_money(r['totals']['gross_payouts'])}, firm {_money(r['totals']['firm_share'])})")
            core = f"EVAL PASS / FUNDED {f['status']} / {len(r['payouts'])} PAYOUT(S) {label}"
        lines.append(f"RULE BASIS — {self.rb['verified_rule_count']} verified, {self.rb['assumed_rule_count']} assumed, "
                     f"{self.rb['custom_rule_count']} custom")
        if self.rb["assumed_rule_count"]:
            lines.append("Simulation used EdgeLab default assumptions for the ASSUMED_DEFAULT rules (not confirmed provider "
                         "rules): " + ", ".join(sorted(self.rb["assumed_rules"])))
        return core, lines

    def _summary(self, r: Mapping) -> dict:
        """The flat per-profile fields every strategy result exposes (no score, no ranking)."""
        p, ev, f = self.p, r["evaluation"], r["funded"]
        started = bool(f.get("started"))
        eb, fb = ev["breach"] or {}, (f.get("breach") or {}) if started else {}
        return {"rule_profile_id": p["profile_id"], "rule_profile_version": p["version"],
                "account_size": p["account_size"], "contract": p["quantity_unit"], "purchase_date": p["purchase_date"],
                "verified_rule_count": self.rb["verified_rule_count"], "assumed_rule_count": self.rb["assumed_rule_count"],
                "custom_rule_count": self.rb["custom_rule_count"], "rule_basis_state": self.rb["basis_state"],
                "lifecycle_status": r["status"],
                "evaluation_status": ev["status"], "evaluation_pass_timestamp": (ev["pass"] or {}).get("ts"),
                "evaluation_failure_timestamp": eb.get("ts"), "evaluation_failure_reason": ev["failure_reason"],
                "evaluation_failure_detail": eb.get("detail"), "state_at_evaluation_breach": eb.get("state_at_breach"),
                "funded_status": f["status"], "funded_start_timestamp": f.get("start_ts"),
                "funded_failure_timestamp": fb.get("ts"), "funded_failure_reason": fb.get("reason"),
                "funded_failure_detail": fb.get("detail"), "state_at_funded_breach": fb.get("state_at_breach"),
                "funded_outcome": f.get("outcome"),
                "payout_count": len(r["payouts"]), "payout_dates": [x["date"] for x in r["payouts"]],
                "payout_eligibility_timestamps": [x["eligibility_ts"] for x in r["payouts"]],
                "payout_gross_amounts": [x["gross"] for x in r["payouts"]],
                "payout_trader_amounts": [x["trader_share"] for x in r["payouts"]],
                "total_trader_payout": r["totals"]["trader_payout"], "total_firm_share": r["totals"]["firm_share"],
                "final_balance": f["balance"] if started else ev["balance"],
                "final_stage": "funded" if started else "evaluation",
                "live_transition_eligible": bool(f.get("live_transition_eligible")),
                "final_status": r["final_status"]}


def evaluate_profiles(trades: pd.DataFrame, profiles: list[Mapping]) -> list[dict]:
    """One independent lifecycle result per profile (no combined score, no ranking)."""
    return [simulate_lifecycle(trades, pr) for pr in profiles]


def compact(result: Mapping) -> dict:
    """The per-strategy record stored with a run: status, summary, rule basis, payouts and the exact rule-profile version
    (day-by-day rows and events are dropped; ``simulate_lifecycle`` on the stored trades reproduces the detail)."""
    out = {k: result[k] for k in ("profile", "simulator_version", "status", "rule_basis_state", "final_status", "headline",
                                  "conventions", "summary") if k in result}
    if "evaluation" not in result:
        return out
    rb = result["rule_basis"]
    out["rule_basis"] = {k: rb[k] for k in ("verified_rule_count", "assumed_rule_count", "custom_rule_count",
                                            "inactive_rule_count", "basis_state", "label")}
    out["rule_basis"]["assumed_rules"] = sorted(rb["assumed_rules"])
    out["rule_basis"]["custom_rules"] = sorted(rb["custom_rules"])
    ev, f = result["evaluation"], result["funded"]
    out["evaluation"] = {k: ev[k] for k in ("status", "failure_reason", "breach", "pass", "profit", "trading_days",
                                            "consistency", "dll_soft_breaches", "max_contracts_requested",
                                            "target_reached_but_not_passed")}
    out["funded"] = {k: f.get(k) for k in ("started", "status", "outcome", "start_ts", "breach", "balance", "trading_days",
                                           "dll_soft_breaches", "max_contracts_requested", "live_transition_eligible",
                                           "next_session_permitted_micros")}
    out["payouts"] = [{k: x[k] for k in ("n", "date", "eligibility_ts", "cycle_profit", "max_permitted", "gross",
                                         "trader_share", "firm_share", "balance_after")} for x in result["payouts"]]
    out["totals"] = result["totals"]
    return out


__all__ = ["simulate_lifecycle", "evaluate_profiles", "compact", "prepare", "quantity_stats", "payout_cap",
           "LIFECYCLE_VERSION", "STATES"]
