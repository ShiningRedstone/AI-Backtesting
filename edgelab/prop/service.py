"""Service-level prop simulation: loads a STORED run read-only, simulates accounts, attaches
lineage, and optionally records the simulation as its own JSON document (never in the run table).
Called only through ``Services.prop_*`` (the one contract the CLI and web API use)."""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Mapping, Sequence

import yaml

from edgelab.core.identity import code_version, hash_obj
from edgelab.prop.rules import PropConfigError, PropRules, validate_rules
from edgelab.prop.simulator import (ORDERING, SIMULATOR_VERSION, PropDataError, simulate_accounts,
                                    simulation_id, trades_fingerprint)

MAX_ACCOUNTS = 20
SIM_ID_CHARS = set("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
PROP_LABEL = ("Prop-account simulation: the account rules below were applied to the RECORDED trades of a "
              "historical backtest under its stated assumptions. It describes what those rules would have "
              "done to that trade sequence; it is not a forecast, and passing it is not evidence that the "
              "strategy is profitable, robust or deployable.")
SYNTHETIC_RULES_LABEL = ("SYNTHETIC TEST-ONLY RULES: at least one account uses an illustrative rule set that "
                         "does not describe any real prop firm or program.")


def config_dir(root: Path) -> Path:
    return Path(root) / "configs" / "prop"


def sims_dir(data_root: Path) -> Path:
    return Path(data_root) / "prop_simulations"


def list_configs(root: Path) -> list[dict]:
    out = []
    for p in sorted(config_dir(root).glob("*.y*ml")):
        text = p.read_text()
        row: dict[str, Any] = {"file": p.name, "text": text}
        try:
            r = validate_rules(yaml.safe_load(text))
            row.update(valid=True, errors=[], id=r.id, name=r["name"], config_hash=r.config_hash,
                       synthetic_test_only=r["synthetic_test_only"])
        except (PropConfigError, yaml.YAMLError) as exc:
            row.update(valid=False, errors=getattr(exc, "errors", [str(exc)]), id=None, name=p.stem,
                       config_hash=None, synthetic_test_only=None)
        out.append(row)
    return out


def resolve_rules(root: Path, src: Any) -> PropRules:
    """A config id (from configs/prop), a mapping, or YAML/JSON text -> validated rules."""
    if isinstance(src, str) and "\n" not in src and ":" not in src:
        hits = [c for c in list_configs(root) if c["id"] == src]
        if not hits:
            raise KeyError(f"no prop config with id {src!r} in configs/prop")
        src = hits[0]["text"]
    if isinstance(src, str):
        try:
            src = yaml.safe_load(src)
        except yaml.YAMLError as exc:
            raise PropConfigError([f"not valid YAML/JSON: {exc}"]) from None
    return validate_rules(src)


def validate_config(root: Path, src: Any) -> dict:
    try:
        r = resolve_rules(root, src)
    except PropConfigError as exc:
        return {"valid": False, "errors": exc.errors}
    return {"valid": True, "errors": [], "id": r.id, "config_hash": r.config_hash, "rules": r.to_dict()}


def _stored_trades_hash(trades) -> str:
    from edgelab.engine.backtester import BacktestResult
    return BacktestResult.trades_hash.fget(SimpleNamespace(trades=trades))


def _definition_hash(rec: Mapping) -> tuple[str | None, str | None, str | None]:
    """(definition_hash, logic_hash, note) exactly as stored in the run record (never recomputed)."""
    dsl = (rec.get("strategy") or {}).get("dsl")
    if not isinstance(dsl, Mapping) or not dsl.get("definition_hash"):
        return None, None, "run has no DSL identity (not a DSL strategy); definition hash unavailable"
    return dsl["definition_hash"], dsl.get("logic_hash"), None


def simulate(svc, run_id: str, accounts: Sequence[Mapping], record: bool = False) -> dict:
    if not isinstance(accounts, Sequence) or isinstance(accounts, (str, bytes)) or not accounts:
        raise ValueError("accounts must be a non-empty list of {account_id?, config, start?}")
    if len(accounts) > MAX_ACCOUNTS:
        raise ValueError(f"at most {MAX_ACCOUNTS} accounts per simulation")
    specs = []
    for i, a in enumerate(accounts):
        if not isinstance(a, Mapping) or "config" not in a:
            raise ValueError(f"account {i + 1} needs a config (id, mapping or YAML text)")
        unknown = set(a) - {"account_id", "config", "start"}
        if unknown:
            raise ValueError(f"account {i + 1}: unknown fields {sorted(unknown)}")
        specs.append({"account_id": str(a.get("account_id") or f"A{i + 1}"),
                      "rules": resolve_rules(svc.root, a["config"]), "start": a.get("start")})

    rec, trades = svc.store.load_run(run_id)                    # read-only
    before = (hash_obj(rec), trades_fingerprint(trades))
    stored_hash = rec.get("trades_hash")
    recomputed = _stored_trades_hash(trades)
    if stored_hash and recomputed != stored_hash:
        raise PropDataError(f"stored trades of {run_id} do not match the run's trades_hash; refusing to simulate")
    sim = simulate_accounts(trades, specs)
    rec2, trades2 = svc.store.load_run(run_id)
    after = (hash_obj(rec2), trades_fingerprint(trades2))
    if before != after or before[1] != trades_fingerprint(trades):
        raise AssertionError("source run changed during a prop simulation")   # must never happen

    ds, a = rec.get("dataset") or {}, rec.get("assumptions") or {}
    costs = a.get("costs") or {}
    def_hash, logic_hash, def_note = _definition_hash(rec)
    t_sorted = trades.sort_values("exit_ts") if len(trades) else trades
    lineage = {
        "source_run_id": run_id, "source_run_status": rec.get("status"),
        "strategy_id": rec["strategy"]["strategy_id"], "definition_hash": def_hash, "logic_hash": logic_hash,
        "definition_hash_note": def_note,
        "dataset_id": ds.get("dataset_id"), "dataset_name": ds.get("dataset_name"),
        "parent_dataset_id": ds.get("parent_dataset_id"), "provider": ds.get("provider"),
        "instrument": ds.get("instrument"), "timeframe": ds.get("timeframe"),
        "source_period": {"dataset_start": str(ds.get("start")), "dataset_end": str(ds.get("end")),
                          "first_entry": str(t_sorted["entry_ts"].min()) if len(trades) else None,
                          "last_exit": str(t_sorted["exit_ts"].max()) if len(trades) else None},
        "cost_profile": costs.get("profile"), "cost_status": a.get("cost_status"),
        "run_config_hash": rec.get("config_hash"), "trades_hash": stored_hash, "trades_hash_verified": True,
        "n_source_trades": int(len(trades)), "ordering": ORDERING, "simulator_version": SIMULATOR_VERSION,
        "simulator_code_version": code_version(),
    }
    sid = simulation_id({"run_id": run_id, "trades_hash": stored_hash or recomputed}, sim["account_configs"])
    from edgelab.analytics.research import research_labels
    labels = [PROP_LABEL]
    if any(r["rules"].doc["synthetic_test_only"] for r in specs):
        labels.append(SYNTHETIC_RULES_LABEL)
    if str(rec.get("notes", "")).startswith("SYNTHETIC"):
        labels.append("SYNTHETIC DATA: the source run is a synthetic demonstration.")
    labels += research_labels([rec])
    hm = rec.get("headline_metrics") or {}
    strategy_result = {k: hm.get(k) for k in ("trade_count", "net_r", "net_usd", "expectancy_r", "profit_factor",
                                              "max_drawdown_r", "max_drawdown_usd", "win_rate", "sample_label")}
    strategy_result["note"] = ("Strategy result of the source run, unchanged: the trade stream the accounts "
                               "replay. Account rules never alter it.")
    out = {"simulation_id": sid, "created_at": datetime.now(timezone.utc).isoformat(), "lineage": lineage,
           "strategy_result": strategy_result, "accounts": sim["accounts"],
           "account_configs": sim["account_configs"], "labels": labels,
           "source_unchanged": {"record_hash": before[0], "trades_fingerprint": before[1], "verified": True},
           "recorded": False}
    if record:
        path = sims_dir(svc.data_root) / f"{sid}.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():                                   # deterministic id: identical content
            path.write_text(json.dumps({**out, "recorded": True}, default=str, indent=1))
        out["recorded"] = True
    return out


def list_simulations(data_root: Path) -> list[dict]:
    rows = []
    for p in sorted(sims_dir(data_root).glob("PROP_*.json")):
        d = json.loads(p.read_text())
        rows.append({"simulation_id": d["simulation_id"], "created_at": d["created_at"],
                     "source_run_id": d["lineage"]["source_run_id"], "strategy_id": d["lineage"]["strategy_id"],
                     "dataset_id": d["lineage"]["dataset_id"],
                     "accounts": [{"account_id": x["summary"]["account_id"], "prop_config_id": x["summary"]["prop_config_id"],
                                   "status": x["summary"]["status"]} for x in d["accounts"]]})
    return rows


def get_simulation(data_root: Path, sim_id: str) -> dict:
    if not sim_id.startswith("PROP_") or not set(sim_id) <= SIM_ID_CHARS:
        raise ValueError("invalid simulation id")
    p = sims_dir(data_root) / f"{sim_id}.json"
    if not p.exists():
        raise KeyError(sim_id)
    return json.loads(p.read_text())
