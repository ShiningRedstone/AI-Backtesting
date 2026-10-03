"""Flip scan (ADR-88): the worst discovery results, flipped (fully mirrored) and tested again as NEW strategies.

    preview -> read-only: which discovery results lose clearly BEFORE costs, which of them can be mirrored exactly,
               which mirrors would be new logic; nothing is saved, evaluated or counted
    create  -> the ONE flip companion protocol of the parent (its pre-registered set of flips, its own trial budget and
               holdout looks, a multiplicity family of the parent's declared budget plus the flips), then the flipped
               strategies are saved to the library with mirror lineage (no evaluation)
    run     -> one protocol-gated search of exactly the registered flips on the discovery window (``batch.run_search``
               -> ``Services._run_cell``: the same engine, costs, fills, prop audit and trial ledger as every strategy;
               one trial per flip, counted in the flip protocol); resumable
    results -> read-only: each flip beside its original (stored runs only)

Selection rule (fixed, pre-registered in the flip protocol): the parent protocol's counted discovery trials whose run
has at least MIN_TRADES trades and a one-sided 95% upper bound of the gross (before-cost) R per trade below zero
(mean + 1.645 x sd / sqrt(n) < 0), worst gross R per trade first; flips of holdout-tested strategies, strategies that
cannot be mirrored exactly (strategy/mirror.py), mirrors whose logic is already a known strategy, and strategies whose
original run used another dataset than the flip search would are skipped with their reason; at most ``cap`` are taken.

A loss before costs is required because a strategy that only loses AFTER costs is a cost-driven loser: its mirror pays
the same costs and loses too. Nothing here negates a stored result: every flipped number comes from a new backtest.
"""
from __future__ import annotations

import math
from contextlib import nullcontext
from typing import Any, Callable, Mapping

from edgelab.research import protocol as rp

MIN_TRADES = 30
Z_CLEAR = 1.6448536269514722            # one-sided 95% normal quantile
DEFAULT_CAP = 200
MAX_CAP = 1000
SELECTION_RULE = ("Flip candidates: the parent protocol's discovery results with at least {min_trades} trades whose "
                  "before-cost R per trade is clearly negative (one-sided 95% upper bound below zero: mean + 1.645 x "
                  "sd / sqrt(n) < 0), worst first; strategies already holdout-tested, strategies that cannot be "
                  "mirrored exactly, mirrors that are already known strategies and strategies that ran on another "
                  "dataset are skipped; at most {cap} flips.")
SKIP_LABEL = {
    "MIRROR_TRAILING": "uses a trailing or breakeven stop (no exact mirror)",
    "MIRROR_NO_PROGRESS": "uses a no-progress exit (no exact mirror)",
    "MIRROR_NO_TARGET": "has no profit target (its mirror would have no stop)",
    "DUPLICATE": "its mirror is already a known strategy (tested as an original)",
    "DATASET": "its original ran on another dataset than the flip search would use",
    "HOLDOUT_TESTED": "already holdout-tested (the inverse result on the holdout would be known)",
    "INVALID": "its mirror could not be built",
}
_CACHE: dict = {}


class FlipError(ValueError):
    def __init__(self, code: str, message: str, **detail: Any):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.detail = code, message, detail

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, **self.detail}


# ======================================================================================== parent / flip protocols
def parent_protocol(svc, protocol_id: str | None = None) -> dict:
    """The ACTIVE research protocol whose discovery results are scanned (never a flip protocol)."""
    act = [p for p in svc.store.list_protocols(status="ACTIVE") if not rp.is_companion(p)]     # ADR-93
    if protocol_id:
        act = [p for p in act if p["protocol_id"] == protocol_id]
    if not act:
        raise FlipError("NO_PROTOCOL", "there is no active research protocol; the flip scan reads a protocol's "
                        "discovery results", requested=protocol_id)
    if len(act) > 1:
        raise FlipError("PROTOCOL_AMBIGUOUS", "several research protocols are active; choose one",
                        active=[p["protocol_id"] for p in act])
    rp.verify_record(act[0])
    return act[0]


def flip_protocol(svc, parent: Mapping) -> dict | None:
    """The flip companion of ``parent`` (at most one is ever created; any status)."""
    flips = svc._flip_protocols(parent)
    return flips[-1] if flips else None


