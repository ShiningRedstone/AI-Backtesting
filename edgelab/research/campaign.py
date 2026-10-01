"""Frozen-manifest discovery CAMPAIGN (ADR-68): a strategy-factory manifest evaluated exhaustively, once, through the existing
protocol-gated search path.

    freeze  -> spec bound to (manifest, protocol, datasets, config); strategies materialized into the library
    check   -> read-only preflight (no backtest, no trial, no holdout access)
    run     -> preflight again, then ONE Phase-4 search (workers = 1) over the frozen spec; resumable

Nothing here evaluates a strategy itself. Execution is ``research.batch.run_search`` -> ``Services._run_cell`` (protocol gate,
discovery stage, trial ledger, base metrics, prop audit) - the same path as every other search. Consequences:

* ONE trial per strategy. Each strategy resolves to exactly one dataset: the protocol's source dataset or a dataset derived
  from it, with the strategy's OWN timeframe (search eligibility requires an exact timeframe match, so the other timeframes
  produce ineligible, never-executed cells). Higher-timeframe (MTF) features are built inside that same evaluation from the
  same bars (``features.mtf``); prop profiles are a downstream audit of the same trades. Neither adds a trial.
  Trial key (protocol.trial_key) = (protocol_id, logic_hash, content hash of the evaluated discovery slice, config_hash).
* Discovery only: the period is the protocol's discovery window (resolved bars); the protocol gate refuses anything else.
* Resumable: the search id is a pure function of the frozen spec and config; completed cells are skipped on a rerun, so a
  completed strategy never produces a second event. Failed cells (exceptions) are recorded as failed, NOT counted as trials
  (protocol semantics, ADR-56) and are retried by the next run; by default the run stops at the first failure.

The campaign never selects, ranks or filters strategies and never touches the holdout.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping

from edgelab.core.identity import hash_obj

CAMPAIGN_FORMAT = 1
STAGE = "discovery"
REQUIRED_WORKERS = 1
EXPECTED_PROFILES = ("LUCID_LUCIDFLEX_50K", "TRADEIFY_GROWTH_50K", "TRADEIFY_SELECT_DAILY_50K", "TRADEIFY_SELECT_FLEX_50K")
FORBIDDEN_DEFINITION_TEXT = ("starting_equity", "lucid", "tradeify")
RESULT_KEYS = ("metrics", "net_r", "expectancy_r", "profit_factor", "run_id", "trades_hash", "prop")
RESOLUTION_POLICY = ("each strategy runs on exactly ONE dataset: from the protocol source dataset's own import (its root "
                     "dataset or any dataset derived from that root via parent_dataset_id), same instrument/provider, "
                     "with the strategy's own timeframe; explicit overrides must satisfy the same scope; higher "
                     "timeframes (MTF) are derived inside the evaluation; the period is the protocol's discovery "
                     "boundary [discovery session open, holdout session open)")


class CampaignError(ValueError):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


# ======================================================================================== manifest
def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def load_manifest(svc, manifest_id: str) -> tuple[Path, dict, list[dict]]:
    from edgelab.strategy import factory
    try:
        d = svc._factory_manifest_dir(manifest_id)
    except KeyError:
        raise CampaignError("MANIFEST_NOT_FOUND", f"factory manifest {manifest_id} is not in this workspace "
                            "(regenerate it with `factory generate` and verify it)", manifest_id=manifest_id)
    return d, factory.read_header(d), list(factory.iter_rows(d))


def manifest_files(d: Path) -> dict:
    return {"manifest_json_sha256": _sha256(d / "manifest.json"), "strategies_jsonl_sha256": _sha256(d / "strategies.jsonl")}


def manifest_problems(manifest_id: str, header: Mapping, rows: list[Mapping]) -> list[str]:
    """Integrity of a manifest as stored: id = hash(identity), strategies digest, counts, unique ids and logic."""
    out = []
    idn = header.get("identity", {})
    if header.get("manifest_id") != manifest_id:
        out.append(f"manifest.json names {header.get('manifest_id')}, expected {manifest_id}")
    if "FM_" + hash_obj(idn)[:16].upper() != manifest_id:
        out.append("manifest identity does not hash to the manifest id")
    digest = hash_obj([[r["strategy_id"], r["definition_hash"], r["family_id"], r["candidate_seq"]] for r in rows])
    if digest != idn.get("strategies_sha256"):
        out.append("strategies.jsonl does not match the manifest's strategies_sha256 (manifest changed)")
    n = (header.get("counts") or {}).get("valid_unique")
    if n != len(rows):
        out.append(f"manifest declares {n} strategies, strategies.jsonl has {len(rows)}")
    for key, label in (("strategy_id", "strategy ids"), ("logic_hash", "logic hashes")):
        seen, dup = set(), set()
        for r in rows:
            (dup if r[key] in seen else seen).add(r[key])
        if dup:
            out.append(f"duplicate {label}: {sorted(dup)[:20]}" + (f" (+{len(dup) - 20} more)" if len(dup) > 20 else ""))
    quotas = idn.get("quotas") or {}
    from collections import Counter
    got = Counter(r["family_id"] for r in rows)
    if quotas and dict(got) != {k: v for k, v in quotas.items() if v}:
        out.append("per-family counts differ from the manifest's frozen quotas")
    return out


# ======================================================================================== per-strategy requirements
def required_htfs(defn: Any) -> set[str]:
    """Every higher timeframe referenced by a feature of the definition (MTF dependencies)."""
    out: set[str] = set()
    if isinstance(defn, Mapping):
        if "feature" in defn and defn.get("timeframe"):
            out.add(str(defn["timeframe"]))
        for v in defn.values():
            out |= required_htfs(v)
    elif isinstance(defn, list):
        for v in defn:
            out |= required_htfs(v)
    return out


def row_problems(row: Mapping, available_tfs: set[str], max_quantity: int) -> list[str]:
    """Why a manifest row cannot be evaluated as one campaign trial (empty = resolvable)."""
    from edgelab.data.schema import timeframe_minutes
    d = row["definition"]
    out = []
    tf = str(d.get("timeframe"))
    try:
        tfm = timeframe_minutes(tf)
    except ValueError:
        return [f"unparseable timeframe {tf!r}"]
    if tf not in available_tfs:
        out.append(f"no dataset resolved for timeframe {tf}")
    for h in sorted(required_htfs(d)):
        try:
            hm = timeframe_minutes(h)
        except ValueError:
            out.append(f"MTF dependency {h!r} unparseable")
            continue
        if hm < tfm or hm % tfm:
            out.append(f"MTF dependency {h} cannot be derived from the {tf} base bars (must be a multiple of it)")
    sz = d.get("sizing") or {}
    if sz.get("contract") != "MNQ":
        out.append(f"sizing does not bind MNQ (contract={sz.get('contract')!r})")
    if sz.get("mode") == "fixed":
        q = sz.get("quantity")
        if not (isinstance(q, int) and not isinstance(q, bool) and 1 <= q <= max_quantity):
            out.append(f"fixed quantity {q!r} is not a whole number of contracts in 1..{max_quantity}")
    elif sz.get("mode") in ("risk", "equity_risk"):
        if sz.get("max_quantity") != max_quantity:
            out.append(f"risk sizing cap {sz.get('max_quantity')!r} != {max_quantity}")
    else:
        out.append(f"unsupported sizing mode {sz.get('mode')!r}")
    text = json.dumps(d).lower()
    out += [f"definition contains {w!r}" for w in FORBIDDEN_DEFINITION_TEXT if w in text]
    out += [f"manifest row carries result field {k!r}" for k in RESULT_KEYS if k in row or k in (row.get("prop_inputs") or {})]
    return out


def assign_cells(rows: list[Mapping], by_tf: Mapping[str, Mapping]) -> list[dict]:
    """The campaign's cells: exactly one per manifest strategy, on its own timeframe's dataset (MTF and prop add none).
    Raises if any strategy has no dataset for its timeframe."""
    out = []
    for r in rows:
        tf = str(r["definition"]["timeframe"])
        if tf not in by_tf:
            raise CampaignError("PREFLIGHT_UNRESOLVED", f"{r['strategy_id']}: no dataset for timeframe {tf}")
        out.append({"strategy_id": r["strategy_id"], "logic_hash": r["logic_hash"], "timeframe": tf,
                    "dataset_id": by_tf[tf]["dataset_id"], "mtf": sorted(required_htfs(r["definition"]))})
    return out


def check_capacity(n_strategies: int, max_cells: int, trial_budget: int, workers: int) -> list[str]:
    """One cell = one trial per strategy: max_cells must equal the universe and fit the protocol budget."""
    out = []
    if max_cells != n_strategies:
        out.append(f"max_cells {max_cells} must equal the {n_strategies} manifest strategies (one cell per strategy)")
    if trial_budget < n_strategies:
        out.append(f"protocol budget {trial_budget} is below the {n_strategies} unique trials the campaign needs")
    if workers != REQUIRED_WORKERS:
        out.append(f"workers must be {REQUIRED_WORKERS} for this campaign (got {workers})")
    return out


# ======================================================================================== protocol / datasets
def governing_protocol(svc, protocol_id: str | None = None) -> dict:
    from edgelab.research.protocol import verify_record
    act = svc.store.list_protocols(status="ACTIVE")
    if protocol_id:
        act = [p for p in act if p["protocol_id"] == protocol_id]
    if len(act) != 1:
        raise CampaignError("PROTOCOL_NOT_RESOLVED", "exactly one ACTIVE research protocol must govern the campaign",
                            active=[p["protocol_id"] for p in svc.store.list_protocols(status="ACTIVE")],
                            requested=protocol_id)
    verify_record(act[0])
    return act[0]


def _root(rows: Mapping[str, Mapping], did: str) -> str:
    seen = set()
    while rows.get(did, {}).get("parent_dataset_id") and did not in seen:
        seen.add(did)
        did = rows[did]["parent_dataset_id"]
    return did


def resolve_datasets(svc, protocol: Mapping, timeframes: set[str], explicit: Mapping[str, str] | None = None) -> tuple[dict, list]:
    """timeframe -> one dataset (see RESOLUTION_POLICY); problems lists every timeframe that cannot resolve."""
    mat = protocol["material"]
    scope, src = mat["scope"], mat["source_dataset"]["dataset_id"]
    rows = {d["dataset_id"]: d for d in svc.list_datasets()}
    root = _root(rows, src)
    family = {did for did in rows if _root(rows, did) == root}
    by_tf, problems = {}, []
    for tf in sorted(timeframes):
        if explicit and tf in explicit:
            if explicit[tf] not in rows:
                problems.append({"timeframe": tf, "reason": f"explicit dataset {explicit[tf]} not found"})
                continue
            if explicit[tf] not in family:
                problems.append({"timeframe": tf, "reason": f"{explicit[tf]} is not the protocol source import "
                                 f"{root} or a dataset derived from it"})
                continue
            cands = [rows[explicit[tf]]]
        else:
            cands = [rows[did] for did in sorted(family) if rows[did]["timeframe"] == tf]
        cands = [d for d in cands if (d["instrument"], d["provider"]) == (scope["instrument"], scope["provider"])
                 and d["timeframe"] == tf]
        if len(cands) != 1:
            problems.append({"timeframe": tf, "reason": ("no dataset of the protocol source import with this timeframe"
                                                         if not cands else "ambiguous: " +
                                                         ", ".join(sorted(d["dataset_id"] for d in cands)))})
            continue
        d = cands[0]
        by_tf[tf] = {k: d[k] for k in ("dataset_id", "timeframe", "content_hash", "start", "end", "parent_dataset_id")}
    return by_tf, problems


def discovery_period(mat: Mapping) -> dict:
    """[discovery session open, holdout session open - 1 s]: every timeframe's bars of the discovery trading dates."""
    import pandas as pd
    w = mat["windows"]
    end = pd.Timestamp(w["holdout"]["boundary_open"]) - pd.Timedelta(seconds=1)
    return {"start": w["discovery"]["boundary_open"], "end": end.isoformat()}


