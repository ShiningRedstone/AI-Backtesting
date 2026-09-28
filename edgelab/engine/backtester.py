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
"""
from __future__ import annotations

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
                                  simulate_exit)
from edgelab.engine.signals import (CausalityReport, Strategy, check_causality,
                                    validate_signals)
from edgelab.engine.sizing import size_trade

log = get_logger("backtester")
NS_PER_MIN = 60_000_000_000
EXIT_ORDER_TYPE = {"STOP": "stop", "STOP_GAP": "stop", "TARGET": "limit", "TARGET_GAP": "limit",
                   "TIME": "market", "SESSION_CLOSE": "market", "MAX_HOLD": "market",
                   "END_OF_DATA": "market"}


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


def _intrabar(ds: ValidatedDataset, ltf: ValidatedDataset | None) -> tuple[IntrabarData | None, dict]:
    if ltf is None:
        return None, {"available": False, "reason": "no lower-timeframe data supplied"}
    ltf.verify_unchanged()
    if ltf.instrument.symbol != ds.instrument.symbol:
        raise BacktestError("intrabar data is for a different instrument")
    if ltf.bars.tf_minutes >= ds.bars.tf_minutes or ds.bars.tf_minutes % ltf.bars.tf_minutes:
        raise BacktestError("intrabar timeframe must be a strict divisor of the bar timeframe")
    start, end, reliable = map_intrabar(ds.bars, ltf.bars)
    lb = ltf.bars
    info = {"available": True, "ltf_minutes": lb.tf_minutes,
            "ltf_dataset_id": ltf.manifest.dataset_id,
            "reliable_bar_fraction": float(reliable.mean()) if len(reliable) else 0.0}
    return IntrabarData(lb.open, lb.high, lb.low, lb.close, start, end, reliable), info


def run_backtest(ds: ValidatedDataset, strategy: Strategy, costs: CostModel, bt_cfg: Mapping,
                 sizing: Mapping | None = None, ltf: ValidatedDataset | None = None) -> BacktestResult:
    if not isinstance(ds, ValidatedDataset):
        raise BacktestError("run_backtest requires a ValidatedDataset (see validate_and_freeze)")
    ds.verify_unchanged()
    if ds.report.status == "FAIL":
        raise BacktestError("dataset failed validation")
    sizing = dict(sizing or {"mode": "fixed", "contracts": 1})
    inst, bars, order = ds.instrument, ds.bars, strategy.order

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
    if costs.status == "unconfigured":
        raise BacktestError("cost model is unconfigured")
    requested = bt_cfg.get("same_bar_policy", "conservative")
    pol = FillPolicy(same_bar=requested, fallback=bt_cfg.get("intrabar_fallback", "conservative"),
                     target_gap_fill=bt_cfg.get("gap_fill", {}).get("target", "limit_price"),
                     limit_penetration=bt_cfg.get("limit_fill", {}).get("penetration_ticks", 0)
                     * inst.tick_size,
                     allow_next_session_entry=bt_cfg.get("entry", {}).get("allow_next_session_entry", False))
    ib, ib_info = _intrabar(ds, ltf) if requested == "intrabar" else (None, {"available": False,
                                                                           "reason": "policy is not intrabar"})
    A = build_market_arrays(ds, bt_cfg)
    max_per_day = bt_cfg.get("max_trades_per_day")
    ts_ns, tf_ns = bars.ts_ns, np.int64(bars.tf_minutes) * NS_PER_MIN
    pv, tick = inst.point_value, inst.tick_size

    skipped: Counter = Counter()
    rows: list[dict] = []
    busy_until = -1
    per_day: Counter = Counter()
    sig_idx = np.flatnonzero(sig.direction)
    for i in sig_idx:
        i = int(i)
        d = int(sig.direction[i])
        if i <= busy_until:
            skipped["BUSY_IN_POSITION_OR_ORDER"] += 1
            continue
        if max_per_day and per_day[A.td[i]] >= max_per_day:
            skipped["MAX_TRADES_PER_DAY"] += 1
            continue
        stop_abs, tgt_abs, level = sig.stop_price[i], sig.target_price[i], sig.entry_price[i]
        # --- sizing at signal time (no knowledge of the fill) -----------------------
        planned_entry = level if order.entry_type != "market" else bars.close[i]
        planned_risk = abs(planned_entry - stop_abs) if not math.isnan(stop_abs) else order.stop_points
        sz = size_trade(sizing, planned_risk, inst)
        if sz.contracts <= 0:
            skipped["SIZE_ZERO"] += 1
            continue
        # --- entry ------------------------------------------------------------------
        e: Entry = find_entry(A, i, d, order.entry_type, level, order.entry_expiry_bars, pol)
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
        x = simulate_exit(A, ib, pol, e, d, stop, target, order.time_exit_bars,
                          order.max_hold_bars, order.entry_type, level)
        busy_until = x["exit_bar"] - 1
        per_day[A.td[i]] += 1

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
        base = costs.round_trip_base(e.order_type, exit_type, n_c, inst, spread_points=spread_used)
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
        rows.append({
            "signal_bar": i, "signal_ts": pd.Timestamp(int(ts_ns[i] + tf_ns), tz="UTC"),
            "entry_bar": e.bar, "entry_ts": pd.Timestamp(int(ts_ns[e.bar]), tz="UTC"),
            "exit_bar": k, "exit_ts": pd.Timestamp(int(ts_ns[k] + tf_ns), tz="UTC"),
            "direction": d, "contracts": n_c, "entry_type": order.entry_type,
            "entry_fill_kind": e.kind,
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
        })

    trades = pd.DataFrame(rows)
    if not trades.empty:
        trades.insert(0, "trade_no", np.arange(1, len(trades) + 1))
    effective = requested
    if requested == "intrabar" and not ib_info.get("available"):
        effective = f"intrabar unavailable -> {pol.fallback}"
    assumptions = {
        "timestamp_convention": "bar open, UTC; bar known at its close",
        "signal_timing": "signal at bar close; earliest fill on the next bar",
        "market_entry": "open of next bar + slippage",
        "stop_entry": "level, or open if gapped through; + stop slippage",
        "limit_entry": f"level, or open if gapped through; needs {bt_cfg.get('limit_fill', {}).get('penetration_ticks', 0)} tick(s) penetration; no slippage",
        "stop_exit": "stop level; open if gapped through (worse); + stop slippage",
        "target_exit": f"limit at target; gap-through fill = {pol.target_gap_fill}",
        "same_bar_policy_requested": requested,
        "same_bar_policy_effective": effective,
        "intrabar_fallback": pol.fallback,
        "entry_bar_uncertainty": "touches not provably after a stop/limit fill are treated as conflicts",
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
    res = BacktestResult(strategy.strategy_id, strategy.spec, trades, dict(skipped),
                         int(len(sig_idx)), assumptions, ds.manifest.to_dict(), causality, ib_info)
    log.event("backtest_complete", strategy=strategy.strategy_id, trades=len(trades),
              signals=len(sig_idx), dataset=ds.manifest.dataset_id)
    return res
