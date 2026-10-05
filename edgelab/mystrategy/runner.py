"""My strategy: its own research protocol, discovery backtests, saved reports and background jobs (ADR-93).

* The protocol is a COMPANION of the active research protocol (role ``my_strategy``, scope ``<inst>@<prov>#my_strategy``):
  the same source dataset, discovery / holdout windows, execution and config hash, its own trial budget (every distinct
  settings combination tested on a window is one trial) and ONE holdout look.
* A backtest runs the ONE engine (``engine.backtester.run_backtest``) with the causality check, the canonical Dukascopy
  BID/ASK cost scenario and MNQ sizing, is recorded as a normal run (IN_SAMPLE) and counted in the ledger. Earlier
  discovery bars may serve as warm-up history (never traded).
* Reports live in ``<data>/my_strategy/backtests/<id>/``: summary.json, trades.json.gz (every trade with its checklist
  and levels), candles.jsonl.gz (the candles of every timeframe each trade used), days.json.gz.
"""
from __future__ import annotations

import gzip
import json
import secrets
import threading
import traceback
from contextlib import nullcontext
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from edgelab.core.fsutil import atomic_write_text
from edgelab.mystrategy import params as P
from edgelab.mystrategy.records import Charts, charts_used, jsonable, report_numbers, trade_rows
from edgelab.mystrategy.strategy import MyStrategy

TRIAL_BUDGET = 300
HOLDOUT_LOOKS = 1
WARMUP_DAYS = 120
ENTRY_POINT = "backtest_strategy"


class MyStrategyError(ValueError):
    def __init__(self, code: str, message: str, **detail):
        super().__init__(message)
        self.code, self.message, self.detail = code, message, detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def home(svc) -> Path:
    p = Path(svc.data_root) / "my_strategy"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, json.dumps(jsonable(obj), indent=1, sort_keys=False))


def _read_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _write_gz(path: Path, obj: Any) -> None:
    tmp = path.with_name(path.name + f".{secrets.token_hex(4)}.tmp")
    with gzip.open(tmp, "wt", encoding="utf-8") as f:
        json.dump(jsonable(obj), f, separators=(",", ":"))
    tmp.replace(path)


def read_gz(path: Path, default=None):
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


# =============================================================================================== settings
def load_overrides(svc) -> dict:
    return (_read_json(home(svc) / "settings.json", {}) or {}).get("overrides", {})


def settings_payload(svc) -> dict:
    ov = load_overrides(svc)
    try:
        s = P.resolve(ov)
        problems = []
    except P.SettingsError as e:          # a stored file from an older version: show it, never guess
        s, problems = P.defaults(), e.issues
    return {"schema": P.schema_payload(), "overrides": ov, "resolved": s, "settings_hash": P.settings_hash(s),
            "problems": problems}


def save_overrides(svc, overrides: dict) -> dict:
    s = P.resolve(overrides)                      # refuses unknown keys / bad values
    _write_json(home(svc) / "settings.json", {"overrides": P.changed(s), "saved_at": _now()})
    return settings_payload(svc)


# =============================================================================================== protocol
def _parent(svc) -> dict | None:
    from edgelab.research.protocol import is_companion
    act = [p for p in svc.store.list_protocols(status="ACTIVE") if not is_companion(p)]
    return act[0] if len(act) == 1 else None


def _scope(parent: dict) -> str:
    from edgelab.research.protocol import MY_STRATEGY_SCOPE_SUFFIX
    sc = parent["material"]["scope"]
    return svc_scope(sc["instrument"], sc["provider"]) + MY_STRATEGY_SCOPE_SUFFIX


def svc_scope(instrument: str, provider: str) -> str:
    return f"{instrument}@{provider}"


def _mine(svc, parent: dict) -> dict | None:
    rows = [p for p in svc.store.list_protocols(_scope(parent), "ACTIVE")
            if (p["material"].get("parent") or {}).get("protocol_id") == parent["protocol_id"]]
    return rows[0] if rows else None