def dataset_problems(svc, protocol: Mapping, by_tf: Mapping) -> list[dict]:
    """Runnability (costs, ASK OHLC, identity, quality) and discovery coverage of every resolved dataset."""
    import pandas as pd
    from edgelab.data.schema import timeframe_minutes
    w = protocol["material"]["windows"]["discovery"]
    first, last = pd.Timestamp(w["first_bar"]), pd.Timestamp(w["last_bar"])
    rows = {d["dataset_id"]: d for d in svc.list_datasets()}
    out = []
    for tf, ref in sorted(by_tf.items()):
        d = rows.get(ref["dataset_id"])
        if d is None:
            out.append({"timeframe": tf, "dataset_id": ref["dataset_id"], "reason": "dataset no longer in the workspace"})
            continue
        if d["content_hash"] != ref["content_hash"]:
            out.append({"timeframe": tf, "dataset_id": ref["dataset_id"], "reason": "dataset content hash changed"})
        e = svc._dataset_eligibility(d, timeframe_minutes(tf))
        if not e["runnable"]:
            out.append({"timeframe": tf, "dataset_id": ref["dataset_id"], "reason": "; ".join(e["reasons"])})
        s0, s1 = pd.Timestamp(d["start"]), pd.Timestamp(d["end"])
        s0 = s0.tz_localize("UTC") if s0.tz is None else s0
        s1 = s1.tz_localize("UTC") if s1.tz is None else s1
        if s0 > first or s1 < last - pd.Timedelta(minutes=timeframe_minutes(tf)):
            out.append({"timeframe": tf, "dataset_id": ref["dataset_id"],
                        "reason": f"covers {d['start']} .. {d['end']}, not the whole discovery window {first} .. {last}"})
    return out


