"""Bootstrapped prop evaluations of ONE stored backtest (ADR-73): "how often would THESE trades pass?".

A DOWNSTREAM, SIMULATED view. It never changes the strategy, its trades, the backtest or the chronological prop audit
stored with every run (``edgelab.prop.service.outcomes``). Method:

* The run's trades are grouped by TRADING DAY exactly as the rule profile's evaluation day boundary defines it (the same
  ``prop.simulator.trading_days`` the lifecycle uses, so a session that starts the evening before stays together). A
  replay draws BLOCKS of ``block_days`` consecutive trading days (with replacement, seeded) until it has as many trading
  days as the original run, and lays them onto the original run's calendar in order: each drawn day's trades keep their
  local clock time and are moved by whole days to the target trading day. Trades, sizes and P&L are taken exactly as recorded (nothing resized or re-costed).
* Every replay goes through the SAME lifecycle engine as the stored audit (``prop.lifecycle.simulate_lifecycle``) with
  the chosen rule profile. A replay is simulated only until its outcome is settled (evaluation failed by a breach, or
  passed and then paid out / breached), extending to the full replay when needed; later trades cannot change a settled
  outcome because the lifecycle processes trades chronologically, so this only saves time.
* Drawdown "what-if" variants override only the profile's drawdown mode (both stages): ``eod_trailing`` or ``static``
  (the modes the lifecycle engine implements). An intraday-trailing drawdown is not implemented and is refused by name.

Outputs are frequencies over the replays (pass, first payout, evaluation breach, incompatible), the median number of
trading days to pass, the failure reasons, and balance paths. They describe resamples of HISTORICAL trades under stated
(partly ASSUMED_DEFAULT) rules: not a forecast, not a probability of future profitability.
"""
from __future__ import annotations

import copy
import hashlib
import json
import math
import threading
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import numpy as np
import pandas as pd

BOOTSTRAP_VERSION = "prop-bootstrap/1"
LOCAL_TZ = "America/New_York"
DEFAULT_N, DEFAULT_BLOCK_DAYS, DEFAULT_SEED = 2000, 5, 0
MAX_N, MAX_BLOCK_DAYS = 5000, 60
PREFIX_DAYS = (20, 60)                       # settle-early horizons before the full replay
PATH_DAYS, PATH_SAMPLES = 120, 30
MODES = {"profile": "Profile rules (as configured)", "eod_trailing": "End-of-day trailing drawdown (what-if)",
         "static": "Static drawdown (what-if)"}
UNSUPPORTED_MODES = {"intraday_trailing": "An intraday-trailing drawdown is not implemented by the prop simulator; it is "
                                          "refused, never approximated."}
END_OF_DATA = "NOT_PASSED_BY_END_OF_DATA"


class BootstrapError(ValueError):
    """Bad parameters or trades that cannot be replayed (reported, never fatal)."""


def params_of(n: Any = None, block_days: Any = None, seed: Any = None, mode: Any = None) -> dict:
    def as_int(v, default, lo, hi, what):
        if v in (None, ""):
            return default
        try:
            x = int(v)
        except (TypeError, ValueError):
            raise BootstrapError(f"{what} must be a whole number") from None
        if not lo <= x <= hi:
            raise BootstrapError(f"{what} must be between {lo} and {hi}")
        return x
    m = str(mode or "profile")
    if m in UNSUPPORTED_MODES:
        raise BootstrapError(UNSUPPORTED_MODES[m])
    if m not in MODES:
        raise BootstrapError(f"mode must be one of {sorted(MODES)}")
    return {"n": as_int(n, DEFAULT_N, 50, MAX_N, "replays"), "block_days": as_int(block_days, DEFAULT_BLOCK_DAYS, 1,
                                                                                    MAX_BLOCK_DAYS, "block length"),
            "seed": as_int(seed, DEFAULT_SEED, 0, 2**31 - 1, "seed"), "mode": m}


def variant_profile(profile: Mapping, mode: str) -> dict:
    """The profile itself, or a what-if copy whose drawdown mode (both stages) is overridden and marked CUSTOM."""
    if mode == "profile":
        return dict(profile)
    p = copy.deepcopy(dict(profile))
    for stage in ("evaluation", "funded"):
        key = f"{stage}.drawdown.mode"
        if key in p.get("rules", {}):
            p["rules"][key] = {"value": mode, "status": "CUSTOM",
                               "basis": f"what-if variant of the bootstrapped evaluation view: {MODES[mode]}"}
    return p


