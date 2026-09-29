# READ-ONLY SMOKE TEST: matched random-entry control (method v2) on the stored 2024 5m HistData dataset.
# Pipeline/implementation check only - NOT a research conclusion, NOT evidence for or against an edge.
# Stores nothing: controls are never run records; the dataset is only read.
import hashlib, json, sys
import yaml
from edgelab.core.identity import hash_obj
from edgelab.services import Services
from edgelab.strategy.compiler import compile_strategy

STRAT = "strategies/fixtures/ema_crossover.yaml"
svc = Services(root=".")
problems = []

def check(cond, what):
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        problems.append(what)

def fingerprint(did):
    det = svc.dataset_detail(did)
    ds = svc.load_dataset(did)                               # re-validates stored bars + hash check
    return {"manifest_hash": det["manifest_hash"], "content_hash": det["manifest"]["content_hash"],
            "bars_hash": ds.bars.content_hash(), "n_bars": len(ds.bars)}

def runs_state():
    r = svc.store.list_runs()
    return len(r), hash_obj(sorted(r.astype(str).to_dict("records"), key=lambda x: x["run_id"])) if len(r) else ""

print("==== 1. PRE-RUN CHECKS")
kids = [d for d in svc.list_datasets() if d["dataset_name"] == "NAS100_HISTDATA_2024" and d["timeframe"] == "5m"]
check(len(kids) == 1, f"exactly one NAS100_HISTDATA_2024 5m dataset (found {[d['dataset_id'] for d in kids]})")
if len(kids) != 1:
    sys.exit("STOP")
did = kids[0]["dataset_id"]
man = svc.dataset_detail(did)["manifest"]
print(f"  dataset {did}  parent {man.get('parent_dataset_id')}  derivation {man.get('derivation')!r}")
check(bool(man.get("parent_dataset_id")) and "resample 1m->5m" in (man.get("derivation") or ""),
      "stored 5m child derived from the stored 1m parent")
doc = yaml.safe_load(open(STRAT))
check(doc["entry"].get("cooldown_bars", 0) == 0, "ema_crossover has cooldown 0 (v2 fix behaviourally identical to v1 here)")
from edgelab.engine.costs import cost_model_from_config
cm = cost_model_from_config(svc.cfg, "NAS100_HISTDATA", provider=man["provider"])
check((cm.profile, cm.status) == ("NAS100_HISTDATA@HISTDATA", "assumed"), f"cost profile {cm.profile} status {cm.status}")
fp_before, runs_before = fingerprint(did), runs_state()
cand_id = compile_strategy(doc, svc.sessions, svc._config_hash()).strategy_id
strat_file_hash = hashlib.sha256(open(STRAT, "rb").read()).hexdigest()
print(f"  run records before: {runs_before[0]}   candidate strategy id: {cand_id}")

print("\n==== 2. RUN random_entry_control(n_controls=20, seed=0)")
r = svc.random_entry_control(STRAT, did, n_controls=20, seed=0)

print("\n==== 3. PROVENANCE / IMMUTABILITY")
fp_after, runs_after = fingerprint(did), runs_state()
check(fp_before == fp_after, f"dataset unchanged (manifest/content/bars hashes, {fp_after['n_bars']} bars)")
check(runs_before == runs_after, f"run records unchanged ({runs_after[0]}; same ids and hashes)")
check(r["stored_as_runs"] is False, "controls not stored as runs")
ids = [x["control_strategy_id"] for x in r["realizations"]]
check(all(i.startswith("CTRL_") for i in ids) and len(set(ids)) == 20, "20 distinct CTRL_ ids")
check(cand_id not in ids and r["candidate"]["strategy_id"] == cand_id, "candidate id unchanged and not among controls")
check(hashlib.sha256(open(STRAT, "rb").read()).hexdigest() == strat_file_hash, "strategy file unchanged")
d, cfg = r["dataset"], r["control_config"]
check((d["dataset_id"], d["provider"], d["instrument"]) == (did, "HISTDATA", "NAS100_HISTDATA"), "dataset/provider/instrument")
check((r["cost_profile"], r["cost_status"]) == ("NAS100_HISTDATA@HISTDATA", "assumed"), "cost profile/status in result")
check((cfg["method"], cfg["base_seed"], cfg["n_controls"], len(cfg["realization_seeds"])) ==
      ("random_entry_conditional_v2", 0, 20, 20), "control method v2, seed 0, 20 realization seeds")
print(f"  validation id {r['validation_id']}  definition hash {r['candidate']['definition_hash'][:16]}...")
print(f"  realization seeds {cfg['realization_seeds']}")

print("\n==== 4. REAL 2024 RESULTS (descriptive only)")
c, m = r["candidate"], r["candidate"]["metrics"]
print(f"  candidate {c['strategy_id']}: signals pre-cooldown {c['pre_cooldown_signals']} / final {c['signals']}; "
      f"trades {m['trade_count']}; gross_r {m['gross_r']:.4f}; net_r {m['net_r']:.4f}; expectancy {m['expectancy_r']:.5f}; "
      f"PF {m['profit_factor']}; max_dd_r {m['max_drawdown_r']:.4f}")
reals = r["realizations"]
print(f"  realizations {len(reals)}; pre-cooldown signals {[x['pre_cooldown_signals'] for x in reals]}")
print(f"  final signals {[x['signals'] for x in reals]}")
tc = [x["trade_count"] for x in reals]
print(f"  trade counts {sorted(tc)} (range {min(tc)}-{max(tc)})")
cmpr = r["comparison"]
for k in ("trade_count", "gross_r", "net_r", "expectancy_r", "profit_factor", "max_drawdown_r"):
    row = cmpr[k]; p = row["percentiles"]
    print(f"  {k:15} candidate {row['candidate']}  control p5 {p.get('5')}  median {row['median']}  p95 {p.get('95')}  "
          f"finite {row['finite_realizations']}  fraction of controls exceeding candidate {row.get('fraction_of_controls_exceeding_candidate')}")

print("\n==== 5. CAUSALITY")
check(all(x["causality_passed"] is True for x in reals), "all 20 realizations passed the engine causality check")
print("  failures:", [x["index"] for x in reals if x["causality_passed"] is not True])

print("\nLABELS:"); print(*("  " + x for x in r["labels"]), sep="\n")
print("\nPipeline/implementation smoke test only - not evidence for or against an edge; fractions are descriptive ranks, not p-values.")
print("==== SMOKE TEST:", "PASS" if not problems else f"FAIL {problems}")