# ======================================================================================== spec
def campaigns_dir(svc) -> Path:
    return Path(svc.data_root) / "campaigns"


def _profiles(svc) -> list[dict]:
    from edgelab.prop import service as ps
    from edgelab.prop.profiles import profile_hash
    return [{"profile_id": p["profile_id"], "version": p["version"], "schema_version": p.get("schema_version"),
             "profile_hash": profile_hash(p)} for p in ps.default_profiles(svc.root)]


def build_spec(svc, manifest_id: str, *, protocol_id: str | None = None, datasets: Mapping[str, str] | None = None,
               workers: int = REQUIRED_WORKERS, max_cells: int | None = None) -> tuple[dict, dict]:
    """(spec, context). Refuses (CampaignError) anything that would make a partial or multi-trial campaign."""
    from edgelab.engine.sizing import DEFAULT_RESEARCH_ACCOUNT
    from edgelab.research.search import search_hash
    from edgelab.strategy import factory_space as S
    d, header, rows = load_manifest(svc, manifest_id)
    probs = manifest_problems(manifest_id, header, rows)
    if probs:
        raise CampaignError("MANIFEST_INVALID", "the manifest failed its integrity checks", problems=probs)
    p = governing_protocol(svc, protocol_id)
    mat = p["material"]
    n = len(rows)
    cap = check_capacity(n, n if max_cells is None else int(max_cells), mat["trial_budget"]["max_unique_trials"], workers)
    if cap:
        raise CampaignError("CAMPAIGN_CAPACITY", "the campaign does not fit one-trial-per-strategy", problems=cap)
    tfs = {str(r["definition"]["timeframe"]) for r in rows}
    by_tf, unres_tf = resolve_datasets(svc, p, tfs, datasets)
    bad_rows = [{"strategy_id": r["strategy_id"], "timeframe": r["definition"]["timeframe"], "problems": pr}
                for r in rows if (pr := row_problems(r, set(by_tf), S.RISK_QUANTITY_CAP))]
    ds_probs = dataset_problems(svc, p, by_tf)
    if unres_tf or bad_rows or ds_probs:
        raise CampaignError("PREFLIGHT_UNRESOLVED", f"{len(bad_rows)} of {n} strategies cannot resolve their data or "
                            "contract binding; no partial campaign is created", unresolved_timeframes=unres_tf,
                            unresolved_rows=bad_rows, dataset_problems=ds_probs)
    w = mat["windows"]["discovery"]
    ids = sorted(r["strategy_id"] for r in rows)
    search_spec = {"search_spec_version": 1, "strategies": {"ids": ids},
                   "datasets": sorted({v["dataset_id"] for v in by_tf.values()}),
                   "period": discovery_period(mat), "max_cells": n,
                   "workers": REQUIRED_WORKERS, "seed": None}
    cfg_hash = svc._config_hash()
    if cfg_hash != mat["config_hash"]:
        raise CampaignError("PROTOCOL_CONFIG_CHANGED", "the research config differs from the protocol's",
                            protocol_config_hash=mat["config_hash"], current_config_hash=cfg_hash)
    s_hash = hash_obj({"search_hash": search_hash(search_spec, cfg_hash), "protocol_id": p["protocol_id"]})   # as plan_search
    idn = header["identity"]
    spec = {
        "object": "edgelab.research_campaign", "campaign_format": CAMPAIGN_FORMAT, "stage": STAGE,
        "manifest": {"manifest_id": manifest_id, "n_strategies": n, **manifest_files(d),
                     **{k: idn[k] for k in ("strategies_sha256", "catalog_sha256", "capability_sha256", "factory_version",
                                            "variation_space_version", "allocation_version", "seed")}},
        "protocol": {"protocol_id": p["protocol_id"], "protocol_version": mat["protocol_version"],
                     "material_hash": p["material_hash"], "max_unique_trials": mat["trial_budget"]["max_unique_trials"],
                     "family_size_rule": mat["multiple_testing"].get("family_size_rule"),
                     "holdout_looks_budget": mat["holdout_budget"]["max_unique_candidate_evaluations"]},
        "discovery": {"trading_dates": w["trading_dates"], "first_bar": w["first_bar"], "last_bar": w["last_bar"],
                      "period": discovery_period(mat)},
        "dataset_resolution": {"policy": RESOLUTION_POLICY, "explicit": dict(datasets or {}),
                               "source_dataset_id": mat["source_dataset"]["dataset_id"], "by_timeframe": by_tf},
        "execution": {"workers": REQUIRED_WORKERS, "max_cells": n, "entry_point": "search_cell",
                      "trials_per_strategy": 1, "holdout": {"enabled": False},
                      "account": dict(DEFAULT_RESEARCH_ACCOUNT), "execution_contract": "MNQ",
                      "max_quantity": S.RISK_QUANTITY_CAP},
        "prop_simulation": {"enabled": True, "downstream_only": True, "profiles": _profiles(svc)},
        "config_hash": cfg_hash,
        "search": {"spec": search_spec, "search_id": "SRCH_" + s_hash[:12].upper(), "search_hash": s_hash},
    }
    return spec, {"rows": rows, "header": header, "protocol": p, "dir": d}