def trading_day_groups(trades: pd.DataFrame, profile: Mapping | None = None) -> tuple[list[pd.Timestamp], list[np.ndarray]]:
    """Trades grouped by the trading day of their exit as the profile's evaluation day boundary defines it (New York
    calendar date when no profile is given); ordered days and the row positions of each day."""
    exits = pd.Series(pd.to_datetime(trades["exit_ts"], utc=True).to_numpy(), index=trades.index)
    if profile is not None and profile.get("schema_version") == 3:
        from edgelab.prop.profiles import resolve
        from edgelab.prop.simulator import trading_days
        R = resolve(profile)
        labels = trading_days(exits - pd.Timedelta(nanoseconds=1), {"trading_day": R["evaluation"]["day_boundary"]})
        dates = pd.DatetimeIndex(pd.to_datetime(labels.to_numpy()))
    else:
        dates = pd.DatetimeIndex(exits).tz_convert(LOCAL_TZ).normalize().tz_localize(None)
    uniq = sorted(set(dates))
    pos = {d: i for i, d in enumerate(uniq)}
    groups: list[list[int]] = [[] for _ in uniq]
    for row, d in enumerate(dates):
        groups[pos[d]].append(row)
    return list(uniq), [np.asarray(g, dtype=int) for g in groups]


def _shift(ts: pd.Series, days: np.ndarray) -> pd.Series:
    """Move UTC timestamps by whole calendar days in New York local time (keeps the local clock time)."""
    local = pd.DatetimeIndex(pd.to_datetime(ts, utc=True)).tz_convert(LOCAL_TZ).tz_localize(None)
    moved = local + pd.to_timedelta(days, unit="D")
    return pd.Series(moved.tz_localize(LOCAL_TZ, ambiguous=False, nonexistent="shift_forward").tz_convert("UTC"),
                     index=ts.index)


def make_replay(trades: pd.DataFrame, dates: list, groups: list[np.ndarray], rng: np.random.Generator,
                block_days: int) -> tuple[pd.DataFrame, np.ndarray]:
    """One block-bootstrap replay laid onto the original calendar. Returns (trades, target-day index per trade)."""
    n_days = len(dates)
    order: list[int] = []
    while len(order) < n_days:
        start = int(rng.integers(0, n_days))
        order.extend(range(start, min(start + block_days, n_days)))
    order = order[:n_days]
    rows, shift, day_idx = [], [], []
    for target, src in enumerate(order):
        g = groups[src]
        rows.append(g)
        shift.append(np.full(len(g), (dates[target] - dates[src]).days))
        day_idx.append(np.full(len(g), target))
    idx = np.concatenate(rows) if rows else np.array([], dtype=int)
    t = trades.iloc[idx].reset_index(drop=True).copy()
    sh = np.concatenate(shift) if shift else np.array([], dtype=int)
    for c in ("entry_ts", "exit_ts", "signal_ts"):
        if c in t.columns:
            t[c] = _shift(t[c], sh).to_numpy()
    t["trade_no"] = np.arange(1, len(t) + 1)
    return t, (np.concatenate(day_idx) if day_idx else np.array([], dtype=int))


def _settled(r: Mapping) -> bool:
    ev = r.get("evaluation") or {}
    if ev.get("status") == "FAIL":
        return ev.get("failure_reason") != END_OF_DATA
    if ev.get("status") == "PASS":
        fu = r.get("funded") or {}
        return int((r.get("totals") or {}).get("n_payouts") or 0) >= 1 or fu.get("status") == "FAIL"
    return r.get("status") in ("INCOMPATIBLE", "NOT_APPLICABLE")


def simulate_replay(replay: pd.DataFrame, day_idx: np.ndarray, profile: Mapping) -> dict:
    """Lifecycle of one replay, simulated on growing prefixes of trading days until the outcome is settled."""
    from edgelab.prop.lifecycle import simulate_lifecycle
    n_days = int(day_idx.max()) + 1 if len(day_idx) else 0
    for horizon in [h for h in PREFIX_DAYS if h < n_days] + [n_days]:
        part = replay[day_idx < horizon] if horizon < n_days else replay
        r = simulate_lifecycle(part, profile)
        if horizon == n_days or _settled(r):
            return r
    return simulate_lifecycle(replay, profile)


