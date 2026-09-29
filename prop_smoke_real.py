# READ-ONLY SMOKE TEST: feed ONE existing stored backtest run (default: the stored ema_crossover
# baseline on the 5m NAS100_HISTDATA 2024 dataset) into the Phase 6 prop simulator.
# Descriptive / research-only. Not a strategy study, not a pass/fail verdict on the strategy.
# The rule sets are the SYNTHETIC TEST-ONLY examples in configs/prop (they describe no real firm).
#
# Proves: an existing real result feeds the simulator; lineage is preserved; the source run
# (record, trades, trades_hash), the run table and the dataset are unchanged; an account report is
# produced. Stores nothing unless --record (then only <data>/prop_simulations/PROP_*.json).
#
# Usage (repo root):  .\.venv\Scripts\python.exe prop_smoke_real.py [--run-id RUN_...] [--year 2024] [--record]
import argparse
import json
import sys

import yaml

from edgelab.core.identity import hash_obj
from edgelab.prop.simulator import PropDataError, trades_fingerprint
from edgelab.services import Services
from edgelab.strategy.compiler import compile_strategy

ap = argparse.ArgumentParser()
ap.add_argument("--run-id", help="stored RUN_ id to use (default: newest ema_crossover run on the year's 5m dataset)")
ap.add_argument("--year", type=int, default=2024)
ap.add_argument("--record", action="store_true", help="also store the simulation document")
a = ap.parse_args()
STRAT = "strategies/fixtures/ema_crossover.yaml"
CONFIGS = ("SYNTH_STATIC_EVAL", "SYNTH_TRAILING_EVAL")
svc = Services(root=".")
problems = []


def check(cond, what):
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        problems.append(what)


def runs_state():
    r = svc.store.list_runs()
    return len(r), hash_obj(sorted(r.astype(str).to_dict("records"), key=lambda x: x["run_id"])) if len(r) else ""


def source_state(rid, did):
    rec, tr = svc.store.load_run(rid)
    det = svc.dataset_detail(did)
    return {"record": hash_obj(rec), "trades": trades_fingerprint(tr), "trades_hash": rec.get("trades_hash"),
            "n_trades": len(tr), "runs": runs_state(), "manifest_hash": det["manifest_hash"]}


if a.run_id:
    run_id = a.run_id
else:
    cand = compile_strategy(yaml.safe_load(open(STRAT)), svc.sessions, svc._config_hash()).strategy_id
    ds = [d["dataset_id"] for d in svc.list_datasets()
          if d["dataset_name"] == f"NAS100_HISTDATA_{a.year}" and d["timeframe"] == "5m"]
    rows = [r for r in svc.list_runs() if r["strategy_id"] == cand and r["dataset_id"] in ds]
    if not rows:
        sys.exit(f"STOP: no stored run of {cand} on the 5m NAS100_HISTDATA_{a.year} dataset. Pass --run-id "
                 f"(stored runs: {[r['run_id'] for r in svc.list_runs()][-20:]}).")
    run_id = sorted(rows, key=lambda r: r["run_id"])[-1]["run_id"]
rec, _ = svc.store.load_run(run_id)
did = rec["dataset"]["dataset_id"]
print(f"source run {run_id}  status {rec.get('status')}  strategy {rec['strategy']['strategy_id']}  dataset {did}")
before = source_state(run_id, did)

reports = []
for i, cfg in enumerate(CONFIGS, 1):
    try:
        reports.append((cfg, svc.prop_simulate(run_id, [{"account_id": f"A{i}", "config": cfg}], record=a.record)))
    except PropDataError as exc:                    # an honest refusal is a valid smoke-test outcome
        print(f"\n{cfg}: REFUSED by the simulator (trade records insufficient for this rule set): {exc}")
after = source_state(run_id, did)

print("\n==== READ-ONLY / LINEAGE CHECKS")
check(before == after, f"source run record, trades ({after['n_trades']}), trades_hash, run table "
                       f"({after['runs'][0]} runs) and dataset manifest unchanged")
check(bool(reports), "at least one account report was generated")
for cfg, r in reports:
    L = r["lineage"]
    check(L["source_run_id"] == run_id and L["dataset_id"] == did
          and L["strategy_id"] == rec["strategy"]["strategy_id"], f"{cfg}: lineage run/strategy/dataset")
    check(L["definition_hash"] == (rec["strategy"].get("dsl") or {}).get("definition_hash"), f"{cfg}: definition hash")
    check(L["trades_hash"] == rec["trades_hash"] and L["trades_hash_verified"], f"{cfg}: trades hash verified")
    check((L["cost_profile"], L["cost_status"]) == (rec["assumptions"]["costs"]["profile"], rec["assumptions"]["cost_status"]),
          f"{cfg}: cost profile/status ({L['cost_profile']}, {L['cost_status']})")
    check(r["account_configs"][0]["prop_config_id"] == cfg and r["account_configs"][0]["prop_config_hash"],
          f"{cfg}: prop config id/hash recorded")

for cfg, r in reports:
    s, S, L = r["accounts"][0]["summary"], r["strategy_result"], r["lineage"]
    print(f"\n==== REPORT  simulation {r['simulation_id']}  (recorded: {r['recorded']})")
    print(*("  " + x for x in r["labels"]), sep="\n")
    print(f"  period {L['source_period']}")
    print("  STRATEGY RESULT (source run, unchanged): " + ", ".join(
        f"{k} {S[k]}" for k in ("trade_count", "net_r", "net_usd", "expectancy_r", "max_drawdown_r", "sample_label")))
    print(f"  PROP ACCOUNT RESULT ({cfg}, synthetic test-only rules): " + json.dumps({k: s[k] for k in (
        "status", "survived", "starting_balance", "ending_balance", "net_pnl_usd", "net_r", "trade_count",
        "trades_not_processed", "trading_days", "target_usd", "profit_target_reached", "drawdown_breach",
        "daily_loss_breach", "max_drawdown_usd_closed", "max_drawdown_usd_intratrade_bound",
        "max_daily_loss_usd_closed", "time_to_target", "time_to_breach", "violation_reason",
        "incomplete_reasons", "trades_crossing_reset", "trades_end_of_data")}, default=str, indent=1))

print("\nDescriptive / research-only. Synthetic test-only rule sets; the outcome is not a verdict on the "
      "strategy and not evidence that it is profitable or deployable.")
print("==== SMOKE TEST:", "PASS" if not problems else f"FAIL {problems}")