# ======================================================================================== selection (read-only)
def _gross(svc, run_ids: list[str]) -> dict[str, dict]:
    """run_id -> {n, mean, sd} of the stored per-trade gross R (before costs)."""
    out: dict[str, dict] = {}
    for i in range(0, len(run_ids), 500):
        chunk = run_ids[i:i + 500]
        try:
            rows = svc.store._query(
                "SELECT run_id, COUNT(*), AVG(gross_r), AVG(gross_r * gross_r) FROM trades WHERE run_id IN "
                f"({', '.join('?' for _ in chunk)}) GROUP BY run_id", tuple(chunk))
        except Exception:                                    # noqa: BLE001 - no trades table yet
            return out
        for rid, n, mean, sq in rows:
            n = int(n or 0)
            if not n or mean is None:
                continue
            var = max(0.0, float(sq) - float(mean) ** 2) * n / (n - 1) if n > 1 else math.nan
            out[rid] = {"n": n, "mean": float(mean), "sd": math.sqrt(var) if math.isfinite(var) else math.nan}
    return out


def upper_bound(g: Mapping) -> float | None:
    """One-sided 95% upper bound of the mean gross R per trade (None when it cannot be computed)."""
    if g["n"] < 2 or not math.isfinite(g["sd"]):
        return None
    return g["mean"] + Z_CLEAR * g["sd"] / math.sqrt(g["n"])


def discovery_results(svc, parent: Mapping) -> list[dict]:
    """The parent's counted discovery trials (latest per strategy) with their gross statistics, worst gross first."""
    latest: dict[str, dict] = {}
    for e in svc.store.list_trial_events(parent["protocol_id"]):
        if e["counted"] and e.get("run_id"):
            latest[e["strategy_id"]] = e
    g = _gross(svc, [e["run_id"] for e in latest.values()])
    out = []
    for sid, e in latest.items():
        st = g.get(e["run_id"])
        ub = upper_bound(st) if st else None
        out.append({"strategy_id": sid, "logic_hash": e["logic_hash"], "run_id": e["run_id"],
                    "dataset_id": e.get("source_dataset_id") or e.get("dataset_id"),
                    "trades": st["n"] if st else 0, "gross_r_per_trade": st["mean"] if st else None,
                    "gross_upper_bound": ub,
                    "clearly_negative": bool(st and st["n"] >= MIN_TRADES and ub is not None and ub < 0)})
    return sorted(out, key=lambda r: (r["gross_r_per_trade"] if r["gross_r_per_trade"] is not None else math.inf,
                                      r["strategy_id"]))