def build_material(parent: dict, budget: int = TRIAL_BUDGET, looks: int = HOLDOUT_LOOKS) -> dict:
    import copy

    from edgelab.research import protocol as rp
    pm = parent["material"]
    mt = {**rp.DEFAULT_MULTIPLE_TESTING, "familywise_alpha": float(pm["multiple_testing"]["familywise_alpha"])}
    acceptance = copy.deepcopy(pm["acceptance_criteria"])
    oc = acceptance.get("oos_confidence") or {}
    if oc.get("method_id") == "min_normal_bootstrap_t_v1":
        oc["bootstrap"]["replicates"] = rp.bootstrap_replicates_for(budget, mt["familywise_alpha"])
    return {
        "protocol_version": rp.PROTOCOL_VERSION, "role": rp.MY_STRATEGY_ROLE, "name": "My strategy",
        "parent": {"protocol_id": parent["protocol_id"], "material_hash": parent["material_hash"],
                   "trial_budget": int(pm["trial_budget"]["max_unique_trials"])},
        "scope": dict(pm["scope"]), "source_dataset": dict(pm["source_dataset"]), "windows": dict(pm["windows"]),
        "execution": dict(pm["execution"]), "config_hash": pm["config_hash"],
        "search_constraints": {
            "strategies": "only the hand-built 'My strategy' (BP Blake's model); one trial = one settings combination "
                          "(settings hash) on one evaluated window",
            "evaluation_windows": "every discovery evaluation lies inside the discovery window (earlier discovery bars "
                                  "may be warm-up history, never traded)",
            "stages": {"discovery": "My strategy backtests", "holdout": "one holdout review of frozen settings"}},
        "trial_budget": {"max_unique_trials": int(budget),
                         "unit": "unique numerical trial = (protocol, settings hash, content hash of the evaluated bars, "
                                 "config hash)"},
        "holdout_budget": {"max_unique_candidate_evaluations": int(looks), "per_candidate": 1},
        "acceptance_criteria": acceptance, "multiple_testing": mt,
        "pre_protocol_exposure": {
            "statement": "The rules were written from BP Blake's public videos (transcripts supplied by the user) before "
                         "any backtest of them; none of the parent protocol's results selected them. The videos show "
                         "trades from May-August 2026, which may lie inside the holdout window.",
            "runs": []},
        "supersedes": None,
    }


def ensure_protocol(svc, lock=None, create: bool = True) -> tuple[dict, dict | None]:
    """(parent, mine). Creates the companion on first use. Refuses without exactly one active research protocol."""
    from edgelab.research import protocol as rp
    guard = lock if lock is not None else nullcontext()
    with guard:
        parent = _parent(svc)
        if parent is None:
            raise MyStrategyError("NO_PROTOCOL", "My strategy needs the workspace's active research protocol (its data, "
                                                 "discovery and holdout dates). None, or more than one, is active.")
        mine = _mine(svc, parent)
        if mine is None and create:
            rec = rp.make_record(build_material(parent), {"code_version": _code_version()})
            svc.store.save_protocol(rec, _scope(parent))
            mine = svc.store.get_protocol(rec["protocol_id"])
        if mine is not None:
            rp.verify_record(mine)
        return parent, mine


def _code_version() -> str:
    import edgelab
    return edgelab.__version__


def windows(mat: dict) -> dict:
    from edgelab.research.campaign import discovery_period
    d = discovery_period(mat)
    h = mat["windows"]["holdout"]
    return {"discovery": d, "holdout": {"start": h["boundary_open"], "end": h["last_bar"]},
            "discovery_trading_dates": mat["windows"]["discovery"]["trading_dates"],
            "holdout_trading_dates": h["trading_dates"]}


def dataset_1m(svc, protocol: dict) -> str:
    """The protocol source import's 1-minute dataset (the strategy reads 1m bars and builds every other timeframe)."""
    from edgelab.research.campaign import resolve_datasets
    by_tf, problems = resolve_datasets(svc, protocol, {"1m"})
    if problems:
        raise MyStrategyError("NO_1M_DATA", "The protocol's data has no single 1-minute dataset: "
                              + problems[0]["reason"])
    return by_tf["1m"]["dataset_id"]


def protocol_info(svc) -> dict:
    try:
        parent, mine = ensure_protocol(svc, create=False)
    except MyStrategyError as e:
        return {"ready": False, "problem": e.message}
    mat = (mine or parent)["material"]
    out = {"ready": True, "parent_protocol_id": parent["protocol_id"], "parent_name": parent["material"].get("name"),
           "protocol_id": mine["protocol_id"] if mine else None, "created": mine is not None,
           **windows(mat),
           "config_ok": svc._config_hash() == mat["config_hash"],
           "trial_budget": TRIAL_BUDGET if mine is None else mine["material"]["trial_budget"]["max_unique_trials"],
           "holdout_looks": HOLDOUT_LOOKS if mine is None else mine["material"]["holdout_budget"][
               "max_unique_candidate_evaluations"]}
    if mine:
        out["trials_used"] = svc.store.count_trials(mine["protocol_id"])
        out["holdout_looks_used"] = len([a for a in svc.store.list_holdout_access(mine["protocol_id"])
                                         if a["status"] != "refused"])
    else:
        out["trials_used"], out["holdout_looks_used"] = 0, 0
    return out


