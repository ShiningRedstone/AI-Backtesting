"""Phase 1 demonstration: a complete, reproducible synthetic backtest.

Run from the project root:   python scripts/phase1_demo.py

What it shows:
  1. environment + config
  2. synthetic NQ-like data -> validation report (and a corrupted copy being refused)
  3. storage with content hashes
  4. lookahead gate (a peeking strategy is rejected)
  5. a breakout backtest with realistic costs and intrabar conflict resolution
  6. the same thing vs. a random-entry null model
  7. a known-answer check (gambler's ruin) proving the fill engine is unbiased
  8. run registry + bit-for-bit reproduction
  9. benchmark

The data is a DRIFTLESS RANDOM WALK: no strategy can have a real edge on it.
A correct engine must therefore report no edge. That is the point.
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from edgelab.analytics.metrics import (breakeven_cost_multiplier, compute_metrics,  # noqa: E402
                                       cost_sensitivity, format_metrics)
from edgelab.core.config import config_hash, load_config  # noqa: E402
from edgelab.core.identity import code_version, environment_fingerprint  # noqa: E402
from edgelab.core.logging import configure_logging  # noqa: E402
from edgelab.data.calendar import load_calendars  # noqa: E402
from edgelab.data.resample import resample_bars  # noqa: E402
from edgelab.data.store import open_store  # noqa: E402
from edgelab.data.synthetic import generate_bars  # noqa: E402
from edgelab.data.validation import DataIntegrityError, validate_and_freeze  # noqa: E402
from edgelab.engine.backtester import BacktestError, run_backtest  # noqa: E402
from edgelab.engine.costs import cost_model_from_config  # noqa: E402
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy  # noqa: E402
from edgelab.instruments import load_instruments  # noqa: E402
from edgelab.research.runs import record_run  # noqa: E402
from edgelab.strategies.examples import Breakout, RandomEntry, _window_mask  # noqa: E402

OUT = ROOT / "reports" / "output"


def h(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


class PeekNextClose(Strategy):
    family = "peek_next_close"

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        with np.errstate(invalid="ignore"):
            s.direction[np.r_[bars.close[1:], np.nan] > bars.close] = 1
        return s


def main() -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    configure_logging(level="WARNING", json_file=ROOT / cfg["logging"]["json_file"], force=True)
    inst = load_instruments(cfg)["NQ"]
    cal = load_calendars(cfg)[inst.calendar]
    costs = cost_model_from_config(cfg, "NQ")

    h("1. ENVIRONMENT & CONFIG")
    env = environment_fingerprint()
    print("  " + "  ".join(f"{k}={v}" for k, v in env.items() if k != "platform"))
    cv = code_version()
    print(f"  code: git {str(cv['git_commit'])[:10]}  source sha256 {cv['source_sha256'][:12]}")
    print(f"  config hash {config_hash(cfg)[:12]}   same-bar policy: {cfg['backtest']['same_bar_policy']}")
    print(f"  NQ: tick {inst.tick_size} = ${inst.tick_value}, point value ${inst.point_value:g}; "
          f"costs/side: comm ${costs.commission_per_side} + fees ${costs.fees_per_side}, "
          f"slippage {costs.slippage_ticks_market} tick market/stop")

    h("2. SYNTHETIC DATA + VALIDATION GATE")
    t0 = time.perf_counter()
    df1, truth = generate_bars(cal, "2024-01-02", "2024-07-01", tf_minutes=1, sigma_per_bar=3.0,
                               session_gap_sigma=8.0, seed=20240101)
    gen_s = time.perf_counter() - t0
    print(f"  generated {len(df1):,} 1m bars in {gen_s:.2f}s  (driftless walk, sigma 3 pts/min, "
          "overnight gaps, seed 20240101)")
    t0 = time.perf_counter()
    ds1 = validate_and_freeze(df1, inst, cal, "1m", 1, "synthetic", "SYN_NQ_1M_2024H1",
                              cfg["validation"], source_detail=truth, contract="synthetic", adjustment="n/a")
    print(f"  validated in {time.perf_counter() - t0:.2f}s")
    print(ds1.report.to_text())
    (OUT / "data_quality_1m.txt").write_text(ds1.report.to_text())

    import pandas as pd
    bad = df1.copy()
    bad.loc[100, "high"] = bad.loc[100, "low"] - 5          # impossible bar
    conflicting = bad.iloc[[500]].assign(close=bad.loc[500, "close"] + 0.25,
                                         high=bad.loc[500, "high"] + 0.25)
    bad = pd.concat([bad, conflicting], ignore_index=True)   # same timestamp, different prices
    try:
        validate_and_freeze(bad, inst, cal, "1m", 1, "synthetic", "CORRUPT", cfg["validation"])
        print("  !! corrupted data was accepted (BUG)")
    except DataIntegrityError as e:
        failed = [c.name for c in e.report.checks if c.status == "FAIL"]
        print(f"\n  corrupted copy REFUSED as expected -> FAIL checks: {failed}")

    df5 = resample_bars(df1, cal, 5)
    ds5 = validate_and_freeze(df5, inst, cal, "5m", 5, "synthetic", "SYN_NQ_5M_2024H1",
                              cfg["validation"], source_detail={**truth, "resampled_from": "SYN_NQ_1M_2024H1"})
    print(f"  resampled to {len(df5):,} 5m bars (session-anchored) -> validation {ds5.report.status}")

    h("3. STORAGE")
    store = open_store(cfg, root=ROOT / "data")
    for ds in (ds1, ds5):
        store.save_dataset(ds.manifest, ds.bars)
    _, reloaded = store.load_dataset("SYN_NQ_5M_2024H1", 5)
    print(f"  backend={store.backend}; datasets saved; reload hash matches manifest: "
          f"{reloaded.content_hash() == ds5.manifest.content_hash}")

    h("4. LOOKAHEAD GATE")
    try:
        run_backtest(ds5, PeekNextClose(OrderSpec("market", stop_points=20)), costs, cfg["backtest"])
        print("  !! peeking strategy accepted (BUG)")
    except BacktestError as e:
        print(f"  peeking strategy REFUSED: {str(e)[:110]}...")

    h("5. BREAKOUT BACKTEST - NQ 5m, NY AM decision window 09:30-11:00 ET")
    order = OrderSpec("market", stop_points=20, target_points=40)
    brk = Breakout(order, lookback=12, side="both", window=("09:30", "11:00"))
    t0 = time.perf_counter()
    res = run_backtest(ds5, brk, costs, cfg["backtest"], ltf=ds1)
    bt_s = time.perf_counter() - t0
    m = compute_metrics(res.trades, sample_thresholds=cfg["sample_size"])
    print(f"  strategy {res.strategy_id}; signals {res.n_signals}, skipped {res.skipped}")
    print(f"  causality: passed on {len(res.causality.cuts_tested)} truncation cuts; "
          f"intrabar data reliable on {res.intrabar['reliable_bar_fraction']:.1%} of bars")
    print(format_metrics(m))
    print(f"  exit reasons: {m['exit_reasons']}")
    print(f"  same-bar conflicts: {m['conflict_bars']} -> "
          f"{res.trades.conflict_resolution[res.trades.conflict_resolution != ''].value_counts().to_dict()}")
    print("\n  cost sensitivity (exact, post hoc):")
    cs = cost_sensitivity(res.trades, cfg["backtest"]["cost_sensitivity_multipliers"])
    for _, r in cs.iterrows():
        print(f"    {r.cost_multiplier:>4.1f}x costs: expectancy {r.expectancy_r:+.3f}R  PF {r.profit_factor:.3f}  "
              f"net {r.net_r:+.1f}R")
    be = breakeven_cost_multiplier(res.trades)
    print(f"  breakeven cost multiple: {be:.2f}x" if be > 0 else
          "  breakeven cost multiple: none - strategy loses even before costs (gross R < 0)")

    lo, hi = m["expectancy_ci95"]
    if lo > 0:
        verdict = "CI excludes 0 in-sample (still requires OOS / walk-forward / multiple-testing checks)"
    elif hi < 0:
        verdict = "CI is entirely below 0: historically negative under these assumptions"
    else:
        verdict = "CI includes 0: NOT statistically distinguishable from zero expectancy"
    print(f"\n  reading: {verdict}.")

    h("6. NULL MODEL - 100 random-entry controls, same window / order / costs / trade rate")
    window = ("09:30", "11:00")
    eligible = int(_window_mask(ds5.bars, window, cal.timezone).sum())
    p_fire = res.n_signals / eligible                      # match the breakout's signal rate
    ncfg = {**cfg["backtest"], "require_causality_check": False}   # RandomEntry causality is unit-tested
    t0 = time.perf_counter()
    null_exp, null_n = [], []
    for seed in range(100):
        rc = run_backtest(ds5, RandomEntry(order, p=p_fire, seed=seed, window=window), costs, ncfg, ltf=ds1)
        null_exp.append(rc.trades.net_r.mean())
        null_n.append(len(rc.trades))
    null_exp, null_n = np.array(null_exp), np.array(null_n)
    pct = float((null_exp < m["expectancy_r"]).mean())
    print(f"  100 controls in {time.perf_counter() - t0:.1f}s; signal prob {p_fire:.3f}/eligible bar")
    print(f"  control trades: median {np.median(null_n):.0f} (range {null_n.min()}-{null_n.max()})  "
          f"vs breakout {m['trade_count']}")
    q = np.percentile(null_exp, [5, 25, 50, 75, 95])
    print(f"  control net expectancy percentiles 5/25/50/75/95: "
          + " / ".join(f"{x:+.3f}" for x in q) + " R")
    print(f"  breakout {m['expectancy_r']:+.3f}R beats {pct:.0%} of random controls "
          f"(one-sided empirical p ~ {1 - pct:.2f})")
    print(f"  reading: {'outperforms the null at the 5% level (single test; not yet corrected for multiple testing)' if pct >= 0.95 else 'NOT distinguishable from random entries'}.")
    print("  note: all controls trade the SAME realized path, so their centre reflects this path,")
    print("  not zero. Engine unbiasedness across independent paths is checked separately")
    print("  (EDGELAB_SLOW_TESTS=1: 40 paths, mean gross R consistent with 0).")
    print("  Ground truth: the data is a driftless random walk, so no edge exists. A positive")
    print("  net P&L on ~170 trades is exactly what chance produces - hence the gates.")

    h("7. KNOWN-ANSWER CHECK - gambler's ruin on the 1m walk (no costs, no gaps)")
    dfk, _ = generate_bars(cal, "2024-01-02", "2024-04-01", tf_minutes=1, sigma_per_bar=3.0, seed=1)
    dsk = validate_and_freeze(dfk, inst, cal, "1m", 1, "synthetic", "SYN_KNOWN", cfg["validation"])
    kcfg = {**cfg["backtest"], "session": {"flatten_daily": False, "hold_overnight": True}}
    from edgelab.engine.costs import CostModel
    kr = run_backtest(dsk, RandomEntry(order, p=0.2, seed=3), CostModel(), kcfg).trades
    br = kr[kr.exit_reason.isin(["STOP", "TARGET"])]
    p_hat, p = (br.exit_reason == "TARGET").mean(), 20 / 60
    se_p = np.sqrt(p * (1 - p) / len(br))
    g = kr.gross_r
    se_g = g.std(ddof=1) / np.sqrt(len(g))
    print(f"  {len(kr):,} trades. P(target first): observed {p_hat:.4f}, theory {p:.4f} "
          f"(z = {(p_hat - p) / se_p:+.2f})")
    print(f"  mean gross R: {g.mean():+.4f} (theory 0; z = {g.mean() / se_g:+.2f})  -> "
          f"{'PASS' if abs((p_hat - p) / se_p) < 4 and abs(g.mean() / se_g) < 4 else 'FAIL'}")

    h("8. RUN REGISTRY + REPRODUCTION")
    run_id = record_run(store, cfg, res, m, seed=None,
                        notes="Phase 1 demo on synthetic random walk; no edge expected.")
    again = run_backtest(ds5, Breakout(order, lookback=12, side="both", window=("09:30", "11:00")),
                         costs, cfg["backtest"], ltf=ds1)
    rec, stored_trades = store.load_run(run_id)
    print(f"  recorded {run_id} (status {rec['status']}); stored trades: {len(stored_trades)}")
    print(f"  re-run trades hash {again.trades_hash[:16]} == stored {rec['trades_hash'][:16]}: "
          f"{again.trades_hash == rec['trades_hash']}")

    h("9. BENCHMARK (single process, pure NumPy; numba not installed)")
    t0 = time.perf_counter()
    n_var, n_tr = 0, 0
    for lb in (6, 12, 24):
        for sp, tp in ((10, 20), (20, 40), (30, 30)):
            for side in ("long", "short"):
                r = run_backtest(ds5, Breakout(OrderSpec("market", stop_points=sp, target_points=tp),
                                               lookback=lb, side=side),
                                 costs, {**cfg["backtest"], "require_causality_check": False}, ltf=ds1)
                n_var += 1
                n_tr += len(r.trades)
    el = time.perf_counter() - t0
    print(f"  {n_var} variants x {len(ds5.bars):,} bars -> {n_tr:,} trades in {el:.2f}s "
          f"({el / n_var * 1000:.0f} ms/variant, {n_tr / el:,.0f} trades/s)")
    print(f"  (single demo backtest incl. 20-cut causality check: {bt_s:.2f}s)")
    store.close()
    print(f"\nArtifacts: {OUT}/data_quality_1m.txt, data store in data/, JSON logs in "
          f"{cfg['logging']['json_file']}")


if __name__ == "__main__":
    main()
