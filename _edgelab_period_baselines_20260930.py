"""Canonical directional Dukascopy EMA PERIOD BASELINES (descriptive slices, NOT out-of-sample tests).
Runs strategies/fixtures/ema_crossover.yaml once per trading-date year on the canonical ASK-OHLC 5m
dataset through the existing Services._run_cell(period=...) path (restrict_to_period: re-validated,
in-memory, parent-linked; nothing stored as a dataset). One recorded run per period (skipped if already
recorded), plus one unrecorded rerun to verify determinism. No ranking, no conclusions."""
import argparse, json, sys
from pathlib import Path
import numpy as np
import pandas as pd

ap = argparse.ArgumentParser()
ap.add_argument("--root", default=".")
ap.add_argument("--dataset", default="NQ_DUKASCOPY_BIDASK_OHLC_2021_2026_5M_96699F7568")
ap.add_argument("--pooled-run", default="RUN_2026_00029")
ap.add_argument("--historical-runs", default="RUN_2026_00027,RUN_2026_00028,RUN_2026_00029")
a = ap.parse_args()
root = Path(a.root).resolve()
sys.path.insert(0, str(root))
from edgelab.analytics.metrics import compute_metrics
from edgelab.core.identity import hash_obj
from edgelab.engine.costs import cost_model_from_config
from edgelab.services import Services

TAG = "period baseline v1"
YAML = (root / "strategies" / "fixtures" / "ema_crossover.yaml").read_text()
bad = []
def check(label, ok, detail=""):
    print(f"  {'OK  ' if ok else 'FAIL'} {label}{('  ' + str(detail)) if detail else ''}")
    if not ok:
        bad.append(label)

def fingerprint(svc, run_id):
    rec, tr = svc.store.load_run(run_id)
    return hash_obj({"rec": json.loads(json.dumps(rec, default=str)), "trades": tr.to_json(date_format="iso")})

def side_stats(trades):
    out = {}
    for d, n in ((1, "long"), (-1, "short")):
        t = trades[trades["direction"] == d] if len(trades) else trades
        out[f"{n}_trades"] = int(len(t))
        out[f"{n}_net_r"] = float(t["net_r"].sum()) if len(t) else 0.0
    return out

