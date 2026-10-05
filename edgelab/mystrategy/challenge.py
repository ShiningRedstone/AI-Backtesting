"""Prop challenge chain of My strategy backtests and the Strategy autotuner (ADR-101).

The backtest's trades go through prop challenge after prop challenge, like a paper account (ADR-81) does on new days:

* an evaluation that fails -> the next challenge starts with the next trade (reset fee, or the evaluation price when no
  reset fee is set);
* an evaluation that passes -> activation fee (if set), the funded account trades on under the profile's funded rules and
  collects every payout; a lost funded account, its live-transition point or its payout limit ends the cycle and a new
  evaluation is bought;
* until the trades run out. A challenge that never got a trade at the end of the data is not bought.

The attempts come from the unchanged lifecycle (``paper.engine.run_attempts`` -> ``prop.lifecycle.simulate_lifecycle``).
Equity-% sizing uses each attempt's own balance: the trades entered from the attempt's start are re-sized from the
profile's starting balance with the ENGINE's own sizing rule (``engine.sizing.size_trade``) and cost model, from the
signal's planned stop exactly as ``run_backtest`` does with ``account.equity_from_ts`` (tests compare both). A trade the
re-sizing would leave at 0 contracts is dropped and counted (``dropped``): the engine would have skipped it and may then
have taken a later signal instead; never seen with a 50K account at 1 %.

The chain is stored WITHOUT fees (``raw``); fees come from Settings -> prop account fees when it is shown (``summary``),
so changing a fee never needs a new backtest. Net = trader payouts - every fee paid.
"""
from __future__ import annotations

import math
from typing import Mapping

import numpy as np
import pandas as pd

CHAIN_VERSION = 1
LABEL = ("Prop challenge chain under the profile's DEFAULT ASSUMED RULES: failed evaluation -> next challenge; passed -> "
         "funded payouts until the funded account ends -> next challenge. Historical result, not a forecast.")


def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


class Resizer:
    """The run's trades entered at or after ``t``, sized as an account starting at ``t`` with ``balance`` would size
    them (equity-% sizing). Fixed-dollar sizing: the trades unchanged."""

    def __init__(self, trades: pd.DataFrame, signals, bars, sizing: Mapping, inst, costs, order):
        from edgelab.engine.backtester import EXIT_ORDER_TYPE
        self.trades = trades.sort_values("trade_no").reset_index(drop=True) if len(trades) else trades
        self.equity_mode = (sizing or {}).get("mode") == "equity_risk"
        self.sizing, self.inst, self.costs, self.order = dict(sizing or {}), inst, costs, order
        self.dropped = 0
        self._entry = pd.to_datetime(self.trades["entry_ts"], utc=True) if len(self.trades) else None
        if self.equity_mode and len(self.trades):
            if costs.spread_source == "dataset":
                raise ValueError("re-sizing needs quote or fixed spreads, not a dataset spread column")
            i = self.trades["signal_bar"].to_numpy(dtype=np.int64)
            d = self.trades["direction"].to_numpy(dtype=np.int64)
            stop = np.asarray(signals.stop_price, dtype=float)[i]
            if order.entry_type != "market":
                planned = np.asarray(signals.entry_price, dtype=float)[i]
            elif costs.spread_source == "quotes":       # the engine's entry side: ASK for buys, BID for sells
                planned = np.where(d > 0, np.asarray(bars.ask_close, dtype=float)[i], np.asarray(bars.close, dtype=float)[i])
            else:
                planned = np.asarray(bars.close, dtype=float)[i]
            self._planned_risk = [abs(p - s) if not math.isnan(s) else order.stop_points for p, s in zip(planned, stop)]
            self._exit_type = [EXIT_ORDER_TYPE[x] for x in self.trades["exit_reason"]]
            self._recs = self.trades.to_dict("records")
            self._ns = [(int(_ts(r["entry_ts"]).value), int(_ts(r["exit_ts"]).value)) for r in self._recs]

    def from_(self, t: pd.Timestamp, balance: float, until: pd.Timestamp | None = None) -> pd.DataFrame:
        if not len(self.trades):
            return self.trades
        keep = (self._entry >= t).to_numpy()
        if until is not None:
            keep = keep & (self._entry < until).to_numpy()
        if not self.equity_mode:
            return self.trades[keep]
        from edgelab.engine.sizing import size_trade
        tr = self.trades
        inst, costs, pv = self.inst, self.costs, self.inst.point_value
        m = costs.multiplier
        equity = float(balance)
        rows, idx = [], np.flatnonzero(keep)
        for k in idx:
            row = self._recs[k]
            sz = size_trade(self.sizing, self._planned_risk[k], inst, equity)
            n = sz.contracts
            if n <= 0:
                self.dropped += 1
                continue
            d = int(row["direction"])
            e_px, x_px = float(row["entry_price_theo"]), float(row["exit_price_theo"])
            base = costs.round_trip_base(str(row["entry_type"]), self._exit_type[k], n, inst, spread_points=None,
                                         entry_price=e_px, exit_price=x_px)
            financing = costs.financing_usd(d, e_px, n, inst, *self._ns[k])
            base_total = (base["commission_usd"] + base["fees_usd"] + base["slippage_usd"] + base["spread_usd"]
                          + financing)
            gross = d * (x_px - e_px) * pv * n
            cost = base_total * m
            risk_usd = float(row["risk_points"]) * pv * n
            new = dict(row)
            new.update(contracts=n, gross_usd=gross, cost_usd=cost, cost_usd_base=base_total, net_usd=gross - cost,
                       risk_usd=risk_usd, gross_r=gross / risk_usd, cost_r=cost / risk_usd, cost_r_base=base_total / risk_usd,
                       net_r=(gross - cost) / risk_usd, commission_usd=base["commission_usd"] * m,
                       fees_usd=base["fees_usd"] * m, slippage_usd=base["slippage_usd"] * m,
                       spread_usd=base["spread_usd"] * m, financing_usd=financing * m, equity_before=equity)
            if "planned_risk_usd" in new:
                new["planned_risk_usd"] = sz.total_risk_usd
            equity += gross - cost
            rows.append(new)
        return pd.DataFrame(rows, columns=tr.columns) if rows else tr.iloc[0:0]


