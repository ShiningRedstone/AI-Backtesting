"""Flipped reruns of Strategy autotuner combinations (ADR-100).

"Rerun with flipped entry" on a combination's bubble: the same settings with every trade taken the other way
(``models.flip``: long <-> short, the setup's stop becomes the target and its target the stop, same prices, so a 1:2
becomes 1:0.5). Flipping a combination that is already flipped gives its unflipped version.

* No exact mirror exists for breakeven, a trailing stop or a limit entry, so the flipped version runs with breakeven off,
  trailing off and market entries (the user's choice); what was switched off is recorded and shown.
* Every flipped rerun is a new strategy and therefore a try: counted in its OWN companion protocol (role
  ``my_autotune_flip``, 500 tries, ONE holdout look), never in the autotuner's 10,000 or My strategy's 300.
* If the flipped settings are already one of the 10,000 combinations, nothing runs: that combination is linked (its
  result is the autotuner's own, or arrives when the autotuner reaches it).
* The computation is the autotuner's ``evaluate`` (the ONE engine, lookahead check on, BID/ASK, MNQ sizing, prop audit)
  on the same inputs (``autotune.load_inputs``). Results: ``<data>/my_strategy/autotune/flips.jsonl`` (numbers only, rules
  version recorded; older rule code is not shown). "Re-run with trades and charts" = a normal My strategy backtest under
  the flip protocol (same trial key, not a new try).
"""
from __future__ import annotations

import json
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

from edgelab.mystrategy import autotune as AT
from edgelab.mystrategy import params as P
from edgelab.mystrategy import runner as R
from edgelab.mystrategy.logic import RULES_VERSION

FLIP_BUDGET = 500
FLIP_LOOKS = 1
ENTRY_POINT = "my_autotune_flip"
OFF_FOR_FLIP = {"manage.be": ("off", "breakeven"), "manage.trail": ("off", "trailing stop"),
                "entry.type": ("market", "limit entry (market entry instead)")}


# =============================================================================================== protocol
def _scope(parent: dict) -> str:
    from edgelab.research.protocol import AUTOTUNE_FLIP_SCOPE_SUFFIX
    sc = parent["material"]["scope"]
    return R.svc_scope(sc["instrument"], sc["provider"]) + AUTOTUNE_FLIP_SCOPE_SUFFIX


def build_material(parent: dict) -> dict:
    from edgelab.research import protocol as rp
    mat = R.build_material(parent, budget=FLIP_BUDGET, looks=FLIP_LOOKS)
    mat.update({
        "role": rp.AUTOTUNE_FLIP_ROLE, "name": "Strategy autotuner - flipped reruns",
        "search_constraints": {
            "strategies": "flipped versions of Strategy autotuner combinations, one per click ('Rerun with flipped "
                          "entry'): the same settings with models.flip toggled; breakeven, trailing and limit entries "
                          "switched off where present (no exact mirror)",
            "evaluation_windows": "the whole discovery window (earlier discovery bars may be warm-up history, never "
                                  "traded)",
            "stages": {"discovery": "flipped reruns and their trade reruns", "holdout": "one holdout look"}},
    })
    mat["pre_protocol_exposure"] = {
        "statement": "Each flip is chosen by the user after seeing its combination's discovery result (a flip of a "
                     "clearly losing combination is selected BECAUSE it lost). The combinations derive from 'test 37', "
                     "itself tuned on the discovery period.",
        "runs": []}
    return mat


def ensure_protocol(svc, lock=None, create: bool = True) -> tuple[dict, dict | None]:
    from edgelab.research import protocol as rp
    guard = lock if lock is not None else nullcontext()
    with guard:
        parent = R._parent(svc)
        if parent is None:
            raise R.MyStrategyError("NO_PROTOCOL", "Flipped reruns need the workspace's active research protocol.")
        rows = [p for p in svc.store.list_protocols(_scope(parent), "ACTIVE")
                if (p["material"].get("parent") or {}).get("protocol_id") == parent["protocol_id"]]
        mine = rows[0] if rows else None
        if mine is None and create:
            rec = rp.make_record(build_material(parent), {"code_version": R._code_version()})
            svc.store.save_protocol(rec, _scope(parent))
            mine = svc.store.get_protocol(rec["protocol_id"])
        if mine is not None:
            rp.verify_record(mine)
        return parent, mine


# =============================================================================================== the flipped settings
def flipped(overrides: dict) -> tuple[dict, list[str]]:
    """(overrides of the flipped version, what had to be switched off). Toggles models.flip; the flipped version of a
    flipped combination is its unflipped one (nothing switched off then)."""
    s = P.resolve(overrides)
    turn_on = not s["models.flip"]
    off: list[str] = []
    new = {**overrides, "models.flip": turn_on}
    if turn_on:
        for k, (value, what) in OFF_FOR_FLIP.items():
            if s[k] != value:
                new[k] = value
                off.append(what)
    return P.changed(P.resolve(new)), off