def campaign_id(spec: Mapping) -> str:
    return "CMP_" + hash_obj(spec, 12).upper()


def library_identity(svc, strategy_id: str) -> tuple | None:
    """The identity the library's stored definition compiles to now (None if it does not compile)."""
    from edgelab.strategy.compiler import compile_definition
    try:
        c = compile_definition(svc.library.load(strategy_id)["definition"], svc.sessions, svc._config_hash())
    except Exception:                                           # noqa: BLE001 - an uncompilable copy is a conflict
        return None
    return (c.identity.strategy_id, c.identity.logic_hash, c.identity.definition_hash)


def materialize(svc, rows: list[Mapping], header: Mapping, cid: str) -> dict:
    """Save every manifest strategy into the library with its exact identity and factory lineage (no evaluation)."""
    from edgelab.strategy.compiler import compile_definition
    from edgelab.strategy.lineage import LineageRecord
    fams = {f["family_id"]: f for f in header.get("families") or []}
    created = present = 0
    mismatches, conflicts = [], []
    for r in rows:
        c = compile_definition(r["definition"], svc.sessions, svc._config_hash())
        if (c.identity.strategy_id, c.identity.logic_hash, c.identity.definition_hash) != \
                (r["strategy_id"], r["logic_hash"], r["definition_hash"]):
            mismatches.append({"strategy_id": r["strategy_id"], "compiled": c.identity.strategy_id})
            continue
        lin = r["lineage"]
        rec = LineageRecord(r["strategy_id"], r["logic_hash"], r["definition_hash"], r["family_id"], "factory_variant",
                            generation_parameters={
                                "manifest_id": header["manifest_id"], "campaign_id": cid,
                                "catalog_sha256": header["identity"]["catalog_sha256"],
                                "family_spec_sha256": lin.get("family_spec_sha256")
                                or (fams.get(r["family_id"]) or {}).get("family_spec_sha256"),
                                "setup": lin.get("setup"), "variation": r["variation"],
                                "candidate_seq": r["candidate_seq"], "allocation_bucket": r["allocation_bucket"],
                                "parent_template": lin.get("parent_template"), "seed": lin.get("seed")},
                            generation_batch_id=header["manifest_id"],
                            versions={k: lin.get(k) for k in ("factory_version", "variation_space_version",
                                                              "allocation_version", "dsl_version", "compiler_version")})
        if svc.library.exists(r["strategy_id"]):                       # never overwritten: it must already be runnable
            if library_identity(svc, r["strategy_id"]) != (r["strategy_id"], r["logic_hash"], r["definition_hash"]):
                conflicts.append(r["strategy_id"])
                continue
        # the EXACT manifest definition is stored (valid DSL input that compiles to the frozen identity); the hashing
        # normal form (canonical_definition) is not guaranteed to be valid DSL input for exit.trailing / no_progress
        if svc.library.save(dict(r["definition"]), c.identity.to_dict(), rec):
            created += 1
        else:
            present += 1
    if mismatches:
        raise CampaignError("IDENTITY_MISMATCH", "manifest rows no longer compile to their recorded identity",
                            mismatches=mismatches[:50], n=len(mismatches))
    if conflicts:
        raise CampaignError("LIBRARY_CONFLICT", "library copies of these strategies do not compile to the manifest "
                            "identity; they are never overwritten (archive them first)", strategies=conflicts[:50],
                            n=len(conflicts))
    return {"created": created, "already_present": present}


