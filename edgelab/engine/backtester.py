"""Single-strategy backtester.

Pipeline:  ValidatedDataset --(causality check)--> signals --> per-signal
entry/exit simulation (engine.fills) --> costs & R accounting --> BacktestResult.

Gates (the engine refuses to run otherwise):
  * data must be a ValidatedDataset whose hash still matches its manifest;
  * the strategy must pass the empirical causality (lookahead) check;
  * every signal must carry a protective stop.

R definition: 1R = |fill - stop| x point value x contracts (risk at the actual
theoretical fill, before costs). gross_R, cost_R and net_R are all in that unit,
so tight-stop strategies show their true cost burden.

Quote model (ADR-55): ``single_series`` (default) triggers and fills every order on the dataset's
one OHLC series (its ``price_basis``); spread, if any, is a separate cost. ``directional_bid_ask``
(cost ``spread_source: quotes``, opt-in) fills buys on the stored ASK OHLC and sells on BID, so gross
P&L is already after the spread and no separate spread is charged. It refuses datasets without
complete, finite ASK OHLC; ASK is never inferred from BID and spread.
"""
from __future__ import annotations

import dataclasses
import math
from collections import Counter
from dataclasses import dataclass, field
from typing import Any, Mapping

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_arrays
from edgelab.core.logging import get_logger
from edgelab.data.calendar import _minutes
from edgelab.data.resample import map_intrabar
from edgelab.data.validation import ValidatedDataset
from edgelab.engine.costs import CostModel
from edgelab.engine.fills import (Entry, FillPolicy, IntrabarData, MarketArrays, find_entry,
                                  simulate_exit, simulate_exit_trailing)
from edgelab.engine.signals import (CausalityReport, Strategy, check_causality,
                                    validate_signals)
from edgelab.engine.sizing import DEFAULT_RESEARCH_ACCOUNT, check_quantity, size_trade
from edgelab.instruments import ExecutionContractError, Instrument, contract_summary, execution_view

log = get_logger("backtester")
NS_PER_MIN = 60_000_000_000
EXIT_ORDER_TYPE = {"STOP": "stop", "STOP_GAP": "stop", "TARGET": "limit", "TARGET_GAP": "limit",
                   "TRAIL_STOP": "stop", "TRAIL_STOP_GAP": "stop", "NO_PROGRESS": "market", "TIME": "market", "SESSION_CLOSE": "market", "MAX_HOLD": "market",
                   "END_OF_DATA": "market", "SIGNAL": "market"}


class BacktestError(RuntimeError):
    pass


@dataclass
class BacktestResult:
    strategy_id: str
    strategy_spec: dict
    trades: pd.DataFrame
    skipped: dict[str, int]
    n_signals: int
    assumptions: dict[str, Any]
    dataset: dict[str, Any]
    causality: CausalityReport | None
    intrabar: dict[str, Any] = field(default_factory=dict)

    @property
    def trades_hash(self) -> str:
        if self.trades.empty:
            return hash_arrays(np.array([]))
        cols = ["entry_bar", "exit_bar", "direction", "contracts", "entry_price_theo",
                "exit_price_theo", "net_usd"]
        return hash_arrays(*(self.trades[c].to_numpy(np.float64) for c in cols))


def _spread_treatment(costs: CostModel) -> str:
    if costs.spread_source == "quotes":
        return "embedded_in_quotes"
    if costs.spread_source == "dataset":
        return "cost_avg_entry_exit"
    return "fixed_cost" if costs.spread_points else "none"


def build_market_arrays(ds: ValidatedDataset, bt_cfg: Mapping) -> MarketArrays:
    b, cal = ds.bars, ds.calendar
    ts = b.ts
    td = cal.trading_dates(ts).astype(np.int64)
    loc = cal.local(ts)
    since_open = (np.asarray(loc.hour * 60 + loc.minute) - cal.open_min) % 1440
    sess = bt_cfg.get("session", {})
    force = np.zeros(len(b), bool)
    if sess.get("flatten_daily", True) and sess.get("flatten_time"):
        flat_since = (_minutes(sess["flatten_time"]) - cal.open_min) % 1440
        force |= since_open + b.tf_minutes >= flat_since
    if not sess.get("hold_overnight", False):
        last_of_day = np.r_[td[1:] != td[:-1], True]
        force |= last_of_day
    entry_allowed = ~force
    return MarketArrays(b.open, b.high, b.low, b.close, force, entry_allowed, td)