def outcome_of(r: Mapping) -> dict:
    ev, fu = r.get("evaluation") or {}, r.get("funded") or {}
    passed = ev.get("status") == "PASS"
    return {"status": r.get("status"), "evaluation": ev.get("status"), "failure_reason": ev.get("failure_reason"),
            "pass_days": (ev.get("pass") or {}).get("trading_days") if passed else None,
            "payouts": int((r.get("totals") or {}).get("n_payouts") or 0), "funded": fu.get("status")}


def summarize(outcomes: list[dict], paths: np.ndarray, profile: Mapping, params: Mapping) -> dict:
    valid = [o for o in outcomes if o["status"] not in ("NOT_APPLICABLE",)]
    n = len(valid)

    def share(pred) -> float | None:
        return (sum(1 for o in valid if pred(o)) / n) if n else None
    days = [o["pass_days"] for o in valid if o["pass_days"] is not None]
    reasons = Counter(o["failure_reason"] or "unknown" for o in valid if o["evaluation"] == "FAIL")
    R = profile.get("rules", {})
    rv = lambda k: (R.get(k) or {}).get("value")                    # noqa: E731
    path_block = {}
    if paths.size:
        pct = {str(p): np.nanpercentile(paths, p, axis=0).round(2).tolist() for p in (10, 50, 90)}
        path_block = {"days": list(range(1, paths.shape[1] + 1)), "percentiles": pct,
                      "samples": paths[:PATH_SAMPLES].round(2).tolist(),
                      "start_balance": rv("account.size"), "target": rv("evaluation.profit_target"),
                      "max_loss": rv("evaluation.drawdown.max_loss"),
                      "note": "Evaluation account balance by trading day of each replay (sum of the replayed trades' net "
                              "P&L, before any rule stops the account); percentiles across replays."}
    return {"replays": len(outcomes), "valid_replays": n,
            "p_pass": share(lambda o: o["evaluation"] == "PASS"),
            "p_first_payout": share(lambda o: o["payouts"] >= 1),
            "p_evaluation_breach": share(lambda o: o["evaluation"] == "FAIL" and o["failure_reason"] != END_OF_DATA),
            "p_not_passed_by_end": share(lambda o: o["failure_reason"] == END_OF_DATA),
            "p_incompatible": share(lambda o: o["status"] == "INCOMPATIBLE"),
            "median_days_to_pass": float(np.median(days)) if days else None,
            "days_to_pass_p10_p90": [float(np.percentile(days, 10)), float(np.percentile(days, 90))] if days else None,
            "failure_reasons": dict(reasons.most_common()), "paths": path_block}


def run_bootstrap(trades: pd.DataFrame, profile: Mapping, params: Mapping, progress=None) -> dict:
    """The full bootstrap of one run under one profile (pure function of its inputs; deterministic per seed)."""
    if not len(trades):
        raise BootstrapError("the run has no trades to replay")
    prof = variant_profile(profile, params["mode"])
    dates, groups = trading_day_groups(trades, prof)
    rng = np.random.default_rng(params["seed"])
    outcomes, paths = [], []
    hdays = min(PATH_DAYS, len(dates))
    for k in range(params["n"]):
        replay, day_idx = make_replay(trades, dates, groups, rng, params["block_days"])
        daily = np.bincount(day_idx, weights=replay["net_usd"].to_numpy(float), minlength=len(dates))[:hdays]
        paths.append(np.cumsum(daily))
        try:
            outcomes.append(outcome_of(simulate_replay(replay, day_idx, prof)))
        except Exception as exc:                              # noqa: BLE001 - one bad replay is recorded, not fatal
            outcomes.append({"status": "NOT_APPLICABLE", "evaluation": None, "failure_reason": f"replay error: {exc}",
                             "pass_days": None, "payouts": 0, "funded": None})
        if progress:
            progress(k + 1)
    start = float(((prof.get("rules") or {}).get("account.size") or {}).get("value") or 0.0)
    out = summarize(outcomes, start + np.asarray(paths), prof, params)
    return out


