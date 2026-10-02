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
from typing import Any, Callable, Mapping

from edgelab.core.fsutil import atomic_write_text
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


_VIEW_CACHE: dict = {}


_TOKENS: dict = {}
VIEW_REFRESH_DURING_RESEARCH_S = 5.0
TOTALS_REFRESH_S = 2.0                                  # live run totals / ETA refresh interval during a run


def _file_change_counter(path: str) -> bytes | None:
    """SQLite's file change counter (header bytes 24-27): incremented by every committed write of ANY connection or
    process (rollback-journal mode)."""
    try:
        with open(path, "rb") as fh:
            head = fh.read(28)
        return head[24:28] if len(head) == 28 else None
    except OSError:
        return None


def _research_running(svc) -> bool:
    jm = getattr(svc, "_jobs", None)
    a = getattr(jm, "_active", None) if jm is not None else None
    return a is not None and getattr(a, "state", None) in ("queued", "running")


def read_outside_lock(svc, guard, fn: Callable):
    """Run a READ-ONLY step of a research run on a pooled read-only connection without the service lock (ADR-78), so
    page requests are not held up; exact values (not a page read). Falls back to the lock when the store has no
    read-only connections or the step turns out to write."""
    import sqlite3
    rc = getattr(svc, "read_context", None)
    if rc is not None:
        with rc(page=False) as ok:
            if ok:
                try:
                    return fn()
                except sqlite3.OperationalError as exc:
                    if "readonly" not in str(exc):
                        raise
    with guard:
        return fn()


def db_token(svc) -> tuple:
    """Changes whenever ANY row of the store changes: the writer connection's own writes (total_changes) or a commit
    by any other connection or process (the file change counter). The same value in every thread, whichever
    connection a request reads through (ADR-78). Read-only views use it as a cache key. Page requests (read context)
    during a running research job reuse it for up to VIEW_REFRESH_DURING_RESEARCH_S, so views are rebuilt at most
    that often instead of after every evaluated strategy; research code itself always sees the exact value."""
    import time
    w = getattr(svc, "writer_store", None) or svc.store
    con = getattr(w, "con", None)
    if con is None:
        return (id(w), object())                            # unknown backend: never cached
    reading = getattr(svc, "in_page_read", lambda: False)()
    now = time.monotonic()
    last = _TOKENS.get(id(w))
    if reading and last is not None and last[1] is w and now - last[2] < VIEW_REFRESH_DURING_RESEARCH_S \
            and _research_running(svc):
        return last[0]
    tok = (id(w), con.total_changes, _file_change_counter(w.path) if w.path != ":memory:" else None)
    _TOKENS[id(w)] = (tok, w, now)
    return tok


def manifest_rows_view(svc, manifest_id: str) -> tuple[dict, list[dict]]:
    """The frozen manifest's header and rows for READ-ONLY views (browser, detail, results, ETA), parsed once and
    reused while its files are unchanged on disk. Runs, freezing and checks keep calling ``load_manifest`` (fresh)."""
    from edgelab.strategy import factory
    try:
        d = svc._factory_manifest_dir(manifest_id)
    except KeyError:
        raise CampaignError("MANIFEST_NOT_FOUND", f"factory manifest {manifest_id} is not in this workspace "
                            "(regenerate it with `factory generate` and verify it)", manifest_id=manifest_id)
    sig = tuple((p.name, p.stat().st_size, p.stat().st_mtime_ns) for p in sorted(Path(d).glob("*")) if p.is_file())
    key = ("manifest", str(d), sig)
    hit = _VIEW_CACHE.get(("manifest", str(d)))
    if hit and hit[0] == key:
        return hit[1], hit[2]
    header, rows = factory.read_header(d), list(factory.iter_rows(d))
    _VIEW_CACHE[("manifest", str(d))] = (key, header, rows)
    return header, rows


def _view_key(svc, cid: str) -> tuple:
    """Everything a campaign's read-only views depend on: its spec file, its run records (+ names) and the store."""
    base = campaigns_dir(svc) / cid
    files = [base / "campaign.json"] + (sorted((base / "runs").glob("*.json")) if (base / "runs").is_dir() else [])
    return (cid, db_token(svc), tuple((p.name, p.stat().st_mtime_ns, p.stat().st_size) for p in files if p.exists()))


