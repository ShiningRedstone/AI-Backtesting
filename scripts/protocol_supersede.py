"""Supersede an UNUSED research protocol by a version-3 protocol with a 10,000-unique-trial budget (ADR-67).

Read-only by default: prints the ACTIVE protocol of the scope, its budgets, family rule and usage, and what the new
protocol would be. ``--apply`` then retires the old protocol (never edits it) and activates the new one with the SAME
dataset, discovery/holdout trading dates, pre-protocol exposure and statement; refused if the old protocol has ANY trial
event or holdout access. Consumes no trial and no holdout look.

    .\\.venv\\Scripts\\python.exe scripts\\protocol_supersede.py --root <workspace>                 (check only)
    .\\.venv\\Scripts\\python.exe scripts\\protocol_supersede.py --root <workspace> --apply         (supersede)
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from edgelab.research import protocol as rp  # noqa: E402
from edgelab.services import Services  # noqa: E402


def plan(svc: Services, instrument: str, provider: str, trial_budget: int) -> dict:
    act = svc.store.list_protocols(svc._scope_key(instrument, provider), "ACTIVE")
    if not act:
        return {"active": None, "note": f"no ACTIVE protocol for {instrument}@{provider}"}
    p = act[0]
    rp.verify_record(p)
    mat = p["material"]
    st = svc.protocol_status(p["protocol_id"])
    return {"active": {"protocol_id": p["protocol_id"], "protocol_version": mat.get("protocol_version"),
                       "max_unique_trials": mat["trial_budget"]["max_unique_trials"],
                       "holdout_looks": mat["holdout_budget"]["max_unique_candidate_evaluations"],
                       "family_size_rule": mat["multiple_testing"].get("family_size_rule", "counted_unique_trials"),
                       "trial_events": st["trials"]["evaluation_events"],
                       "unique_trials": st["trials"]["unique_numerical_trials"],
                       "holdout_looks_used": st["holdout"]["looks_used"],
                       "holdout_accesses": len(svc.store.list_holdout_access(p["protocol_id"]))},
            "new": {"protocol_version": rp.PROTOCOL_VERSION, "max_unique_trials": trial_budget,
                    "holdout_looks": mat["holdout_budget"]["max_unique_candidate_evaluations"],
                    "family_size_rule": "declared_max_unique_trials",
                    "per_test_alpha": rp.FAMILYWISE_ALPHA / trial_budget,
                    "bootstrap_replicates": rp.bootstrap_replicates_for(trial_budget),
                    "dataset_id": mat["source_dataset"]["dataset_id"],
                    "discovery": mat["windows"]["discovery"]["trading_dates"],
                    "holdout": mat["windows"]["holdout"]["trading_dates"],
                    "pre_protocol_exposure_runs": [r["run_id"] for r in mat["pre_protocol_exposure"]["runs"]]}}


def apply(svc: Services, instrument: str, provider: str, trial_budget: int, name: str) -> dict:
    act = svc.store.list_protocols(svc._scope_key(instrument, provider), "ACTIVE")
    if not act:
        raise SystemExit(f"no ACTIVE protocol for {instrument}@{provider}")
    mat = act[0]["material"]
    exp = mat["pre_protocol_exposure"]
    return svc.create_protocol(
        mat["source_dataset"]["dataset_id"], tuple(mat["windows"]["discovery"]["trading_dates"]),
        tuple(mat["windows"]["holdout"]["trading_dates"]), name=name or mat["name"],
        pre_protocol_exposure=[{"run_id": r["run_id"], "note": r.get("note", "")} for r in exp["runs"]],
        exposure_statement=exp["statement"], trial_budget=trial_budget,
        holdout_looks=mat["holdout_budget"]["max_unique_candidate_evaluations"], supersedes=act[0]["protocol_id"])


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--root", required=True, help="workspace root (contains configs/ and the data directory)")
    ap.add_argument("--instrument", default="NQ_DUKASCOPY")
    ap.add_argument("--provider", default="DUKASCOPY")
    ap.add_argument("--trial-budget", type=int, default=rp.DEFAULT_TRIAL_BUDGET)
    ap.add_argument("--name", default="")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args(argv)
    svc = Services(root=a.root)
    try:
        print(json.dumps(plan(svc, a.instrument, a.provider, a.trial_budget), indent=1, default=str))
        if a.apply:
            new = apply(svc, a.instrument, a.provider, a.trial_budget, a.name)
            print(json.dumps({"new_protocol_id": new["protocol_id"], "supersedes": new["material"]["supersedes"],
                              "status": svc.protocol_status(new["protocol_id"])["trials"]}, indent=1, default=str))
    finally:
        svc.store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