def select(svc, parent: Mapping, cap: int = DEFAULT_CAP) -> dict:
    """The flip selection (read-only): every clearly negative discovery result in order, with its mirror or the reason
    it is skipped, until ``cap`` flips are chosen (the rest is listed as not examined)."""
    from edgelab.research import campaign as C
    from edgelab.strategy.compiler import compile_definition
    from edgelab.strategy.mirror import MirrorRefusal, mirror_definition, refusal_reason
    if not isinstance(cap, int) or isinstance(cap, bool) or not 1 <= cap <= MAX_CAP:
        raise FlipError("CAP_INVALID", f"the scan size must be a whole number from 1 to {MAX_CAP}", cap=cap)
    res = discovery_results(svc, parent)
    neg = [r for r in res if r["clearly_negative"]]
    looked = {a["logic_hash"] for a in svc.store.list_holdout_access(parent["protocol_id"]) if a["status"] != "refused"}
    exposed = rp.holdout_exposed(parent)                     # holdout-tested under an EARLIER protocol (pool switch)
    known = {r["strategy_id"] for r in svc.library.list(include_archived=True)}     # id = STR_ + logic hash prefix
    by_tf: dict[str, Mapping] = {}
    tried: set[str] = set()
    rows, chosen, skipped = [], [], {}
    for r in neg:
        row = {**r, "mirror": None, "skip": None}
        rows.append(row)
        if len(chosen) >= cap:
            continue                                         # not examined: the cap is reached
        try:
            doc = svc.library.load(r["strategy_id"])
        except (KeyError, FileNotFoundError):
            doc = None
        if doc is None:
            row["skip"] = "INVALID"
        else:
            row["timeframe"] = tf = str(doc["definition"].get("timeframe"))
            why = refusal_reason(doc["definition"])
            if tf not in tried:
                tried.add(tf)
                by_tf.update(C.resolve_datasets(svc, parent, {tf})[0])
            if r["logic_hash"] in looked or r["strategy_id"] in exposed:
                row["skip"] = "HOLDOUT_TESTED"
            elif why is not None:
                row["skip"] = why.code
            elif (by_tf.get(tf) or {}).get("dataset_id") != r["dataset_id"]:
                row["skip"] = "DATASET"
            else:
                try:
                    mdef = mirror_definition(doc["definition"], parent_strategy_id=r["strategy_id"])
                    ident = compile_definition(mdef, svc.sessions, svc._config_hash()).identity
                except (MirrorRefusal, ValueError) as exc:
                    row["skip"], row["error"] = "INVALID", str(exc)
                else:
                    if ident.strategy_id in known or any(m["logic_hash"] == ident.logic_hash for m in chosen):
                        row["skip"] = "DUPLICATE"
                        row["duplicate_of"] = ident.strategy_id
                    else:
                        row["mirror"] = m = {
                            "strategy_id": ident.strategy_id, "logic_hash": ident.logic_hash,
                            "definition_hash": ident.definition_hash, "timeframe": tf, "dataset_id": r["dataset_id"],
                            "mirror_of": {"strategy_id": r["strategy_id"], "logic_hash": r["logic_hash"],
                                          "run_id": r["run_id"], "trades": r["trades"],
                                          "gross_r_per_trade": r["gross_r_per_trade"]}}
                        row["_definition"], row["_identity"], row["_doc"] = mdef, ident.to_dict(), doc
                        chosen.append(m)
        if row["skip"]:
            skipped[row["skip"]] = skipped.get(row["skip"], 0) + 1
    used_tfs = {m["timeframe"] for m in chosen}
    return {"parent_protocol_id": parent["protocol_id"], "cap": cap,
            "rule": SELECTION_RULE.format(min_trades=MIN_TRADES, cap=cap),
            "n_discovery_results": len(res), "n_with_enough_trades": sum(1 for r in res if r["trades"] >= MIN_TRADES),
            "n_clearly_negative": len(neg), "n_selected": len(chosen),
            "n_not_examined": sum(1 for x in rows if not x["mirror"] and not x["skip"]),
            "skipped": {k: {"count": v, "label": SKIP_LABEL[k]} for k, v in sorted(skipped.items())},
            "rows": rows, "selected": chosen,
            "datasets_by_timeframe": {tf: dict(by_tf[tf]) for tf in sorted(used_tfs)}}


# ======================================================================================== create (writes)
def create(svc, protocol_id: str | None = None, cap: int = DEFAULT_CAP, holdout_looks: int | None = None) -> dict:
    """Create the flip protocol of the parent (once) and save its flipped strategies to the library. No evaluation."""
    from edgelab.core.identity import code_version
    from edgelab.research.campaign import discovery_period
    parent = parent_protocol(svc, protocol_id)
    pm = parent["material"]
    if flip_protocol(svc, parent) is not None:
        raise FlipError("FLIP_EXISTS", "this research protocol already has its flip scan (one per protocol)",
                        protocol_id=parent["protocol_id"])
    if svc._config_hash() != pm["config_hash"]:
        raise FlipError("PROTOCOL_CONFIG_CHANGED", "the research config differs from the protocol's",
                        protocol_id=parent["protocol_id"])
    sel = select(svc, parent, cap)
    if not sel["selected"]:
        raise FlipError("FLIP_EMPTY", "no discovery result qualifies for flipping (clearly negative before costs, "
                        "mirrorable, new logic)", skipped=sel["skipped"], clearly_negative=sel["n_clearly_negative"])
    looks_used = sum(1 for a in svc.store.list_holdout_access(parent["protocol_id"]) if a["status"] != "refused")
    selection = {k: sel[k] for k in ("rule", "cap", "n_discovery_results", "n_with_enough_trades", "n_clearly_negative",
                                     "n_selected", "n_not_examined")}
    selection.update(min_trades=MIN_TRADES, z_one_sided=Z_CLEAR, ranked_by="before-cost R per trade, worst first",
                     skipped={k: v["count"] for k, v in sel["skipped"].items()})
    name = f"Flip scan of {pm.get('name') or parent['protocol_id']}"
    material = rp.build_flip_material(
        parent, mirror_set=sel["selected"], selection=selection, datasets_by_timeframe=sel["datasets_by_timeframe"],
        discovery_period=discovery_period(pm), parent_looks_used=looks_used,
        holdout_looks=holdout_looks or rp.DEFAULT_HOLDOUT_LOOKS, name=name)
    rec = rp.make_record(material, {"code_version": code_version()})
    key = svc._scope_key(pm["scope"]["instrument"], pm["scope"]["provider"]) + rp.FLIP_SCOPE_SUFFIX
    svc.store.save_protocol(rec, key)
    saved = _save_mirrors(svc, rec, [(r["_definition"], r["_identity"], r["_doc"], r["mirror"])
                                     for r in sel["rows"] if r.get("mirror")])
    return {"flip_protocol_id": rec["protocol_id"], "parent_protocol_id": parent["protocol_id"],
            "n_flips": len(sel["selected"]), "library": saved,
            "family_size": rp.family_size(material, 0), "holdout_looks": material["holdout_budget"]["max_unique_candidate_evaluations"]}