# ------------------------------------------------------------------------------ cache + background jobs
_jobs: dict[str, dict] = {}
_lock = threading.Lock()


def cache_dir(svc) -> Path:
    return Path(svc.data_root) / "prop_bootstrap"


def cache_key(trades_hash: str, profile_hash: str, params: Mapping) -> str:
    blob = json.dumps({"v": BOOTSTRAP_VERSION, "trades": trades_hash, "profile": profile_hash,
                       **{k: params[k] for k in ("n", "block_days", "seed", "mode")}}, sort_keys=True)
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def _profile(svc, profile_id: str) -> dict:
    from edgelab.prop.service import _profile_hash, default_profiles
    for p in default_profiles(svc.root):
        if p["profile_id"] == profile_id:
            return {**p, "_hash": _profile_hash(p)}
    raise BootstrapError(f"unknown prop rule profile {profile_id!r}")


def request(svc, run_id: str, profile_id: str, raw: Mapping) -> dict:
    """Return the cached result, or start (or report) the background bootstrap for (run, profile, params)."""
    params = params_of(raw.get("n"), raw.get("block_days"), raw.get("seed"), raw.get("mode"))
    with svc.lock:
        rec, trades = svc.store.load_run(run_id)
    prof = _profile(svc, profile_id)
    key = cache_key(str(rec.get("trades_hash")), prof["_hash"], params)
    head = {"run_id": run_id, "profile_id": profile_id, "profile_version": prof.get("version"),
            "mode_label": MODES[params["mode"]], **params, "key": key,
            "method": f"block bootstrap of New York trading days (blocks of {params['block_days']} days), "
                      f"{params['n']} replays, seed {params['seed']}, through the prop lifecycle simulator",
            "label": "Simulated · resampled historical trades under stated rules (some rules are assumed defaults); "
                     "not a forecast", "unsupported_modes": UNSUPPORTED_MODES, "modes": MODES}
    path = cache_dir(svc) / f"{key}.json"
    if path.is_file():
        try:
            return {**head, "state": "done", "result": json.loads(path.read_text(encoding="utf-8"))}
        except (OSError, ValueError):
            path.unlink(missing_ok=True)
    with _lock:
        job = _jobs.get(key)
        if job is None or job["state"] == "error":
            job = {"state": "running", "done": 0, "total": params["n"], "error": None}
            _jobs[key] = job
            prof_clean = {k: v for k, v in prof.items() if k != "_hash"}
            threading.Thread(target=_work, args=(svc, key, path, trades, prof_clean, params), daemon=True,
                             name=f"edgelab-prop-bootstrap-{key[:8]}").start()
        snap = dict(job)
    return {**head, **snap}


def _work(svc, key: str, path: Path, trades: pd.DataFrame, prof: dict, params: dict) -> None:
    def progress(k: int) -> None:
        with _lock:
            _jobs[key]["done"] = k
    try:
        res = run_bootstrap(trades, prof, params, progress)
        res["computed_at"] = datetime.now(timezone.utc).isoformat()
        res["version"] = BOOTSTRAP_VERSION
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(res, default=_json_default), encoding="utf-8")
        tmp.replace(path)
        with _lock:
            _jobs[key].update(state="done")
    except Exception as exc:                                  # noqa: BLE001 - reported to the UI
        with _lock:
            _jobs[key].update(state="error", error=f"{type(exc).__name__}: {exc}")


def _json_default(o):
    if isinstance(o, (np.integer,)):
        return int(o)
    if isinstance(o, (np.floating,)):
        return None if not math.isfinite(float(o)) else float(o)
    return str(o)


def cached(svc, run_id: str, rec: Mapping, profile_id: str, params: Mapping | None = None) -> dict | None:
    """A finished result from the cache only (never starts work)."""
    p = params_of(**(params or {}))
    try:
        prof = _profile(svc, profile_id)
    except BootstrapError:
        return None
    path = cache_dir(svc) / f"{cache_key(str(rec.get('trades_hash')), prof['_hash'], p)}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None
    except (OSError, ValueError):
        return None


__all__ = ["BOOTSTRAP_VERSION", "MODES", "UNSUPPORTED_MODES", "BootstrapError", "params_of", "variant_profile",
           "trading_day_groups", "make_replay", "simulate_replay", "run_bootstrap", "request", "cached"]
