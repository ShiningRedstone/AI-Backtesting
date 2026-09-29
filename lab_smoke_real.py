# STRATEGY LAB GUI-PATH SMOKE TEST on a real stored dataset. Product integration test only:
# NOT strategy research, NOT evidence for or against any edge, no optimization.
#
# Drives the SAME HTTP API the Strategy Lab GUI calls (Flask test client, no server needed):
#   duplicate ema_crossover -> change ONE parameter (fast 9 -> 8) -> validate -> save new version
#   -> one backtest on the 5m NAS100_HISTDATA_<year> dataset -> verify run lineage
#   -> a very small variation batch (fast in {7, 8}: one new child) -> verify batch/child lineage
#   -> comparison + provenance read models.
# It WRITES to the chosen workspace: the first invocation adds 2 strategy versions (the edit + 1
# variant) and 1 variation batch record; EVERY invocation adds 1 run record (labelled IN_SAMPLE).
# Datasets are only read (and re-validated on load). Re-running reuses the same strategy ids.
#
# Usage:  .\.venv\Scripts\python.exe lab_smoke_real.py [--root .] [--year 2024]
#         (--root: the workspace the desktop app uses, e.g. %LOCALAPPDATA%\EdgeLab, or the repo root)
import argparse
import copy
import json
import sys

from edgelab.web.app import create_app

ap = argparse.ArgumentParser()
ap.add_argument("--root", default=".")
ap.add_argument("--year", type=int, default=2024)
a = ap.parse_args()
c = create_app(a.root).test_client()
problems = []


def check(cond, what):
    print(("  OK   " if cond else "  FAIL ") + what)
    if not cond:
        problems.append(what)
    return cond


def call(method, url, body=None, ok=(200, 202)):
    r = c.get(url) if method == "GET" else c.post(url, json=body)
    data = r.get_json()
    if r.status_code not in ok:
        sys.exit(f"STOP: {method} {url} -> {r.status_code}: {json.dumps(data)[:600]}")
    return data


lib = call("GET", "/api/strategies")
ema = [s for s in lib if s["name"] == "ema_crossover"]
if len(ema) != 1:
    sys.exit(f"STOP: expected one stored 'ema_crossover' strategy, found {[s['strategy_id'] for s in ema]}. "
             "Save strategies/fixtures/ema_crossover.yaml in the Strategy Lab first.")
base_id = ema[0]["strategy_id"]
base = call("GET", f"/api/strategies/{base_id}")
rows = call("POST", "/api/backtests/readiness", {"strategy": base_id})["datasets"]
cand = [d for d in rows if d["dataset_name"] == f"NAS100_HISTDATA_{a.year}" and d["timeframe"] == "5m"]
if len(cand) != 1:
    sys.exit(f"STOP: expected one 5m NAS100_HISTDATA_{a.year} dataset, found {[d['dataset_id'] for d in cand]}")
ds = cand[0]
print(f"base {base_id} (definition {base['definition_hash'][:16]})  dataset {ds['dataset_id']}")

print("\n1. dataset selection")
check(ds["runnable"], f"dataset eligible ({ds['quality_status']}, costs {ds['cost']['status']} {ds['cost'].get('profile')})")
check(all(not d["runnable"] for d in rows if d["reasons"]), "ineligible datasets carry reasons")
print("   caveats: " + " | ".join(ds.get("limitations") or ["none"]))