def _save_mirrors(svc, flip: Mapping, items: list[tuple]) -> dict:
    from edgelab.strategy.lineage import LineageRecord
    created = present = 0
    for mdef, ident, doc, m in items:
        gp0 = ((doc.get("lineage") or [{}])[0] or {}).get("generation_parameters") or {}
        rec = LineageRecord(ident["strategy_id"], ident["logic_hash"], ident["definition_hash"],
                            doc.get("family_id") or (mdef.get("family") or {}).get("id") or "flipped", "mirror",
                            parent_strategy_id=m["mirror_of"]["strategy_id"],
                            generation_parameters={"mirror_of": m["mirror_of"]["strategy_id"],
                                                   "mirror_of_run_id": m["mirror_of"]["run_id"],
                                                   "flip_protocol_id": flip["protocol_id"],
                                                   "variation": mirrored_variation(gp0.get("variation")),
                                                   "mirror_of_variation": gp0.get("variation"),
                                                   "rule": "full mirror (strategy/mirror.py)"},
                            generation_batch_id=flip["protocol_id"])
        if svc.library.save(mdef, ident, rec):
            created += 1
        else:
            present += 1
    return {"created": created, "already_present": present}


def mirrored_variation(v: Any) -> dict | None:
    """The original's factory variation as a description of its mirror (names only, never logic): the direction is
    swapped and the stop / target / order / trailing details, which the mirror changes, are left out."""
    from edgelab.strategy.mirror import DIRECTION
    if not isinstance(v, Mapping):
        return None
    out = {k: val for k, val in v.items() if k not in ("stop", "target", "order", "trailing", "direction")}
    if v.get("direction") in DIRECTION:
        out["direction"] = DIRECTION[v["direction"]]
    return out


def materialize(svc, flip: Mapping) -> dict:
    """Make sure every registered flip is in the library with its registered identity (rebuilt from its original by the
    same pure mirror; refuses if it no longer compiles to the registered identity). Idempotent."""
    from edgelab.strategy.compiler import compile_definition
    from edgelab.strategy.mirror import mirror_definition
    todo, bad = [], []
    for m in flip["material"]["mirror_set"]:
        if svc.library.exists(m["strategy_id"]):
            continue
        doc = svc.library.load(m["mirror_of"]["strategy_id"])
        mdef = mirror_definition(doc["definition"], parent_strategy_id=m["mirror_of"]["strategy_id"])
        ident = compile_definition(mdef, svc.sessions, svc._config_hash()).identity
        if (ident.strategy_id, ident.logic_hash) != (m["strategy_id"], m["logic_hash"]):
            bad.append(m["strategy_id"])
            continue
        todo.append((mdef, ident.to_dict(), doc, m))
    if bad:
        raise FlipError("FLIP_IDENTITY_CHANGED", "registered flips no longer rebuild to their registered identity",
                        strategies=bad[:20], n=len(bad))
    return _save_mirrors(svc, flip, todo)


# ======================================================================================== run
def search_spec(flip: Mapping) -> dict:
    """The flip scan's search: exactly the registered flips, their datasets, the discovery period, one cell each."""
    mat = flip["material"]
    ms = mat["mirror_set"]
    return {"search_spec_version": 1, "strategies": {"ids": sorted(m["strategy_id"] for m in ms)},
            "datasets": sorted({m["dataset_id"] for m in ms}), "period": dict(mat["search"]["period"]),
            "max_cells": len(ms), "workers": 1, "seed": None}