def _path(svc) -> Path:
    return AT.home(svc) / "flips.jsonl"


def read_flips(svc) -> dict[int, dict]:
    """Latest flip record per source combination (current rule code only)."""
    out: dict[int, dict] = {}
    p = _path(svc)
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:
                continue
            if int(r.get("rules_version") or 1) == RULES_VERSION:
                out[int(r["source_n"])] = r
    return out


def _append(svc, row: dict) -> None:
    line = json.dumps(R.jsonable(row), separators=(",", ":"), allow_nan=False)
    with AT._FILE_LOCK, open(_path(svc), "a", encoding="utf-8") as f:
        f.write(line + "\n")


def _row(svc, n: int) -> tuple[dict, dict]:
    man = AT.frozen_manifest(svc)
    if man is None:
        raise R.MyStrategyError("NOT_STARTED", "Start the autotuner first.")
    row = next((r for r in man["rows"] if r["n"] == int(n)), None)
    if row is None:
        raise R.MyStrategyError("NO_COMBINATION", f"There is no combination {n}.")
    return man, row


def info(svc, n: int) -> dict:
    """What 'Rerun with flipped entry' would do for combination n, and the flip if it already exists."""
    man, row = _row(svc, n)
    ov, off = flipped(row["overrides"])
    h = P.settings_hash(ov)
    design = next((r["n"] for r in man["rows"] if r["settings_hash"] == h), None)
    have = read_flips(svc).get(int(n))
    return R.jsonable({"n": int(n), "already_flipped": bool(P.resolve(row["overrides"])["models.flip"]),
                       "switched_off": off, "settings_hash": h, "design_n": design, "flip": have})


def run_flip(svc, n: int, *, lock=None, progress: Callable | None = None) -> dict:
    """Run (or link) the flipped version of combination n. Returns the flip record."""
    from edgelab.research import protocol as rp
    man, row = _row(svc, n)
    ov, off = flipped(row["overrides"])
    h = P.settings_hash(ov)
    base = {"source_n": int(n), "settings_hash": h, "overrides": ov, "switched_off": off, "created_at": R._now(),
            "rules_version": RULES_VERSION, "app_version": R._code_version()}
    design = next((r["n"] for r in man["rows"] if r["settings_hash"] == h), None)
    if design is not None:                               # already one of the 10,000: link it, never a new try
        rec = {**base, "kind": "design", "design_n": design}
        _append(svc, rec)
        return rec
    _, mine = ensure_protocol(svc, lock)
    pid, mat = mine["protocol_id"], mine["material"]
    if svc._config_hash() != mat["config_hash"]:
        raise R.MyStrategyError("PROTOCOL_CONFIG_CHANGED", "The research settings differ from the protocol's.")
    if progress:
        progress("Loading and checking the price data")
    ds, es, start, end, td_from = AT.load_inputs(svc, mine, lock)
    s = P.resolve(ov)
    if P.smt_used(s) and es is None:
        raise R.MyStrategyError("ES_DATA_REQUIRED", "This combination uses SMT divergence; import ES data covering the "
                                                    "discovery period first.")
    content = R.evaluated_hash(ds, s, es)
    key = rp.trial_key(pid, h, content, mat["config_hash"])
    guard = lock if lock is not None else nullcontext()
    with guard:                                          # refuse BEFORE computing when the flip budget is used
        if not svc.store.trial_counted(pid, key) and \
                svc.store.count_trials(pid) >= mat["trial_budget"]["max_unique_trials"]:
            raise R.MyStrategyError("TRIAL_BUDGET_EXHAUSTED", f"All {mat['trial_budget']['max_unique_trials']} flipped "
                                                              "reruns are used.")
    if progress:
        progress("Running the flipped version with the lookahead check and the engine")
    import time
    t0 = time.perf_counter()
    out = AT.evaluate(svc.cfg, ds, es, str(svc.root), (start, end), td_from, ov)
    out["duration_s"] = round(time.perf_counter() - t0, 2)
    with guard:
        svc.store.add_trial_event({
            "protocol_id": pid, "trial_id": "TR_" + key[:12].upper(), "trial_key": key, "status": "completed",
            "entry_point": ENTRY_POINT, "strategy_id": out.get("strategy_id"), "logic_hash": h, "definition_hash": h,
            "family": "my_strategy", "dataset_id": ds.manifest.dataset_id, "source_dataset_id": ds.manifest.dataset_id,
            "evaluated_content_hash": content, "window_start": str(start), "window_end": str(end),
            "config_hash": mat["config_hash"], "cost_scenario": mat["execution"].get("cost_scenario"),
            "proposal_id": None, "search_id": None, "run_id": None, "error": None, "created_at": R._now()})
    rec = {**base, "kind": "flip", "trial_id": "TR_" + key[:12].upper(), **out}
    _append(svc, rec)
    return rec