def freeze(svc, manifest_id: str, **kw) -> dict:
    """Build, freeze (write once) and materialize a campaign. No evaluation, no trial."""
    spec, ctx = build_spec(svc, manifest_id, **kw)
    cid = campaign_id(spec)
    d = campaigns_dir(svc) / cid
    path = d / "campaign.json"
    doc = {"campaign_id": cid, "spec_hash": hash_obj(spec), "spec": spec}
    if path.exists():
        if json.loads(path.read_text()) != json.loads(json.dumps(doc, default=str)):
            raise CampaignError("CAMPAIGN_TAMPERED", f"{path} exists with different content; it is never overwritten")
    else:
        d.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(doc, indent=1, sort_keys=True, default=str))
        tmp.replace(path)
    mat = materialize(svc, ctx["rows"], ctx["header"], cid)
    return {"campaign_id": cid, "path": str(path), "materialized": mat, "spec": spec}


def load(svc, cid: str) -> dict:
    path = campaigns_dir(svc) / cid / "campaign.json"
    if not path.exists():
        raise CampaignError("CAMPAIGN_NOT_FOUND", f"no frozen campaign {cid} in this workspace")
    doc = json.loads(path.read_text())
    if doc.get("campaign_id") != cid or campaign_id(doc["spec"]) != cid or hash_obj(doc["spec"]) != doc.get("spec_hash"):
        raise CampaignError("CAMPAIGN_TAMPERED", f"{path} does not match its frozen identity")
    return doc["spec"]


