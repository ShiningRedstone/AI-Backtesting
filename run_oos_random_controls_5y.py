# READ-ONLY STUDY: matched random-entry control (random_entry_conditional_v2) on the five stored
# HistData 5m OOS periods. Descriptive conditional null comparison only - not a significance test,
# not evidence for or against an edge, not a forecast. Stores nothing (controls are never run records).
#
# OOS window per year = exactly the evaluate_oos convention: research.validation.oos_windows(dataset
# start, dataset end, "<YEAR>-07-01") -> [split, end] (UTC; bar opens inclusive), passed as `period`.
#
# Seeds: each calendar year gets its own deterministic stream, effective seed =
# SeedSequence([base_seed, year]).generate_state(1)[0] (no Python hash()); that seed is the base seed
# of the unchanged control, which spawns its 200 realization seeds from it. Same base seed + year ->
# same effective seed; different years -> different, independent streams.
#
# Usage (repo root):  .\.venv\Scripts\python.exe run_oos_random_controls_5y.py [--controls 200] [--seed 0]
import argparse, hashlib, sys
import numpy as np
import yaml
from edgelab.core.identity import hash_obj
from edgelab.research.validation import freeze_definition, oos_windows
from edgelab.services import Services
from edgelab.strategy.compiler import compile_strategy

ap = argparse.ArgumentParser()
ap.add_argument("--controls", type=int, default=200)
ap.add_argument("--seed", type=int, default=0)
a = ap.parse_args()
YEARS = (2019, 2020, 2021, 2022, 2024)
STRAT = "strategies/fixtures/ema_crossover.yaml"
METRICS = ("trade_count", "gross_r", "net_r", "expectancy_r", "profit_factor", "max_drawdown_r")
svc = Services(root=".")
problems = []


def check(cond, what):
    print(("    OK   " if cond else "    FAIL ") + what)
    if not cond:
        problems.append(what)


def fingerprint(did):
    det, ds = svc.dataset_detail(did), svc.load_dataset(did)          # reload re-validates + hash-checks
    return (det["manifest_hash"], det["manifest"]["content_hash"], ds.bars.content_hash(), len(ds.bars))


def runs_state():
    r = svc.store.list_runs()
    return len(r), hash_obj(sorted(r.astype(str).to_dict("records"), key=lambda x: x["run_id"])) if len(r) else ""


def rng(vals):
    return f"{min(vals)}-{max(vals)}"


def pct(row, ps):
    return "  ".join(f"p{p} {row['percentiles'].get(str(p)) if row['percentiles'] else None}" for p in ps)


doc = yaml.safe_load(open(STRAT))
strat_sha = hashlib.sha256(open(STRAT, "rb").read()).hexdigest()
def_hash = freeze_definition(doc)[1]
cand_id = compile_strategy(doc, svc.sessions, svc._config_hash()).strategy_id
print(f"strategy {STRAT}  candidate id {cand_id}  definition hash {def_hash}")
print(f"controls per period {a.controls}  base seed {a.seed}")


def year_seed(base: int, year: int) -> int:
    return int(np.random.SeedSequence([int(base), int(year)]).generate_state(1)[0])


SEEDS = {y: year_seed(a.seed, y) for y in YEARS}
print("effective per-year seeds (SeedSequence([base_seed, year])): "
      + ", ".join(f"{y}: {s}" for y, s in SEEDS.items()))
check(len(set(SEEDS.values())) == len(YEARS), "every year has a different effective seed")
datasets = {}
for y in YEARS:
    k = [d for d in svc.list_datasets() if d["dataset_name"] == f"NAS100_HISTDATA_{y}" and d["timeframe"] == "5m"]
    if len(k) != 1:
        sys.exit(f"STOP: expected one 5m NAS100_HISTDATA_{y} dataset, found {[d['dataset_id'] for d in k]}")
    datasets[y] = k[0]["dataset_id"]