def run(svc, flip_id: str, *, processes: int = 1, lock=None, cancel: Callable[[], bool] | None = None,
        on_cell: Callable[..., None] | None = None) -> dict:
    """Run (or resume) the flip search through the normal protocol-gated search path."""
    from edgelab.research.batch import run_search
    guard = lock if lock is not None else nullcontext()
    with guard:
        flip = svc.store.get_protocol(flip_id)
        rp.verify_record(flip)
        if not rp.is_flip(flip):
            raise FlipError("NOT_A_FLIP_PROTOCOL", f"{flip_id} is not a flip protocol")
        if flip["status"] != "ACTIVE":
            raise FlipError("PROTOCOL_NOT_ACTIVE", "the flip protocol is retired", protocol_id=flip_id)
        materialize(svc, flip)
    return run_search(svc, search_spec(flip), processes, lock=lock, cancel=cancel, on_cell=on_cell)


# ======================================================================================== results (read-only)
def results(svc, parent: Mapping, flip: Mapping) -> dict:
    """Each registered flip beside its original, from stored runs and the flip protocol's ledgers only."""
    from edgelab.research import overview as ov
    from edgelab.research.campaign import db_token
    key = ("results", flip["protocol_id"], flip["status"], db_token(svc), svc.library._fingerprint(),
           ov.criteria_profile(svc))
    hit = _CACHE.get(("results", flip["protocol_id"]))
    if hit and hit[0] == key:
        return hit[1]
    mat = flip["material"]
    fid = flip["protocol_id"]
    facets = {f["strategy_id"]: f for f in ov.library_facets(svc)}
    latest: dict[str, dict] = {}
    by_run: dict[str, dict] = {}
    for r in ov.run_records(svc):
        by_run[r["run_id"]] = r
        if r["status"] == "IN_SAMPLE" and not r["holdout"]:
            latest[r["strategy_id"]] = r
    batches = [b for b in svc.store.list_search_batches() if b.get("protocol_id") == fid]
    cells: dict[str, dict] = {}
    for b in batches:
        for c in svc.store.list_search_cells(b["search_id"], current=True):
            if c["status"] != "ineligible":
                cells[c["strategy_id"]] = c
    looks = {a["logic_hash"]: a for a in svc.store.list_holdout_access(fid) if a["status"] != "refused"}

    def side(sid: str, run: Mapping | None) -> dict:
        f = facets.get(sid, {})
        out = {"strategy_id": sid, "display_name": f.get("display_name") or f.get("name"),
               "family_id": f.get("family_id"), "family_name": f.get("family_name"), "timeframe": f.get("timeframe"),
               "run_id": None, "trades": None, "net_r_per_trade": None, "gross_r_per_trade": None, "net_r": None,
               "profit_factor": None, "max_drawdown_r": None, "prop_pass_eval": None, "prop_pass_payout": None,
               "survivor": False, "synthetic": None}
        if run:
            out.update(run_id=run["run_id"], trades=run["trade_count"], net_r_per_trade=run["expectancy_r"],
                       gross_r_per_trade=run["gross_r_per_trade"], net_r=run["net_r"], profit_factor=run["profit_factor"],
                       max_drawdown_r=run["max_drawdown_r"], prop_pass_eval=run["prop_pass_eval"],
                       prop_pass_payout=run["prop_pass_payout"], survivor=run["survivor"], synthetic=run["synthetic"])
        return out

    rows = []
    for m in mat["mirror_set"]:
        fl = side(m["strategy_id"], latest.get(m["strategy_id"]))
        c = cells.get(m["strategy_id"])
        fl["status"] = c["status"] if c else "not_started"
        fl["error"] = c.get("error") if c else None
        a = looks.get(m["logic_hash"])
        if a:
            res = rp_json(a.get("result_json"))
            fl["holdout"] = {"status": a["status"], "outcome": res.get("outcome"), "run_id": a.get("run_id")}
        else:
            fl["holdout"] = None
        rows.append({"flip": fl, "original": side(m["mirror_of"]["strategy_id"], by_run.get(m["mirror_of"]["run_id"]))})
    n_done = sum(1 for r in rows if r["flip"]["status"] == "completed")
    looks_budget = mat["holdout_budget"]["max_unique_candidate_evaluations"]
    out = {"protocol_id": fid, "status": flip["status"], "name": mat.get("name"), "created_at": flip.get("created_at"),
           "selection": mat["selection"], "family_size": rp.family_size(mat, 0),
           "per_test_alpha": rp.bonferroni(mat["multiple_testing"], rp.family_size(mat, 0))["per_test_alpha"],
           "trials": {"used": svc.store.count_trials(fid), "budget": mat["trial_budget"]["max_unique_trials"]},
           "holdout": {"looks_used": len(looks), "looks_budget": looks_budget,
                       "looks_left": max(0, looks_budget - len(looks))},
           "counts": {"flips": len(rows), "completed": n_done,
                      "failed": sum(1 for r in rows if r["flip"]["status"] == "failed"),
                      "remaining": len(rows) - n_done,
                      "net_positive": sum(1 for r in rows if (r["flip"]["net_r_per_trade"] or 0) > 0),
                      "pass_eval": sum(1 for r in rows if r["flip"]["prop_pass_eval"]),
                      "payout": sum(1 for r in rows if r["flip"]["prop_pass_payout"]),
                      "survivors": sum(1 for r in rows if r["flip"]["survivor"])},
           "search_ids": [b["search_id"] for b in batches], "criteria_profile": ov.criteria_profile(svc),
           "rows": rows}
    _CACHE[("results", flip["protocol_id"])] = (key, out)
    return out