# ======================================================================================== ledger view
def ledger(svc, spec: Mapping) -> dict:
    """Trials, failures and holdout accesses of the campaign's protocol, attributed to this campaign or not."""
    pid, sid = spec["protocol"]["protocol_id"], spec["search"]["search_id"]
    ev = svc.store.list_trial_events(pid)
    mine = [e for e in ev if e.get("search_id") == sid]
    counted = [e for e in mine if e["counted"]]
    keys: dict[str, set] = {}
    for e in counted:
        keys.setdefault(e["strategy_id"], set()).add(e["trial_key"])
    cells = svc.store.list_search_cells(sid) if svc.store.get_search_batch(sid) else []
    by_status: dict[str, int] = {}
    for c in cells:
        by_status[c["status"]] = by_status.get(c["status"], 0) + 1
    return {"search_id": sid, "protocol_trials_total": sum(1 for e in ev if e["counted"]),
            "campaign_trials": len(counted), "foreign_trials": sum(1 for e in ev if e["counted"] and e.get("search_id") != sid),
            "campaign_failed_events": sum(1 for e in mine if e["status"] == "failed"),
            "campaign_duplicate_events": sum(1 for e in mine if e["status"] == "completed" and not e["counted"]),
            "strategies_with_trial": len(keys), "strategies_with_multiple_trial_keys": sorted(s for s, k in keys.items() if len(k) > 1),
            "cells_by_status": by_status, "holdout_accesses": len(svc.store.list_holdout_access(pid))}