def _ask_bars(b):
    """The ASK side as a BarArrays view (OHLC = observed ASK OHLC), for map_intrabar."""
    from edgelab.data.schema import BarArrays
    return BarArrays(b.ts_ns, b.ask_open, b.ask_high, b.ask_low, b.ask_close, b.volume, b.tf_minutes)


def _intrabar(ds: ValidatedDataset, ltf: ValidatedDataset | None, directional: bool = False
              ) -> tuple[IntrabarData | None, IntrabarData | None, dict]:
    """(bid_or_single_side, ask_side_or_None, info). Directional replay needs BOTH sides in the
    lower-timeframe data; an HTF bar is reliable only if both sides reproduce it exactly."""
    if ltf is None:
        return None, None, {"available": False, "reason": "no lower-timeframe data supplied"}
    ltf.verify_unchanged()
    if ltf.instrument.symbol != ds.instrument.symbol:
        raise BacktestError("intrabar data is for a different instrument")
    if ltf.bars.tf_minutes >= ds.bars.tf_minutes or ds.bars.tf_minutes % ltf.bars.tf_minutes:
        raise BacktestError("intrabar timeframe must be a strict divisor of the bar timeframe")
    lb = ltf.bars
    if directional and not lb.has_ask_ohlc:
        return None, None, {"available": False, "ltf_dataset_id": ltf.manifest.dataset_id,
                            "ltf_has_ask_ohlc": False,
                            "reason": "directional BID/ASK replay needs ASK OHLC in the lower-timeframe "
                                      "dataset; it has none (never inferred) - fallback policy applies"}
    start, end, reliable = map_intrabar(ds.bars, lb)
    ask = None
    if directional:
        _, _, rel_ask = map_intrabar(_ask_bars(ds.bars), _ask_bars(lb))
        reliable = reliable & rel_ask
        ask = IntrabarData(lb.ask_open, lb.ask_high, lb.ask_low, lb.ask_close, start, end, reliable)
    info = {"available": True, "ltf_minutes": lb.tf_minutes,
            "ltf_dataset_id": ltf.manifest.dataset_id,
            "reliable_bar_fraction": float(reliable.mean()) if len(reliable) else 0.0}
    if directional:
        info.update({"ltf_has_ask_ohlc": True, "quote_sides": "entry replay on the entry side (buy: ask, "
                     "sell: bid), exit replay on the exit side; reliable only where BID and ASK both reproduce the bar"})
    return IntrabarData(lb.open, lb.high, lb.low, lb.close, start, end, reliable), ask, info


def _check_quotes(bars) -> None:
    if not bars.has_ask_ohlc:
        raise BacktestError("cost model uses spread_source quotes (directional BID/ASK execution), but this "
                            "dataset has no ASK OHLC - import the ASK feed's OHLC (never inferred from spread)")
    for k in ("ask_open", "ask_high", "ask_low", "ask_close"):
        bad = ~np.isfinite(getattr(bars, k))
        if bad.any():
            raise BacktestError(f"directional BID/ASK execution: {int(bad.sum())} non-finite {k} value(s) "
                                f"(first at bar {int(np.argmax(bad))}); refusing rather than treating them as 'no trigger'")


