"""Strategy pool 2 in the workspace (ADR-86): generate the second 10,000 and move research to a 20,000-trial protocol.

Two steps, both started from Run backtest -> Research runs:

1. ``generate``: build pool 2 (``factory_space_p2``) excluding every logic hash of the workspace's pool-1 manifest, write
   it to <data>/strategy_factory/<FM_...>/ and check its integrity. No data, no backtest, no protocol access.
2. ``switch`` (irreversible, typed confirmation): the ACTIVE protocol is retired (kept unchanged as evidence) and a new
   protocol is activated on the SAME dataset, discovery/holdout trading dates, costs and execution, with a trial budget
   of 20,000 (pool 1 + pool 2) and a holdout-look budget equal to the looks the old protocol still had left (looks already
   used are carried over; the strategies they tested are listed as prior exposure and are never tested again). Then
   both pools are frozen as campaigns under the new protocol (each names the other as its sibling, so their trials are
   accounted to the same declared 20,000 family). Re-running is safe: a half-finished switch is completed, never
   repeated.

Pool 1's manifest and campaign are never modified. Nothing here evaluates a strategy.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping

TOTAL_BUDGET = 20_000
CONFIRM_WORD = "SWITCH"
PROTOCOL_NAME = "Strategy pools 1 + 2 (20,000 strategies)"


class Pool2Error(ValueError):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.detail = code, message, detail


def _headers(svc) -> list[tuple[str, dict]]:
    from edgelab.strategy import factory
    d = svc.factory_dir
    out = []
    for m in sorted(d.glob("FM_*")) if d.exists() else []:
        if (m / "manifest.json").exists():
            try:
                out.append((m.name, factory.read_header(m)))
            except (OSError, ValueError):
                continue
    return out


def _pool_of(header: Mapping) -> int:
    return int(((header.get("identity") or {}).get("pool") or {}).get("pool", 1))


def _campaign_specs(svc) -> list[dict]:
    from edgelab.research import campaign as C
    out = []
    root = C.campaigns_dir(svc)
    for d in sorted(root.glob("CMP_*")) if root.is_dir() else []:
        try:
            out.append({"campaign_id": d.name, "spec": C.load(svc, d.name)})
        except C.CampaignError:
            continue
    return out


def _active_protocol(svc) -> dict | None:
    act = svc.store.list_protocols(status="ACTIVE")
    return act[0] if len(act) == 1 else None


def pool1_manifest_id(svc, specs: list[dict] | None = None, active: Mapping | None = None) -> str | None:
    """The pool-1 manifest: the full (10,000) pool-1 manifest a frozen campaign of this workspace uses (the one under
    the active protocol first)."""
    specs = _campaign_specs(svc) if specs is None else specs
    heads = dict(_headers(svc))
    pid = (active or {}).get("protocol_id")
    cands = [s["spec"]["manifest"]["manifest_id"] for s in sorted(
        specs, key=lambda s: s["spec"]["protocol"]["protocol_id"] != pid)]
    for mid in cands:
        h = heads.get(mid)
        if h and _pool_of(h) == 1:
            return mid
    return None


def pool2_manifest_id(svc, pool1: str | None) -> str | None:
    if not pool1:
        return None
    from edgelab.strategy import factory_space_p2 as P2
    for mid, h in _headers(svc):
        idn = h.get("identity") or {}
        if _pool_of(h) == 2 and (idn.get("excluded_pool") or {}).get("manifest_id") == pool1 \
                and idn.get("variation_space_version") == P2.VARIATION_SPACE_VERSION:
            return mid
    return None


# ======================================================================================== status (read-only)
def status(svc) -> dict:
    """Everything the two buttons need, read-only (manifest headers, campaign specs, protocol counters)."""
    from edgelab.research import campaign as C
    specs = _campaign_specs(svc)
    act = _active_protocol(svc)
    p1 = pool1_manifest_id(svc, specs, act)
    p2 = pool2_manifest_id(svc, p1)
    heads = dict(_headers(svc))
    out: dict = {"pool1": None, "pool2": None, "protocol": None, "switched": False, "switch_incomplete": False,
                 "old_campaign": None, "campaigns_under_active": [], "blockers": [], "warnings": [],
                 "confirm_word": CONFIRM_WORD, "total_budget": TOTAL_BUDGET}
    if p1:
        out["pool1"] = {"manifest_id": p1, "n_strategies": heads[p1]["counts"]["valid_unique"]}
    if p2:
        h = heads[p2]
        out["pool2"] = {"manifest_id": p2, "n_strategies": h["counts"]["valid_unique"],
                        "n_families": len(h["identity"]["quotas"]), "seed": h["identity"]["seed"],
                        "candidates_generated": h["counts"]["candidates_generated"]}
    if act is not None:
        st = svc.protocol_status(act["protocol_id"])
        mat = act["material"]
        out["protocol"] = {"protocol_id": act["protocol_id"], "name": mat.get("name"),
                           "trial_budget": st["trials"]["budget"], "trials_used": st["trials"]["unique_numerical_trials"],
                           "holdout_looks_budget": st["holdout"]["budget"], "holdout_looks_used": st["holdout"]["looks_used"],
                           "discovery": st["windows"]["discovery"]["trading_dates"],
                           "holdout": st["windows"]["holdout"]["trading_dates"]}
        under = [s for s in specs if s["spec"]["protocol"]["protocol_id"] == act["protocol_id"]]
        out["campaigns_under_active"] = [{"campaign_id": s["campaign_id"],
                                          "manifest_id": s["spec"]["manifest"]["manifest_id"],
                                          "n_strategies": s["spec"]["manifest"]["n_strategies"]} for s in under]
        mids = {s["spec"]["manifest"]["manifest_id"] for s in under}
        ours = mat.get("name") == PROTOCOL_NAME and mat["trial_budget"]["max_unique_trials"] == TOTAL_BUDGET
        out["switched"] = bool(ours and p1 in mids and p2 in mids)
        out["switch_incomplete"] = bool(ours and not out["switched"])
        if not ours:
            old = [s for s in under if s["spec"]["manifest"]["manifest_id"] == p1]
            if old:
                ids = old[0]["spec"]["search"]["spec"]["strategies"]["ids"]
                prog = C.progress(svc, old[0]["spec"], ids)
                out["old_campaign"] = {"campaign_id": old[0]["campaign_id"], "completed": prog["completed"],
                                       "remaining": prog["remaining"], "failed": prog["failed"]}
    out["blockers"], out["warnings"] = _switch_findings(out)
    return out


def _switch_findings(st: Mapping) -> tuple[list[str], list[str]]:
    blockers, warnings = [], []
    if st["switched"]:
        return ["Research already uses the 20,000-strategy protocol with both pools."], []
    if st["pool1"] is None:
        blockers.append("No pool-1 research campaign (10,000 strategies) was found in this workspace.")
    if st["pool2"] is None:
        blockers.append("Create strategy pool 2 first.")
    if st["protocol"] is None:
        blockers.append("Exactly one active research protocol is needed.")
    elif not st["switch_incomplete"]:
        p = st["protocol"]
        left = p["holdout_looks_budget"] - p["holdout_looks_used"]
        if left < 1:
            blockers.append(f"All {p['holdout_looks_budget']} holdout looks of the current protocol are used; carrying "
                            "them over would leave the new protocol with none.")
        if p["holdout_looks_used"]:
            warnings.append(f"{p['holdout_looks_used']} holdout look(s) were already used. The new protocol gets the "
                            f"{left} look(s) that are left, and the strategies already tested cannot be tested again.")
        oc = st["old_campaign"]
        if oc and oc["remaining"]:
            warnings.append(f"Pool 1 still has {oc['remaining']:,} strategies without a result under the current protocol. "
                            "After the switch they can no longer run under it (everything re-runs under the new one).")
        warnings.append("Pool 1 is re-run from the start under the new protocol (it takes about as long as the first run). "
                        "Its current results stay as evidence under the retired protocol.")
    return blockers, warnings


# ======================================================================================== step 1: generate
def load_pool1(svc, mid: str) -> tuple[dict, list[dict]]:
    from edgelab.research import campaign as C
    d, header, rows = C.load_manifest(svc, mid)
    probs = C.manifest_problems(mid, header, rows)
    if probs:
        raise Pool2Error("POOL1_MANIFEST_INVALID", "the pool-1 manifest failed its integrity checks", problems=probs)
    return header, rows


def generate(svc, progress: Callable[[str], None] | None = None, *, seed: int | None = None,
             quotas: Mapping[str, int] | None = None) -> dict:
    """Generate, write and check pool 2 (excluding every pool-1 logic). Deterministic: the same pool 1 gives the same
    pool 2 (same manifest id). ``seed`` / ``quotas`` exist for tests and the CLI; the app uses the frozen defaults."""
    from edgelab.research import campaign as C
    from edgelab.strategy import factory
    from edgelab.strategy import factory_space_p2 as P2
    say = progress or (lambda _m: None)
    p1 = pool1_manifest_id(svc, None, _active_protocol(svc))
    if p1 is None:
        raise Pool2Error("POOL1_NOT_FOUND", "no pool-1 research campaign (10,000 strategies) in this workspace")
    say("Reading pool 1 (its strategies are excluded from pool 2)")
    _, rows1 = load_pool1(svc, p1)
    exclude = {"manifest_id": p1, "logic_hashes": [r["logic_hash"] for r in rows1]}
    alloc = P2.allocate()
    total = len(alloc)

    def on_family(fid, got, seq):
        done = list(alloc).index(fid) + 1 if fid in alloc else 0
        say(f"Generating pool 2: {done} of {total} families ({P2.FAMILY_BY_ID[fid].name})")

    res = factory.generate(P2.DEFAULT_SEED if seed is None else int(seed), quotas, S=P2, exclude=exclude,
                           progress=on_family)
    say("Writing and checking pool 2")
    d = factory.write_manifest(res, svc.factory_dir / res.manifest_id)
    header, rows = factory.read_header(d), list(factory.iter_rows(d))
    probs = C.manifest_problems(res.manifest_id, header, rows)
    overlap = len({r["logic_hash"] for r in rows} & set(exclude["logic_hashes"]))
    if probs or overlap:
        raise Pool2Error("POOL2_MANIFEST_INVALID", "the written pool-2 manifest failed its checks", problems=probs,
                         overlap_with_pool1=overlap)
    c = header["counts"]
    return {"manifest_id": res.manifest_id, "pool1_manifest_id": p1, "n_strategies": c["valid_unique"],
            "per_family": c["per_family_valid"], "candidates_generated": c["candidates_generated"],
            "rejected": c["rejected"], "duplicates": c["duplicates"],
            "excluded_as_pool1": sum(1 for x in res.duplicates if str(x["duplicate_of"]).startswith("earlier pool"))}


def verify(svc, mid: str) -> dict:
    """Regenerate a pool-2 manifest from its recorded seed / quotas and the pool-1 manifest it excludes."""
    from edgelab.strategy import factory
    from edgelab.strategy import factory_space_p2 as P2
    h = factory.read_header(svc._factory_manifest_dir(mid))
    idn = h["identity"]
    for k, v in (("factory_version", P2.FACTORY_VERSION), ("variation_space_version", P2.VARIATION_SPACE_VERSION),
                 ("allocation_version", P2.ALLOCATION_VERSION)):
        if idn[k] != v:
            return {"reproducible": False, "reason": f"{k} differs: manifest {idn[k]} vs code {v}"}
    p1 = idn["excluded_pool"]["manifest_id"]
    _, rows1 = load_pool1(svc, p1)
    res = factory.generate(idn["seed"], None if idn["full_allocation"] else idn["quotas"], S=P2,
                           exclude={"manifest_id": p1, "logic_hashes": [r["logic_hash"] for r in rows1]})
    return {"reproducible": res.manifest_id == mid, "manifest_id": mid, "regenerated_manifest_id": res.manifest_id}


# ======================================================================================== step 2: switch
def _old_campaign(svc, p1: str, pid: str) -> dict:
    for s in _campaign_specs(svc):
        if s["spec"]["manifest"]["manifest_id"] == p1 and s["spec"]["protocol"]["protocol_id"] == pid:
            return s
    raise Pool2Error("POOL1_CAMPAIGN_NOT_FOUND", "the pool-1 campaign of the active protocol was not found")


def _exposure(svc, old: Mapping) -> tuple[list[dict], dict]:
    """Holdout looks of the old protocol: the runs (prior exposure of the new protocol) and the counts."""
    acc = svc.store.list_holdout_access(old["protocol_id"])
    looks = [a for a in acc if a["status"] != "refused"]
    runs = [{"run_id": a["run_id"], "note": f"holdout look under {old['protocol_id']} (access {a['access_id']}, "
                                            f"logic {a['logic_hash']}); never tested again"}
            for a in looks if a.get("run_id") and svc.store.has_run(a["run_id"])]
    return runs, {"looks_used": len(looks), "strategies": sorted({a["strategy_id"] for a in looks})}


def switch(svc, confirm: str, progress: Callable[[str], None] | None = None) -> dict:
    from edgelab.research import campaign as C
    from edgelab.research.protocol import ProtocolRefusal
    say = progress or (lambda _m: None)
    if confirm != CONFIRM_WORD:
        raise Pool2Error("CONFIRMATION_REQUIRED", f"type {CONFIRM_WORD} to confirm the protocol switch")
    st = status(svc)
    if st["switched"]:
        return {"already_switched": True, "protocol_id": st["protocol"]["protocol_id"],
                "campaigns": st["campaigns_under_active"]}
    if st["blockers"]:
        raise Pool2Error("SWITCH_BLOCKED", "; ".join(st["blockers"]), blockers=st["blockers"])
    p1, p2 = st["pool1"]["manifest_id"], st["pool2"]["manifest_id"]
    act = _active_protocol(svc)
    say("Checking both pools, the datasets and the protocol (nothing is changed yet)")
    if st["switch_incomplete"]:
        new = act
        prior_specs = [s for s in _campaign_specs(svc) if s["spec"]["manifest"]["manifest_id"] == p1
                       and s["spec"]["protocol"]["protocol_id"] != new["protocol_id"]]
        if not prior_specs:
            raise Pool2Error("POOL1_CAMPAIGN_NOT_FOUND", "the earlier pool-1 campaign (its datasets) was not found")
        datasets = {tf: v["dataset_id"] for tf, v in prior_specs[-1]["spec"]["dataset_resolution"]["by_timeframe"].items()}
        old_pid = None
    else:
        old = act
        mat = old["material"]
        oc = _old_campaign(svc, p1, old["protocol_id"])
        datasets = {tf: v["dataset_id"] for tf, v in oc["spec"]["dataset_resolution"]["by_timeframe"].items()}
        if svc._config_hash() != mat["config_hash"]:
            raise Pool2Error("PROTOCOL_CONFIG_CHANGED", "the research configuration differs from the current protocol's; "
                             "the new protocol must keep the same costs and execution")
        for mid in (p1, p2):                                    # every row resolves on the same datasets
            _, header, rows = C.load_manifest(svc, mid)
            probs = C.manifest_problems(mid, header, rows)
            bad = [r["strategy_id"] for r in rows if C.row_problems(r, set(datasets), 40)]
            if probs or bad:
                raise Pool2Error("MANIFEST_NOT_RUNNABLE", f"{mid} cannot run on the current datasets",
                                 problems=probs, unresolved=bad[:20])
        runs, ex = _exposure(svc, old)
        left = mat["holdout_budget"]["max_unique_candidate_evaluations"] - ex["looks_used"]
        trials = svc.protocol_status(old["protocol_id"])["trials"]["unique_numerical_trials"]
        statement = (f"Replaces {old['protocol_id']} (retired, never edited; {trials} unique trials and "
                     f"{ex['looks_used']} holdout looks recorded there). Same dataset, discovery and holdout trading dates, "
                     f"costs and execution. Trial budget {TOTAL_BUDGET} = strategy pool 1 ({p1}) + strategy pool 2 ({p2}); "
                     f"holdout-look budget = {mat['holdout_budget']['max_unique_candidate_evaluations']} - "
                     f"{ex['looks_used']} looks already used = {left}. Strategies holdout-tested under "
                     f"{old['protocol_id']} are listed below and are never tested again: "
                     + (", ".join(ex["strategies"]) or "none") + ".")
        say("Activating the 20,000-strategy protocol (the current one is retired, unchanged)")
        try:
            new = svc.create_protocol(mat["source_dataset"]["dataset_id"],
                                      tuple(mat["windows"]["discovery"]["trading_dates"]),
                                      tuple(mat["windows"]["holdout"]["trading_dates"]), name=PROTOCOL_NAME,
                                      pre_protocol_exposure=runs, exposure_statement=statement,
                                      trial_budget=TOTAL_BUDGET, holdout_looks=left, replaces=old["protocol_id"])
        except ProtocolRefusal as exc:
            raise Pool2Error("PROTOCOL_REFUSED", str(exc), code_detail=getattr(exc, "code", None)) from None
        for k in ("windows", "execution", "config_hash", "source_dataset", "scope"):
            if new["material"][k] != mat[k]:                       # must never happen: same inputs -> same material
                raise Pool2Error("PROTOCOL_MATERIAL_DIFFERS", f"the new protocol's {k} differs from the old one's")
        old_pid = old["protocol_id"]
    pid = new["protocol_id"]
    have = {s["spec"]["manifest"]["manifest_id"]: s["campaign_id"] for s in _campaign_specs(svc)
            if s["spec"]["protocol"]["protocol_id"] == pid}
    specs = {mid: C.build_spec(svc, mid, protocol_id=pid, datasets=datasets)[0] for mid in (p1, p2)}
    sib = {mid: [{"manifest_id": o, "search_id": specs[o]["search"]["search_id"],
                  "n_strategies": specs[o]["manifest"]["n_strategies"]} for o in (p1, p2) if o != mid] for mid in (p1, p2)}
    out = {}
    for label, mid in (("pool 1", p1), ("pool 2", p2)):
        if mid in have:
            out[label] = {"campaign_id": have[mid], "already_frozen": True}
            continue
        say(f"Freezing {label} as a research campaign and adding its strategies to the library")
        r = C.freeze(svc, mid, protocol_id=pid, datasets=datasets, siblings=sib[mid])
        out[label] = {"campaign_id": r["campaign_id"], "materialized": r["materialized"]}
    say("Done")
    return {"protocol_id": pid, "retired_protocol_id": old_pid, "trial_budget": TOTAL_BUDGET,
            "holdout_looks": new["material"]["holdout_budget"]["max_unique_candidate_evaluations"], "campaigns": out}


def run_names(svc) -> dict:
    """Display labels for the campaign switcher: pool and protocol state (no identity changes)."""
    labels = {}
    act = _active_protocol(svc)
    heads = dict(_headers(svc))
    for s in _campaign_specs(svc):
        h = heads.get(s["spec"]["manifest"]["manifest_id"]) or {}
        pool = _pool_of(h) if h else 1
        active = act is not None and s["spec"]["protocol"]["protocol_id"] == act["protocol_id"]
        labels[s["campaign_id"]] = f"Strategy pool {pool}" + ("" if active else " (retired protocol)")
    return labels