# ======================================================================================== preflight
def check(svc, cid: str) -> dict:
    """Read-only preflight: never backtests, never writes a ledger row, never reads the holdout."""
    from edgelab.engine.sizing import DEFAULT_RESEARCH_ACCOUNT
    from edgelab.instruments import contract_for
    from edgelab.research.protocol import verify_record
    from edgelab.research.search import plan_search
    from edgelab.strategy import factory_space as S
    from edgelab.strategy.daytrading import check_engine_config
    out: list[dict] = []

    def ok(name: str, cond: bool, detail: Any = None) -> bool:
        out.append({"check": name, "ok": bool(cond), "detail": detail})
        return bool(cond)

    try:
        spec = load(svc, cid)
    except CampaignError as exc:
        ok("campaign spec frozen and intact", False, exc.to_dict())
        return {"campaign_id": cid, "ready": False, "checks": out}
    ok("campaign spec frozen and intact", True, cid)
    m, pr, ex = spec["manifest"], spec["protocol"], spec["execution"]
    n = m["n_strategies"]
    # ---- manifest
    try:
        d, header, rows = load_manifest(svc, m["manifest_id"])
        probs = manifest_problems(m["manifest_id"], header, rows)
        files = manifest_files(d)
        ok("manifest id / hash / files unchanged", not probs and files == {k: m[k] for k in files},
           probs or {"manifest_id": m["manifest_id"], **files})
    except CampaignError as exc:
        ok("manifest id / hash / files unchanged", False, exc.to_dict())
        return {"campaign_id": cid, "ready": False, "checks": out, "spec": spec}
    ok(f"exactly {n} strategies", len(rows) == n == ex["max_cells"], {"rows": len(rows), "max_cells": ex["max_cells"]})
    ok("strategy ids unique", len({r["strategy_id"] for r in rows}) == len(rows))
    ok("logic hashes unique", len({r["logic_hash"] for r in rows}) == len(rows))
    by_tf = spec["dataset_resolution"]["by_timeframe"]
    bad = [{"strategy_id": r["strategy_id"], "problems": p} for r in rows
           if (p := row_problems(r, set(by_tf), ex["max_quantity"]))]
    ok("every strategy: timeframe dataset, MTF dependencies, MNQ whole contracts, no account/prop text", not bad,
       {"unresolved": bad[:100], "n_unresolved": len(bad)} if bad else {"timeframes": sorted(by_tf)})
    # ---- protocol
    try:
        p = governing_protocol(svc, pr["protocol_id"])
        mat = p["material"]
        verify_record(p)
        ok("active protocol is the frozen one (id, version 3, material unchanged)",
           p["material_hash"] == pr["material_hash"] and mat["protocol_version"] == pr["protocol_version"] == 3,
           {"protocol_id": p["protocol_id"], "protocol_version": mat["protocol_version"]})
        ok("protocol capacity covers the campaign", mat["trial_budget"]["max_unique_trials"] >= n,
           mat["trial_budget"]["max_unique_trials"])
        ok("multiplicity family = declared trial budget",
           mat["multiple_testing"].get("family_size_rule") == "declared_max_unique_trials")
        ok("discovery window = protocol discovery", spec["discovery"]["first_bar"] == mat["windows"]["discovery"]["first_bar"]
           and spec["discovery"]["last_bar"] == mat["windows"]["discovery"]["last_bar"]
           and spec["search"]["spec"]["period"] == discovery_period(mat) == spec["discovery"]["period"],
           spec["discovery"]["trading_dates"])
        ok("research config = protocol config", svc._config_hash() == mat["config_hash"] == spec["config_hash"])
        led = ledger(svc, spec)
        done = led["cells_by_status"].get("completed", 0)
        ok("no foreign trials on the protocol", led["foreign_trials"] == 0, led["foreign_trials"])
        ok("protocol trials = completed campaign cells (0 before launch)",
           led["protocol_trials_total"] == led["campaign_trials"] == done,
           {"protocol_trials": led["protocol_trials_total"], "campaign_trials": led["campaign_trials"], "completed": done,
            "state": "fresh" if done == 0 else "resume"})
        ok("one trial key per strategy", not led["strategies_with_multiple_trial_keys"],
           led["strategies_with_multiple_trial_keys"][:20])
        ok("no holdout access", led["holdout_accesses"] == 0, led["holdout_accesses"])
    except CampaignError as exc:
        ok("active protocol is the frozen one (id, version 3, material unchanged)", False, exc.to_dict())
        led = None
    # ---- datasets
    p_for_ds = {"material": {"windows": {"discovery": spec["discovery"]}}}
    dp = dataset_problems(svc, p_for_ds, by_tf)
    ok("resolved datasets present, unchanged, runnable and covering discovery", not dp, dp or sorted(
        v["dataset_id"] for v in by_tf.values()))
    # ---- materialization + plan (no evaluation)
    try:
        plan = plan_search(spec["search"]["spec"], svc)
        el = plan.eligible_cells()
        per = {}
        for c in el:
            per[c["strategy_id"]] = per.get(c["strategy_id"], 0) + 1
        ok("plan: exactly one eligible cell per strategy", len(el) == n and len(per) == n and set(per.values()) == {1},
           {"eligible": len(el), "planned": plan.counts["planned"], "search_id": plan.search_id})
        ok("search id = frozen search id", plan.search_id == spec["search"]["search_id"])
        ok("plan governed by the frozen protocol", plan.protocol_id == pr["protocol_id"], plan.protocol_id)
        want = {c["strategy_id"]: c["dataset_id"] for c in assign_cells(rows, by_tf)} if not bad else {}
        wrong = [c["strategy_id"] for c in el if want.get(c["strategy_id"]) != c["dataset_id"]]
        ok("every cell runs on its timeframe's resolved dataset", not wrong and len(want) == n, wrong[:20])
    except Exception as exc:                                    # noqa: BLE001 - reported, never raised
        ok("plan: exactly one eligible cell per strategy", False, f"{type(exc).__name__}: {exc} "
           "(run `research campaign-freeze` to materialize the strategies)")
    lib_bad = [r["strategy_id"] for r in rows if not svc.library.exists(r["strategy_id"])
               or library_identity(svc, r["strategy_id"]) != (r["strategy_id"], r["logic_hash"], r["definition_hash"])]
    ok("library copies compile to the identical frozen identity", not lib_bad, {"missing_or_changed": lib_bad[:20],
                                                                                "n": len(lib_bad)})
    # ---- environment
    mnq = contract_for(svc.cfg, {"contract": "MNQ"})
    ok("MNQ specification ($2/pt, 0.25 tick, $0.50/tick, whole contracts)",
       (mnq.point_value, mnq.tick_size, mnq.tick_value, mnq.min_size, mnq.size_step) == (2.0, 0.25, 0.5, 1.0, 1.0),
       {"point_value": mnq.point_value, "tick_size": mnq.tick_size, "tick_value": mnq.tick_value})
    ok("$50,000 research account", float(DEFAULT_RESEARCH_ACCOUNT["starting_equity"]) == 50_000.0
       and ex["account"]["starting_equity"] == 50_000.0, DEFAULT_RESEARCH_ACCOUNT)
    ok("max research quantity 40", ex["max_quantity"] == S.RISK_QUANTITY_CAP == 40)
    ok("day-trading engine config", not check_engine_config(svc.cfg["backtest"]), check_engine_config(svc.cfg["backtest"]))
    prof = _profiles(svc)
    ok("four prop profiles load (schema 3) and match the frozen versions",
       [x["profile_id"] for x in prof] == list(EXPECTED_PROFILES) and all(x["schema_version"] == 3 for x in prof)
       and prof == spec["prop_simulation"]["profiles"], prof)
    ok("workers = 1", ex["workers"] == REQUIRED_WORKERS == spec["search"]["spec"]["workers"])
    ok(f"max_cells = {n}", ex["max_cells"] == n == spec["search"]["spec"]["max_cells"])
    ok("holdout disabled", ex["holdout"] == {"enabled": False})
    ready = all(c["ok"] for c in out)
    return {"campaign_id": cid, "ready": ready, "checks": out, "ledger": led,
            "manifest_id": m["manifest_id"], "protocol_id": pr["protocol_id"], "search_id": spec["search"]["search_id"],
            "note": "read-only preflight: no backtest, no trial, no holdout access"}