def run_backtest(ds: ValidatedDataset, strategy: Strategy, costs: CostModel, bt_cfg: Mapping,
                 sizing: Mapping | None = None, ltf: ValidatedDataset | None = None,
                 contract: Instrument | None = None, account: Mapping | None = None) -> BacktestResult:
    """``contract`` (ADR-63): the execution contract named by ``sizing["contract"]`` (from configs/instruments.yaml).
    A strategy that names a contract and is run without its spec is REFUSED, never silently sized on the data series."""
    if not isinstance(ds, ValidatedDataset):
        raise BacktestError("run_backtest requires a ValidatedDataset (see validate_and_freeze)")
    ds.verify_unchanged()
    if ds.report.status == "FAIL":
        raise BacktestError("dataset failed validation")
    sizing = dict(sizing or {"mode": "fixed", "contracts": 1})
    inst, bars, order = ds.instrument, ds.bars, strategy.order
    contract_name = sizing.get("contract")
    if contract_name:
        if contract is None or contract.symbol != contract_name:
            raise BacktestError(f"sizing names the execution contract {contract_name!r} but its specification was not "
                                "supplied (configs/instruments.yaml); refusing to size on the data series instead")
        try:
            inst = execution_view(ds.instrument, contract)
        except ExecutionContractError as exc:
            raise BacktestError(str(exc)) from None

    causality = None
    if bt_cfg.get("require_causality_check", True):
        causality = check_causality(strategy, bars, n_cuts=bt_cfg.get("causality_cuts", 20))
        if not causality.passed:
            log.event("causality_failed", severity="ERROR", strategy=strategy.strategy_id,
                      error=causality.detail)
            raise BacktestError(f"LOOKAHEAD DETECTED: {causality.detail}")

    sig = strategy.generate_signals(bars)
    validate_signals(sig, len(bars), order)

    if costs.spread_source == "dataset" and bars.spread is None:
        raise BacktestError("cost model uses dataset spread, but this dataset has no spread column")
    directional = costs.spread_source == "quotes"
    if directional:
        _check_quotes(bars)
    if costs.status == "unconfigured":
        raise BacktestError("cost model is unconfigured")
    requested = bt_cfg.get("same_bar_policy", "conservative")
    pol = FillPolicy(same_bar=requested, fallback=bt_cfg.get("intrabar_fallback", "conservative"),
                     target_gap_fill=bt_cfg.get("gap_fill", {}).get("target", "limit_price"),
                     limit_penetration=bt_cfg.get("limit_fill", {}).get("penetration_ticks", 0)
                     * inst.tick_size,
                     allow_next_session_entry=bt_cfg.get("entry", {}).get("allow_next_session_entry", False))
    ib, ib_ask, ib_info = (_intrabar(ds, ltf, directional) if requested == "intrabar" else
                           (None, None, {"available": False, "reason": "policy is not intrabar"}))
    A = build_market_arrays(ds, bt_cfg)
    basis = ds.manifest.price_basis
    if directional:     # same session masks; OHLC = observed ASK
        A_ask = dataclasses.replace(A, o=bars.ask_open, h=bars.ask_high, l=bars.ask_low, c=bars.ask_close)
        sides = {1: (A_ask, A, ib_ask, ib, "ask", "bid"), -1: (A, A_ask, ib, ib_ask, "bid", "ask")}
    else:
        sides = {d_: (A, A, ib, ib, basis, basis) for d_ in (1, -1)}
    cfg_cap = bt_cfg.get("max_trades_per_day")
    max_per_day = min([c for c in (cfg_cap, sig.max_trades_per_day) if c]) if (cfg_cap or sig.max_trades_per_day) else None
    if "starting_equity" in sizing:
        raise BacktestError("sizing carries starting_equity, but the account size is a RUN parameter (account=), "
                            "not part of a strategy's sizing or identity")
    acct = dict(account) if account is not None else dict(DEFAULT_RESEARCH_ACCOUNT)
    if not (acct.get("starting_equity", 0) > 0):
        raise BacktestError("account.starting_equity must be > 0")
    equity = float(acct["starting_equity"]) if sizing.get("mode") == "equity_risk" else None
    # ADR-81 (paper accounts only): equity starts counting at this time; trades entered earlier never change it.
    # Absent (every research backtest) -> exactly the previous behaviour.
    eq_from = acct.get("equity_from_ts")
    if eq_from is None:
        eq_from_ns = None
    else:
        _t = pd.Timestamp(eq_from)
        eq_from_ns = int((_t.tz_convert("UTC") if _t.tzinfo else _t.tz_localize("UTC")).value)
    last_exit_bar, blocked_days = -10**12, set()
    STOP_REASONS, TARGET_REASONS = ("STOP", "STOP_GAP", "TRAIL_STOP", "TRAIL_STOP_GAP"), ("TARGET", "TARGET_GAP")
    ts_ns, tf_ns = bars.ts_ns, np.int64(bars.tf_minutes) * NS_PER_MIN
    pv, tick = inst.point_value, inst.tick_size

    skipped: Counter = Counter()
    rows: list[dict] = []
    busy_until = -1
    per_day: Counter = Counter()
    sig_idx = np.flatnonzero(sig.direction)
    exit_idx = {1: None if sig.exit_long is None else np.flatnonzero(sig.exit_long),
                -1: None if sig.exit_short is None else np.flatnonzero(sig.exit_short)}
    for i in sig_idx:
        i = int(i)
        d = int(sig.direction[i])
        if i <= busy_until:
            skipped["BUSY_IN_POSITION_OR_ORDER"] += 1
            continue
        if max_per_day and per_day[A.td[i]] >= max_per_day:
            skipped["MAX_TRADES_PER_DAY"] += 1
            continue
        if sig.exit_cooldown_bars and i - last_exit_bar < sig.exit_cooldown_bars:
            skipped["REENTRY_COOLDOWN"] += 1
            continue
        if sig.block_after and A.td[i] in blocked_days:
            skipped["REENTRY_BLOCKED"] += 1
            continue
        stop_abs, tgt_abs, level = sig.stop_price[i], sig.target_price[i], sig.entry_price[i]
        # --- sizing at signal time (no knowledge of the fill) -----------------------
        A_ent, A_ex, ib_ent, ib_ex, side_in, side_out = sides[d]
        planned_entry = level if order.entry_type != "market" else A_ent.c[i]
        planned_risk = abs(planned_entry - stop_abs) if not math.isnan(stop_abs) else order.stop_points
        try:
            sz = size_trade(sizing, planned_risk, inst, equity)
        except ValueError as exc:
            if not contract_name:
                raise
            raise BacktestError(str(exc)) from None
        if sz.contracts <= 0:
            skipped["SIZE_ZERO"] += 1
            continue
        if contract_name:
            try:
                check_quantity(sz.contracts, inst, "executed quantity")
            except ValueError as exc:
                raise BacktestError(str(exc)) from None
        eq_before = equity
        # --- entry ------------------------------------------------------------------
        e: Entry = find_entry(A_ent, i, d, order.entry_type, level, order.entry_expiry_bars, pol)
        if not e.filled:
            skipped[e.reason] += 1
            busy_until = max(busy_until, e.last_bar - 1)
            continue
        stop = stop_abs if not math.isnan(stop_abs) else e.price - d * order.stop_points
        if not math.isnan(tgt_abs):
            target = tgt_abs
        elif order.target_points is not None:
            target = e.price + d * order.target_points
        else:
            target = math.nan
        risk_pts = d * (e.price - stop)
        if not risk_pts > 0:
            skipped["STOP_BEYOND_FILL"] += 1   # optimistic edge case, disclosed in assumptions
            continue
        if not math.isnan(target) and not d * (target - e.price) > 0:
            skipped["TARGET_BEYOND_FILL"] += 1
            continue
        sx = None
        flags = exit_idx[d]
        if flags is not None and len(flags):
            j = int(np.searchsorted(flags, e.bar))        # first exit flag at/after the entry bar
            if j < len(flags):
                sx = int(flags[j]) + 1                    # executes at the next bar's open
        if sig.trail is None:
            x = simulate_exit(A_ex, ib_ex, pol, e, d, stop, target, order.time_exit_bars,
                              order.max_hold_bars, order.entry_type, level, signal_exit_bar=sx,
                              ent=A_ent if directional else None, ib_ent=ib_ent if directional else None)
        else:
            x = simulate_exit_trailing(A_ex, ib_ex, pol, e, d, stop, target, order.time_exit_bars,
                                       order.max_hold_bars, order.entry_type, level, sig.trail, sig.trail_atr,
                                       sig.trail_level_long if d > 0 else sig.trail_level_short, i,
                                       signal_exit_bar=sx, ent=A_ent if directional else None,
                                       ib_ent=ib_ent if directional else None)
        busy_until = x["exit_bar"] - 1
        per_day[A.td[i]] += 1
        last_exit_bar = x["exit_bar"]
        if sig.block_after and (sig.block_after == "any" or x["exit_reason"] in
                                (STOP_REASONS if sig.block_after == "stop" else TARGET_REASONS)):
            blocked_days.add(A.td[x["exit_bar"]])

        # --- costs & R ----------------------------------------------------------------
        n_c = sz.contracts
        exit_type = EXIT_ORDER_TYPE[x["exit_reason"]]
        k_exit = x["exit_bar"]
        spread_used = None
        if costs.spread_source == "dataset":
            sp = 0.5 * (bars.spread[e.bar] + bars.spread[k_exit])
            if not math.isfinite(sp):
                raise BacktestError(f"dataset spread missing at bar {e.bar} or {k_exit}; "
                                    "cannot charge spread for this trade")
            spread_used = float(sp)
        base = costs.round_trip_base(e.order_type, exit_type, n_c, inst, spread_points=spread_used,
                                     entry_price=e.price, exit_price=x["exit_price_theo"])
        financing = costs.financing_usd(d, e.price, n_c, inst, int(ts_ns[e.bar]),
                                        int(ts_ns[k_exit] + tf_ns))
        base_total = (base["commission_usd"] + base["fees_usd"] + base["slippage_usd"]
                      + base["spread_usd"] + financing)
        m = costs.multiplier
        entry_slip = costs.slippage_points(e.order_type, inst) * m
        exit_slip = costs.slippage_points(exit_type, inst) * m
        gross_usd = d * (x["exit_price_theo"] - e.price) * pv * n_c
        cost_usd = base_total * m
        risk_usd = risk_pts * pv * n_c
        k = x["exit_bar"]
        if equity is not None and (eq_from_ns is None or int(ts_ns[e.bar]) >= eq_from_ns):
            equity += gross_usd - cost_usd          # realised net P&L: known to every LATER signal, never to this one
        rows.append({
            "signal_bar": i, "signal_ts": pd.Timestamp(int(ts_ns[i] + tf_ns), tz="UTC"),
            "entry_bar": e.bar, "entry_ts": pd.Timestamp(int(ts_ns[e.bar]), tz="UTC"),
            "exit_bar": k, "exit_ts": pd.Timestamp(int(ts_ns[k] + tf_ns), tz="UTC"),
            "direction": d, "contracts": n_c, "entry_type": order.entry_type,
            "entry_fill_kind": e.kind, "entry_quote_side": side_in, "exit_quote_side": side_out,
            "entry_price_theo": e.price, "entry_price_eff": e.price + d * entry_slip,
            "stop_price": stop, "target_price": target,
            "exit_price_theo": x["exit_price_theo"], "exit_price_eff": x["exit_price_theo"] - d * exit_slip,
            "exit_reason": x["exit_reason"], "conflict_resolution": x["conflict_resolution"],
            "risk_points": risk_pts, "risk_usd": risk_usd,
            "gross_usd": gross_usd,
            "commission_usd": base["commission_usd"] * m, "fees_usd": base["fees_usd"] * m,
            "slippage_usd": base["slippage_usd"] * m, "spread_usd": base["spread_usd"] * m,
            "financing_usd": financing * m,
            "cost_usd": cost_usd, "cost_usd_base": base_total, "net_usd": gross_usd - cost_usd,
            "gross_r": gross_usd / risk_usd, "cost_r": cost_usd / risk_usd,
            "cost_r_base": base_total / risk_usd, "net_r": (gross_usd - cost_usd) / risk_usd,
            "bars_held": k - e.bar + 1, "holding_minutes": (k - e.bar + 1) * bars.tf_minutes,
            "mfe_points": x["mfe_points"], "mae_points": x["mae_points"],
            "mfe_r": x["mfe_points"] / risk_pts, "mae_r": x["mae_points"] / risk_pts,
            **({"final_stop_price": x["final_stop"], "trail_updates": x["trail_updates"]}
               if sig.trail is not None else {}),
            **({"equity_before": eq_before} if equity is not None else {}),
            **({"planned_risk_usd": sz.total_risk_usd} if contract_name else {}),
        })

    trades = pd.DataFrame(rows)
    if not trades.empty:
        trades.insert(0, "trade_no", np.arange(1, len(trades) + 1))
    effective = requested
    if requested == "intrabar" and not ib_info.get("available"):
        effective = f"intrabar unavailable -> {pol.fallback}"
    if directional:
        buy, sell = "on ASK (buy)", "on BID (sell)"
        ent_s, ex_s = " - long: ASK, short: BID", " - long: BID, short: ASK"
    else:
        buy = sell = ""
        ent_s = ex_s = f" - on the single {basis} series"
    assumptions = {
        "timestamp_convention": "bar open, UTC; bar known at its close",
        "signal_timing": "signal at bar close; earliest fill on the next bar",
        "market_entry": f"open of next bar + slippage{ent_s}",
        "stop_entry": f"level, or open if gapped through; + stop slippage{ent_s}",
        "limit_entry": f"level, or open if gapped through; needs {bt_cfg.get('limit_fill', {}).get('penetration_ticks', 0)} tick(s) penetration; no slippage{ent_s}",
        "stop_exit": f"stop level; open if gapped through (worse); + stop slippage{ex_s}",
        "target_exit": f"limit at target; gap-through fill = {pol.target_gap_fill}{ex_s}",
        "close_exit": f"session close / time / max hold / end of data at the bar close{ex_s}",
        "signal_exit": f"open of the next bar{ex_s}",
        "quote_model": "directional_bid_ask" if directional else "single_series",
        "execution_sides": {"buy": "ask", "sell": "bid"} if directional else {"buy": basis, "sell": basis},
        "spread_treatment": _spread_treatment(costs),
        "dataset_has_ask_ohlc": bool(bars.has_ask_ohlc),
        "gross_pnl": ("directional quote fill prices: gross is AFTER the bid/ask spread; no separate spread "
                      "cost; cost sensitivity / breakeven scale commission, fees, slippage and financing only"
                      if directional else "single-series fill prices; spread (if any) is a separate cost"),
        "same_bar_policy_requested": requested,
        "same_bar_policy_effective": effective,
        "intrabar_fallback": pol.fallback,
        "entry_bar_uncertainty": ("touches not provably after a stop/limit fill are treated as conflicts"
                                  + ("; two-sided: a stop touch on a level-fill bar is never provable, a target "
                                     "touch only if the entry side had to pass the fill level to reach it"
                                     if directional else "")),
        "time_exit": "close of the Nth bar, entry bar counts as bar 1",
        "session": dict(bt_cfg.get("session", {})),
        "positions": "one position or working order at a time; no pyramiding",
        "max_trades_per_day": max_per_day,
        "sizing": sizing,
        "costs": costs.to_dict(),
        "cost_status": costs.status,
        "r_unit": "|fill - stop| x point value x contracts (theoretical fill, before costs)",
        "holding_time": "bar resolution (bars_held x timeframe)",
        "excursions": "bar resolution; entry bar included in full",
        "stop_beyond_fill": "signals whose absolute stop is already breached at fill are skipped "
                            "(optimistic); count reported in skipped",
        "end_of_data": "open position closed at final bar close (reason END_OF_DATA)",
    }
    if contract_name:
        assumptions["execution_contract"] = {
            **contract_summary(contract, ds.instrument),
            "sizing_reference": "contracts = floor(risk budget / (PLANNED stop points x point value)), decided at the "
                                "signal from the signal-bar close (or the order level) and the initial stop; the realised "
                                "initial risk at the fill (risk_usd) can differ by the entry gap; planned_risk_usd is the "
                                "sized figure and never exceeds the budget"}
    if sizing.get("mode") == "equity_risk":
        assumptions["equity_sizing"] = {
            "rule": "risk budget = risk_pct % of equity at the SIGNAL; equity = starting_equity + net P&L (after costs) "
                    "of trades already exited; quantity from the planned initial stop, rounded down, optional cap",
            "account": acct.get("name", "run parameter"), "starting_equity": acct["starting_equity"],
            "final_equity": equity,
            "path_dependent": "sizes depend on earlier trades of THIS run (a different window or start changes them)"}
        if eq_from_ns is not None:                  # ADR-81 paper accounts only
            assumptions["equity_sizing"]["equity_from_ts"] = pd.Timestamp(eq_from_ns, tz="UTC").isoformat()
    if sig.max_trades_per_day or sig.exit_cooldown_bars or sig.block_after:
        assumptions["strategy_trade_management"] = {
            "max_trades_per_day": sig.max_trades_per_day, "exit_cooldown_bars": sig.exit_cooldown_bars,
            "block_after": sig.block_after,
            "rule": "counted on executed trades; re-entry rules use only exits that have already happened"}
    if sig.trail is not None:
        assumptions["trailing_stop"] = {
            "spec": dataclasses.asdict(sig.trail),
            "timing": "decided at a bar's close from bars <= k, effective from bar k+1; never loosened",
            "extreme": "favorable extreme on the exit side of the quote model, from the fill price",
            "exit_reasons": "TRAIL_STOP / TRAIL_STOP_GAP when the stop had moved, else STOP / STOP_GAP",
            "risk_unit": "R stays |fill - INITIAL stop| (the trailed stop does not change risk_points)"}
    res = BacktestResult(strategy.strategy_id, strategy.spec, trades, dict(skipped),
                         int(len(sig_idx)), assumptions, ds.manifest.to_dict(), causality, ib_info)
    log.event("backtest_complete", strategy=strategy.strategy_id, trades=len(trades),
              signals=len(sig_idx), dataset=ds.manifest.dataset_id)
    return res