results, rows = {}, []
for y in YEARS:
    did = datasets[y]
    m = svc.store.get_manifest(did)
    _, oos = oos_windows(m.start, m.end, f"{y}-07-01")
    period = (oos.start, oos.end)
    print(f"\n==== {y}  dataset {did}  OOS [{oos.start.isoformat()}, {oos.end.isoformat()}]  "
          f"base seed {a.seed} -> effective seed {SEEDS[y]}")
    fp0, runs0 = fingerprint(did), runs_state()
    r = svc.random_entry_control(STRAT, did, n_controls=a.controls, seed=SEEDS[y], period=period,
                                 sample_status="OUT_OF_SAMPLE")
    fp1, runs1 = fingerprint(did), runs_state()
    reals, c, cm, cfg, d = r["realizations"], r["candidate"], r["candidate"]["metrics"], r["control_config"], r["dataset"]
    results[y] = r
    print("  integrity:")
    check(all(x["causality_passed"] is True for x in reals) and len(reals) == a.controls,
          f"all {len(reals)} realizations passed causality")
    check(r["stored_as_runs"] is False and runs0 == runs1, f"no run records created ({runs1[0]} before and after)")
    check(all(x["control_strategy_id"].startswith("CTRL_") for x in reals) and
          cand_id not in {x["control_strategy_id"] for x in reals}, "CTRL_ ids, candidate id not among controls")
    check((c["strategy_id"], c["definition_hash"]) == (cand_id, def_hash), "candidate id and definition hash unchanged")
    check(fp0 == fp1, f"dataset manifest/content/bar hashes unchanged ({fp1[3]} bars)")
    check(d["parent_dataset_id"] == did and d["provider"] == "HISTDATA" and d["instrument"] == "NAS100_HISTDATA",
          "OOS window dataset is a restriction of the stored dataset; provider/instrument")
    check((r["cost_profile"], r["cost_status"]) == ("NAS100_HISTDATA@HISTDATA", "assumed"), "cost profile/status")
    check(cfg["method"] == "random_entry_conditional_v2" and cfg["base_seed"] == SEEDS[y]
          and len(cfg["realization_seeds"]) == a.controls, "control method v2, year seed, realization seeds")
    check(r["sample_status"] == "OUT_OF_SAMPLE" and "in-sample" not in r["labels"][0], "labelled out-of-sample")
    check(cfg["period"] == [str(oos.start), str(oos.end)], "provenance records the OOS period")
    print(f"  validation id {r['validation_id']}  OOS dataset {d['dataset_id']}")
    print(f"  CANDIDATE {c['strategy_id']}: signals pre-cooldown {c['pre_cooldown_signals']} final {c['signals']}; "
          f"trades {cm['trade_count']}; gross_r {cm['gross_r']:.4f}; net_r {cm['net_r']:.4f}; "
          f"expectancy {cm['expectancy_r']:.5f}; PF {cm['profit_factor']}; max_dd_r {cm['max_drawdown_r']:.4f}")
    print(f"  CONTROLS {len(reals)}: pre-cooldown signals {rng([x['pre_cooldown_signals'] for x in reals])}; "
          f"final signals {rng([x['signals'] for x in reals])}; trades {rng([x['trade_count'] for x in reals])}")
    cmp_ = r["comparison"]
    print(f"    net_r           {pct(cmp_['net_r'], (5, 25, 50, 75, 95))}")
    for k, ps in (("expectancy_r", (5, 50, 95)), ("profit_factor", (5, 50, 95)), ("max_drawdown_r", (5, 50, 95))):
        print(f"    {k:15} {pct(cmp_[k], ps)}  (finite {cmp_[k]['finite_realizations']})")
    print("    fraction of controls exceeding the candidate: " + ", ".join(
        f"{k} {cmp_[k].get('fraction_of_controls_exceeding_candidate')}" for k in METRICS))
    n = cmp_["net_r"]
    rows.append((y, cm["net_r"], cm["expectancy_r"], cm["trade_count"], cm["gross_r"], n["median"],
                 n["percentiles"].get("5"), n["percentiles"].get("95"),
                 n.get("fraction_of_controls_exceeding_candidate")))

print("\n==== AGGREGATE (descriptive conditional null comparison per OOS period; no pooled test)")
print("| Year | Candidate OOS Net R | Candidate Expectancy | Control Median Net R | Control P5-P95 Net R | Fraction of controls exceeding candidate (net R) |")
print("|---|---|---|---|---|---|")
for y, net, ex, _, _, med, p5, p95, frac in rows:
    print(f"| {y} | {net:.4f} | {ex:.5f} | {med:.4f} | {p5:.4f} to {p95:.4f} | {frac} |")
tn, tt, tg = sum(r[1] for r in rows), sum(r[3] for r in rows), sum(r[4] for r in rows)
print(f"Candidate OOS totals over the five periods (descriptive sums): trades {tt}, gross R {tg:.4f}, "
      f"net R {tn:.4f}, net R per trade {tn / tt if tt else float('nan'):.5f}. Control distributions are NOT pooled.")

all_real_seeds = [s for y in YEARS for s in results[y]["control_config"]["realization_seeds"]]
check(len(set(all_real_seeds)) == len(all_real_seeds), "no realization seed shared between years")

print(f"\n==== REPRODUCIBILITY (2019 repeated with effective seed {SEEDS[2019]})")
again = svc.random_entry_control(STRAT, datasets[2019], n_controls=a.controls, seed=SEEDS[2019],
                                 period=tuple(results[2019]["control_config"]["period"]),
                                 sample_status="OUT_OF_SAMPLE")
same = (again["comparison"] == results[2019]["comparison"]
        and [x["trades_hash"] for x in again["realizations"]] == [x["trades_hash"] for x in results[2019]["realizations"]]
        and again["candidate"]["trades_hash"] == results[2019]["candidate"]["trades_hash"])
check(same, "2019 repeat: identical comparison, candidate trades hash and all control trades hashes")

print("\nLABELS (2019 result):")
print(*("  " + x for x in results[2019]["labels"]), sep="\n")
print("\nDescriptive conditional null comparison only; fractions are ranks within the control distribution, "
      "not p-values; not evidence for or against an edge; not a forecast.")
print("==== STUDY INTEGRITY:", "PASS" if not problems else f"FAIL {problems}")