def resizer_for(cfg: Mapping, ds, strat, trades: pd.DataFrame) -> Resizer:
    """The re-sizer of one engine run (``strat`` = the strategy that was run; its signals on ``ds``)."""
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.instruments import contract_for, execution_view
    sizing = dict(strat.sizing)
    inst = ds.instrument
    contract = contract_for(cfg, sizing)
    if sizing.get("contract"):
        inst = execution_view(ds.instrument, contract)
    costs = cost_model_from_config(cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    sig = getattr(strat, "last_signals", None)
    if sig is None and sizing.get("mode") == "equity_risk" and len(trades):
        sig = strat.generate_signals(ds.bars)          # a replay strategy: its stored signals (cheap)
    return Resizer(trades, sig, ds.bars, sizing, inst, costs, strat.order)


def chain(rs: Resizer, profile: Mapping, start, until=None) -> dict:
    """The fee-free attempt chain of one rule profile from ``start`` (trades entered before ``until`` only)."""
    from edgelab.paper.engine import run_attempts
    from edgelab.prop.simulator import PropDataError
    from edgelab.prop.profiles import resolve
    start = _ts(start)
    until = None if until is None else _ts(until)
    balance = float(resolve(profile)["evaluation"]["starting_balance"])
    before = rs.dropped
    try:
        r = run_attempts(lambda t: rs.from_(t, balance, until), profile, {}, start)
    except PropDataError as exc:
        return {"v": CHAIN_VERSION, "error": str(exc)}
    atts = [a for a in r["attempts"]]
    while atts and atts[-1]["n_trades"] == 0:           # a challenge never traded at the end of the data: not bought
        atts.pop()
    return {"v": CHAIN_VERSION, "attempts": [
        {"status": a["status"], "passed": bool(a.get("passed_at")), "payouts": int(a.get("payouts") or 0),
         "trader": round(float(a.get("trader_payout") or 0.0), 2), "trades": int(a["n_trades"]),
         "start": a["start"], "end": a["end"], "reason": a.get("reason")} for a in atts],
        "stopped": r["stopped"], "dropped": rs.dropped - before}


def chains(cfg: Mapping, root, ds, strat, trades: pd.DataFrame, start, profile_ids=None, until=None) -> dict:
    """{profile_id: raw chain} for the registered rule profiles (all, or ``profile_ids``)."""
    from edgelab.prop.service import default_profiles
    rs = resizer_for(cfg, ds, strat, trades)
    contract = (strat.sizing or {}).get("contract")
    out = {}
    for p in default_profiles(root):
        pid = p["profile_id"]
        if profile_ids is not None and pid not in profile_ids:
            continue
        unit = p["rules"]["account.quantity_unit"]["value"] if p.get("schema_version") == 3 else p.get("quantity_unit")
        if unit and contract != unit:
            out[pid] = {"v": CHAIN_VERSION, "error": f"the profile limits {unit} contracts; these trades are {contract}"}
            continue
        out[pid] = chain(rs, p, start, until)
    return out


def summary(raw: Mapping | None, fees: Mapping | None) -> dict | None:
    """Counts and money of one raw chain with the given fees (Settings -> prop account fees, discount applied)."""
    if not raw:
        return None
    if raw.get("error"):
        return {"error": raw["error"]}
    from edgelab.paper.engine import fee_schedule
    fs = fee_schedule(fees)
    atts = raw.get("attempts") or []
    fees_total, missing, prev = 0.0, False, None
    for a in atts:
        new_eval = prev is None or prev != "failed"
        amount = fs["eval_price"] if new_eval else (fs["reset_fee"] if fs["reset_fee"] is not None else fs["eval_price"])
        if amount is None:
            missing = True
        else:
            fees_total += amount
        if a["passed"] and fs["activation_fee"] is not None:
            fees_total += fs["activation_fee"]
        prev = a["status"]
    passes = sum(1 for a in atts if a["passed"])
    trader = round(sum(a["trader"] for a in atts), 2)
    last = atts[-1]["status"] if atts else None
    return {"challenges": len(atts), "fails": sum(1 for a in atts if a["status"] == "failed"), "passes": passes,
            "funded_lost": sum(1 for a in atts if a["status"] == "funded_lost"),
            "funded_completed": sum(1 for a in atts if a["status"] == "funded_completed"),
            "payouts": sum(a["payouts"] for a in atts), "trader_payouts": trader,
            "avg_payout_per_pass": round(trader / passes, 2) if passes else None,
            "fees_total": round(fees_total, 2), "fees_complete": not missing,
            "net": round(trader - fees_total, 2) if not missing else None,
            "open_at_end": last in ("in_progress", "funded"), "incompatible": last == "incompatible",
            "stopped": raw.get("stopped"), "dropped": int(raw.get("dropped") or 0)}
