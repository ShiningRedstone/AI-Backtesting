"""Read models for the research terminal UI: home overview, strategy explorer, research dashboard,
per-run analytics and the candidate pipeline.

Everything here READS existing stores (strategy library, run registry, search batches, research
protocols and their ledgers, prop simulations, AI generations) and re-uses existing analytics
(``compute_metrics``, the Phase-5 breakdowns, ``cost_sensitivity``). Nothing here evaluates a
strategy, records a trial, touches a protocol ledger, reads the holdout for a new evaluation or
changes any stored object. No ranking score, no "best" label, no promotion: rows keep the status of
the runs they come from, every aggregate carries its basis (gross / net of stated costs) and scope
(in-sample / out-of-sample / walk-forward / holdout), and pipeline states are derived from stored
facts only (never inferred from a metric).
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

import numpy as np
import pandas as pd

from edgelab.research.lab import SCOPE_LABEL

WEEKDAYS = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")
MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
LOCAL_TZ = "America/New_York"
MAX_PAGE_SIZE = 200
MAX_POOLED_RUNS = 200
ROLLING_WINDOW = 50
SCOPES = {"in_sample": ("IN_SAMPLE",), "oos": ("OUT_OF_SAMPLE",), "walk_forward": ("WALK_FORWARD",),
          "any": ("IN_SAMPLE", "OUT_OF_SAMPLE", "WALK_FORWARD")}
DESCRIPTIVE = ("Descriptive statistics of stored backtests under the stated assumptions: historical, not a "
               "forecast and not evidence of an edge by themselves. Runs of related strategies share data "
               "and are not independent.")
SORT_KEYS = ("strategy_id", "name", "family_id", "timeframe", "trade_count", "trades_per_week", "win_rate",
             "expectancy_r", "gross_r_per_trade", "net_r", "profit_factor", "max_drawdown_r", "cost_r_per_trade",
             "created_at", "n_runs", "avg_rr", "short_name")


def _f(x) -> float | None:
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def _loads(x):
    if isinstance(x, (dict, list)) or x is None:
        return x
    try:
        return json.loads(x)
    except (TypeError, ValueError):
        return None


# ------------------------------------------------------------------------------ facts (cached)
_FACET_CACHE: dict[str, Any] = {}


def _operand_kind(side: Any) -> str | None:
    if isinstance(side, Mapping):
        return str(side.get("type")) if side.get("type") is not None else None
    return None


def trailing_kind(ex: Mapping) -> str:
    """How a strategy trails its stop: none, breakeven, level:<feature> (e.g. an EMA or swing level), distance:<kind>."""
    tr = ex.get("trailing")
    if not isinstance(tr, Mapping):
        return "none"
    mode = str(tr.get("mode") or "")
    if mode == "breakeven":
        return "breakeven"
    if mode == "level":
        lv = tr.get("level") or {}
        return f"level:{lv.get('feature') or _operand_kind(lv) or 'level'}" if isinstance(lv, Mapping) else "level"
    if mode == "distance":
        d = tr.get("distance") or {}
        return f"distance:{_operand_kind(d) or 'points'}" if isinstance(d, Mapping) else "distance"
    return mode or "none"


def strategy_facets(doc: Mapping) -> dict:
    """Filterable descriptors of one stored strategy, straight from its definition."""
    d = doc.get("definition") or {}
    entry, ex = d.get("entry") or {}, d.get("exit") or {}
    fam = d.get("family") or {}
    lineage = doc.get("lineage") or [{}]
    first = lineage[0] if lineage else {}
    gp = first.get("generation_parameters") or {}
    return {"strategy_id": doc.get("strategy_id"), "name": doc.get("name") or d.get("name"),
            "family_id": doc.get("family_id") or fam.get("id"), "family_name": fam.get("name"),
            "category": fam.get("category"), "hypothesis": fam.get("hypothesis"),
            "timeframe": d.get("timeframe"), "session": entry.get("session"),
            "direction": entry.get("direction"),
            "entry_type": str((entry.get("order") or {}).get("type") or "market"),
            "stop_type": _operand_kind(ex.get("stop")), "target_type": _operand_kind(ex.get("target")) or "none",
            "target_multiple": _f((ex.get("target") or {}).get("multiple")) if isinstance(ex.get("target"), Mapping) else None,
            "trailing": trailing_kind(ex), "signal_exit": "yes" if ex.get("signal") else "no",
            "source": first.get("generation_method"), "proposal_id": gp.get("proposal_id"),
            "parent_strategy_id": first.get("parent_strategy_id"),
            "created_at": first.get("generation_timestamp"), "logic_hash": doc.get("logic_hash"),
            "archived": bool(doc.get("archived")), **display_names(doc)}


_HASH_TAIL = re.compile(r"[_ ][0-9a-f]{6,}$")


def display_names(doc: Mapping) -> dict:
    """Readable names of one stored strategy (presentation only; identity untouched): ``display_name`` (family and
    distinguishing settings, as on the strategy page) and ``short_name`` (the distinguishing settings alone, for tables
    that show the family separately). Never contains the id-like hash suffix of generated machine names."""
    from edgelab.strategy import presentation as pr
    d = doc.get("definition") or {}
    gp = ((doc.get("lineage") or [{}])[0] or {}).get("generation_parameters") or {}
    v = gp.get("variation") if isinstance(gp.get("variation"), Mapping) else None
    row = {"definition": d, "variation": v, "family_id": doc.get("family_id"), "strategy_id": doc.get("strategy_id")}
    try:
        full = pr.display_name(row)
    except Exception:                                        # noqa: BLE001 - presentation must never break a listing
        full = pr.humanize(_HASH_TAIL.sub("", str(doc.get("name") or d.get("name") or "Strategy")))
    fp = (v or {}).get("family_params") or {}
    short = pr._params_text(pr._name_params(fp)) if fp else pr.humanize(_HASH_TAIL.sub("", str(doc.get("name") or d.get("name") or "")))
    return {"display_name": full, "short_name": short or full}


FACETS_CACHE_VERSION = "facets/2"


def facets_cache_path(svc) -> Path:
    """Where the on-disk facets cache of this workspace lives: OUTSIDE the workspace (opening a workspace never writes
    into it), in a per-user cache folder (``EDGELAB_VIEW_CACHE``; Windows ``%LOCALAPPDATA%\\EdgeLab-Cache``; elsewhere
    ``$XDG_CACHE_HOME`` or ``~/.cache``), one file per workspace data root."""
    import hashlib
    import os
    base = os.environ.get("EDGELAB_VIEW_CACHE")
    if not base:
        if os.name == "nt" and os.environ.get("LOCALAPPDATA"):
            base = str(Path(os.environ["LOCALAPPDATA"]) / "EdgeLab-Cache" / "view_cache")
        else:
            base = str(Path(os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")) / "edgelab" / "view_cache")
    key = hashlib.sha256(str(Path(svc.data_root).resolve()).encode()).hexdigest()[:20]
    return Path(base) / f"library_facets_{key}.json"


def library_facets(svc) -> list[dict]:
    """Facets of every active strategy, cached on the library's own content fingerprint: in memory, and on disk under
    a per-user cache folder (``facets_cache_path``) so a restart does not re-read every strategy file (a speed cache only: derived from the
    library, rebuilt whenever the fingerprint or this code's version differs, safe to delete)."""
    lib = svc.library
    fp = lib._fingerprint()
    key = f"{lib.root}:{fp}"
    hit = _FACET_CACHE.get("facets")
    if hit and hit[0] == key:
        return hit[1]
    disk = facets_cache_path(svc)
    try:
        doc = json.loads(disk.read_text(encoding="utf-8"))
        if doc.get("version") == FACETS_CACHE_VERSION and doc.get("fingerprint") == fp:
            _FACET_CACHE["facets"] = (key, doc["rows"])
            return doc["rows"]
    except (OSError, ValueError, KeyError, TypeError):
        pass
    rows = []
    for r in lib.list():
        try:
            rows.append(strategy_facets(lib.load(r["strategy_id"])))
        except (KeyError, ValueError, OSError):
            continue
    _FACET_CACHE["facets"] = (key, rows)
    try:
        disk.parent.mkdir(parents=True, exist_ok=True)
        tmp = disk.with_suffix(".tmp")
        tmp.write_text(json.dumps({"version": FACETS_CACHE_VERSION, "fingerprint": fp, "rows": rows}, default=str),
                       encoding="utf-8")
        tmp.replace(disk)
    except OSError:
        pass                                                  # an unwritable cache only costs speed
    return rows


def run_records(svc) -> list[dict]:
    """Every stored run record (no trades), oldest first; cached on (count, last run id)."""
    from edgelab.research.campaign import db_token
    key = db_token(svc)                                       # any store write invalidates (records are immutable)
    hit = _FACET_CACHE.get("runs")
    if hit and hit[0] == key:
        out = hit[1]
    else:
        rows = svc.store._query("SELECT run_id, record_json FROM runs ORDER BY run_id")
        out = [run_row(rid, _loads(raw) or {}) for rid, raw in rows]
        _FACET_CACHE["runs"] = (key, out)
    hr = holdout_run_ids(svc)
    crit = criteria_profile(svc)
    return [{**apply_criteria(r, crit), "holdout": True, "scope": HOLDOUT_SCOPE} if r["run_id"] in hr
            else {**apply_criteria(r, crit), "holdout": False} for r in out]


DEFAULT_CRITERIA_PROFILE = "LUCID_LUCIDFLEX_50K"
CAMPAIGN_RUN_REF = re.compile(r"^(CMP_[0-9A-F]{12})/(CR_\d{8}_\d{6}_[0-9A-F]{6})$")


def campaign_run_scope(svc, ref: Any) -> set[str] | None:
    """The strategy ids selected in one research run ("<campaign id>/<run record id>"), or None for all backtests.
    Read from the run's persisted scope file (written once when the run started; never edited here)."""
    if not ref:
        return None
    m = CAMPAIGN_RUN_REF.match(str(ref))
    if not m:
        raise ValueError("campaign_run must be '<campaign id>/<run record id>'")
    from edgelab.research import campaign as C
    try:
        return set(C.load_scope(svc, m.group(1), m.group(2))["strategy_ids"])
    except C.CampaignError as exc:
        raise ValueError(f"research run {ref} has no stored selection") from exc


def criteria_profile(svc) -> str | None:
    """The prop rule profile whose stored audit decides "passes evaluation / payout" and survivors (workspace preference,
    display only; default LucidFlex 50K). None only when the preference cannot be read."""
    try:
        return svc.ui_preferences()["prop_criteria_profile"]
    except Exception:                                        # noqa: BLE001 - unreadable preferences: fall back
        return DEFAULT_CRITERIA_PROFILE


def apply_criteria(row: Mapping, profile_id: str | None) -> dict:
    """Pass flags and the survivor flag of one run row under ONE rule profile's stored chronological audit
    (``profile_id`` None: any profile, the ADR-73 rule). A run without an audit for that profile is not a survivor and
    its pass flags are None (not audited), never False."""
    prop = row.get("prop") or []
    if profile_id is None:
        ev = any(p["evaluation"] == "PASS" for p in prop) if prop else None
        pay = any(p["passes_with_payout"] for p in prop) if prop else None
    else:
        p = next((x for x in prop if x.get("profile_id") == profile_id), None)
        ev = (p["evaluation"] == "PASS") if p else None
        pay = bool(p["passes_with_payout"]) if p else None
    return {**row, "criteria_profile": profile_id, "prop_pass_eval": ev, "prop_pass_payout": pay,
            "survivor": bool(pay and (row.get("expectancy_r") or 0.0) > 0)}


HOLDOUT_SCOPE = "Holdout evaluation (protocol)"


def holdout_run_ids(svc) -> set[str]:
    """Runs made by a protocol holdout evaluation (status OUT_OF_SAMPLE, but never an ordinary OOS test)."""
    try:
        return {r[0] for r in svc.store._query("SELECT run_id FROM holdout_access WHERE run_id IS NOT NULL")}
    except Exception:                                        # noqa: BLE001 - no protocol tables in this store
        return set()


def prop_summary(rec: Mapping) -> list[dict]:
    """The chronological prop audit stored with the run (ADR-64/65), one compact row per rule profile."""
    out = []
    for x in (rec.get("prop") or {}).get("profiles") or []:
        prof, ev = x.get("profile") or {}, x.get("evaluation") or {}
        evs = ev.get("status") or (x.get("summary") or {}).get("evaluation_status")
        payouts = int((x.get("totals") or {}).get("n_payouts") or (x.get("summary") or {}).get("payout_count") or 0)
        size = _f(prof.get("account_size"))
        name = " ".join(str(v) for v in (prof.get("provider"), prof.get("product")) if v) or None
        if name and size:
            name += f" {size / 1000:g}K"
        out.append({"profile_id": prof.get("profile_id"), "profile_name": name, "version": prof.get("version"),
                    "status": x.get("status"),
                    "evaluation": evs, "failure_reason": ev.get("failure_reason"), "payouts": payouts,
                    "pass_days": (ev.get("pass") or {}).get("trading_days"),
                    "passes_with_payout": evs == "PASS" and payouts >= 1,
                    "rule_basis_state": x.get("rule_basis_state")})
    return out


def run_row(run_id: str, rec: Mapping, criteria: str | None = None) -> dict:
    d, a, s = rec.get("dataset") or {}, rec.get("assumptions") or {}, rec.get("strategy") or {}
    hm = rec.get("headline_metrics") or {}
    n = int(hm.get("trade_count") or 0)
    gross, cost = _f(hm.get("gross_r")), _f(hm.get("cost_r"))
    status = rec.get("status") or "IN_SAMPLE"
    costs = a.get("costs") or {}
    aw, al = _f(hm.get("avg_winner_r")), _f(hm.get("avg_loser_r"))
    prop = prop_summary(rec)
    row = {"run_id": run_id, "created_at": rec.get("created_at"), "status": status,
            "scope": SCOPE_LABEL.get(status, status), "strategy_id": s.get("strategy_id"),
            "strategy_name": (s.get("dsl") or {}).get("name"), "dataset_id": d.get("dataset_id"),
            "instrument": d.get("instrument"), "provider": d.get("provider"), "timeframe": d.get("timeframe"),
            "start": str(d.get("start")), "end": str(d.get("end")),
            "synthetic": str(rec.get("notes") or "").startswith("SYNTHETIC"),
            "notes": rec.get("notes"), "cost_status": a.get("cost_status"), "cost_profile": costs.get("profile"),
            "cost_scenario": costs.get("scenario"), "spread_source": costs.get("spread_source"),
            "trade_count": n, "trades_per_week": _f(hm.get("trades_per_week")), "win_rate": _f(hm.get("win_rate")),
            "expectancy_r": _f(hm.get("expectancy_r")), "net_r": _f(hm.get("net_r")),
            "gross_r": gross, "cost_r": cost,
            "gross_r_per_trade": gross / n if gross is not None and n else None,
            "cost_r_per_trade": cost / n if cost is not None and n else None,
            "profit_factor": _f(hm.get("profit_factor")), "max_drawdown_r": _f(hm.get("max_drawdown_r")),
            "sample_label": hm.get("sample_label"),
            "avg_winner_r": aw, "avg_loser_r": al, "avg_rr": aw / abs(al) if aw is not None and al not in (None, 0.0) else None,
            "max_loss_streak": hm.get("max_loss_streak"), "avg_hold_minutes": _f(hm.get("avg_hold_minutes")),
            "prop": prop}
    # survivor (ADR-73/74): positive net R per trade AND the recorded trade sequence passes an evaluation and reaches the
    # first payout under the criteria rule profile (Settings; any profile when none is given). In-sample unless labelled.
    return apply_criteria(row, criteria)


def protocol_facts(svc) -> dict:
    """Protocol records, shortlists and holdout-ledger rows (read-only; SQLite research storage)."""
    out = {"protocols": [], "active": [], "shortlisted": {}, "holdout": {}, "search_protocol": {}, "holdout_runs": set()}
    try:
        protos = svc.store.list_protocols()
    except Exception:                                        # noqa: BLE001 - a store without protocol tables
        return out
    for p in protos:
        row = {"protocol_id": p["protocol_id"], "status": p["status"], "created_at": p.get("created_at"),
               "name": p["material"].get("name"), "scope": p["material"].get("scope"),
               "protocol_version": p["material"].get("protocol_version")}
        out["protocols"].append(row)
        if p["status"] == "ACTIVE":
            out["active"].append(p["protocol_id"])
        try:
            for h in svc.store.list_holdout_access(p["protocol_id"]):
                res = _loads(h.get("result_json")) or {}
                if h.get("run_id"):
                    out["holdout_runs"].add(h["run_id"])
                out["holdout"].setdefault(h["strategy_id"], []).append({
                    "protocol_id": p["protocol_id"], "access_id": h["access_id"], "status": h["status"],
                    "reason_code": h.get("reason_code"), "run_id": h.get("run_id"), "search_id": h.get("search_id"),
                    "outcome": res.get("outcome"), "accepted": False, "created_at": h.get("created_at"),
                    "random_control": (res.get("criteria") or {}).get("random_control")})
        except Exception:                                    # noqa: BLE001
            pass
    try:
        for b in svc.store.list_search_batches():
            out["search_protocol"][b["search_id"]] = b.get("protocol_id")
            tag = _loads(b.get("shortlist_json")) or {}
            for sid in tag.get("strategy_ids") or []:
                out["shortlisted"].setdefault(sid, []).append({"search_id": b["search_id"],
                                                               "protocol_id": tag.get("protocol_id") or b.get("protocol_id")})
    except Exception:                                        # noqa: BLE001
        pass
    return out


def _trial_strategies(svc, active: list[str]) -> set[str]:
    ids: set[str] = set()
    for pid in active:
        try:
            ids |= {e["strategy_id"] for e in svc.store.list_trial_events(pid) if e.get("counted")}
        except Exception:                                    # noqa: BLE001
            pass
    return ids


# ------------------------------------------------------------------------------ pipeline
PIPELINE_STAGES = (
    ("hypothesis", "Hypothesis"), ("proposal_validation", "Proposal validation"),
    ("numerical_testing", "Numerical testing"), ("controls", "Controls"), ("oos", "Out-of-sample"),
    ("shortlist", "Shortlist"), ("holdout_authorization", "Holdout authorization"),
    ("holdout_result", "Holdout result"), ("paper_evaluation", "Paper evaluation"), ("human_review", "Human review"))


def pipeline_state(f: Mapping, runs: list[Mapping], pf: Mapping, trial_ids: set[str]) -> dict:
    """Stage states of one strategy from stored facts only. States: done | failed | refused | pending |
    not_recorded | not_available. Nothing here is a verdict about profitability."""
    sid = f["strategy_id"]
    statuses = Counter(r["status"] for r in runs)
    ho = pf["holdout"].get(sid, [])
    looks = [h for h in ho if h["status"] != "refused"]
    done = [h for h in looks if h["status"] == "completed"]
    st: dict[str, dict] = {}
    src = f.get("source") or "user"
    st["hypothesis"] = {"state": "done", "evidence": (f"AI proposal {f['proposal_id']}" if f.get("proposal_id")
                                                       else f"authored ({src})") + (
        f"; family hypothesis: {f['hypothesis']}" if f.get("hypothesis") else "")}
    st["proposal_validation"] = {"state": "done", "evidence": "stored in the library: passed the DSL validator and "
                                                              "deterministic compiler (identity " + str(f.get("logic_hash") or "")[:12] + ")"}
    n_is = statuses.get("IN_SAMPLE", 0)
    st["numerical_testing"] = ({"state": "done", "evidence": f"{n_is} in-sample run(s)"
                                + ("; counted trial under the active protocol" if sid in trial_ids else "")}
                               if n_is or sid in trial_ids else {"state": "pending", "evidence": "no stored run"})
    ctrl = [h for h in done if h.get("random_control")]
    st["controls"] = ({"state": "done", "evidence": f"random-entry control recorded with {len(ctrl)} holdout evaluation(s)"}
                      if ctrl else {"state": "not_recorded", "evidence": "random-entry controls are returned when run "
                                    "and are not stored as runs; only holdout evaluations persist their control"})
    n_oos = sum(1 for r in runs if r["status"] in ("OUT_OF_SAMPLE", "WALK_FORWARD") and not r.get("holdout"))
    st["oos"] = ({"state": "done", "evidence": f"{n_oos} out-of-sample / walk-forward run(s)"} if n_oos > 0
                 else {"state": "pending", "evidence": "no out-of-sample or walk-forward run"})
    sl = pf["shortlisted"].get(sid, [])
    st["shortlist"] = ({"state": "done", "evidence": "shortlist tag in " + ", ".join(x["search_id"] for x in sl)
                        + " (a tag, not acceptance)"} if sl else {"state": "pending", "evidence": "not shortlisted"})
    refused = [h for h in ho if h["status"] == "refused"]
    if looks:
        st["holdout_authorization"] = {"state": "done", "evidence": f"{len(looks)} look(s) granted"}
    elif refused:
        st["holdout_authorization"] = {"state": "refused", "evidence": "refused: " + ", ".join(
            sorted({str(h['reason_code']) for h in refused}))}
    else:
        st["holdout_authorization"] = {"state": "pending", "evidence": "no holdout access"}
    if done:
        met = any(h.get("outcome") == "HOLDOUT_CRITERIA_MET" for h in done)
        st["holdout_result"] = {"state": "done" if met else "failed",
                                "evidence": ", ".join(str(h.get("outcome")) for h in done) + " (never 'accepted')"}
    elif any(h["status"] == "failed" for h in looks):
        st["holdout_result"] = {"state": "failed", "evidence": "holdout evaluation failed (look consumed)"}
    else:
        st["holdout_result"] = {"state": "pending", "evidence": "no holdout result"}
    st["paper_evaluation"] = {"state": "not_available", "evidence": "paper trading is not implemented (future phase)"}
    st["human_review"] = {"state": "not_available", "evidence": "human-discretion review is not implemented (future phase)"}
    order = [k for k, _ in PIPELINE_STAGES]
    reached = [k for k in order if st[k]["state"] == "done"]
    return {"stages": [{"id": k, "label": lbl, **st[k]} for k, lbl in PIPELINE_STAGES],
            "furthest_stage": reached[-1] if reached else None,
            "note": "Derived from stored facts (library, runs, protocol ledgers, search shortlists). A stage being "
                    "'done' records that it happened, not that the strategy has an edge."}


def _summary_state(p: Mapping) -> str:
    s = {x["id"]: x["state"] for x in p["stages"]}
    if s["holdout_result"] in ("done", "failed"):
        return "holdout_criteria_met" if s["holdout_result"] == "done" else "holdout_criteria_not_met"
    if s["holdout_authorization"] == "done":
        return "holdout_granted"
    if s["shortlist"] == "done":
        return "shortlisted"
    if s["oos"] == "done":
        return "oos_tested"
    if s["numerical_testing"] == "done":
        return "tested"
    return "untested"


STATE_LABEL = {"untested": "Untested", "tested": "Tested (in-sample)", "oos_tested": "OOS tested",
               "shortlisted": "Shortlisted (tag)", "holdout_granted": "Holdout look granted",
               "holdout_criteria_met": "Holdout criteria met", "holdout_criteria_not_met": "Holdout criteria not met"}


# ------------------------------------------------------------------------------ explorer
def _explorer_rows(svc, scope: str) -> tuple[list[dict], dict]:
    """Every strategy's explorer row for one scope (before filtering / sorting), memoized until the library, the store
    or the display preferences change (read-only; the rows are rebuilt from stored facts, never edited)."""
    from edgelab.research.campaign import db_token
    try:
        prefs = svc.ui_preferences()
    except Exception:                                        # noqa: BLE001
        prefs = {"favorites": [], "prop_criteria_profile": None}
    key = (svc.library.root, svc.library._fingerprint(), db_token(svc), scope, tuple(prefs["favorites"]),
           prefs.get("prop_criteria_profile"))
    hit = _FACET_CACHE.get(("explorer", scope))
    if hit and hit[0] == key:
        return hit[1], hit[2]
    facets = library_facets(svc)
    runs = run_records(svc)
    pf = protocol_facts(svc)
    trial_ids = _trial_strategies(svc, pf["active"])
    by_strat: dict[str, list[dict]] = {}
    for r in runs:
        by_strat.setdefault(r["strategy_id"], []).append(r)
    favorites = set(prefs["favorites"])
    rows = []
    for f in facets:
        sruns = by_strat.get(f["strategy_id"], [])
        scoped = [r for r in sruns if r["status"] in SCOPES[scope] and not r["holdout"]]
        ref = scoped[-1] if scoped else None
        pipe = pipeline_state(f, sruns, pf, trial_ids)
        state = _summary_state(pipe)
        oos = [r for r in sruns if r["status"] in ("OUT_OF_SAMPLE", "WALK_FORWARD") and not r["holdout"]]
        protos = sorted({x["protocol_id"] for x in pf["shortlisted"].get(f["strategy_id"], []) if x["protocol_id"]}
                        | {x["protocol_id"] for x in pf["holdout"].get(f["strategy_id"], [])})
        ho = [h for h in pf["holdout"].get(f["strategy_id"], []) if h["status"] != "refused"]
        rows.append({**f, "state": state, "state_label": STATE_LABEL[state], "n_runs": len(sruns),
                     "n_oos_runs": len(oos), "protocols": protos,
                     "oos_expectancy_r": oos[-1]["expectancy_r"] if oos else None,
                     "oos_run_id": oos[-1]["run_id"] if oos else None,
                     "holdout_outcome": ho[-1]["outcome"] if ho else None,
                     "holdout_random_control_p": ((ho[-1].get("random_control") or {}).get("p_value") if ho else None),
                     "ref_run": ref and {k: ref[k] for k in ("run_id", "status", "scope", "dataset_id", "instrument",
                                                             "synthetic", "cost_status", "start", "end")},
                     **{k: (ref or {}).get(k) for k in ("instrument", "trade_count", "trades_per_week", "win_rate",
                                                        "expectancy_r", "gross_r_per_trade", "net_r", "profit_factor",
                                                        "max_drawdown_r", "cost_r_per_trade", "sample_label",
                                                        "synthetic", "avg_rr", "max_loss_streak", "avg_hold_minutes",
                                                        "prop_pass_eval", "prop_pass_payout")},
                     "survivor": bool(ref and ref.get("survivor")), "favorite": f["strategy_id"] in favorites})

    _FACET_CACHE[("explorer", scope)] = (key, rows, pf)
    return rows, pf


def explorer(svc, params: Mapping[str, Any]) -> dict:
    scope = str(params.get("scope") or "in_sample")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {sorted(SCOPES)}")
    sort = str(params.get("sort") or "strategy_id")
    if sort not in SORT_KEYS:
        raise ValueError(f"sort must be one of {list(SORT_KEYS)}")
    desc = str(params.get("order") or "asc") == "desc"
    page = max(1, int(params.get("page") or 1))
    size = min(MAX_PAGE_SIZE, max(1, int(params.get("page_size") or 50)))
    protocol = params.get("protocol")
    rows, pf = _explorer_rows(svc, scope)

    def keep(r: dict) -> bool:
        q = str(params.get("q") or "").strip().lower()
        if q and q not in " ".join(str(r.get(k) or "") for k in ("strategy_id", "name", "family_id", "family_name",
                                                                  "hypothesis", "category")).lower():
            return False
        if params.get("survivors_only") in ("1", "true", True) and not r["survivor"]:
            return False
        if params.get("favorites_only") in ("1", "true", True) and not r["favorite"]:
            return False
        if params.get("prop") == "eval" and not r["prop_pass_eval"]:
            return False
        if params.get("prop") == "payout" and not r["prop_pass_payout"]:
            return False
        for key in ("family_id", "timeframe", "session", "direction", "entry_type", "stop_type", "target_type",
                    "source", "instrument", "state", "trailing", "signal_exit"):
            v = params.get(key)
            if v not in (None, "") and str(r.get(key)) != str(v):
                return False
        if params.get("strategy_id") and str(params["strategy_id"]).upper() not in str(r["strategy_id"]):
            return False
        if protocol and protocol not in r["protocols"]:
            return False
        if params.get("tested_only") in ("1", "true", True) and r["trade_count"] is None:
            return False
        mt = _f(params.get("min_trades"))
        if mt is not None and (r["trade_count"] or 0) < mt:
            return False
        mw = _f(params.get("max_trades_per_week"))
        if mw is not None and (r["trades_per_week"] is None or r["trades_per_week"] > mw):
            return False
        return True

    in_run = campaign_run_scope(svc, params.get("campaign_run"))
    kept = [r for r in rows if keep(r) and (in_run is None or r["strategy_id"] in in_run)]
    present = [r for r in kept if r.get(sort) is not None]
    missing = [r for r in kept if r.get(sort) is None]
    present.sort(key=lambda r: (r[sort] if not isinstance(r[sort], str) else r[sort].lower()), reverse=desc)
    ordered = present + missing                               # values missing sort last either way
    total = len(ordered)
    facet_values = {k: sorted({str(r[k]) for r in rows if r.get(k) not in (None, "")})
                    for k in ("family_id", "timeframe", "session", "direction", "entry_type", "stop_type",
                              "target_type", "source", "instrument", "state", "trailing", "signal_exit")}
    return {"rows": ordered[(page - 1) * size: page * size], "total": total, "page": page, "page_size": size,
            "pages": max(1, math.ceil(total / size)), "scope": scope, "scope_label": {
                "in_sample": "In-sample (exploratory)", "oos": "Out-of-sample", "walk_forward": "Walk-forward",
                "any": "Latest run of any status"}[scope],
            "sort": sort, "order": "desc" if desc else "asc", "facets": facet_values,
            "states": STATE_LABEL, "protocols": pf["protocols"], "library_total": len(rows),
            "criteria_profile": criteria_profile(svc), "campaign_run": params.get("campaign_run") or None,
            "basis": "Metrics come from the latest stored run of each strategy in the chosen scope, net of that "
                     "run's stated costs unless labelled gross. Win rate is shown but never used to rank.",
            "note": DESCRIPTIVE}


def strategy_pipeline(svc, strategy_id: str) -> dict:
    doc = svc.library.load(strategy_id)
    f = strategy_facets(doc)
    runs = [r for r in run_records(svc) if r["strategy_id"] == strategy_id]
    pf = protocol_facts(svc)
    return {"strategy_id": strategy_id, "facets": f, **pipeline_state(f, runs, pf, _trial_strategies(svc, pf["active"])),
            "holdout": pf["holdout"].get(strategy_id, []), "shortlists": pf["shortlisted"].get(strategy_id, [])}


def pipeline_board(svc) -> dict:
    facets = library_facets(svc)
    runs = run_records(svc)
    pf = protocol_facts(svc)
    trial_ids = _trial_strategies(svc, pf["active"])
    by_strat: dict[str, list[dict]] = {}
    for r in runs:
        by_strat.setdefault(r["strategy_id"], []).append(r)
    counts = {k: {"done": 0, "failed": 0, "refused": 0, "pending": 0, "not_recorded": 0, "not_available": 0}
              for k, _ in PIPELINE_STAGES}
    cands = []
    for f in facets:
        p = pipeline_state(f, by_strat.get(f["strategy_id"], []), pf, trial_ids)
        for s in p["stages"]:
            counts[s["id"]][s["state"]] += 1
        state = _summary_state(p)
        if state not in ("untested", "tested"):
            cands.append({"strategy_id": f["strategy_id"], "name": f["name"], "family_id": f["family_id"],
                          "state": state, "state_label": STATE_LABEL[state], "stages": p["stages"]})
    return {"stages": [{"id": k, "label": lbl, "counts": counts[k]} for k, lbl in PIPELINE_STAGES],
            "candidates": cands, "n_strategies": len(facets), "protocols": pf["protocols"],
            "note": "Counts of stored facts per stage. Stages after the holdout (paper evaluation, human review) "
                    "are not implemented yet and never show progress."}


# ------------------------------------------------------------------------------ research dashboard
def _hist(values: Iterable[float | None], bins: int = 20, lo: float | None = None, hi: float | None = None) -> dict:
    v = np.array([x for x in values if x is not None and math.isfinite(x)], dtype=float)
    if not len(v):
        return {"edges": [], "counts": [], "n": 0, "clipped": 0}
    a = float(np.min(v)) if lo is None else lo
    b = float(np.max(v)) if hi is None else hi
    if b <= a:
        b = a + 1.0
    clipped = int(((v < a) | (v > b)).sum())
    counts, edges = np.histogram(np.clip(v, a, b), bins=bins, range=(a, b))
    return {"edges": [float(e) for e in edges], "counts": [int(c) for c in counts], "n": int(len(v)),
            "clipped": clipped, "median": float(np.median(v)), "mean": float(v.mean())}


def _group(runs: list[dict], key) -> list[dict]:
    groups: dict[str, list[dict]] = {}
    for r in runs:
        groups.setdefault(str(key(r)), []).append(r)
    out = []
    for g, rs in sorted(groups.items()):
        n = sum(r["trade_count"] for r in rs)
        net = sum(r["net_r"] or 0.0 for r in rs)
        gross = sum(r["gross_r"] or 0.0 for r in rs)
        exp = [r["expectancy_r"] for r in rs if r["expectancy_r"] is not None]
        out.append({"group": g, "runs": len(rs), "trades": n,
                    "net_r_per_trade": net / n if n else None, "gross_r_per_trade": gross / n if n else None,
                    "median_run_expectancy_r": float(np.median(exp)) if exp else None,
                    "pct_runs_positive_net": (sum(1 for x in exp if x > 0) / len(exp)) if exp else None})
    return out


def research_dashboard(svc, params: Mapping[str, Any]) -> dict:
    scope = str(params.get("scope") or "in_sample")
    if scope not in SCOPES:
        raise ValueError(f"scope must be one of {sorted(SCOPES)}")
    fac = {f["strategy_id"]: f for f in library_facets(svc)}
    allruns = run_records(svc)
    n_holdout = sum(1 for r in allruns if r["holdout"] and r["status"] in SCOPES[scope])
    runs = [r for r in allruns if r["status"] in SCOPES[scope] and r["trade_count"] > 0 and not r["holdout"]]
    if params.get("instrument"):
        runs = [r for r in runs if r["instrument"] == params["instrument"]]
    if params.get("dataset_id"):
        runs = [r for r in runs if r["dataset_id"] == params["dataset_id"]]
    if params.get("family_id"):
        runs = [r for r in runs if (fac.get(r["strategy_id"]) or {}).get("family_id") == params["family_id"]]
    include_synth = params.get("include_synthetic") in ("1", "true", True)
    n_synth = sum(1 for r in runs if r["synthetic"])
    if not include_synth:
        runs = [r for r in runs if not r["synthetic"]]
    fx = lambda k: (lambda r: (fac.get(r["strategy_id"]) or {}).get(k) or "(unknown)")   # noqa: E731
    dims = {"instrument": lambda r: r["instrument"] or "(unknown)", "timeframe": lambda r: r["timeframe"] or "(unknown)",
            "session": fx("session"), "entry_type": fx("entry_type"), "stop_type": fx("stop_type"),
            "target_type": fx("target_type"), "direction": fx("direction"), "family": fx("family_id"),
            "source": fx("source")}
    exp = [r["expectancy_r"] for r in runs]
    pooled = _pooled_calendar(svc, runs[-MAX_POOLED_RUNS:])
    return {"scope": scope, "scope_label": SCOPE_LABEL.get(SCOPES[scope][0], scope) if scope != "any" else "All statuses",
            "basis": "net of each run's stated costs (gross columns labelled)", "n_runs": len(runs),
            "n_strategies": len({r["strategy_id"] for r in runs}), "n_trades": int(sum(r["trade_count"] for r in runs)),
            "synthetic_excluded": 0 if include_synth else n_synth, "includes_synthetic": include_synth,
            "holdout_runs_excluded": n_holdout,
            "breakdowns": {k: _group(runs, fn) for k, fn in dims.items()},
            "distributions": {
                "expectancy_r": _hist(exp, 24),
                "profit_factor": _hist([r["profit_factor"] for r in runs], 24, 0.0, 4.0),
                "max_drawdown_r": _hist([r["max_drawdown_r"] for r in runs], 24, 0.0),
                "trade_count": _hist([float(r["trade_count"]) for r in runs], 24, 0.0),
                "win_rate": _hist([r["win_rate"] for r in runs], 20, 0.0, 1.0)},
            "pct_runs_positive_net": (sum(1 for x in exp if x is not None and x > 0) / len(exp)) if exp else None,
            "cost_share": _cost_share(runs), "calendar": pooled,
            "filters": {"instruments": sorted({r["instrument"] for r in run_records(svc) if r["instrument"]}),
                        "datasets": sorted({r["dataset_id"] for r in run_records(svc) if r["dataset_id"]}),
                        "families": sorted({f["family_id"] for f in fac.values() if f.get("family_id")})},
            "note": DESCRIPTIVE + " Each run counts once; variations of one family are correlated."}


def _cost_share(runs: list[dict]) -> dict:
    gross = sum(r["gross_r"] or 0.0 for r in runs)
    cost = sum(r["cost_r"] or 0.0 for r in runs)
    n = sum(r["trade_count"] for r in runs)
    return {"gross_r": gross, "cost_r": cost, "net_r": gross - cost, "trades": n,
            "cost_r_per_trade": cost / n if n else None}


def _pooled_calendar(svc, runs: list[dict]) -> dict:
    frames = []
    for r in runs:
        try:
            _, t = svc.store.load_run(r["run_id"])
        except KeyError:
            continue
        if len(t):
            frames.append(t[["entry_ts", "net_r", "gross_r"]])
    if not frames:
        return {"weekday": [], "month": [], "year": [], "runs_pooled": 0}
    t = pd.concat(frames, ignore_index=True)
    local = pd.DatetimeIndex(t["entry_ts"]).tz_convert(LOCAL_TZ)
    def agg(labels, order):
        out = []
        for k in order:
            m = labels == k
            n = int(m.sum())
            out.append({"bucket": k, "trades": n, "net_r": float(t.loc[m, "net_r"].sum()) if n else 0.0,
                        "net_r_per_trade": float(t.loc[m, "net_r"].mean()) if n else None,
                        "gross_r_per_trade": float(t.loc[m, "gross_r"].mean()) if n else None})
        return out
    wd = np.array([WEEKDAYS[i] for i in local.weekday])
    mo = np.array([MONTHS[i - 1] for i in local.month])
    yr = np.array([str(y) for y in local.year])
    return {"weekday": agg(wd, WEEKDAYS), "month": agg(mo, MONTHS), "year": agg(yr, sorted(set(yr.tolist()))),
            "runs_pooled": len(frames), "timezone": LOCAL_TZ,
            "note": f"Trades of up to {MAX_POOLED_RUNS} most recent runs pooled by entry time ({LOCAL_TZ}); runs "
                    "overlap in time, so the same market period can be counted several times."}


# ------------------------------------------------------------------------------ run analytics
def _streak_lengths(sign: np.ndarray) -> dict:
    wins: Counter = Counter()
    losses: Counter = Counter()
    cur, run = 0, 0
    for s in list(sign) + [0]:
        if s != 0 and s == cur:
            run += 1
            continue
        if cur > 0:
            wins[run] += 1
        elif cur < 0:
            losses[run] += 1
        cur, run = (s, 1) if s != 0 else (0, 0)
    return {"wins": {int(k): int(v) for k, v in sorted(wins.items())},
            "losses": {int(k): int(v) for k, v in sorted(losses.items())}}


MC_PATHS, MC_FAN_SIMS, MC_MAX_POINTS = 40, 500, 300


def _mc_paths(net: np.ndarray, seed: int = 0) -> dict:
    """Seeded bootstrap resamples of the observed per-trade net R as cumulative paths (display only):
    a few sample paths and the 5/50/95th percentile fan over MC_FAN_SIMS resamples."""
    n = len(net)
    if n == 0:
        return {"paths": [], "fan": {}, "n_sims": 0}
    rng = np.random.default_rng(seed)
    sims = np.cumsum(net[rng.integers(0, n, size=(MC_FAN_SIMS, n))], axis=1)
    idx = np.unique(np.linspace(0, n - 1, min(n, MC_MAX_POINTS)).round().astype(int))
    fan = {str(p): np.percentile(sims[:, idx], p, axis=0).round(4).tolist() for p in (5, 50, 95)}
    return {"paths": sims[:MC_PATHS][:, idx].round(4).tolist(), "fan": fan, "index": (idx + 1).tolist(),
            "observed": np.cumsum(net)[idx].round(4).tolist(), "n_sims": MC_FAN_SIMS, "seed": seed, "method": "bootstrap",
            "note": "Resampling of the observed trades (bootstrap, with replacement), not a market simulation: it shows "
                    "path dispersion of THESE trades, not future outcomes."}


def run_analytics(svc, run_id: str) -> dict:
    """Descriptive analytics of ONE stored run (reads its stored trades; evaluates nothing)."""
    from edgelab.analytics import research as ra
    from edgelab.analytics.metrics import breakeven_cost_multiplier, compute_metrics
    from edgelab.research import lab
    rec, trades = svc.store.load_run(run_id)
    row = run_row(run_id, rec)
    if run_id in holdout_run_ids(svc):
        row.update(holdout=True, scope=HOLDOUT_SCOPE)
    thr = svc.cfg.get("sample_size")
    labels = ra.research_labels([rec], None)
    base = {"run_id": run_id, "run": row, "labels": labels, "note": DESCRIPTIVE,
            "basis": {"net": "net of the run's stated costs", "gross": "before costs"}}
    if not len(trades):
        return {**base, "n_trades": 0}
    t = trades.sort_values(["exit_ts", "entry_ts"], kind="mergesort").reset_index(drop=True)
    local = pd.DatetimeIndex(t["entry_ts"]).tz_convert(LOCAL_TZ)
    exit_local = pd.DatetimeIndex(t["exit_ts"]).tz_convert(LOCAL_TZ)
    net = t["net_r"].to_numpy(float)

    def bucket(labels_, order=None):
        return ra.bucket_table(t, np.asarray(labels_), order=order, sample_thresholds=thr)

    direction = np.where(t["direction"].to_numpy() > 0, "long", "short")
    years = np.array([str(y) for y in local.year])
    heat: dict[str, dict] = {}
    for y, m, r in zip(local.year, local.month, net):
        cell = heat.setdefault(str(y), {}).setdefault(MONTHS[m - 1], {"net_r": 0.0, "trades": 0})
        cell["net_r"] += float(r)
        cell["trades"] += 1
    roll = pd.Series(net).rolling(ROLLING_WINDOW, min_periods=ROLLING_WINDOW).mean()
    idx = np.arange(len(net))
    if len(idx) > lab.MAX_CURVE_POINTS:
        idx = np.unique(np.linspace(0, len(net) - 1, lab.MAX_CURVE_POINTS).round().astype(int))
    rolling = [{"i": int(i) + 1, "exit_ts": str(t["exit_ts"].iloc[i]), "value": _f(roll.iloc[i])} for i in idx
               if _f(roll.iloc[i]) is not None]
    quote = None
    if "entry_quote_side" in t.columns:
        q = Counter(f"{d}: entry {a} / exit {b}" for d, a, b in zip(direction, t["entry_quote_side"].fillna("?"),
                                                                     t["exit_quote_side"].fillna("?")))
        quote = {"counts": dict(q), "rule": "directional BID/ASK: long enters on ASK and exits on BID; short enters on "
                                            "BID and exits on ASK (when the run used quote-based execution)"}
    mults = svc.cfg["backtest"].get("cost_sensitivity_multipliers", [0.5, 1.0, 1.5, 2.0, 3.0])
    per_month = Counter(f"{y}-{m:02d}" for y, m in zip(local.year, local.month))
    return {**base, "n_trades": int(len(t)),
            "metrics": {"net": compute_metrics(t, "net_r", thr), "gross": compute_metrics(t, "gross_r", thr)},
            "curve": lab.run_curve(svc, run_id),
            "rolling_expectancy": {"window": ROLLING_WINDOW, "points": rolling, "basis": "net"},
            "direction": bucket(direction, ["long", "short"]),
            "year": bucket(years, sorted(set(years.tolist()))),
            "month": bucket(np.array([MONTHS[m - 1] for m in local.month]), [m for m in MONTHS]),
            "weekday": bucket(np.array([WEEKDAYS[d] for d in local.weekday]), [w for w in WEEKDAYS]),
            "session": ra.session_breakdown(t, svc.sessions, thr),
            "hour": ra.hour_breakdown(t, LOCAL_TZ, thr),
            "exit_reason": bucket(t["exit_reason"].astype(str).to_numpy()),
            "entry_type": bucket(t["entry_type"].astype(str).to_numpy()) if "entry_type" in t.columns else [],
            "monthly_heatmap": {"years": sorted(heat), "months": list(MONTHS), "cells": heat, "basis": "net R",
                                "timezone": LOCAL_TZ},
            "r_histogram": {"net": _hist(net, 30), "gross": _hist(t["gross_r"].to_numpy(float), 30)},
            "holding_minutes": _hist(t["holding_minutes"].to_numpy(float), 24, 0.0),
            "win_loss": {"wins": int((net > 0).sum()), "losses": int((net < 0).sum()), "flat": int((net == 0).sum())},
            "streaks": _streak_lengths(np.sign(net)),
            "trades_per_month": [{"month": k, "trades": v} for k, v in sorted(per_month.items())],
            "cost_sensitivity": ra.cost_sensitivity_table(t, mults),
            "breakeven_cost_multiplier": _f(breakeven_cost_multiplier(t)),
            "monte_carlo": svc._monte_carlo(t, 1000, 0),
            "monte_carlo_paths": _mc_paths(net),
            "quote_sides": quote,
            "mfe_mae": {"avg_mfe_r": _f(t["mfe_r"].mean()) if "mfe_r" in t.columns else None,
                        "avg_mae_r": _f(t["mae_r"].mean()) if "mae_r" in t.columns else None},
            "first_exit_local": str(exit_local[0]), "timezone": LOCAL_TZ}


# ------------------------------------------------------------------------------ overview
QUOTE_SIDES = {"long_entry": "ASK", "long_exit": "BID", "short_entry": "BID", "short_exit": "ASK"}


def execution_model(svc, instrument: str | None, provider: str | None, has_ask_ohlc: bool) -> dict:
    """The cost/execution model the engine would use for this instrument (config read, nothing run)."""
    from edgelab.engine.costs import CostConfigError, cost_model_from_config
    try:
        cm = cost_model_from_config(svc.cfg, instrument, provider=provider).to_dict()
    except CostConfigError as exc:
        return {"status": "unconfigured", "reason": str(exc), "instrument": instrument, "provider": provider}
    except Exception as exc:                                 # noqa: BLE001
        return {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}", "instrument": instrument}
    quotes = cm.get("spread_source") == "quotes"
    return {"status": cm.get("status"), "instrument": instrument, "provider": provider,
            "cost_scenario": cm.get("scenario"), "cost_profile": cm.get("profile"),
            "spread_source": cm.get("spread_source"),
            "quote_model": "directional_bid_ask" if quotes else "single_series",
            "quote_sides": QUOTE_SIDES if quotes else None, "has_ask_ohlc": has_ask_ohlc,
            "spread_treatment": ("embedded in historical BID/ASK quotes (no separate spread cost)" if quotes
                                 else "configured spread cost on a single price series"),
            "cost_model": cm}


def overview(svc) -> dict:
    """Home dashboard: system facts, protocol budgets, data/execution identity, recent activity."""
    from edgelab.core.identity import code_version
    facts = library_facets(svc)
    runs = run_records(svc)
    pf = protocol_facts(svc)
    status_counts = Counter(r["status"] for r in runs)
    protocols = []
    for pid in pf["active"]:
        try:
            protocols.append(svc.protocol_status(pid))
        except Exception as exc:                            # noqa: BLE001 - shown, never hidden
            protocols.append({"protocol_id": pid, "error": f"{type(exc).__name__}: {exc}"})
    try:
        pref = svc.preferred_dataset()
    except Exception:                                        # noqa: BLE001
        pref = None
    dataset, execution = None, None
    proto_did = next((p["source_dataset"]["dataset_id"] for p in protocols if "source_dataset" in p), None)
    did = proto_did or ((pref or {}).get("preferred") or {}).get("dataset_id")
    if did:
        try:
            dataset = svc.dataset_detail(did)
            m = dataset["manifest"]
            execution = execution_model(svc, m.get("instrument"), m.get("provider"), bool(m.get("has_ask_ohlc")))
        except Exception:                                    # noqa: BLE001
            dataset = {"dataset_id": did, "error": "dataset not found in this workspace"}
    try:
        searches = svc.list_searches()
    except Exception:                                        # noqa: BLE001
        searches = []
    try:
        gens = svc.ai_generations()
    except Exception:                                        # noqa: BLE001
        gens = []
    try:
        props = svc.list_prop_simulations()
    except Exception:                                        # noqa: BLE001
        props = []
    warnings = []
    if not pf["active"]:
        warnings.append({"level": "warn", "text": "No ACTIVE research protocol: discovery evaluations are not governed "
                                                   "by a locked holdout or trial budget."})
    synth = sum(1 for r in runs if r["synthetic"])
    if synth:
        warnings.append({"level": "info", "text": f"{synth} stored run(s) are on SYNTHETIC data (labelled everywhere)."})
    by_cost = Counter(str(r["cost_status"]) for r in runs if r["cost_status"] not in (None, "configured"))
    for st, n in sorted(by_cost.items()):
        warnings.append({"level": "info" if st == "assumed" else "warn",
                         "text": f"{n} run(s) use cost status '{st}'" + (" (a stated research assumption, not a "
                                 "broker-verified rate)" if st == "assumed" else "; see each run's assumptions")})
    recent = [{k: r[k] for k in ("run_id", "created_at", "status", "scope", "holdout", "strategy_id", "strategy_name", "dataset_id",
                                 "trade_count", "expectancy_r", "net_r", "profit_factor", "max_drawdown_r", "synthetic",
                                 "cost_status")} for r in runs[-10:][::-1]]
    cands = []
    for sid, rows in pf["holdout"].items():
        for h in rows:
            cands.append({"strategy_id": sid, **{k: h[k] for k in ("protocol_id", "status", "reason_code", "outcome",
                                                                  "created_at", "run_id")}})
    for sid, rows in pf["shortlisted"].items():
        for s in rows:
            cands.append({"strategy_id": sid, "protocol_id": s["protocol_id"], "status": "shortlisted",
                          "search_id": s["search_id"]})
    return {"facts": {"strategies": len(facts), "families": len({f["family_id"] for f in facts if f["family_id"]}),
                      "runs": len(runs), "runs_by_status": dict(status_counts),
                      "searches": len(searches), "variation_batches": len(svc.library.list_batches()),
                      "prop_simulations": len(props), "ai_generations": len(gens),
                      "datasets": len(svc.store.list_datasets()), "store_backend": svc.store.backend},
            "protocols": protocols, "protocol_records": pf["protocols"],
            "dataset": dataset, "dataset_source": "active protocol" if proto_did else ("preferred research dataset"
                                                                                       if did else None),
            "preferred_dataset": pref, "execution": execution,
            "recent_runs": recent,
            "recent_searches": [{k: s.get(k) for k in ("search_id", "created_at", "status", "n_trials", "n_evaluated",
                                                      "n_failed", "protocol_id")} for s in searches[-8:][::-1]],
            "candidates": cands[-20:], "warnings": warnings, "code_version": code_version(),
            "note": "System facts and stored results. Nothing here labels a strategy profitable; a positive "
                    "backtest is a historical result under stated assumptions."}