# =============================================================================================== read views
def _result_of(svc, rec: dict, results: dict | None = None) -> dict | None:
    if rec.get("kind") == "design":
        return (results if results is not None else AT.read_results(svc)).get(int(rec["design_n"]))
    return rec


def points(svc, profile: str | None, rows: dict, results: dict) -> list[dict]:
    """The pink bubbles: every flipped rerun with a finished result."""
    out = []
    for n, rec in sorted(read_flips(svc).items()):
        res = _result_of(svc, rec, results)
        src = rows.get(n)
        if not res or "error" in res or "metrics" not in res or src is None:
            continue
        pr = (res.get("prop") or {}).get(profile) if profile else None
        out.append({"n": n, "flip_of": n, "design_n": rec.get("design_n"), "label": f"Flipped #{n}: {src['label']}",
                    "stage": "flip", "options": src["options"], **res["metrics"],
                    "prop_evaluation": pr.get("evaluation") if pr else None,
                    "prop_payouts": pr.get("payouts") if pr else None,
                    "prop_trader_payout": pr.get("trader_payout") if pr else None})
    return out


def detail(svc, n: int) -> dict:
    """The flipped version's panel, in the same shape as a combination's (plus flip_of / switched_off)."""
    man, row = _row(svc, n)
    rec = read_flips(svc).get(int(n))
    if rec is None:
        raise KeyError(f"flip of {n}")
    off_keys = [k for k, (_, what) in OFF_FOR_FLIP.items() if what in rec["switched_off"]]
    flip_change = {"option": "flip", "theme": "direction", "label": "Flipped entry",
                   "changes": {"models.flip": bool(rec["overrides"].get("models.flip", False)),
                               **{k: OFF_FOR_FLIP[k][0] for k in off_keys}},
                   "reason": "Your request: every trade the other way (long <-> short, the stop becomes the target and the "
                             "target the stop, same prices)." + (" Switched off for the flip (no exact mirror): "
                                                                 + ", ".join(rec["switched_off"]) + "."
                                                                 if rec["switched_off"] else ""),
                   "priority": 1, "source": "Your request"}
    reruns = [b for b in R.list_backtests(svc)
              if (R._read_json(R.home(svc) / "backtests" / b["id"] / "summary.json") or {}).get("autotune_flip_of") == int(n)]
    flipped_row = {"n": int(n), "stage": "flip", "options": row["options"] + ["flip"],
                   "label": f"Flipped #{n}: {row['label']}", "overrides": rec["overrides"],
                   "settings_hash": rec["settings_hash"], "changes": row["changes"] + [flip_change]}
    return R.jsonable({"row": flipped_row, "result": _result_of(svc, rec), "base": man["base"],
                       "base_label": man["base_label"], "reruns": reruns, "flip_of": int(n),
                       "design_n": rec.get("design_n"), "switched_off": rec["switched_off"]})


def rerun(svc, n: int, *, lock=None, progress: Callable | None = None) -> dict:
    """The flipped version as a normal My strategy backtest (trade records + candles) under the protocol that counted it
    (the autotuner's for a linked combination, the flip protocol otherwise): same trial key, never a new try."""
    man, row = _row(svc, n)
    rec = read_flips(svc).get(int(n))
    if rec is None:
        raise R.MyStrategyError("NO_FLIP", "Run the flipped version first.")
    if rec.get("kind") == "design":
        return AT.rerun(svc, int(rec["design_n"]), lock=lock, progress=progress)
    _, mine = ensure_protocol(svc, lock)
    return R.backtest(svc, rec["overrides"], label=f"Autotuner #{n} flipped: {row['label']}"[:160], lock=lock,
                      progress=progress, protocol=mine, extra={"autotune_flip_of": int(n),
                                                               "autotune_manifest": man["manifest_hash"]})


def status(svc) -> dict:
    try:
        _, mine = ensure_protocol(svc, create=False)
    except R.MyStrategyError:
        return {"budget": FLIP_BUDGET, "used": 0}
    return {"budget": FLIP_BUDGET, "used": svc.store.count_trials(mine["protocol_id"]) if mine else 0,
            "holdout_looks": FLIP_LOOKS}