def rp_json(x: Any) -> dict:
    import json
    if not x:
        return {}
    try:
        return json.loads(x)
    except (TypeError, ValueError):
        return {}


# ======================================================================================== page model
def preview(svc, protocol_id: str | None = None, cap: int = DEFAULT_CAP) -> dict:
    """What the Flip scan page shows (read-only): the flip protocol's results once created, else a fresh selection."""
    parent = parent_protocol(svc, protocol_id)
    flip = flip_protocol(svc, parent)
    pm = parent["material"]
    base = {"parent": {"protocol_id": parent["protocol_id"], "name": pm.get("name"),
                       "trial_budget": pm["trial_budget"]["max_unique_trials"],
                       "discovery_trading_dates": pm["windows"]["discovery"]["trading_dates"],
                       "holdout_trading_dates": pm["windows"]["holdout"]["trading_dates"]},
            "min_trades": MIN_TRADES, "default_cap": DEFAULT_CAP, "max_cap": MAX_CAP,
            "default_holdout_looks": rp.DEFAULT_HOLDOUT_LOOKS}
    if flip is not None:
        return {**base, "state": "created", "flip": results(svc, parent, flip)}
    from edgelab.research.campaign import db_token
    key = ("preview", parent["protocol_id"], cap, db_token(svc), svc.library._fingerprint(), svc._config_hash())
    hit = _CACHE.get("preview")
    if hit and hit[0] == key:
        sel = hit[1]
    else:
        sel = _public(svc, select(svc, parent, cap))
        _CACHE["preview"] = (key, sel)
    return {**base, "state": "preview", "selection": sel,
            "family_size": int(pm["trial_budget"]["max_unique_trials"]) + sel["n_selected"]}


def _public(svc, sel: Mapping) -> dict:
    """The selection for the page: no internal objects, with display names and the original's net R per trade."""
    from edgelab.research import overview as ov
    facets = {f["strategy_id"]: f for f in ov.library_facets(svc)}
    want = {r["run_id"] for r in sel["rows"]}
    net = {r["run_id"]: r for r in ov.run_records(svc) if r["run_id"] in want}
    rows = []
    for r in sel["rows"]:
        f, run = facets.get(r["strategy_id"], {}), net.get(r["run_id"]) or {}
        rows.append({k: v for k, v in r.items() if not k.startswith("_")} | {
            "display_name": f.get("display_name") or f.get("name"), "family_id": f.get("family_id"),
            "family_name": f.get("family_name"), "net_r_per_trade": run.get("expectancy_r"),
            "synthetic": run.get("synthetic"), "skip_label": SKIP_LABEL.get(r["skip"]) if r["skip"] else None})
    return {**{k: v for k, v in sel.items() if k != "rows"}, "rows": rows}