def cached_view(svc, kind: str, cid: str, build):
    """Memoize one read-only campaign view (tree, detail) until anything it depends on changes."""
    key = (kind, _view_key(svc, cid))
    hit = _VIEW_CACHE.get((kind, cid))
    if hit and hit[0] == key:
        return hit[1]
    out = build()
    _VIEW_CACHE[(kind, cid)] = (key, out)
    return out


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
    from edgelab.research.protocol import is_flip, verify_record
    act = [p for p in svc.store.list_protocols(status="ACTIVE") if not is_flip(p)]     # ADR-87: never a flip protocol
    if protocol_id:
        act = [p for p in act if p["protocol_id"] == protocol_id]
    if len(act) != 1:
        raise CampaignError("PROTOCOL_NOT_RESOLVED", "exactly one ACTIVE research protocol must govern the campaign",
                            active=[p["protocol_id"] for p in svc.store.list_protocols(status="ACTIVE") if not is_flip(p)],
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
def run(svc, cid: str, *, workers: int = REQUIRED_WORKERS, max_failures: int = 0, _preflight: Mapping | None = None,
        processes: int = 1) -> dict:
    """Preflight, then the protocol-gated search of the frozen spec (resumes; stops after ``max_failures`` failed cells).
    The whole campaign scope (CLI ``campaign-run``)."""
    out = run_scope(svc, cid, workers=workers, max_failures=max_failures, _preflight=_preflight, source="cli",
                    processes=processes)
    return {k: out[k] for k in ("campaign_id", "search_id", "status", "ledger", "complete", "failed_cells",
                                "n_failed_cells", "note", "run_record_id")}


def max_processes() -> int:
    """CPU cores research runs may use on this machine (ADR-77)."""
    import os
    return max(1, os.cpu_count() or 1)


def default_processes() -> int:
    """All cores but one (at least 1): the default for research runs (ADR-77)."""
    return max(1, max_processes() - 1)


def run_scope(svc, cid: str, *, families: list[str] | None = None, strategy_ids: list[str] | None = None,
              workers: int = REQUIRED_WORKERS, max_failures: int = 0,
              lock=None, cancel: Callable[[], bool] | None = None, on_progress: Callable[[dict], None] | None = None,
              _preflight: Mapping | None = None, source: str = "cli", processes: int = 1) -> dict:
    """Run a family SCOPE (None = every family) of a frozen campaign: preflight, then the SAME frozen search with only the
    scope's cells executing (``batch.run_search(include=...)``). Out-of-scope cells stay pending for a later run; the
    search id, cell ids, trial keys and budget accounting are the campaign's own. A durable run record (history) is
    written to <data>/campaigns/<CMP>/runs/ at every step; the authoritative results are the usual store rows.
    ``processes`` (ADR-77) is HOW MANY CPU cores compute cells at the same time: an execution setting, not part of the
    frozen spec, the search id or any identity. Results are written by this process alone, in plan order, so the cells,
    trades, run order and trial ledger are the same as with 1; a stop lets the cells already computing on other cores
    finish (they are recorded like any cell)."""
    from contextlib import nullcontext
    from edgelab.research.batch import run_search
    guard = lock if lock is not None else nullcontext()
    if workers != REQUIRED_WORKERS:
        raise CampaignError("WORKERS_NOT_ALLOWED", f"this campaign runs with workers={REQUIRED_WORKERS} only", workers=workers)
    if not isinstance(processes, int) or isinstance(processes, bool) or not 1 <= processes <= max_processes():
        raise CampaignError("PROCESSES_INVALID", f"processes must be 1..{max_processes()} (CPU cores of this machine)",
                            processes=processes)
    spec = load(svc, cid)                                       # CAMPAIGN_TAMPERED / NOT_FOUND before anything else
    _, header, rows = load_manifest(svc, spec["manifest"]["manifest_id"])
    ids, fams = scope_ids(header, rows, families, strategy_ids)
    whole = families is None and strategy_ids is None
    by_id = {r["strategy_id"]: r for r in rows}
    fam_of = {r["strategy_id"]: r["family_id"] for r in rows}
    sid = spec["search"]["search_id"]
    rec = new_run_record(spec, fams, len(ids), source)
    rec["all_families"] = whole
    rec["processes"] = processes
    rec["scope_kind"] = "all" if whole else "families" if strategy_ids is None else "strategies"
    save_scope(svc, cid, rec["run_record_id"], ids)                 # the selected scope, persisted once
    rec["scope_file"] = f"{rec['run_record_id']}.scope.json"
    remaining_rows = [by_id[i] for i in ids]

    def emit(**kw) -> None:
        rec.update(kw)
        rec["updated_at"] = _now()
        try:
            save_run_record(svc, rec)
        except PermissionError:                  # Windows: the file stayed open elsewhere through every retry. A progress
            if rec["status"] not in ("preflight", "running"):   # update is skipped (the next one rewrites the whole
                raise                                           # record); a final status is never lost silently
        if on_progress is not None:
            on_progress(dict(rec))

    import time as _time
    t_pre = _time.perf_counter()
    emit(status="preflight", phase=f"Preparing research - {len(ids):,} strategies selected; verifying the frozen campaign "
                                   f"({spec['manifest']['n_strategies']:,} strategies, datasets, protocol, prop profiles; no backtest)")
    try:
        if _preflight is None:                                   # read-only: never holds the service lock (ADR-78)
            _preflight = read_outside_lock(svc, guard, lambda: check(svc, cid))
        if not _preflight["ready"]:
            failed = [c for c in _preflight["checks"] if not c["ok"]]
            emit(status="failed", phase="preflight failed; nothing was evaluated", finished_at=_now(),
                 errors=[{"check": c["check"], "detail": c["detail"]} for c in failed][:50])
            raise CampaignError("PREFLIGHT_FAILED", "preflight failed; nothing was evaluated", failed=failed,
                                run_record_id=rec["run_record_id"])
        before = read_outside_lock(svc, guard, lambda: progress(svc, spec, ids))
        est = read_outside_lock(svc, guard, lambda: estimate(svc, spec, remaining_rows, before))
        emit(status="running", phase="Running research", started_at=_now(), totals=before, eta=est,
             preflight_seconds=round(_time.perf_counter() - t_pre, 3),
             counts={"completed_this_run": 0, "failed_this_run": 0, "skipped_completed": 0})

        refreshed = [0.0]

        def on_cell(event: str, c: Mapping, k: int, total: int) -> None:
            from edgelab.strategy.presentation import display_name
            cnt = dict(rec["counts"])
            if event == "start":
                row = by_id.get(c["strategy_id"], {})
                emit(current={"strategy_id": c["strategy_id"], "family_id": fam_of.get(c["strategy_id"]),
                              "display_name": display_name(row) if row else c["strategy_id"],
                              "timeframe": c.get("strategy_timeframe"), "dataset_id": c["dataset_id"],
                              "index": k + 1, "of": total, "started_at": _now()})
                return
            if event == "skip":
                cnt["skipped_completed"] += 1
            elif c["_status"] == "completed":
                cnt["completed_this_run"] += 1
            else:
                cnt["failed_this_run"] += 1
                errs = list(rec["errors"])[-49:] + [{"strategy_id": c["strategy_id"], "error": c.get("_error"),
                                                     "at": _now()}]
                rec["errors"] = errs
            now = _time.monotonic()
            if event == "done" and (now - refreshed[0] >= TOTALS_REFRESH_S or k + 1 >= total):
                refreshed[0] = now                           # progress and ETA from the PERSISTED cells, read-only,
                tot = read_outside_lock(svc, guard, lambda: progress(svc, spec, ids))      # at most every 2 s
                est = read_outside_lock(svc, guard, lambda: estimate(svc, spec, remaining_rows, tot))
                emit(counts=cnt, totals=tot, eta=est, current=None)
            elif event == "done":
                emit(counts=cnt, current=None)
            else:
                emit(counts=cnt)

        def stop() -> bool:
            if cancel is not None and cancel():
                return True
            b = read_outside_lock(svc, guard, lambda: svc.store.get_search_batch(sid)) or {}
            return int(b.get("n_failed") or 0) > max_failures

        out = run_search(svc, spec["search"]["spec"], processes, lock=lock, cancel=stop,
                         include=None if whole else set(ids), on_cell=on_cell)
    except CampaignError:
        raise
    except BaseException as exc:                              # infrastructure failure: recorded, never a trial
        emit(status="failed", phase="stopped by an error", finished_at=_now(),
             errors=list(rec["errors"])[-49:] + [{"error": f"{type(exc).__name__}: {exc}", "at": _now()}])
        raise
    with guard:
        led = ledger(svc, spec)
        after = progress(svc, spec, ids)
        failed = [{"strategy_id": c["strategy_id"], "error": c["error"]} for c in svc.store.list_search_cells(sid)
                  if c["status"] == "failed"]
    user_cancel = cancel is not None and cancel()
    final = ("cancelled" if user_cancel else "stopped_on_failure" if out["status"] == "cancelled"
             else "completed" if after["remaining"] == 0 else "incomplete")
    emit(status=final, phase=final, finished_at=_now(), totals=after, current=None, batch_status=out["status"],
         eta=estimate(svc, spec, remaining_rows, after))
    return {"campaign_id": cid, "search_id": sid, "status": out["status"], "run_status": final, "ledger": led,
            "scope": after, "complete": led["campaign_trials"] == spec["manifest"]["n_strategies"],
            "failed_cells": failed[:100], "n_failed_cells": len(failed), "run_record_id": rec["run_record_id"],
            "note": ("failed cells are infrastructure/runtime errors: recorded as failed protocol events, NOT counted as "
                     "trials, retried by the next campaign-run; completed cells are never re-evaluated")}


# ======================================================================================== scope / progress / history
def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def family_order(header: Mapping, rows: list[Mapping]) -> list[str]:
    """Families in the manifest catalog's own order (never a ranking)."""
    order = [f["family_id"] for f in header.get("families") or []]
    extra = sorted({r["family_id"] for r in rows} - set(order))
    return [f for f in order if any(r["family_id"] == f for r in rows)] + extra


def scope_ids(header: Mapping, rows: list[Mapping], families: list[str] | None,
              strategy_ids: list[str] | None = None) -> tuple[list[str], list[str]]:
    """(strategy ids, families) of a run scope: explicit frozen strategy ids, or whole families, or None = everything.
    The frozen ids are used exactly as they are (manifest order); unknown ids or families are refused."""
    allf = family_order(header, rows)
    if strategy_ids is not None:
        want = set(strategy_ids)
        known = {r["strategy_id"] for r in rows}
        unknown = sorted(want - known)
        if unknown or not want:
            raise CampaignError("UNKNOWN_STRATEGY" if unknown else "EMPTY_SCOPE",
                                "select at least one frozen strategy of this campaign's manifest", unknown=unknown[:20],
                                n_unknown=len(unknown))
        ids = [r["strategy_id"] for r in rows if r["strategy_id"] in want]
        fams = sorted({r["family_id"] for r in rows if r["strategy_id"] in want}, key=allf.index)
        return ids, fams
    if families is None:
        return [r["strategy_id"] for r in rows], allf
    fams = list(dict.fromkeys(families))
    unknown = [f for f in fams if f not in allf]
    if unknown or not fams:
        raise CampaignError("UNKNOWN_FAMILY" if unknown else "EMPTY_SCOPE",
                            "select at least one family of this campaign's manifest", unknown=unknown,
                            available=allf)
    want = set(fams)
    return [r["strategy_id"] for r in rows if r["family_id"] in want], [f for f in allf if f in want]


def _cells(svc, spec: Mapping) -> dict[str, dict]:
    sid = spec["search"]["search_id"]
    key = ("cells", sid, db_token(svc))
    hit = _VIEW_CACHE.get(("cells", sid))
    if hit and hit[0] == key:
        return hit[1]
    if not svc.store.get_search_batch(sid):
        return {}
    out = {c["strategy_id"]: c for c in svc.store.list_search_cells(sid, current=True) if c["status"] != "ineligible"}
    _VIEW_CACHE[("cells", sid)] = (key, out)
    return out


def progress(svc, spec: Mapping, ids: list[str]) -> dict:
    """Durable progress of a scope from the search's stored cells (the authoritative state)."""
    cells = _cells(svc, spec)
    by: dict[str, int] = {}
    for s in ids:
        st = (cells.get(s) or {}).get("status", "not_started")
        by[st] = by.get(st, 0) + 1
    done = by.get("completed", 0)
    return {"strategies": len(ids), "completed": done, "failed": by.get("failed", 0),
            "remaining": len(ids) - done, "by_status": by,
            "fraction_done": round(done / len(ids), 6) if ids else None}


MIN_OBS = 3            # observations a timing group needs before its median is used


def timing_observations(svc, spec: Mapping, fam_of: Mapping[str, str], tf_of: Mapping[str, str]) -> list[dict]:
    """Persisted per-cell execution durations of this campaign's search (completed cells of every run, current or not)."""
    sid = spec["search"]["search_id"]
    if not svc.store.get_search_batch(sid):
        return []
    out = []
    for c in svc.store.list_search_cells(sid):
        if c.get("duration_s") is None or c["status"] not in ("completed", "failed"):
            continue
        out.append({"strategy_id": c["strategy_id"], "family_id": fam_of.get(c["strategy_id"]),
                    "timeframe": tf_of.get(c["strategy_id"]), "started_at": c.get("started_at"),
                    "finished_at": c.get("finished_at"), "duration_s": float(c["duration_s"]),
                    "success": c["status"] == "completed"})
    return out


def _median(xs: list[float]) -> float:
    xs = sorted(xs)
    n = len(xs)
    return xs[n // 2] if n % 2 else (xs[n // 2 - 1] + xs[n // 2]) / 2.0


def estimate_remaining(observations: list[Mapping], remaining: list[Mapping], min_obs: int = MIN_OBS) -> dict:
    """Data-based ETA for `remaining` [{family_id, timeframe}] from observed durations (workers = 1, so a plain sum).
    Each remaining strategy uses the median of the most specific group with >= min_obs observations:
    (family, timeframe) -> timeframe -> all. Fewer than min_obs observations in total -> state 'estimating' (no number)."""
    ok = [o for o in observations if o.get("success") and o.get("duration_s") is not None]
    base = {"state": "estimating", "n_observations": len(ok), "min_observations": min_obs, "workers": 1,
            "remaining_strategies": len(remaining), "remaining_seconds": None, "basis": None,
            "note": "estimate from observed execution durations of this campaign; not an exact completion time"}
    if len(ok) < min_obs or not remaining:
        if not remaining:
            base.update(state="estimate", remaining_seconds=0.0, basis={"all": {"n": len(ok)}})
        return base
    by_ft: dict[tuple, list[float]] = {}
    by_tf: dict[str, list[float]] = {}
    for o in ok:
        by_ft.setdefault((o.get("family_id"), o.get("timeframe")), []).append(o["duration_s"])
        by_tf.setdefault(o.get("timeframe"), []).append(o["duration_s"])
    overall = _median([o["duration_s"] for o in ok])
    total = 0.0
    used = {"family_timeframe": 0, "timeframe": 0, "all": 0}
    for r in remaining:
        k = (r.get("family_id"), r.get("timeframe"))
        if len(by_ft.get(k, [])) >= min_obs:
            total += _median(by_ft[k])
            used["family_timeframe"] += 1
        elif len(by_tf.get(r.get("timeframe"), [])) >= min_obs:
            total += _median(by_tf[r.get("timeframe")])
            used["timeframe"] += 1
        else:
            total += overall
            used["all"] += 1
    base.update(state="estimate", remaining_seconds=round(total, 1),
                basis={"groups_used": used, "overall_median_s": round(overall, 3),
                       "median_by_timeframe_s": {tf: round(_median(v), 3) for tf, v in sorted(by_tf.items()) if len(v) >= min_obs}})
    return base


def estimate(svc, spec: Mapping, scope_rows: list[Mapping], prog: Mapping | None = None) -> dict:
    """ETA for the not-yet-completed strategies of a scope, from this campaign's persisted cell timings."""
    cells = _cells(svc, spec)
    fam_of = {r["strategy_id"]: r["family_id"] for r in scope_rows}
    tf_of = {r["strategy_id"]: str(r["definition"]["timeframe"]) for r in scope_rows}
    remaining = [{"family_id": r["family_id"], "timeframe": tf_of[r["strategy_id"]]} for r in scope_rows
                 if (cells.get(r["strategy_id"]) or {}).get("status") != "completed"]
    header, rows = manifest_rows_view(svc, spec["manifest"]["manifest_id"])
    all_fam = {r["strategy_id"]: r["family_id"] for r in rows}
    all_tf = {r["strategy_id"]: str(r["definition"]["timeframe"]) for r in rows}
    obs = timing_observations(svc, spec, all_fam, all_tf)
    return estimate_remaining(obs, remaining)


def scopes_dir(svc, cid: str) -> Path:
    return runs_dir(svc, cid)


def save_scope(svc, cid: str, rid: str, ids: list[str]) -> None:
    d = runs_dir(svc, cid)
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(d / f"{rid}.scope.json",
                      json.dumps({"run_record_id": rid, "campaign_id": cid, "strategy_ids": list(ids)}))


def load_scope(svc, cid: str, rid: str) -> dict:
    p = runs_dir(svc, cid) / f"{rid}.scope.json"
    if not p.exists():
        raise CampaignError("SCOPE_NOT_FOUND", f"no persisted scope for run {rid}")
    return json.loads(p.read_text())


def tree(svc, cid: str) -> dict:
    """The research browser: All strategies -> families (catalog order) -> strategies (manifest order), with presentation
    names, timeframes and persisted status. Frozen ids only; nothing is ranked."""
    from edgelab.strategy.presentation import display_name
    spec = load(svc, cid)
    header, rows = manifest_rows_view(svc, spec["manifest"]["manifest_id"])
    cells = _cells(svc, spec)
    meta = {f["family_id"]: f for f in header.get("families") or []}
    by_tf = spec["dataset_resolution"]["by_timeframe"]
    fams = []
    for fid in family_order(header, rows):
        mine = [r for r in rows if r["family_id"] == fid]
        m = meta.get(fid, {})
        strategies = []
        for r in mine:
            c = cells.get(r["strategy_id"]) or {}
            tf = str(r["definition"]["timeframe"])
            strategies.append({"strategy_id": r["strategy_id"], "display_name": display_name(r), "timeframe": tf,
                               "status": c.get("status", "not_started"), "run_id": c.get("run_id")})
        fams.append({"family_id": fid, "name": m.get("name", fid), "group": m.get("group"), "hypothesis": m.get("hypothesis"),
                     "n_strategies": len(mine), "completed": sum(1 for x in strategies if x["status"] == "completed"),
                     "failed": sum(1 for x in strategies if x["status"] == "failed"), "strategies": strategies})
    ids = [r["strategy_id"] for r in rows]
    return {"campaign_id": cid, "n_strategies": len(rows), "families": fams, "progress": progress(svc, spec, ids),
            "datasets": {tf: v["dataset_id"] for tf, v in sorted(by_tf.items())},
            "data_line": "Nasdaq / canonical BID-ASK research data (Dukascopy USATECH index CFD proxy); each strategy runs on "
                         "the dataset of its own frozen timeframe",
            "note": "run scope browser: families in catalog order, strategies in manifest order; not a ranking"}


def runs_dir(svc, cid: str) -> Path:
    return campaigns_dir(svc) / cid / "runs"


def new_run_record(spec: Mapping, families: list[str], n: int, source: str) -> dict:
    import uuid
    rid = "CR_" + _now()[:19].replace("-", "").replace(":", "").replace("T", "_") + "_" + uuid.uuid4().hex[:6].upper()
    return {"object": "edgelab.campaign_run", "run_record_id": rid, "campaign_id": campaign_id(spec), "source": source,
            "search_id": spec["search"]["search_id"], "protocol_id": spec["protocol"]["protocol_id"],
            "protocol_version": spec["protocol"]["protocol_version"], "manifest_id": spec["manifest"]["manifest_id"],
            "families": list(families), "all_families": None, "n_scope": n, "created_at": _now(), "started_at": None,
            "finished_at": None, "updated_at": None, "status": "created", "phase": "", "current": None,
            "counts": {"completed_this_run": 0, "failed_this_run": 0, "skipped_completed": 0}, "totals": None,
            "errors": [], "workers": REQUIRED_WORKERS, "holdout": "disabled", "eta": None, "preflight_seconds": None,
            "scope_kind": None, "scope_file": None}


def save_run_record(svc, rec: Mapping) -> None:
    d = runs_dir(svc, rec["campaign_id"])
    d.mkdir(parents=True, exist_ok=True)
    atomic_write_text(d / (rec["run_record_id"] + ".json"), json.dumps(rec, indent=1, sort_keys=True, default=str))


def run_records(svc, cid: str) -> list[dict]:
    d = runs_dir(svc, cid)
    out = []
    for f in sorted(d.glob("CR_*.json")) if d.is_dir() else []:
        if f.name.endswith(".scope.json"):
            continue
        try:
            out.append(json.loads(f.read_text()))
        except (OSError, ValueError):
            continue
    return sorted(out, key=lambda r: r.get("created_at") or "", reverse=True)


def reconcile_run_records(svc) -> list[str]:
    """Run records left 'preflight'/'running' by a process that is gone -> 'interrupted' (resumable)."""
    fixed = []
    root = campaigns_dir(svc)
    for d in sorted(root.glob("CMP_*")) if root.is_dir() else []:
        for rec in run_records(svc, d.name):
            if rec.get("status") in ("created", "preflight", "running"):
                rec.update(status="interrupted", phase="interrupted (the app or process stopped); resumable",
                           current=None, updated_at=_now())
                save_run_record(svc, rec)
                fixed.append(rec["run_record_id"])
    return fixed


def list_campaigns(svc) -> list[dict]:
    """Every frozen campaign in the workspace with its durable progress and latest run (read-only)."""
    out = []
    root = campaigns_dir(svc)
    for d in sorted(root.glob("CMP_*")) if root.is_dir() else []:
        try:
            spec = load(svc, d.name)
        except CampaignError as exc:
            out.append({"campaign_id": d.name, "error": exc.to_dict()})
            continue
        ids = spec["search"]["spec"]["strategies"]["ids"]
        recs = run_records(svc, d.name)
        out.append({**summary(spec), "progress": progress(svc, spec, ids), "n_runs": len(recs),
                    "latest_run": recs[0] if recs else None})
    return out


def summary(spec: Mapping) -> dict:
    """Governance header of a campaign (what controls every run of it)."""
    ex = spec["execution"]
    return {"campaign_id": campaign_id(spec), "created_from": "frozen campaign spec", "stage": spec["stage"],
            "manifest": {k: spec["manifest"][k] for k in ("manifest_id", "n_strategies", "strategies_sha256",
                                                          "factory_version", "variation_space_version")},
            "protocol": dict(spec["protocol"]), "search_id": spec["search"]["search_id"],
            "discovery": {"trading_dates": spec["discovery"]["trading_dates"], "period": spec["discovery"]["period"]},
            "datasets": {tf: {"dataset_id": v["dataset_id"], "content_hash": v["content_hash"]}
                         for tf, v in sorted(spec["dataset_resolution"]["by_timeframe"].items())},
            "execution": {"workers": ex["workers"], "max_cells": ex["max_cells"], "holdout": ex["holdout"],
                          "account": ex["account"], "execution_contract": ex["execution_contract"],
                          "max_quantity": ex["max_quantity"], "trials_per_strategy": ex["trials_per_strategy"]},
            "prop_simulation": spec["prop_simulation"], "config_hash": spec["config_hash"]}


def detail(svc, cid: str) -> dict:
    """Campaign governance + per-family scope progress (catalog order) + run history."""
    spec = load(svc, cid)
    header, rows = manifest_rows_view(svc, spec["manifest"]["manifest_id"])
    cells = _cells(svc, spec)
    meta = {f["family_id"]: f for f in header.get("families") or []}
    fams = []
    for fid in family_order(header, rows):
        mine = [r for r in rows if r["family_id"] == fid]
        tfs: dict[str, int] = {}
        for r in mine:
            tf = str(r["definition"]["timeframe"])
            tfs[tf] = tfs.get(tf, 0) + 1
        by: dict[str, int] = {}
        for r in mine:
            st = (cells.get(r["strategy_id"]) or {}).get("status", "not_started")
            by[st] = by.get(st, 0) + 1
        m = meta.get(fid, {})
        fams.append({"family_id": fid, "name": m.get("name", fid), "group": m.get("group"),
                     "hypothesis": m.get("hypothesis"), "n_strategies": len(mine), "timeframes": tfs,
                     "completed": by.get("completed", 0), "failed": by.get("failed", 0),
                     "remaining": len(mine) - by.get("completed", 0)})
    ids = [r["strategy_id"] for r in rows]
    recs = run_records(svc, cid)
    return {**summary(spec), "families": fams, "progress": progress(svc, spec, ids), "runs": recs,
            "eta": estimate(svc, spec, rows), "latest_run": recs[0] if recs else None,
            "prop_results_available": any(c["status"] == "completed" for c in cells.values()),
            "data_line": "Nasdaq / canonical BID-ASK research data (Dukascopy USATECH index CFD proxy); dataset chosen "
                         "automatically from each strategy's frozen timeframe",
            "note": "family selection is a run scope, not a quality filter; families are listed in catalog order"}


HEADLINE_KEYS = ("trade_count", "net_r", "expectancy_r", "profit_factor", "max_drawdown_r", "sample_label")


def family_results(svc, cid: str, family_id: str) -> dict:
    """Stored per-strategy results of one family (manifest order; nothing is re-run, nothing is ranked)."""
    from edgelab.strategy.presentation import display_name, explanation
    spec = load(svc, cid)
    header, rows = manifest_rows_view(svc, spec["manifest"]["manifest_id"])
    mine = [r for r in rows if r["family_id"] == family_id]
    if not mine:
        raise CampaignError("UNKNOWN_FAMILY", f"{family_id} is not a family of this campaign")
    by_tf = spec["dataset_resolution"]["by_timeframe"]
    cells = _cells(svc, spec)
    out = []
    for r in mine:
        c = cells.get(r["strategy_id"]) or {}
        head = json.loads(c["headline_json"]) if c.get("headline_json") else {}
        tf = str(r["definition"]["timeframe"])
        out.append({"strategy_id": r["strategy_id"], "logic_hash": r["logic_hash"], "name": r["definition"].get("name"),
                    "display_name": display_name(r), "explanation": explanation(r),
                    "timeframe": tf, "dataset_id": by_tf.get(tf, {}).get("dataset_id"),
                    "status": c.get("status", "not_started"), "run_id": c.get("run_id"), "error": c.get("error"),
                    "duration_s": c.get("duration_s"), "trades_hash": c.get("trades_hash"),
                    "result_available": bool(c.get("run_id")), "headline": {k: head.get(k) for k in HEADLINE_KEYS}})
    fmeta = {f["family_id"]: f for f in header.get("families") or []}.get(family_id, {})
    tfs: dict[str, int] = {}
    for r in mine:
        tfs[str(r["definition"]["timeframe"])] = tfs.get(str(r["definition"]["timeframe"]), 0) + 1
    return {"campaign_id": cid, "family_id": family_id, "name": fmeta.get("name", family_id), "group": fmeta.get("group"),
            "hypothesis": fmeta.get("hypothesis"), "n": len(out), "timeframes": tfs,
            "completed": sum(1 for x in out if x["status"] == "completed"),
            "remaining": sum(1 for x in out if x["status"] != "completed"), "strategies": out,
            "note": "historical results under the campaign's stated assumptions, in manifest order (not ranked)"}


def strategy_result(svc, cid: str, strategy_id: str) -> dict:
    """One strategy's stored result with full provenance (base metrics + prop audit; nothing is re-run)."""
    spec = load(svc, cid)
    header, rows = manifest_rows_view(svc, spec["manifest"]["manifest_id"])
    row = next((r for r in rows if r["strategy_id"] == strategy_id), None)
    if row is None:
        raise CampaignError("UNKNOWN_STRATEGY", f"{strategy_id} is not in this campaign")
    from edgelab.strategy.presentation import present
    c = _cells(svc, spec).get(strategy_id) or {}
    tf = str(row["definition"]["timeframe"])
    fmeta = {f["family_id"]: f for f in header.get("families") or []}.get(row["family_id"], {})
    out = {"campaign_id": cid, "strategy_id": strategy_id, "family_id": row["family_id"],
           "family_name": fmeta.get("name", row["family_id"]), "status": c.get("status", "not_started"),
           "presentation": present(row), "timeframe": tf,
           "dataset_id": spec["dataset_resolution"]["by_timeframe"].get(tf, {}).get("dataset_id"),
           "sizing": row["definition"].get("sizing"), "account": spec["execution"]["account"],
           "execution_contract": spec["execution"]["execution_contract"], "duration_s": c.get("duration_s"),
           "error": c.get("error"), "run_id": c.get("run_id"),
           "provenance": {"protocol_id": spec["protocol"]["protocol_id"], "protocol_version": spec["protocol"]["protocol_version"],
                          "campaign_id": cid, "search_id": spec["search"]["search_id"],
                          "manifest_id": spec["manifest"]["manifest_id"], "strategies_sha256": spec["manifest"]["strategies_sha256"],
                          "logic_hash": row["logic_hash"], "definition_hash": row["definition_hash"],
                          "config_hash": spec["config_hash"], "account": spec["execution"]["account"],
                          "execution_contract": spec["execution"]["execution_contract"],
                          "prop_profiles": spec["prop_simulation"]["profiles"]}}
    if c.get("run_id") and svc.store.has_run(c["run_id"]):
        rec, trades = svc.store.load_run(c["run_id"])
        out.update({"dataset": rec.get("dataset"), "metrics": rec.get("metrics") or rec.get("headline_metrics"),
                    "trades_hash": rec.get("trades_hash"), "n_trades": int(len(trades)), "prop": rec.get("prop"),
                    "run_status": rec.get("status"), "created_at": rec.get("created_at")})
        out["provenance"].update(dataset_id=(rec.get("dataset") or {}).get("dataset_id"),
                                 dataset_content_hash=(rec.get("dataset") or {}).get("content_hash"))
    return out


def status(svc, cid: str) -> dict:
    spec = load(svc, cid)
    led = ledger(svc, spec)
    return {"campaign_id": cid, "manifest_id": spec["manifest"]["manifest_id"], "protocol_id": spec["protocol"]["protocol_id"],
            "n_strategies": spec["manifest"]["n_strategies"], "ledger": led,
            "complete": led["campaign_trials"] == spec["manifest"]["n_strategies"]}