print("\n2. duplicate -> edit one parameter -> validate -> save new version")
draft = call("POST", f"/api/strategies/{base_id}/duplicate", {"name": "ema_crossover_lab_smoke"})["draft"]
draft["parameters"]["fast"]["value"] = 8
v = call("POST", "/api/strategies/validate", {"definition": draft})
check(v["valid"], "edited definition valid")
saved = call("POST", "/api/strategies/save", {"definition": draft, "parent_strategy_id": base_id, "method": "duplicate"})
sid = saved["strategy_id"]
check(sid == v["identity"]["strategy_id"] and sid != base_id, f"new version {sid} (created now: {saved['created']})")
check(saved["definition_hash"] != base["definition_hash"], "definition hash differs from the parent")
check(call("GET", f"/api/strategies/{base_id}")["definition_hash"] == base["definition_hash"], "parent version unchanged")
prov = call("GET", f"/api/strategies/{sid}/research")
check(prov["lineage"]["parent_strategy_id"] == base_id and prov["lineage"]["parent_definition_hash"] == base["definition_hash"],
      "provenance: parent id and parent definition hash")

print("\n3. one backtest")
bt = call("POST", "/api/backtests", {"strategy": sid, "dataset_id": ds["dataset_id"]})
run = call("GET", f"/api/results/{bt['run_id']}")["record"]
check(run["strategy"]["strategy_id"] == sid and run["strategy"]["dsl"]["definition_hash"] == saved["definition_hash"],
      f"run {bt['run_id']} tied to the new version and its definition hash")
check(run["dataset"]["dataset_id"] == ds["dataset_id"] and run["status"] == "IN_SAMPLE", "dataset and IN_SAMPLE status")
check(run["assumptions"]["costs"]["profile"] == ds["cost"].get("profile"), f"cost profile {run['assumptions']['costs']['profile']}")
curve = call("GET", f"/api/results/{bt['run_id']}/curve")
check(curve["n_trades"] == bt["metrics"]["trade_count"], f"equity curve over {curve['n_trades']} trades")
m = bt["metrics"]
print(f"   (descriptive only) trades {m['trade_count']}, net R {m['net_r']:.3f}, expectancy {m['expectancy_r']:.4f}")

print("\n4. very small variation batch (fast in {7, 8})")
spec = {"variation_spec_version": 1, "name": "lab_smoke", "mode": "grid", "max_variants": 5,
        "dimensions": [{"parameter": "fast", "values": [7, 8]}]}
prev = call("POST", "/api/variations/preview", {"base": sid, "spec": spec})
check(prev["ok"] and prev["combinations_list"] == [{"fast": 7}, {"fast": 8}], "exact combinations shown before generation")
gen = call("POST", "/api/variations", {"base": sid, "spec": spec, "save": True})
check(gen["generated"] == 1 and len(gen["same_as_base_combinations"]) == 1, "1 new child; fast=8 recognized as the base")
child = gen["variants"][0]["strategy_id"]
cp = call("GET", f"/api/strategies/{child}/research")
check(cp["lineage"]["parent_strategy_id"] == sid and cp["lineage"]["generation_batch"]["batch_id"] == gen["batch_id"],
      f"child {child}: parent {sid}, batch {gen['batch_id']}")
check(cp["lineage"]["parent_definition_hash"] == saved["definition_hash"], "child records the parent definition hash")

print("\n5. comparison and provenance read models")
cmp_ = call("GET", f"/api/compare?source=lineage&id={sid}")
row = next((r for r in cmp_["rows"] if r["run_id"] == bt["run_id"]), None)
check(row is not None and row["scope"] == "In-sample (exploratory)" and not row["validated"], "run listed, scoped in-sample, not validated")
check(row is not None and "rank" not in row and "score" not in row, "no rank or score")
prov = call("GET", f"/api/strategies/{sid}/research")
check(bt["run_id"] in [r["run_id"] for r in prov["runs"]] and gen["batch_id"] in prov["variation_batches_from_this_strategy"],
      "provenance lists the run and the batch")

print("\nIn the GUI: Strategy Lab -> the new version -> Research tab shows the same run, batch and provenance;")
print("Compare -> 'A strategy and all its descendants' shows the comparison row.")
print("Product integration test only; not strategy research evidence.")
print("==== LAB SMOKE TEST:", "PASS" if not problems else f"FAIL {problems}")