# =============================================================================================== backtests
def _ts(x) -> pd.Timestamp:
    t = pd.Timestamp(x)
    return t.tz_localize("UTC") if t.tzinfo is None else t.tz_convert("UTC")


def _engine_inputs(svc, ds, strat):
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.instruments import check_identity, contract_for
    check_identity(ds.instrument)
    costs = cost_model_from_config(svc.cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    return costs, contract_for(svc.cfg, strat.sizing)


def _trading_date_ord(calendar, ts: pd.Timestamp) -> int:
    import numpy as np
    d = calendar.trading_dates(pd.DatetimeIndex([ts]))[0]
    return int(np.datetime64(d, "D").astype(np.int64))


def es_for(svc, s: dict, ds=None, win=None):
    """The ES series for these settings (ADR-95). Required (refused otherwise) when SMT can change the trades; when it
    cannot, it is still used if imported (the trade checklist shows SMT) but never part of the result's identity."""
    from edgelab.mystrategy import es as ES
    try:
        es = ES.load(svc.data_root)
    except ES.EsError as e:
        raise MyStrategyError(e.code, e.message)
    used = P.smt_used(s)
    if es is None:
        if used:
            raise MyStrategyError("ES_DATA_REQUIRED", "These settings use SMT divergence, which needs the ES data. Import it "
                                                      "under My strategy -> Settings -> ES data for SMT, or switch SMT off.")
        return None
    if ds is not None and win is not None and len(ds.bars):
        lo = max(int(win[0].value), int(ds.bars.ts_ns[0]))
        hi = min(int(win[1].value), int(ds.bars.ts_ns[-1]))
        if not es.covers(lo, hi - 3 * 86_400_000_000_000):
            if used:
                raise MyStrategyError("ES_DATA_RANGE", f"The ES data ({es.manifest['first_bar_open_utc'][:10]} - "
                                                       f"{es.manifest['last_bar_open_utc'][:10]}) does not cover this "
                                                       "backtest's dates.")
    return es


def evaluated_hash(ds, s: dict, es) -> str:
    """Content of what was evaluated: the NQ bars, plus the ES data when SMT can change the trades."""
    if es is None or not P.smt_used(s):
        return ds.manifest.content_hash
    from edgelab.core.identity import hash_obj
    return hash_obj({"bars": ds.manifest.content_hash, "es": es.content_hash})


def run_window(svc, s: dict, start, end, *, stage: str, lock=None, skip=None, progress: Callable | None = None,
               protocol: dict | None = None):
    """Run the strategy on [start, end] of the protocol's source dataset with warm-up history before ``start``.
    Returns (strategy, result, ds, trade_window). No recording. ``protocol`` = another companion (the autotuner,
    ADR-97) instead of My strategy's own."""
    from edgelab.engine.backtester import run_backtest
    mine = protocol if protocol is not None else ensure_protocol(svc, lock)[1]
    mat = mine["material"]
    if svc._config_hash() != mat["config_hash"]:
        raise MyStrategyError("PROTOCOL_CONFIG_CHANGED", "The research settings (costs, fills, sessions) differ from the "
                                                         "protocol's. Restore them first (Run backtest -> Research runs).")
    w = windows(mat)
    lo, hi = (_ts(w["discovery"]["start"]), _ts(w["discovery"]["end"])) if stage == "discovery" else \
        (_ts(w["holdout"]["start"]), _ts(w["holdout"]["end"]))
    start = lo if start is None else _ts(start)
    end = hi if end is None else _ts(end)
    if stage == "discovery" and (start < lo or end > hi or end <= start):
        raise MyStrategyError("HOLDOUT_LOCKED", "Backtests may only use the discovery period "
                                                f"({lo.date()} - {hi.date()}); the holdout stays locked.")
    data_start = max(_ts(w["discovery"]["start"]), start - pd.Timedelta(days=WARMUP_DAYS))
    did = dataset_1m(svc, mine)
    if progress:
        progress("Loading and checking the price data")
    ds = svc._cell_dataset(did, (data_start, end), lock)
    if not ds.bars.has_ask_ohlc:
        raise MyStrategyError("ASK_OHLC_REQUIRED", "The dataset has no ASK prices; BID/ASK execution needs them.")
    es = es_for(svc, s, ds, (start, end))
    if stage == "discovery":                 # refuse BEFORE computing when the budget is used (a new trial)
        from edgelab.research import protocol as rp
        key = rp.trial_key(mine["protocol_id"], P.settings_hash(s), evaluated_hash(ds, s, es), mat["config_hash"])
        guard = lock if lock is not None else nullcontext()
        with guard:
            if not svc.store.trial_counted(mine["protocol_id"], key) and \
                    svc.store.count_trials(mine["protocol_id"]) >= mat["trial_budget"]["max_unique_trials"]:
                raise MyStrategyError("TRIAL_BUDGET_EXHAUSTED", "All tries of the My strategy protocol are used.")
    td_from = _trading_date_ord(ds.calendar, start)
    strat = MyStrategy(s, ds.calendar, skip=skip, trade_from_td=td_from, es=es)
    costs, contract = _engine_inputs(svc, ds, strat)
    if progress:
        progress("Running the strategy with the lookahead check and the engine")
    res = run_backtest(ds, strat, costs, svc.cfg["backtest"], sizing=strat.sizing, contract=contract)
    return strat, res, ds, (start, end), mine


def _record(svc, s: dict, strat, res, ds, win, mine, *, status: str, notes: str, lock=None, count: bool = True) -> dict:
    from edgelab.prop.service import outcomes
    from edgelab.research import protocol as rp
    from edgelab.research.runs import record_run
    met = report_numbers(res.trades, win[0], win[1], svc.cfg.get("sample_size"))
    from edgelab.analytics.metrics import compute_metrics
    full_met = compute_metrics(res.trades, sample_thresholds=svc.cfg.get("sample_size"), span=win)
    prop = outcomes(svc.root, res.trades, assumptions=res.assumptions)
    guard = lock if lock is not None else nullcontext()
    h = P.settings_hash(s)
    pid, mat = mine["protocol_id"], mine["material"]
    key = rp.trial_key(pid, h, evaluated_hash(ds, s, strat.es), mat["config_hash"])
    with guard:
        if count and not svc.store.trial_counted(pid, key) and \
                svc.store.count_trials(pid) >= mat["trial_budget"]["max_unique_trials"]:
            raise MyStrategyError("TRIAL_BUDGET_EXHAUSTED", "All tries of the My strategy protocol are used.")
        run_id = record_run(svc.store, svc.cfg, res, full_met, notes=notes, status=status, prop=prop)
        counted = None
        if count:
            d = res.dataset
            counted = svc.store.add_trial_event({
                "protocol_id": pid, "trial_id": "TR_" + key[:12].upper(), "trial_key": key, "status": "completed",
                "entry_point": ENTRY_POINT, "strategy_id": res.strategy_id, "logic_hash": h, "definition_hash": h,
                "family": "my_strategy", "dataset_id": d.get("dataset_id"),
                "source_dataset_id": d.get("parent_dataset_id") or d.get("dataset_id"),
                "evaluated_content_hash": evaluated_hash(ds, s, strat.es), "window_start": str(win[0]), "window_end": str(win[1]),
                "config_hash": mat["config_hash"], "cost_scenario": mat["execution"].get("cost_scenario"),
                "proposal_id": None, "search_id": None, "run_id": run_id, "error": None, "created_at": _now()})
    return {"run_id": run_id, "trial_id": "TR_" + key[:12].upper(), "trial_counted": counted, "prop": prop, **met}


def _prop_brief(prop: dict, profile_id: str = "LUCID_LUCIDFLEX_50K") -> dict | None:
    for p in prop.get("profiles", []):
        if (p.get("profile") or {}).get("profile_id") == profile_id:
            ev = p.get("evaluation") or {}
            return {"profile": profile_id, "status": p.get("status"), "evaluation": ev.get("status"),
                    "payouts": (p.get("totals") or {}).get("n_payouts"),
                    "trader_payout": (p.get("totals") or {}).get("trader_payout"), "headline": p.get("headline")}
    return None


def challenge_of(svc, ds, strat, res, win) -> dict:
    """ADR-101: the fee-free prop challenge chain of every rule profile over the report's window (never fails a report)."""
    from edgelab.mystrategy import challenge as CH
    try:
        return {"version": CH.CHAIN_VERSION, "start": win[0].isoformat(),
                "profiles": CH.chains(svc.cfg, svc.root, ds, strat, res.trades, win[0])}
    except Exception as exc:                             # noqa: BLE001 - shown as unavailable, the report stands
        return {"version": CH.CHAIN_VERSION, "error": f"{type(exc).__name__}: {exc}"[:300]}


def build_report(folder: Path, s: dict, strat, res, ds, win, rec: dict, *, kind: str, label: str = "",
                 extra: dict | None = None, challenge: dict | None = None) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    rows = trade_rows(res.trades)
    charts = Charts(ds.bars, ds.calendar, s["models.price_series"])
    docs, candle_lines = [], []
    for r in rows:
        expl = strat.explanations.get(int(r["signal_bar"]), {})
        tfs = charts_used(expl)
        docs.append({**r, "explanation": jsonable(expl), "charts": tfs})
        candle_lines.append({"trade_no": r["trade_no"],
                             "candles": charts.for_trade(tfs, int(r["signal_bar"]), int(r["exit_bar"]))})
    summary = {
        "id": folder.name, "kind": kind, "label": label, "created_at": _now(), "app_version": _code_version(),
        "settings_hash": P.settings_hash(s), "settings_changed": P.changed(s), "settings": s,
        "window": {"start": win[0].isoformat(), "end": win[1].isoformat()},
        "dataset": {k: res.dataset.get(k) for k in ("dataset_id", "parent_dataset_id", "content_hash", "start", "end",
                                                    "instrument", "provider", "price_basis")},
        "run_id": rec.get("run_id"), "trial_id": rec.get("trial_id"), "trial_counted": rec.get("trial_counted"),
        "strategy_id": res.strategy_id, "n_signals": res.n_signals, "skipped": res.skipped,
        "causality_passed": None if res.causality is None else bool(res.causality.passed),
        "metrics": rec["metrics"], "monthly": rec["monthly"], "prop": _prop_brief(rec.get("prop") or {}),
        "rule_stats": dict(strat.stats), "trade_count": len(rows), "challenge": challenge,
        "es_data": None if strat.es is None else {"es_id": strat.es.manifest.get("es_id"),
                                                  "content_hash": strat.es.content_hash,
                                                  "instrument": strat.es.manifest["identity"]["instrument"],
                                                  "price_basis": strat.es.manifest["identity"]["price_basis"],
                                                  "smt_affects_trades": P.smt_used(s)},
        "assumptions": {k: res.assumptions.get(k) for k in ("quote_model", "same_bar_policy_effective", "sizing",
                                                            "costs", "trailing_stop", "max_trades_per_day",
                                                            "strategy_trade_management")},
        **(extra or {}),
    }
    _write_json(folder / "summary.json", summary)
    _write_gz(folder / "trades.json.gz", docs)
    with gzip.open(folder / "candles.jsonl.gz", "wt", encoding="utf-8") as f:
        for line in candle_lines:
            f.write(json.dumps(jsonable(line), separators=(",", ":")) + "\n")
    _write_gz(folder / "days.json.gz", strat.days)
    return summary


def new_id(prefix: str) -> str:
    return f"{prefix}_{datetime.now(timezone.utc).strftime('%Y%m%d_%H%M%S')}_{secrets.token_hex(2)}"


def backtest(svc, overrides: dict | None, start=None, end=None, *, label: str = "", lock=None,
             progress: Callable | None = None, protocol: dict | None = None, extra: dict | None = None) -> dict:
    s = P.resolve(overrides if overrides is not None else load_overrides(svc))
    strat, res, ds, win, mine = run_window(svc, s, start, end, stage="discovery", lock=lock, progress=progress,
                                           protocol=protocol)
    if progress:
        progress("Recording the run and writing the trade records")
    rec = _record(svc, s, strat, res, ds, win, mine, status="IN_SAMPLE",
                  notes=f"My strategy discovery backtest {label}".strip(), lock=lock)
    folder = home(svc) / "backtests" / new_id("BT")
    return build_report(folder, s, strat, res, ds, win, rec, kind="discovery_backtest", label=label,
                        extra={"protocol_id": mine["protocol_id"], **(extra or {})},
                        challenge=challenge_of(svc, ds, strat, res, win))


def list_backtests(svc, kind: str = "backtests") -> list[dict]:
    root = home(svc) / kind
    out = []
    if root.exists():
        for d in sorted(root.iterdir(), reverse=True):
            sm = _read_json(d / "summary.json")
            if sm:
                m = sm.get("metrics") or {}
                out.append({k: sm.get(k) for k in ("id", "kind", "label", "created_at", "settings_hash", "window",
                                                   "trade_count", "prop", "settings_changed", "exported", "favorite",
                                                   "challenge", "optimizer_run")} | {
                    "metrics": {k: m.get(k) for k in ("win_rate", "expectancy_r", "net_r", "net_usd", "trades_per_week",
                                                      "profit_factor", "max_drawdown_r", "months_losing",
                                                      "months_total", "avg_planned_rr", "avg_win_r")}})
    return out


def _bt_folder(svc, bt_id: str) -> Path:
    import re
    if not re.fullmatch(r"(BT|HO|HD)_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", bt_id):
        raise KeyError(bt_id)
    for kind in ("backtests", "holdout"):
        p = home(svc) / kind / bt_id
        if (p / "summary.json").exists():
            return p
    raise KeyError(bt_id)


def get_backtest(svc, bt_id: str) -> dict:
    p = _bt_folder(svc, bt_id)
    sm = _read_json(p / "summary.json")
    trades = read_gz(p / "trades.json.gz", [])
    return {**sm, "trades": [{k: t.get(k) for k in ("trade_no", "entry_ts", "exit_ts", "direction", "entry_price_theo",
                                                    "stop_price", "target_price", "exit_price_theo", "exit_reason",
                                                    "net_r", "net_usd", "contracts", "risk_points")} |
                             {"model": (t.get("explanation") or {}).get("model"),
                              "checklist": (t.get("explanation") or {}).get("checklist"),
                              "quality": (t.get("explanation") or {}).get("quality"),
                              "confirmation_tf": ((t.get("explanation") or {}).get("confirmation") or {}).get("tf"),
                              "r_planned": ((t.get("explanation") or {}).get("target") or {}).get("r_planned")}
                             for t in trades]}


_META_LOCK = threading.Lock()


def set_meta(svc, bt_id: str, *, favorite: bool | None = None, label: str | None = None) -> dict:
    """Favourite / rename one of the user's reports (ADR-101): display only, the numbers never change."""
    if favorite is not None and not isinstance(favorite, bool):
        raise MyStrategyError("BAD_VALUE", "favorite must be true or false")
    if label is not None:
        if not isinstance(label, str):
            raise MyStrategyError("BAD_VALUE", "The name must be text.")
        label = " ".join(label.split())[:160]
    p = _bt_folder(svc, bt_id) / "summary.json"
    with _META_LOCK:
        sm = _read_json(p)
        if sm is None:
            raise KeyError(bt_id)
        if favorite is not None:
            sm["favorite"] = favorite
        if label is not None:
            sm.setdefault("label_original", sm.get("label") or "")
            sm["label"] = label
        _write_json(p, sm)
    return {"id": bt_id, "favorite": bool(sm.get("favorite")), "label": sm.get("label") or ""}


def get_trade(svc, bt_id: str, trade_no: int) -> dict:
    p = _bt_folder(svc, bt_id)
    trades = read_gz(p / "trades.json.gz", [])
    doc = next((t for t in trades if int(t["trade_no"]) == int(trade_no)), None)
    if doc is None:
        raise KeyError(f"trade {trade_no}")
    candles = {}
    with gzip.open(p / "candles.jsonl.gz", "rt", encoding="utf-8") as f:
        for line in f:
            row = json.loads(line)
            if int(row["trade_no"]) == int(trade_no):
                candles = row["candles"]
                break
    return {**doc, "candles": candles, "backtest_id": bt_id, "count": len(trades)}


# =============================================================================================== jobs
class Jobs:
    """One My strategy job at a time, process-local (like single backtests)."""

    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._lock = threading.Lock()

    def active(self) -> dict | None:
        with self._lock:
            return next((dict(j) for j in self._jobs.values() if j["state"] == "running"), None)

    def start(self, kind: str, fn: Callable[[Callable], Any], meta: dict | None = None) -> dict:
        with self._lock:
            if any(j["state"] == "running" for j in self._jobs.values()):
                raise MyStrategyError("JOB_RUNNING", "A My strategy job is already running.")
            jid = "MSJ_" + secrets.token_hex(6)
            job = {"job_id": jid, "kind": kind, "state": "running", "step": "Starting", "created_at": _now(),
                   "finished_at": None, "result": None, "error": None, **(meta or {})}
            self._jobs[jid] = job

        def step(text: str):
            job["step"] = text

        def work():
            try:
                job["result"] = jsonable(fn(step))
                job["state"] = "completed"
            except P.SettingsError as e:
                job["state"], job["error"] = "failed", {"kind": "SETTINGS", "message": str(e)}
            except Exception as e:                       # noqa: BLE001 - reported, never swallowed
                if hasattr(e, "code") and hasattr(e, "message"):      # MyStrategyError / UploadError: plain message
                    job["state"], job["error"] = "failed", {"kind": e.code, "message": e.message}
                else:
                    job["state"], job["error"] = "failed", {"kind": type(e).__name__, "message": str(e),
                                                            "trace": traceback.format_exc()[-2000:]}
            job["finished_at"] = _now()
        threading.Thread(target=work, daemon=True, name=f"my-strategy-{kind}").start()
        return dict(job)

    def get(self, jid: str) -> dict:
        with self._lock:
            j = self._jobs.get(jid)
            if j is None:
                raise KeyError(jid)
            return dict(j)


def jobs_of(svc) -> Jobs:
    j = svc.__dict__.get("_my_strategy_jobs")
    if j is None:
        j = svc.__dict__["_my_strategy_jobs"] = Jobs()
    return j


# =============================================================================================== test plans
MAX_PLAN_VARIANTS = 40


def check_plan(plan: dict) -> list[dict]:
    """A plan = {"name", "note", "base": {settings}, "variants": [{"label", "overrides": {settings}}], "window"?}.
    Every variant must resolve (unknown keys / bad values refuse the whole plan before anything runs)."""
    if not isinstance(plan, dict) or not isinstance(plan.get("variants"), list) or not plan["variants"]:
        raise MyStrategyError("BAD_PLAN", "A test plan needs a non-empty 'variants' list.")
    if len(plan["variants"]) > MAX_PLAN_VARIANTS:
        raise MyStrategyError("BAD_PLAN", f"A test plan may hold at most {MAX_PLAN_VARIANTS} variants.")
    base = plan.get("base") or {}
    out, issues = [], []
    for k, v in enumerate(plan["variants"]):
        ov = {**base, **(v.get("overrides") or {})}
        try:
            s = P.resolve(ov)
            out.append({"label": str(v.get("label") or f"variant {k + 1}")[:80], "overrides": P.changed(s),
                        "settings_hash": P.settings_hash(s)})
        except P.SettingsError as e:
            issues.append(f"variant {k + 1}: " + "; ".join(e.issues))
    if issues:
        raise MyStrategyError("BAD_PLAN", " | ".join(issues))
    return out


def run_plan(svc, plan: dict, *, lock=None, progress: Callable | None = None) -> dict:
    variants = check_plan(plan)
    win = plan.get("window") or {}
    pl_id = new_id("PL")
    rows = []
    for k, v in enumerate(variants):
        if progress:
            progress(f"Variant {k + 1} of {len(variants)}: {v['label']}")
        try:
            sm = backtest(svc, v["overrides"], win.get("start"), win.get("end"),
                          label=f"{plan.get('name', 'plan')}: {v['label']}", lock=lock)
            rows.append({**v, "backtest_id": sm["id"], "trade_count": sm["trade_count"], "metrics": sm["metrics"],
                         "prop": sm["prop"], "monthly": sm["monthly"], "rule_stats": sm["rule_stats"]})
        except MyStrategyError as e:
            rows.append({**v, "error": {"kind": e.code, "message": e.message}})
            if e.code == "TRIAL_BUDGET_EXHAUSTED":
                break
    res = {"id": pl_id, "name": plan.get("name"), "note": plan.get("note"), "created_at": _now(), "window": win,
           "variants": rows}
    _write_json(home(svc) / "plans" / f"{pl_id}.json", res)
    return res


def list_plan_results(svc) -> list[dict]:
    root = home(svc) / "plans"
    out = []
    if root.exists():
        for f in sorted(root.glob("PL_*.json"), reverse=True):
            d = _read_json(f) or {}
            out.append({"id": d.get("id"), "name": d.get("name"), "created_at": d.get("created_at"),
                        "variants": len(d.get("variants") or []), "exported": d.get("exported")})
    return out


def plan_result(svc, pl_id: str) -> dict:
    import re
    if not re.fullmatch(r"PL_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", pl_id):
        raise KeyError(pl_id)
    d = _read_json(home(svc) / "plans" / f"{pl_id}.json")
    if d is None:
        raise KeyError(pl_id)
    return d


# =============================================================================================== export for Claude
def export_dir() -> Path:
    """Where exports are saved: <Downloads>/MunyunLab for Claude (env EDGELAB_EXPORT_DIR overrides; tests)."""
    import os
    env = os.environ.get("EDGELAB_EXPORT_DIR")
    if env:
        p = Path(env)
    else:
        home_dir = Path(os.environ.get("USERPROFILE") or Path.home())
        dl = home_dir / "Downloads"
        p = (dl if dl.is_dir() else home_dir) / "MunyunLab for Claude"
    p.mkdir(parents=True, exist_ok=True)
    return p


def export(svc, report_ids: list[str], include_candles: bool = False) -> dict:
    """One ZIP with the chosen reports (summary, every trade with its checklist / levels / explanation, day statistics,
    optionally the candles) for the user to attach in the chat. Marks each report as exported."""
    import zipfile
    if not report_ids:
        raise MyStrategyError("NOTHING_SELECTED", "Select at least one backtest.")
    ids = []
    for rid in report_ids:                       # a test-plan result brings its variants' backtests along
        ids.append(rid)
        if rid.startswith("PL_"):
            ids += [v["backtest_id"] for v in plan_result(svc, rid)["variants"] if v.get("backtest_id")]
    folders = []
    for rid in dict.fromkeys(ids):
        if rid.startswith(("PL_", "SR_")):
            folders.append((rid, None))
        else:
            folders.append((rid, _bt_folder(svc, rid)))
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    target = export_dir() / f"MunyunLab_my_strategy_{stamp}.zip"
    index = {"exported_at": _now(), "app_version": _code_version(), "include_candles": include_candles, "reports": []}
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for rid, folder in folders:
            if folder is None and rid.startswith("SR_"):           # a finished setup review (ADR-96)
                from edgelab.mystrategy import setup_review as SR
                for n, text in SR.export_files(svc, rid).items():
                    z.writestr(f"{rid}/{n}", text)
                index["reports"].append({"id": rid, "kind": "setup_review"})
                continue
            if folder is None:
                res = plan_result(svc, rid)
                z.writestr(f"{rid}/plan_result.json", json.dumps(res, indent=1))
                index["reports"].append({"id": rid, "kind": "plan_result", "name": res.get("name")})
                continue
            names = ["summary.json", "trades.json.gz", "days.json.gz"] + (["candles.jsonl.gz"] if include_candles else [])
            for n in names:
                if (folder / n).exists():
                    z.write(folder / n, f"{rid}/{n}")
            sm = _read_json(folder / "summary.json") or {}
            if sm.get("review_id"):
                st = _read_json(home(svc) / "reviews" / sm["review_id"] / "state.json")
                if st:
                    z.writestr(f"{rid}/review_state.json", json.dumps(st, indent=1))
            index["reports"].append({"id": rid, "kind": sm.get("kind"), "label": sm.get("label"),
                                     "trade_count": sm.get("trade_count"), "settings_hash": sm.get("settings_hash")})
        z.writestr("index.json", json.dumps(index, indent=1))
    mark = {"at": _now(), "file": target.name}
    for rid, folder in folders:
        if folder is None and rid.startswith("SR_"):
            from edgelab.mystrategy import setup_review as SR
            SR.mark_exported(svc, rid, mark)
        elif folder is None:
            res = plan_result(svc, rid)
            res["exported"] = mark
            _write_json(home(svc) / "plans" / f"{rid}.json", res)
        else:
            sm = _read_json(folder / "summary.json")
            sm["exported"] = mark
            _write_json(folder / "summary.json", sm)
    return {"path": str(target), "file": target.name, "folder": str(target.parent), "bytes": target.stat().st_size,
            "reports": [r for r, _ in folders]}


def open_export_folder() -> dict:
    """Open THE export folder in the file manager (no other path is ever opened)."""
    import os
    import subprocess
    import sys
    p = export_dir()
    if sys.platform == "win32":
        os.startfile(str(p))                       # noqa: S606 - fixed folder only
    elif sys.platform == "darwin":
        subprocess.Popen(["open", str(p)])         # noqa: S603,S607
    else:
        subprocess.Popen(["xdg-open", str(p)])     # noqa: S603,S607
    return {"folder": str(p)}


def all_reports(svc) -> list[dict]:
    """Every report for the Trades tab: backtests and finished holdout results (newest first). A holdout report of a
    review still in progress is never listed (the mechanical result would bias the trader's decisions)."""
    from edgelab.mystrategy import review as RV
    st = RV.current(svc)
    open_rv = st["id"] if st and st.get("status") == "in_progress" else None
    rows = list_backtests(svc) + [r for r in list_backtests(svc, "holdout")
                                  if not (open_rv and r["id"][3:] == open_rv[3:])]
    return sorted(rows, key=lambda r: str(r.get("created_at")), reverse=True)