svc = Services(root=root)
try:
    cm = cost_model_from_config(svc.cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")
    print("== CANONICAL PROFILE")
    check("scenario dukascopy_directional_cost_assumption_v1, spread_source quotes",
          (cm.scenario, cm.spread_source) == ("dukascopy_directional_cost_assumption_v1", "quotes"))
    if bad:
        sys.exit("STOP: not the canonical directional config; nothing run.")
    hist = {r: fingerprint(svc, r) for r in a.historical_runs.split(",") if r}
    ds = svc.load_dataset(a.dataset)                     # re-validates + re-checks the stored content hash
    src_hash, m = ds.manifest.content_hash, ds.manifest
    print(f"  source {a.dataset} content {src_hash} quality {m.quality_status} calendar {m.calendar} "
          f"bars {len(ds.bars)} {m.start} .. {m.end} has_ask_ohlc {m.has_ask_ohlc}")

    # ---- period boundaries by TRADING DATE (session calendar), so no session is cut at UTC midnight
    ts = ds.bars.ts
    td = pd.DatetimeIndex(ds.calendar.trading_dates(ts))
    years = sorted(set(td.year))
    first_td, last_td = td.min(), td.max()
    periods = []
    for y in years:
        mask = td.year == y
        idx = np.flatnonzero(mask)
        partial = (y == years[0] and first_td > pd.Timestamp(f"{y}-01-03")) or (y == years[-1] and last_td < pd.Timestamp(f"{y}-12-30"))
        periods.append({"label": f"{y}{' partial' if partial else ''}", "year": int(y), "partial": bool(partial),
                        "requested_trading_dates": [str(td[idx[0]].date()) if partial and y == years[0] else f"{y}-01-01",
                                                    str(td[idx[-1]].date()) if partial and y == years[-1] else f"{y}-12-31"],
                        "start_ts": ts[idx[0]], "end_ts": ts[idx[-1]], "n_bars_source": int(mask.sum())})
    check("periods cover every source bar exactly once", sum(p["n_bars_source"] for p in periods) == len(ts))

    existing = {}
    for r in svc.list_runs():
        if str(r.get("notes", "")).startswith(TAG):
            existing[str(r["notes"]).split(" | ")[0]] = r["run_id"]

    rows = []
    for p in periods:
        key = f"{TAG} {p['label']}"
        notes = (f"{key} | trading dates {p['requested_trading_dates'][0]}..{p['requested_trading_dates'][1]} "
                 f"(calendar {m.calendar}); bars [{p['start_ts']}, {p['end_ts']}]; source {a.dataset} content {src_hash}; "
                 f"descriptive slice, NOT out-of-sample; indicators warm up from the slice start")
        per = (p["start_ts"], p["end_ts"])
        if key in existing:
            run_id = existing[key]
            print(f"\n== {p['label']}: already recorded as {run_id}; not recorded again")
            rec, trades = svc.store.load_run(run_id)
            met = compute_metrics(trades, sample_thresholds=svc.cfg.get("sample_size"))
            rerun = svc._run_cell(YAML, a.dataset, False, period=per)
            res_hash, skipped, cell_ds, asm = rec["trades_hash"], rec.get("skipped_signals"), rerun["ds"], rec["assumptions"]
            strat, code, cfgh, dsrec = rec["strategy"], rec["code_version"], rec["config_hash"], rec["dataset"]
        else:
            cell = svc._run_cell(YAML, a.dataset, True, period=per, notes=notes)
            run_id = cell["run_id"]
            rec, trades = svc.store.load_run(run_id)
            met, res_hash, skipped, cell_ds = cell["metrics"], cell["result"].trades_hash, dict(cell["result"].skipped), cell["ds"]
            asm, strat, code, cfgh, dsrec = rec["assumptions"], rec["strategy"], rec["code_version"], rec["config_hash"], rec["dataset"]
            rerun = svc._run_cell(YAML, a.dataset, False, period=per)
            print(f"\n== {p['label']}: recorded {run_id}")
        b = cell_ds.bars
        c = asm["costs"]
        row = {"period": p["label"], "partial": p["partial"], "run_id": run_id,
               "requested_trading_dates": p["requested_trading_dates"],
               "first_bar": str(pd.Timestamp(int(b.ts_ns[0]), tz="UTC")), "last_bar": str(pd.Timestamp(int(b.ts_ns[-1]), tz="UTC")),
               "n_bars": len(b), "slice_dataset_id": dsrec.get("dataset_id"), "slice_content_hash": dsrec.get("content_hash"),
               "slice_quality": dsrec.get("quality_status"), "source_dataset_id": dsrec.get("parent_dataset_id"),
               "source_content_hash": src_hash, "derivation": dsrec.get("derivation"),
               "strategy_id": strat["strategy_id"], "logic_hash": strat["dsl"]["logic_hash"],
               "definition_hash": strat["dsl"]["definition_hash"], "compiler": strat["dsl"]["compiler_version"],
               "config_hash": cfgh, "code_version": code, "scenario": c.get("scenario"), "quote_model": asm.get("quote_model"),
               "execution_sides": asm.get("execution_sides"), "spread_treatment": asm.get("spread_treatment"),
               "dataset_has_ask_ohlc": asm.get("dataset_has_ask_ohlc"), "spread_source": c.get("spread_source"),
               "commission": {k: c.get(k) for k in ("commission_mode", "commission_per_million", "fees_per_side")},
               "slippage": {k: c.get(k) for k in ("slippage_unit", "slippage_ticks_market", "slippage_ticks_stop", "slippage_ticks_limit")},
               "financing_mode": c.get("financing_mode"), "calendar": dsrec.get("calendar"), "session": asm.get("session"),
               "same_bar_policy": asm.get("same_bar_policy_effective"), "trades_hash": res_hash,
               "rerun_trades_hash_equal": rerun["result"].trades_hash == res_hash, "skipped": skipped}
        row.update({k: met.get(k) for k in ("trade_count", "gross_r", "cost_r", "net_r", "net_usd", "expectancy_r",
                                            "expectancy_ci95", "profit_factor", "max_drawdown_r", "sample_label", "exit_reasons")})
        row.update(side_stats(trades))
        rows.append(row)
        check(f"{p['label']}: bars within [{p['start_ts']}, {p['end_ts']}] and == source bars in that window",
              int(b.ts_ns[0]) == p["start_ts"].value and int(b.ts_ns[-1]) == p["end_ts"].value and len(b) == p["n_bars_source"])
        check(f"{p['label']}: ASK OHLC preserved; canonical quotes costs; same strategy",
              row["dataset_has_ask_ohlc"] is True and b.has_ask_ohlc and row["spread_source"] == "quotes"
              and row["quote_model"] == "directional_bid_ask" and row["spread_treatment"] == "embedded_in_quotes"
              and row["execution_sides"] == {"buy": "ask", "sell": "bid"} and row["source_dataset_id"] == a.dataset)
        check(f"{p['label']}: deterministic (unrecorded rerun gives the same trades_hash)", row["rerun_trades_hash_equal"])

    print("\n== POOLED REFERENCE (stored run, metrics recomputed read-only from its stored trades)")
    rec29, tr29 = svc.store.load_run(a.pooled_run)
    m29 = compute_metrics(tr29, sample_thresholds=svc.cfg.get("sample_size"))
    pooled = {"period": "FULL (pooled reference)", "run_id": a.pooled_run, "trades_hash": rec29["trades_hash"],
              "scenario": rec29["assumptions"]["costs"].get("scenario"), "quote_model": rec29["assumptions"].get("quote_model"),
              **{k: m29.get(k) for k in ("trade_count", "gross_r", "cost_r", "net_r", "net_usd", "expectancy_r",
                                         "expectancy_ci95", "profit_factor", "max_drawdown_r", "sample_label", "exit_reasons")},
              **side_stats(tr29), "skipped": rec29.get("skipped_signals")}

    print("\n== TABLE (R unless stated; descriptive, unranked)")
    cols = ["period", "run_id", "trade_count", "gross_r", "cost_r", "net_r", "net_usd", "expectancy_r", "expectancy_ci95",
            "profit_factor", "max_drawdown_r", "long_trades", "short_trades", "long_net_r", "short_net_r"]
    t = pd.DataFrame([{k: r.get(k) for k in cols} for r in rows + [pooled]])
    with pd.option_context("display.width", 250, "display.max_columns", 30):
        print(t.to_string(index=False, float_format=lambda v: f"{v:,.4f}"))
    print("\n  exit reasons / skipped:")
    for r in rows + [pooled]:
        print(f"   {r['period']:24s} exits {r['exit_reasons']}  skipped {r['skipped']}")
    tot = {k: sum(r[k] for r in rows) for k in ("trade_count", "gross_r", "cost_r", "net_r")}
    print(f"\n  sum of slices {tot} vs pooled trades {pooled['trade_count']} net {pooled['net_r']:.4f} "
          f"(not expected to match exactly: each slice restarts indicator warm-up and position state)")

    print("\n== FULL LINEAGE PER PERIOD")
    print(json.dumps(rows, indent=1, default=str))

    print("\n== IMMUTABILITY")
    check("source dataset content hash unchanged", svc.load_dataset(a.dataset).manifest.content_hash == src_hash)
    check("no slice stored as a dataset", not any("__" in d["dataset_id"] and d["dataset_id"].startswith(a.dataset)
                                                   for d in svc.list_datasets()))
    for r, h in hist.items():
        check(f"historical {r} unchanged", fingerprint(svc, r) == h)
finally:
    svc.store.close()
print("\n== RESULT:", "ALL CHECKS PASS" if not bad else f"{len(bad)} FAILED: {bad}")
print("DISCLOSURE: descriptive in-sample period slices of one fixed strategy under stated research assumptions "
      "(assumed commission/slippage; quality WARN; provider-defined volume; regular hours verified, holidays/early "
      "closes not fully verified; 14 whole missing trading days; 538 partial 5m buckets; nothing filled). "
      "Not out-of-sample, not ranked, not evidence for or against an edge.")