# ======================================================================================== run
def run(svc, cid: str, *, workers: int = REQUIRED_WORKERS, max_failures: int = 0, _preflight: Mapping | None = None) -> dict:
    """Preflight, then the protocol-gated search of the frozen spec (resumes; stops after ``max_failures`` failed cells)."""
    from edgelab.research.batch import run_search
    if workers != REQUIRED_WORKERS:
        raise CampaignError("WORKERS_NOT_ALLOWED", f"this campaign runs with workers={REQUIRED_WORKERS} only", workers=workers)
    load(svc, cid)                                              # CAMPAIGN_TAMPERED / NOT_FOUND before anything else
    rep = _preflight or check(svc, cid)
    if not rep["ready"]:
        raise CampaignError("PREFLIGHT_FAILED", "preflight failed; nothing was evaluated",
                            failed=[c for c in rep["checks"] if not c["ok"]])
    spec = load(svc, cid)
    sid = spec["search"]["search_id"]

    def stop() -> bool:
        b = svc.store.get_search_batch(sid) or {}
        return int(b.get("n_failed") or 0) > max_failures

    out = run_search(svc, spec["search"]["spec"], REQUIRED_WORKERS, cancel=stop)
    led = ledger(svc, spec)
    failed = [{"strategy_id": c["strategy_id"], "error": c["error"]} for c in svc.store.list_search_cells(sid)
              if c["status"] == "failed"]
    return {"campaign_id": cid, "search_id": sid, "status": out["status"], "ledger": led,
            "complete": led["campaign_trials"] == spec["manifest"]["n_strategies"],
            "failed_cells": failed[:100], "n_failed_cells": len(failed),
            "note": ("failed cells are infrastructure/runtime errors: recorded as failed protocol events, NOT counted as "
                     "trials, retried by the next campaign-run; completed cells are never re-evaluated")}


def status(svc, cid: str) -> dict:
    spec = load(svc, cid)
    led = ledger(svc, spec)
    return {"campaign_id": cid, "manifest_id": spec["manifest"]["manifest_id"], "protocol_id": spec["protocol"]["protocol_id"],
            "n_strategies": spec["manifest"]["n_strategies"], "ledger": led,
            "complete": led["campaign_trials"] == spec["manifest"]["n_strategies"]}
