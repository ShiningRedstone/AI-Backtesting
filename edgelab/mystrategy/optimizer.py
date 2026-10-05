"""Strategy autotuner (ADR-101): a step-by-step optimiser of My strategy that starts from one of the user's backtests.

* **Start.** The settings of a My strategy backtest the user picks. Position-size settings (``FIXED``) and the flip switch
  are never changed.
* **Tweaks.** Every setting that can matter under the current settings gets its neighbouring values (a switch flipped,
  another choice, a number one step up or down, a time 15 minutes earlier or later). Settings that cannot change the
  trades under the current settings (``INERT_UNLESS``) are not tried; invalid or self-contradicting results are skipped.
* **Train / check.** The discovery period is split by trading days: the first 70 % ("train") chooses, the last 30 %
  ("check") confirms. A tweak becomes the new best only when it improves BOTH parts. Tweaks are tried in fixed batches of
  ``BATCH`` (the continuation of the last successful change first, then a fixed shuffle); the best improving tweak of a
  batch is taken, so the result never depends on how many CPU cores ran it. The run stops when no tweak of the current
  best improves the check part any more, at the run's try limit, at the protocol's budget, or when the user stops it.
* **Score.** The user's goals first (``ui.autotune_goals``: how many ticked rules are met, then how far the unmet ones
  are from their values), then the prop challenge chain's net (payouts - fees) under the pass-criteria account and the
  Settings fees, then net R. Count rules (losing months, payouts) are scaled to each part's share of the trading days.
* **Speed.** A try runs the ONE engine without the empirical lookahead check and writes no trade records or candles
  (numbers only). Every new best gets the full lookahead check (in parallel); a best that fails it is discarded with
  everything built on it. When the run finishes, the final best is backtested normally (trade records, candles and the
  check again) under this protocol: the same trial, not a new try. Worker processes share one copy of the price data.
* **Protocol.** A companion of the active research protocol (role ``my_optimizer``): same data, windows, execution and
  config hash; its own budget of 5,000 tries (one try = one settings combination on the discovery period; a combination
  tried again is never a new try) and ONE holdout look. Nothing here looks at the holdout window.
"""
from __future__ import annotations

import json
import os
import random
import threading
import time
import traceback
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.mystrategy import challenge as CH
from edgelab.mystrategy import params as P
from edgelab.mystrategy import runner as R
from edgelab.mystrategy.logic import RULES_VERSION

OPTIMIZER_VERSION = 1
TRIAL_BUDGET = 5000
HOLDOUT_LOOKS = 1
ENTRY_POINT = "my_optimizer"
TRAIN_SHARE = 0.70
BATCH = 32
DEFAULT_MAX_TRIES = 1000
MAX_TRIES_LIMIT = TRIAL_BUDGET
TIME_STEP = 15                           # minutes
FIXED = ("risk.mode", "risk.pct", "risk.usd", "risk.max_contracts", "models.flip")
# one step of every number setting (a tweak = one step up or down, inside the setting's range)
STEPS = {
    "session.judas_minutes": 5, "bias.weight_1d": 0.5, "bias.weight_4h": 0.5, "bias.weight_1h": 0.5,
    "bias.weight_15m": 0.5, "bias.fvg_lookback": 10, "bias.events_per_tf": 1, "bias.fvg_min_points": 1.0,
    "bias.structure_strength": 1, "bias.min_score": 0.5, "bias.ath_days": 20, "bias.ath_pct": 0.25,
    "draw.swing_strength": 1, "draw.min_points": 10.0, "draw.max_points": 100.0, "eq.swing_strength": 1,
    "eq.level": 0.05, "eq.tolerance": 0.05, "filters.chop_minutes": 30, "filters.chop_min_gaps": 1,
    "filters.chop_max_flip": 0.1, "filters.min_quality": 1, "key.fvg_min_points": 1.0, "key.fvg_max_age": 20,
    "key.cisd_max_candles": 1, "key.rb_wick_ratio": 0.05, "key.tolerance_points": 0.5, "key.min_levels": 1,
    "leg.swing_strength": 1, "leg.min_points": 5.0, "leg.max_minutes": 30, "leg.equal_points": 1.0,
    "ifg.min_gap_points": 0.25, "ifg.close_buffer": 0.25, "ifg.max_wait": 5, "ifg.body_ratio": 0.05,
    "ifg.range_x_avg": 0.1, "entry.limit_minutes": 5, "stop.buffer": 0.5, "stop.min_points": 1.0,
    "stop.max_points": 10.0, "target.fixed_r": 0.25, "target.swing_strength": 1, "target.min_r": 0.25,
    "target.max_r": 0.25, "manage.be_r": 0.25, "manage.be_offset": 0.25, "manage.be_min_r": 0.1,
    "manage.trail_start_r": 0.25, "manage.trail_buffer": 0.5, "day.max_trades": 1, "day.cooldown": 5,
}
# a setting that cannot change the trades unless this holds (changing it would only spend a try)
INERT_UNLESS = {
    "bias.structure_strength": lambda s: s["bias.method"] in ("structure", "both") and s["bias.override"] == "auto",
    "bias.ath_days": lambda s: s["bias.ath_rule"] and s["models.direction"] != "long_only" and s["bias.override"] != "long",
    "bias.ath_pct": lambda s: s["bias.ath_rule"] and s["models.direction"] != "long_only" and s["bias.override"] != "long",
    "bias.ath_rule": lambda s: s["models.direction"] != "long_only" and s["bias.override"] != "long",
    "bias.tie_break": lambda s: s["bias.override"] == "auto",
    "bias.min_score": lambda s: s["bias.override"] == "auto",
    "bias.method": lambda s: s["bias.override"] == "auto",
    "bias.tf_15m": lambda s: s["bias.override"] == "auto",
    "bias.tf_1h": lambda s: s["bias.override"] == "auto",
    "bias.tf_4h": lambda s: s["bias.override"] == "auto",
    "bias.tf_1d": lambda s: s["bias.override"] == "auto",
    "bias.weight_1d": lambda s: s["bias.override"] == "auto" and s["bias.tf_1d"],
    "bias.weight_4h": lambda s: s["bias.override"] == "auto" and s["bias.tf_4h"],
    "bias.weight_1h": lambda s: s["bias.override"] == "auto" and s["bias.tf_1h"],
    "bias.weight_15m": lambda s: s["bias.override"] == "auto" and s["bias.tf_15m"],
    "bias.respect_needs_touch": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    "bias.events_per_tf": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    "bias.fvg_lookback": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    "bias.fvg_min_points": lambda s: s["bias.override"] == "auto" and s["bias.method"] != "structure",
    **{k: (lambda s: s["draw.required"] or (s["target.draw"] and s["target.mode"] == "liquidity"))
       for k in ("draw.tf_1d", "draw.tf_4h", "draw.tf_1h", "draw.tf_15m", "draw.swing_strength", "draw.prev_day",
                 "draw.unfilled_fvg", "draw.nwog", "draw.min_points", "draw.max_points")},
    "eq.tolerance": lambda s: s["eq.enabled"], "eq.range": lambda s: s["eq.enabled"],
    "eq.tf": lambda s: s["eq.enabled"] and s["eq.range"] == "swings", "eq.level": lambda s: s["eq.enabled"],
    "eq.swing_strength": lambda s: s["eq.enabled"] and s["eq.range"] == "swings",
    "filters.chop_minutes": lambda s: s["filters.chop"],
    "filters.chop_min_gaps": lambda s: s["filters.chop"], "filters.chop_max_flip": lambda s: s["filters.chop"],
    "filters.smt_in_score": lambda s: s["filters.min_quality"] > 0,
    "key.cisd_needs_fvg": lambda s: s["key.cisd"], "key.cisd_first_touch": lambda s: s["key.cisd"],
    "key.cisd_max_candles": lambda s: s["key.cisd"],
    "key.rb_needs_ce": lambda s: s["key.rejection_block"], "key.rb_wick_ratio": lambda s: s["key.rejection_block"],
    "key.rb_min_tf": lambda s: s["key.rejection_block"],
    "key.fvg_sweep_rule": lambda s: s["key.fvg"], "key.fvg_max_age": lambda s: s["key.fvg"],
    "key.fvg_min_points": lambda s: s["key.fvg"],
    "key.judas_min_tf": lambda s: s["models.judas"], "session.judas_minutes": lambda s: s["models.judas"],
    "leg.judas_open_side": lambda s: s["models.judas"], "leg.judas_from_open": lambda s: s["models.judas"],
    "ifg.rule_judas": lambda s: s["models.judas"], "ifg.rule_ny": lambda s: s["models.ny_4step"],
    "ifg.body_ratio": lambda s: s["ifg.displacement"], "ifg.range_x_avg": lambda s: s["ifg.displacement"],
    "entry.limit_minutes": lambda s: s["entry.type"] == "limit_gap",
    "target.fixed_r": lambda s: s["target.mode"] == "fixed_r",
    "target.min_r": lambda s: s["target.mode"] == "liquidity", "target.max_r": lambda s: s["target.mode"] == "liquidity",
    "target.below_min": lambda s: s["target.mode"] == "liquidity", "target.draw": lambda s: s["target.mode"] == "liquidity",
    "target.swings_1m": lambda s: s["target.mode"] == "liquidity", "target.swings_5m": lambda s: s["target.mode"] == "liquidity",
    "target.swings_15m": lambda s: s["target.mode"] == "liquidity",
    "target.swings_1h": lambda s: s["target.mode"] == "liquidity",
    "target.unfilled_fvg": lambda s: s["target.mode"] == "liquidity",
    "target.session_levels": lambda s: s["target.mode"] == "liquidity",
    "target.no_level": lambda s: s["target.mode"] == "liquidity",
    "target.above_max": lambda s: s["target.mode"] == "liquidity",
    "target.swing_strength": lambda s: s["target.mode"] == "liquidity",
    "manage.be_min_r": lambda s: s["manage.be"] == "leg_swing", "manage.be_r": lambda s: s["manage.be"] == "r",
    "manage.be_offset": lambda s: s["manage.be"] != "off",
    "manage.trail_start_r": lambda s: s["manage.trail"] != "off",
    "manage.trail_buffer": lambda s: s["manage.trail"] != "off",
    "day.cooldown": lambda s: s["day.max_trades"] > 1,
}
CONTRADICTS = [
    lambda s: s["models.direction"] == "long_only" and s["bias.override"] == "short",
    lambda s: s["models.direction"] == "short_only" and s["bias.override"] == "long",
]
METRIC_KEYS = ("trade_count", "win_rate", "expectancy_r", "net_r", "net_usd", "trades_per_week", "profit_factor",
               "max_drawdown_r", "max_drawdown_usd", "months_losing", "months_total", "months_winning",
               "avg_planned_rr", "avg_win_r", "max_loss_streak")
GOAL_KEYS = ("win_rate", "trades_per_week", "losing_months", "profit", "rr", "prop")
_FILE_LOCK = threading.Lock()


def home(svc) -> Path:
    p = R.home(svc) / "optimizer"
    (p / "runs").mkdir(parents=True, exist_ok=True)
    return p


# =============================================================================================== protocol
def _scope(parent: dict) -> str:
    from edgelab.research.protocol import OPTIMIZER_SCOPE_SUFFIX
    sc = parent["material"]["scope"]
    return R.svc_scope(sc["instrument"], sc["provider"]) + OPTIMIZER_SCOPE_SUFFIX


def build_material(parent: dict) -> dict:
    from edgelab.research import protocol as rp
    mat = R.build_material(parent, budget=TRIAL_BUDGET, looks=HOLDOUT_LOOKS)
    mat.update({
        "role": rp.OPTIMIZER_ROLE, "name": "Strategy autotuner",
        "search_constraints": {
            "strategies": "settings combinations of the 'My strategy' rules reached by the step-by-step optimiser "
                          f"(version {OPTIMIZER_VERSION}) from the user's own backtests; one trial = one settings "
                          "combination on one evaluated window",
            "evaluation_windows": "the whole discovery window (split 70 / 30 by trading days into a choosing and a "
                                  "checking part; earlier discovery bars may be warm-up history, never traded)",
            "stages": {"discovery": "optimiser tries, their lookahead checks and saved backtests",
                       "holdout": "one holdout look"}},
    })
    mat["pre_protocol_exposure"] = {
        "statement": "The rules were written from BP Blake's public videos. Every optimiser run starts from a backtest the "
                     "user chose after discovery-period backtests of the My strategy protocol, so the starting points "
                     "are already selected on the discovery period. The videos show trades from May-August 2026, which "
                     "may lie inside the holdout window.",
        "runs": []}
    return mat


def ensure_protocol(svc, lock=None, create: bool = True) -> tuple[dict, dict | None]:
    from edgelab.research import protocol as rp
    guard = lock if lock is not None else nullcontext()
    with guard:
        parent = R._parent(svc)
        if parent is None:
            raise R.MyStrategyError("NO_PROTOCOL", "The autotuner needs the workspace's active research protocol (its "
                                                   "data, discovery and holdout dates). None, or more than one, is active.")
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


# =============================================================================================== tweaks
def _step_values(p: dict, v) -> list[tuple[int, object]]:
    step = STEPS[p["key"]]
    out = []
    for sgn in (1, -1):
        x = v + sgn * step
        x = int(round(x)) if p["type"] == "int" else round(float(x), 6)
        if "min" in p:
            x = max(x, p["min"])
        if "max" in p:
            x = min(x, p["max"])
        if x != v:
            out.append((sgn, x))
    return out


def _time_values(v: str) -> list[tuple[int, str]]:
    m = P.minutes(v)
    return [(sgn, f"{x // 60:02d}:{x % 60:02d}") for sgn in (1, -1) for x in [m + sgn * TIME_STEP] if 0 <= x < 1440]


def tunable() -> list[str]:
    return [p["key"] for p in P.SCHEMA if p["key"] not in FIXED]


def neighbours(s: dict, es_ok: bool) -> list[dict]:
    """Every one-setting tweak of the resolved settings ``s`` that is valid, can change the trades and is new."""
    seen = {P.settings_hash(s)}
    out = []
    for p in P.SCHEMA:
        k = p["key"]
        if k in FIXED or not INERT_UNLESS.get(k, lambda _s: True)(s):
            continue
        cur, t = s[k], p["type"]
        if t == "bool":
            cands = [(0, not cur)]
        elif t == "choice":
            cands = [(0, o) for o in p["options"] if o != cur]
        elif t in ("int", "float"):
            cands = _step_values(p, cur)
        else:
            cands = _time_values(cur)
        for sgn, v in cands:
            new = {**s, k: v}
            if k == "filters.min_quality" and not es_ok and not s["filters.smt"]:
                new["filters.smt_in_score"] = False      # without ES data a higher score cannot count SMT
            try:
                rs = P.resolve(P.changed(new))
            except P.SettingsError:
                continue
            if any(c(rs) for c in CONTRADICTS) or (P.smt_used(rs) and not es_ok):
                continue
            h = P.settings_hash(rs)
            if h in seen:
                continue
            seen.add(h)
            out.append({"key": k, "from": cur, "to": v, "dir": sgn, "overrides": P.changed(rs), "settings_hash": h})
    return out


def ordered(nbs: list[dict], seed_hash: str, last: dict | None) -> list[dict]:
    """A fixed order: the continuation of the last successful change first, then a shuffle seeded by the settings."""
    nbs = list(nbs)
    random.Random(int(seed_hash[:12], 16)).shuffle(nbs)
    if last:
        nbs.sort(key=lambda n: 0 if (n["key"] == last["key"] and n["dir"] == last["dir"] and n["dir"] != 0) else 1)
    return nbs


# =============================================================================================== score
def goal_rows(met: dict, cs: dict | None, goals: dict, share: float) -> list[dict]:
    """Each ticked goal on one part: met or not, and how far off (0 = met). Count goals are scaled by ``share``."""
    rows = []
    for k in GOAL_KEYS:
        g = goals.get(k) or {}
        if not g.get("on"):
            continue
        val = g.get("value")
        if k in ("win_rate", "trades_per_week", "rr"):
            x = met.get({"win_rate": "win_rate", "trades_per_week": "trades_per_week", "rr": "avg_planned_rr"}[k])
            target = float(val) / 100 if k == "win_rate" else float(val)
            ok = x is not None and x >= target
            short = 1.0 if x is None else max(0.0, target - x) / max(target, 0.01)
        elif k == "losing_months":
            lim = float(val) * share
            x = int(met.get("months_losing") or 0)
            ok = x <= lim + 1e-9
            short = max(0.0, x - lim) / max(lim, 1.0)
        elif k == "profit":
            x = met.get("net_r")
            ok = x is not None and x > 0
            e = met.get("expectancy_r")
            short = 1.0 if e is None else max(0.0, -float(e))
        else:                                                   # prop: a passed challenge and N payouts
            need = float(val) * share
            ok_cs = cs is not None and not cs.get("error")
            x = cs["payouts"] if ok_cs else 0
            passes = cs["passes"] if ok_cs else 0
            ok = passes >= 1 and x >= need - 1e-9
            short = (0.0 if passes >= 1 else 0.5) + max(0.0, need - x) / max(need, 1.0)
        rows.append({"goal": k, "ok": bool(ok), "short": round(float(short), 9), "value": x})
    return rows


def score(part: dict, ctx: dict, share: float) -> dict:
    """The score of one part. ``key`` orders scores: goals met, then closeness, then prop net, then net R."""
    met = part.get("metrics") or {}
    cs = CH.summary((part.get("chains") or {}).get(ctx["profile"]), ctx["fees"])
    rows = goal_rows(met, cs, ctx["goals"], share)
    n_ok = sum(1 for r in rows if r["ok"])
    short = round(sum(r["short"] for r in rows if not r["ok"]), 9)
    net = cs.get("net") if cs and not cs.get("error") else None
    nr = met.get("net_r")
    return {"met": n_ok, "goals": len(rows), "short": short, "prop_net": net, "net_r": nr, "rows": rows,
            "chain": cs, "key": [n_ok, -short, -1e12 if net is None else float(net), -1e12 if nr is None else float(nr)]}


def better(a: dict, b: dict) -> bool:
    return tuple(a["key"]) > tuple(b["key"])


# =============================================================================================== worker processes
_W: dict = {}

# ADR-98 measurements (4 years of 1-minute data, 1.39 M bars): about 820 MB per worker for the program and its working
# memory + its share of the price data; reusing zone results between tries costs ~1.1 KB per bar.
WORKER_BASE_MB = 820
USABLE_SHARE = 0.80
PARENT_RESERVE_MB = 1024
MEMO_BYTES_PER_ENTRY = 200
MEMO_MB_PER_BAR = 0.0011
HIGH_PRIORITY_CLASS = 0x00000080     # Windows


def plan_workers(requested: int, ds, shared: bool, available: int | None = None) -> dict:
    """How many worker processes fit, and whether each keeps zone results between tries."""
    from edgelab.research.memory import dataset_bytes, system_memory
    requested = max(1, int(requested))
    if available is None:
        _, available = system_memory()
    ds_mb = dataset_bytes(ds) / 2 ** 20
    worker_mb = WORKER_BASE_MB + (0.5 if shared else 1.5) * ds_mb
    if not available:
        return {"processes": requested, "requested": requested, "limited_by_memory": False, "memo_entries": 0,
                "worker_mb": round(worker_mb), "available_gb": None}
    usable = max(0.0, USABLE_SHARE * available / 2 ** 20 - PARENT_RESERVE_MB)
    fit = max(1, int(usable // worker_mb))
    n = min(requested, fit)
    memo_entries = 0
    spare = (usable - n * worker_mb) / n if n else 0
    need = MEMO_MB_PER_BAR * len(ds.bars)
    if n == requested and spare >= need:                 # cores are the limit: spend spare memory on reuse
        memo_entries = int(spare * 2 ** 20 / MEMO_BYTES_PER_ENTRY)
    return {"processes": n, "requested": requested, "limited_by_memory": n < requested, "memo_entries": memo_entries,
            "worker_mb": round(worker_mb), "available_gb": round(available / 2 ** 30, 1)}


def plan_note(pl: dict, shared: bool) -> str:
    free = f"{pl['available_gb']} GB free" if pl.get("available_gb") is not None else "free memory unknown"
    head = (f"{pl['processes']} of {pl['requested']} CPU cores: limited by memory ({free})" if pl["limited_by_memory"]
            else f"{pl['processes']} CPU core{'s' if pl['processes'] != 1 else ''} ({free})")
    extra = ["high priority", "price data shared" if shared else "price data copied per core"]
    if pl["memo_entries"]:
        extra.append("reusing work between tries")
    return head + " · " + ", ".join(extra)


def share_dataset(ds):
    """(shared memory block or None, payload for the workers). The bars go into ONE shared block; the workers attach
    read-only views to it. Falls back to a normal copy per worker if shared memory is not available."""
    import copy
    from multiprocessing import shared_memory
    names = ("ts_ns", "open", "high", "low", "close", "volume", "spread", "ask_open", "ask_high", "ask_low", "ask_close")
    try:
        arrays = {k: getattr(ds.bars, k) for k in names if getattr(ds.bars, k, None) is not None}
        layout, off = [], 0
        for k, a in arrays.items():
            layout.append((k, a.dtype.str, int(a.shape[0]), off))
            off += (a.nbytes + 63) // 64 * 64
        shm = shared_memory.SharedMemory(create=True, size=max(off, 64))
        for (k, dt, n, o), a in zip(layout, arrays.values()):
            view = np.ndarray((n,), dtype=np.dtype(dt), buffer=shm.buf, offset=o)
            view[:] = a
            del view
        shell = copy.copy(ds)
        object.__setattr__(shell, "bars", None)
        return shm, {"shm": shm.name, "layout": layout, "tf_minutes": ds.bars.tf_minutes, "shell": shell}
    except Exception:                                    # noqa: BLE001 - fall back: every worker gets its own copy
        return None, {"ds": ds}


def _attach(payload: dict):
    if "ds" in payload:
        return payload["ds"], None
    import copy
    from multiprocessing import shared_memory

    from edgelab.data.schema import BarArrays
    shm = shared_memory.SharedMemory(name=payload["shm"])  # the parent owns the block and frees it after the run
    arrays = {}
    for k, dt, n, o in payload["layout"]:
        a = np.ndarray((n,), dtype=np.dtype(dt), buffer=shm.buf, offset=o)
        a.flags.writeable = False
        arrays[k] = a
    bars = BarArrays(tf_minutes=payload["tf_minutes"], **arrays)
    ds = copy.copy(payload["shell"])
    object.__setattr__(ds, "bars", bars)
    ds.verify_unchanged()                                # the shared bars ARE the validated bars (content hash)
    return ds, shm


def _raise_priority() -> None:
    try:
        if os.name == "nt":
            import ctypes
            k = ctypes.windll.kernel32
            k.SetPriorityClass(k.GetCurrentProcess(), HIGH_PRIORITY_CLASS)
    except Exception:                                    # noqa: BLE001 - priority is a convenience, never required
        pass


def _worker_init(cfg: dict, data, es, root: str, win: tuple, split, td_from: int, memo_entries: int = 0,
                 high_priority: bool = False) -> None:
    from edgelab.mystrategy import logic as L
    ds, shm = _attach(data) if isinstance(data, dict) else (data, None)
    if memo_entries:
        L.enable_shared_memo(memo_entries)
    if high_priority:
        _raise_priority()
    _W.update(cfg=cfg, ds=ds, es=es, root=root, win=win, split=split, td_from=td_from, shm=shm)


def evaluate(cfg: dict, ds, es, root, win: tuple, split, td_from: int, overrides: dict, profiles: list[str] | None,
             *, check: bool = False) -> dict:
    """One settings combination through the engine on the whole discovery window, numbers per part. ``check=False``
    (a try): no lookahead check, the train and check parts with the chains of ``profiles``. ``check=True`` (a new best):
    the empirical lookahead check, plus the whole window with every profile's chain and the months. Pure: no files."""
    from edgelab.engine.backtester import run_backtest
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.instruments import check_identity, contract_for
    from edgelab.mystrategy.records import report_numbers
    from edgelab.mystrategy.strategy import MyStrategy
    from edgelab.prop.service import default_profiles
    s = P.resolve(overrides)
    if P.smt_used(s) and es is None:
        raise R.MyStrategyError("ES_DATA_REQUIRED", "These settings use SMT divergence, which needs ES data covering "
                                                    "the discovery period.")
    strat = MyStrategy(s, ds.calendar, trade_from_td=td_from, es=es)
    check_identity(ds.instrument)
    costs = cost_model_from_config(cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    bt = dict(cfg["backtest"])
    if not check:
        bt["require_causality_check"] = False          # a try: the check runs on every new best and the final result
    res = run_backtest(ds, strat, costs, bt, sizing=strat.sizing, contract=contract_for(cfg, strat.sizing))
    tr = res.trades
    rs = CH.resizer_for(cfg, ds, strat, tr)
    entry = pd.to_datetime(tr["entry_ts"], utc=True) if len(tr) else None
    profs = [p for p in default_profiles(root) if profiles is None or p["profile_id"] in profiles]
    sample = cfg.get("sample_size")
    parts = {}
    plan = [("train", win[0], split), ("check", split, win[1])] + ([("full", win[0], win[1])] if check else [])
    for name, a, b in plan:
        if not len(tr):
            sub = tr
        elif name == "full":
            sub = tr
        else:
            sub = tr[((entry >= a) & (entry < b)).to_numpy()] if name == "train" else tr[(entry >= a).to_numpy()]
        nums = report_numbers(sub, a, b, sample)
        met = nums["metrics"]
        part = {"metrics": {k: met.get(k) for k in METRIC_KEYS},
                "chains": {p["profile_id"]: CH.chain(rs, p, a, until=b if name == "train" else None) for p in profs}}
        if name == "full":
            part["monthly"] = nums["monthly"]
            part["weekly"] = met.get("weekly")
            part["prop_brief_all"] = True
        parts[name] = part
    return {"settings_hash": P.settings_hash(s), "strategy_id": res.strategy_id, "trades_hash": res.trades_hash,
            "causality_passed": None if res.causality is None else bool(res.causality.passed),
            "n_signals": res.n_signals, "trade_count": int(len(tr)), "parts": parts,
            "evaluated_content_hash": R.evaluated_hash(ds, s, es)}


def _task(kind: str, tag, overrides: dict, profiles) -> dict:
    t0 = time.perf_counter()
    try:
        out = evaluate(_W["cfg"], _W["ds"], _W["es"], _W["root"], _W["win"], _W["split"], _W["td_from"], overrides,
                       profiles, check=(kind == "check"))
    except Exception as exc:                             # noqa: BLE001 - recorded as a failed try / check
        code = getattr(exc, "code", None)
        msg = getattr(exc, "message", None) or str(exc)
        out = {"error": {"kind": code or ("LOOKAHEAD" if "LOOKAHEAD" in msg else type(exc).__name__), "message": msg,
                         **({} if code else {"trace": traceback.format_exc()[-1500:]})}}
    out.update(kind=kind, tag=tag, duration_s=round(time.perf_counter() - t0, 2))
    return out


# =============================================================================================== inputs
def load_inputs(svc, protocol: dict, lock=None):
    """(ds, es, start, end, td_from) of a protocol's whole discovery window: the 1-minute source dataset (validated,
    ASK prices required) and the ES series when it covers the window (else None)."""
    w = R.windows(protocol["material"])
    start, end = R._ts(w["discovery"]["start"]), R._ts(w["discovery"]["end"])
    ds = svc._cell_dataset(R.dataset_1m(svc, protocol), (start, end), lock)
    if not ds.bars.has_ask_ohlc:
        raise R.MyStrategyError("ASK_OHLC_REQUIRED", "The dataset has no ASK prices; BID/ASK execution needs them.")
    from edgelab.mystrategy import es as ES
    try:
        es = ES.load(svc.data_root)
    except ES.EsError:
        es = None
    if es is not None and not es.covers(max(int(start.value), int(ds.bars.ts_ns[0])),
                                        min(int(end.value), int(ds.bars.ts_ns[-1])) - 3 * 86_400_000_000_000):
        es = None
    return ds, es, start, end, R._trading_date_ord(ds.calendar, start)


def split_point(ds, start, end) -> tuple[pd.Timestamp, float]:
    """The first bar of the trading day that starts the last 30 % of the discovery trading days, and the train share."""
    ts = ds.bars.ts
    mask = np.asarray((ts >= start) & (ts <= end))
    td = ds.calendar.trading_dates(ts[mask])
    days = np.unique(td)
    if len(days) < 10:
        raise R.MyStrategyError("TOO_SHORT", "The discovery period is too short to split into a choosing and a checking part.")
    k = int(len(days) * TRAIN_SHARE)
    first = int(np.argmax(td >= days[k]))
    return pd.Timestamp(ts[mask][first]).tz_convert("UTC"), k / len(days)


# =============================================================================================== stored tries (cache)
def _tries_path(svc) -> Path:
    return home(svc) / "tries.jsonl"


def cache_key(settings_hash: str, content: str, split: pd.Timestamp, config_hash: str) -> str:
    return f"{settings_hash}|{content}|{split.isoformat()}|{config_hash}|r{RULES_VERSION}|c{CH.CHAIN_VERSION}"


def read_cache(svc) -> dict[str, dict]:
    out: dict[str, dict] = {}
    p = _tries_path(svc)
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:                  # a line being written right now
                continue
            if "error" in r:
                continue
            prev = out.get(r["key"])
            if prev is not None:                # several lines (another profile, a check): merge
                merged = {**prev, **{k: v for k, v in r.items() if k != "parts"}}
                parts = {n: dict(v) for n, v in prev.get("parts", {}).items()}
                for n, v in r.get("parts", {}).items():
                    cur = parts.setdefault(n, {})
                    cur.update({k: x for k, x in v.items() if k != "chains"})
                    cur["chains"] = {**cur.get("chains", {}), **v.get("chains", {})}
                merged["parts"] = parts
                if prev.get("causality_passed") is True and r.get("causality_passed") is None:
                    merged["causality_passed"] = True
                out[r["key"]] = merged
            else:
                out[r["key"]] = r
    return out


def _append(path: Path, row: dict) -> None:
    line = json.dumps(R.jsonable(row), separators=(",", ":"), allow_nan=False)
    with _FILE_LOCK, open(path, "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


# =============================================================================================== the run
def _run_path(svc, run_id: str) -> Path:
    import re
    if not re.fullmatch(r"OPT_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}", run_id or ""):
        raise KeyError(run_id)
    return home(svc) / "runs" / f"{run_id}.json"


class Run:
    """The one optimiser run of this process (background thread + worker processes)."""

    def __init__(self):
        self.state: dict = {"running": False}
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def info(self) -> dict:
        with self._lock:
            return dict(self.state)

    def _set(self, **kw) -> None:
        with self._lock:
            self.state.update(kw)

    def _bump(self, k: str, by: int = 1) -> None:
        with self._lock:
            self.state[k] = self.state.get(k, 0) + by

    def start(self, svc, start_id: str, processes: int, max_tries: int, lock=None) -> dict:
        with self._lock:
            if self.state.get("running"):
                raise R.MyStrategyError("AUTOTUNE_RUNNING", "The autotuner is already running.")
            self._stop.clear()
            self.state = {"running": True, "stopping": False, "step": "Starting", "started_at": R._now(),
                          "processes": processes, "tries_now": 0, "reused_now": 0, "failed_now": 0, "bests_now": 0,
                          "checks_pending": 0, "error": None, "finished_at": None, "run_id": None,
                          "start_id": start_id}
        self._thread = threading.Thread(target=self._work, args=(svc, start_id, processes, max_tries, lock), daemon=True,
                                        name="my-optimizer")
        self._thread.start()
        return self.info()

    def stop(self) -> dict:
        self._stop.set()
        self._set(stopping=True, step="Stopping: finishing the tries already running")
        return self.info()

    def _work(self, svc, start_id, processes, max_tries, lock) -> None:
        try:
            self._run(svc, start_id, processes, max_tries, lock)
        except Exception as exc:                         # noqa: BLE001 - shown on the page, never swallowed
            msg = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            err = {"kind": getattr(exc, "code", type(exc).__name__), "message": msg,
                   **({} if hasattr(exc, "code") else {"trace": traceback.format_exc()[-2000:]})}
            self._set(error=err, step="Failed")
            rec = getattr(self, "_rec", None)
            if rec is not None and rec.get("status") == "running":
                rec.update(status="failed", error=err, finished_at=R._now())
                self._save(svc)
        finally:
            self._set(running=False, stopping=False, finished_at=R._now())

    # ------------------------------------------------------------------------------------------- setup
    def _run(self, svc, start_id: str, processes: int, max_tries: int, lock) -> None:
        guard = lock if lock is not None else nullcontext()
        self._rec = None
        self._set(step="Checking the protocol and the starting backtest")
        _, mine = ensure_protocol(svc, lock)
        mat = mine["material"]
        if svc._config_hash() != mat["config_hash"]:
            raise R.MyStrategyError("PROTOCOL_CONFIG_CHANGED", "The research settings (costs, fills, sessions) differ "
                                                               "from the protocol's. Restore them first.")
        start_sm = start_summary(svc, start_id)
        s0 = P.resolve(start_sm.get("settings_changed") or {})
        ctx = run_context(svc)
        self._set(step="Loading and checking the price data")
        ds, es, start, end, td_from = load_inputs(svc, mine, lock)
        if P.smt_used(s0) and es is None:
            raise R.MyStrategyError("ES_DATA_REQUIRED", "The starting backtest uses SMT divergence with ES. Import ES data "
                                                        "that covers the whole discovery period first (My strategy -> "
                                                        "Settings -> ES data for SMT).")
        split, train_share = split_point(ds, start, end)
        run_id = R.new_id("OPT")
        self._rec = {
            "id": run_id, "optimizer_version": OPTIMIZER_VERSION, "rules_version": RULES_VERSION,
            "app_version": R._code_version(), "created_at": R._now(), "status": "running", "protocol_id": mine["protocol_id"],
            "start": {"id": start_id, "label": start_sm.get("label") or "", "created_at": start_sm.get("created_at"),
                      "overrides": P.changed(s0), "settings_hash": P.settings_hash(s0)},
            "goals": ctx["goals"], "profile": ctx["profile"], "profile_name": ctx["profile_name"], "fees": ctx["fees"],
            "window": {"start": start.isoformat(), "end": end.isoformat(), "split": split.isoformat(),
                       "train_share": round(train_share, 6)},
            "max_tries": int(max_tries), "batch": BATCH, "dataset_content_hash": ds.manifest.content_hash,
            "es_content_hash": None if es is None else es.content_hash,
            "tries": 0, "reused": 0, "failed": 0, "rejected_by_check": 0, "bests": [], "stop_reason": None,
            "final_backtest": None, "finished_at": None, "error": None}
        self._set(run_id=run_id)
        self._save(svc)
        ctx = {**ctx, "train_share": train_share, "check_share": 1 - train_share}
        self._ctx, self._mine, self._ds, self._es = ctx, mine, ds, es
        self._split, self._win = split, (start, end)
        self._cache = read_cache(svc)
        self._tries_file = home(svc) / "runs" / f"{run_id}.tries.jsonl"
        shm, payload = share_dataset(ds)
        shared = shm is not None
        pl = plan_workers(processes, ds, shared)
        self._set(processes=pl["processes"], memory_note=plan_note(pl, shared), memory_plan=pl, step="Running")
        self._rec["memory_plan"] = pl
        self._pool_args = (dict(svc.cfg), payload, es, str(svc.root), (start, end), split, td_from)
        self._n_workers, self._memo = pl["processes"], pl["memo_entries"]
        self._pool = None
        self._checks: dict = {}                          # future -> best index
        self._reverted = False
        try:
            self._new_pool()
            self._search(svc, s0, int(max_tries), guard)
        finally:
            self._close_pool(cancel=True)
            if shm is not None:
                shm.close()
                try:
                    shm.unlink()
                except FileNotFoundError:
                    pass
        if self._rec["status"] == "running":
            self._finish(svc, lock)

    # ------------------------------------------------------------------------------------------- pool
    def _new_pool(self) -> None:
        import multiprocessing
        from concurrent.futures import ProcessPoolExecutor
        cfg, payload, es, root, win, split, td_from = self._pool_args
        self._pool = ProcessPoolExecutor(max_workers=self._n_workers, mp_context=multiprocessing.get_context("spawn"),
                                         initializer=_worker_init,
                                         initargs=(cfg, payload, es, root, win, split, td_from, self._memo, True))

    def _close_pool(self, cancel: bool) -> None:
        if self._pool is None:
            return
        if cancel:
            for p in list(getattr(self._pool, "_processes", {}).values()):   # running lookahead checks: stop them
                try:
                    p.terminate()
                except Exception:                        # noqa: BLE001
                    pass
        try:
            self._pool.shutdown(wait=True, cancel_futures=True)
        except Exception:                                # noqa: BLE001 - a terminated pool may complain
            pass
        self._pool = None

    def _restart_pool(self) -> None:
        self._close_pool(cancel=True)
        self._n_workers, self._memo = max(1, self._n_workers // 2), 0
        self._bump("restarts")
        self._set(processes=self._n_workers,
                  memory_note=f"A worker process stopped; continuing with {self._n_workers} core(s).")
        if self.state.get("restarts", 0) > 4:
            raise R.MyStrategyError("WORKERS_FAILED", "Worker processes kept stopping (memory?).")
        self._new_pool()

    def _submit(self, kind: str, tag, overrides: dict, profiles):
        return self._pool.submit(_task, kind, tag, overrides, profiles)

    # ------------------------------------------------------------------------------------------- search
    def _key_of(self, settings_hash: str, content: str) -> str:
        return cache_key(settings_hash, content, self._split, self._mine["material"]["config_hash"])

    def _content(self, overrides: dict) -> str:
        return R.evaluated_hash(self._ds, P.resolve(overrides), self._es)

    def _cached(self, key: str) -> dict | None:
        c = self._cache.get(key)
        if c is None:
            return None
        parts = c.get("parts") or {}
        if all(self._ctx["profile"] in (parts.get(n) or {}).get("chains", {}) for n in ("train", "check")):
            return c
        return None

    def _search(self, svc, s0: dict, max_tries: int, guard) -> None:
        rec = self._rec
        start = {"key": None, "from": None, "to": None, "dir": 0, "overrides": P.changed(s0),
                 "settings_hash": P.settings_hash(s0)}
        self._set(step="Scoring the starting backtest")
        got = self._evaluate_batch(svc, [start], guard, max_tries)
        if not got:
            rec["stop_reason"] = "stopped" if self._stop.is_set() else "budget"
            return
        nb, out = got[0]
        if "error" in out:
            raise R.MyStrategyError(out["error"]["kind"], "The starting settings could not be run: "
                                                          + out["error"]["message"])
        self._add_best(svc, nb, out, None)
        seen = {start["settings_hash"]}
        last = None
        while not self._stop.is_set():
            cur = rec["bests"][self._current]
            s = P.resolve(cur["overrides"])
            nbs = [n for n in neighbours(s, self._es is not None) if n["settings_hash"] not in seen]
            nbs = ordered(nbs, cur["settings_hash"], last)
            self._set(step=f"Trying tweaks of best #{cur['n']} ({len(nbs)} to try)", neighbourhood=len(nbs))
            if not nbs:
                rec["stop_reason"] = "no_tweaks_left"
                break
            accepted, train_better, exhausted, reverted = None, False, False, False
            for b0 in range(0, len(nbs), BATCH):
                batch = nbs[b0:b0 + BATCH]
                got = self._evaluate_batch(svc, batch, guard, max_tries)
                for n in batch:
                    seen.add(n["settings_hash"])
                if self._reverted:
                    self._reverted, reverted = False, True
                    break
                scored, cands = [], []
                for i, (nb, out) in enumerate(got):
                    if "error" in out:
                        continue
                    tr = score(out["parts"]["train"], self._ctx, self._ctx["train_share"])
                    ck = score(out["parts"]["check"], self._ctx, self._ctx["check_share"])
                    bt, bc = better(tr, cur["train"]), better(ck, cur["check"])
                    train_better |= bt
                    rec["rejected_by_check"] += bool(bt and not bc)
                    scored.append((i, nb, out, tr, ck, bt and not bc))
                    if bt and bc:
                        cands.append((tuple(tr["key"]), tuple(ck["key"]), -(b0 + i), i, nb, out, tr, ck))
                if cands:
                    accepted = max(cands, key=lambda c: c[:3])
                for i, nb, out, tr, ck, rej in scored:
                    self._log_try(nb, out, tr, ck, accepted=accepted is not None and accepted[3] == i, rejected=rej)
                if accepted is not None:
                    break
                if len(got) < len(batch):                # stopped, try limit or protocol budget reached
                    exhausted = True
                    break
            if reverted:
                last = None
                continue
            if accepted is not None:
                _, _, _, _, nb, out, tr, ck = accepted
                self._add_best(svc, nb, out, cur["n"], tr, ck)
                last = {"key": nb["key"], "dir": nb["dir"]}
                continue
            if exhausted:
                rec["stop_reason"] = rec.get("stop_reason") or ("stopped" if self._stop.is_set() else "limit")
                break
            rec["stop_reason"] = "check_stopped_improving" if train_better else "no_improvement"
            break
        if self._stop.is_set() and not rec.get("stop_reason"):
            rec["stop_reason"] = "stopped"
        self._set(step="Waiting for the lookahead checks of the best results")
        self._drain_checks(svc, wait=not self._stop.is_set())

    def _log_try(self, nb: dict, out: dict, tr: dict, ck: dict, *, accepted: bool, rejected: bool) -> None:
        def brief(sc):
            c = sc["chain"] or {}
            return {"met": sc["met"], "goals": sc["goals"], "short": sc["short"], "prop_net": sc["prop_net"],
                    "net_r": sc["net_r"], "payouts": c.get("payouts"), "passes": c.get("passes"), "fails": c.get("fails")}
        m_t, m_c = out["parts"]["train"]["metrics"], out["parts"]["check"]["metrics"]
        self._bump("logged")
        row = {"i": self.state.get("logged", 0), "best": self._current, "key": nb["key"], "from": nb["from"],
               "to": nb["to"], "settings_hash": nb["settings_hash"], "accepted": accepted, "rejected_by_check": rejected,
               "reused": bool(out.get("_reused")), "train": brief(tr), "check": brief(ck),
               "trades": {"train": m_t.get("trade_count"), "check": m_c.get("trade_count")},
               "win_rate": {"train": m_t.get("win_rate"), "check": m_c.get("win_rate")},
               "duration_s": out.get("duration_s")}
        _append(self._tries_file, row)

    def _evaluate_batch(self, svc, batch: list[dict], guard, max_tries: int) -> list[tuple[dict, dict]]:
        """Results of the batch in batch order (cached ones reused). Fewer than the batch when the run is stopped or a
        limit is reached before all could start. Lookahead checks finishing meanwhile are handled."""
        from concurrent.futures import FIRST_COMPLETED, wait
        from concurrent.futures.process import BrokenProcessPool

        from edgelab.research import protocol as rp
        rec, mine = self._rec, self._mine
        pid, budget = mine["protocol_id"], mine["material"]["trial_budget"]["max_unique_trials"]
        results: dict[int, dict] = {}
        todo: list[tuple[int, dict, str, str]] = []
        for i, nb in enumerate(batch):
            content = self._content(nb["overrides"])
            key = self._key_of(nb["settings_hash"], content)
            c = self._cached(key)
            if c is not None:
                results[i] = {**c, "_reused": True}
                rec["reused"] += 1
                self._bump("reused_now")
                continue
            todo.append((i, nb, content, key))
        # limits: this run's try limit, then the protocol's budget (a key counted before is free)
        room = max(0, max_tries - rec["tries"])
        with guard:
            used = svc.store.count_trials(pid)
            allowed, new = [], 0
            for t in todo:
                tk = rp.trial_key(pid, t[1]["settings_hash"], t[2], mine["material"]["config_hash"])
                fresh = not svc.store.trial_counted(pid, tk)
                if len(allowed) >= room or (fresh and used + new >= budget):
                    if rec.get("stop_reason") is None:
                        rec["stop_reason"] = "limit" if len(allowed) >= room else "budget"
                    break
                new += fresh
                allowed.append(t)
        if self._stop.is_set():
            allowed = []
        pending = {k: t for k, t in enumerate(allowed)}
        inflight: dict = {}
        prof = [self._ctx["profile"]]

        def submit(k):
            inflight[self._submit("try", k, pending[k][1]["overrides"], prof)] = k
        for k in pending:
            submit(k)
        while inflight:
            fin, _ = wait(list(inflight) + list(self._checks), return_when=FIRST_COMPLETED)
            redo, broken = [], False
            for fut in fin:
                if fut in self._checks:
                    n = self._checks.pop(fut)
                    try:
                        out = fut.result()
                    except BrokenProcessPool:
                        broken = True
                        self._checks[fut] = n            # resubmitted below
                        continue
                    self._on_check(svc, n, out)
                    continue
                k = inflight.pop(fut)
                try:
                    out = fut.result()
                except BrokenProcessPool:
                    broken = True
                    redo.append(k)
                    continue
                i, nb, content, key = pending[k]
                self._record_try(svc, nb, out, content, key, guard)
                results[i] = out
            if broken:
                redo += list(inflight.values())
                checks = list(self._checks.values())
                inflight.clear()
                self._checks.clear()
                self._restart_pool()
                for k in redo:
                    submit(k)
                for n in checks:
                    self._submit_check(n)
        self._save(svc)
        blocked = [t[0] for t in todo[len(allowed):]]
        cut = min(blocked) if blocked else len(batch)    # decisions only on the part of the batch before a stop
        return [(batch[i], results[i]) for i in sorted(results) if i < cut]

    def _record_try(self, svc, nb: dict, out: dict, content: str, key: str, guard) -> None:
        from edgelab.research import protocol as rp
        mine, ds = self._mine, self._ds
        pid, mat = mine["protocol_id"], mine["material"]
        tk = rp.trial_key(pid, nb["settings_hash"], out.get("evaluated_content_hash") or content, mat["config_hash"])
        failed = "error" in out
        w = R.windows(mat)["discovery"]
        with guard:
            svc.store.add_trial_event({
                "protocol_id": pid, "trial_id": "TR_" + tk[:12].upper(), "trial_key": tk,
                "status": "failed" if failed else "completed", "entry_point": ENTRY_POINT,
                "strategy_id": out.get("strategy_id"), "logic_hash": nb["settings_hash"],
                "definition_hash": nb["settings_hash"], "family": "my_strategy", "dataset_id": ds.manifest.dataset_id,
                "source_dataset_id": ds.manifest.dataset_id, "evaluated_content_hash": out.get("evaluated_content_hash") or content,
                "window_start": str(w["start"]), "window_end": str(w["end"]), "config_hash": mat["config_hash"],
                "cost_scenario": mat["execution"].get("cost_scenario"), "proposal_id": None, "search_id": None,
                "run_id": None, "error": None if not failed else json.dumps(out["error"])[:500], "created_at": R._now()})
        self._rec["tries"] += 1
        if failed:
            self._rec["failed"] += 1
            self._bump("failed_now")
        self._bump("tries_now")
        self._set(last_duration_s=out.get("duration_s"))
        row = {"key": key, "settings_hash": nb["settings_hash"], "overrides": nb["overrides"],
               "rules_version": RULES_VERSION, "finished_at": R._now(), "run_id": self._rec["id"],
               **{k: out.get(k) for k in ("trades_hash", "strategy_id", "trade_count", "n_signals", "duration_s",
                                          "causality_passed", "parts", "error")}}
        _append(_tries_path(svc), {k: v for k, v in row.items() if v is not None or k == "causality_passed"})
        if not failed:
            prev = self._cache.get(key)
            self._cache[key] = row if prev is None else {**prev, **row}

    # ------------------------------------------------------------------------------------------- bests & checks
    def _add_best(self, svc, nb: dict, out: dict, parent: int | None, tr: dict | None = None, ck: dict | None = None):
        rec = self._rec
        tr = tr or score(out["parts"]["train"], self._ctx, self._ctx["train_share"])
        ck = ck or score(out["parts"]["check"], self._ctx, self._ctx["check_share"])
        b = {"n": len(rec["bests"]), "parent": parent, "change": None if nb["key"] is None else
             {"key": nb["key"], "from": nb["from"], "to": nb["to"]}, "overrides": nb["overrides"],
             "settings_hash": nb["settings_hash"], "trades_hash": out.get("trades_hash"), "found_at": R._now(),
             "tries_before": rec["tries"] + rec["reused"], "train": tr, "check": ck, "lookahead": "pending",
             "full": None, "status": "best"}
        rec["bests"].append(b)
        self._current = b["n"]
        self._bump("bests_now")
        self._set(current_best=b["n"])
        cached_full = (self._cache.get(self._key_of(nb["settings_hash"], self._content(nb["overrides"]))) or {})
        if cached_full.get("causality_passed") is True and "full" in (cached_full.get("parts") or {}):
            self._apply_check(b, {**cached_full, "kind": "check"})
        else:
            self._submit_check(b["n"])
        self._save(svc)

    def _submit_check(self, n: int) -> None:
        b = self._rec["bests"][n]
        self._checks[self._submit("check", n, b["overrides"], None)] = n
        self._set(checks_pending=len(self._checks))

    def _apply_check(self, b: dict, out: dict) -> bool:
        ok = "error" not in out and out.get("causality_passed") is True and out.get("trades_hash") == b["trades_hash"]
        if ok:
            full = out["parts"]["full"]
            from edgelab.mystrategy.challenge import summary as chain_summary
            b["lookahead"] = "passed"
            b["full"] = {"metrics": full["metrics"], "monthly": full.get("monthly"), "weekly": full.get("weekly"),
                         "chains": {pid: chain_summary(raw, self._ctx["fees_all"].get(pid))
                                    for pid, raw in (full.get("chains") or {}).items()},
                         "score": score(full, self._ctx, 1.0)}
        else:
            err = out.get("error") or {}
            b["lookahead"] = "failed"
            b["lookahead_detail"] = (err.get("message") if err else
                                     "the trades with the check differ from the trades without it")[:500]
        return ok

    def _on_check(self, svc, n: int, out: dict) -> None:
        rec = self._rec
        b = rec["bests"][n]
        ok = self._apply_check(b, out)
        if ok and "error" not in out:
            key = self._key_of(b["settings_hash"], out.get("evaluated_content_hash") or self._content(b["overrides"]))
            row = {"key": key, "settings_hash": b["settings_hash"], "overrides": b["overrides"],
                   "rules_version": RULES_VERSION, "finished_at": R._now(), "run_id": rec["id"], "check_run": True,
                   **{k: out.get(k) for k in ("trades_hash", "strategy_id", "trade_count", "n_signals",
                                              "causality_passed", "parts")}}
            _append(_tries_path(svc), row)
            prev = self._cache.get(key) or {}
            parts = {**(prev.get("parts") or {})}
            for nm, v in row["parts"].items():
                parts[nm] = {**parts.get(nm, {}), **v, "chains": {**(parts.get(nm, {}).get("chains") or {}),
                                                                  **(v.get("chains") or {})}}
            self._cache[key] = {**prev, **row, "parts": parts}
        if not ok:
            if n == 0:
                raise R.MyStrategyError("LOOKAHEAD", "The starting settings failed the lookahead check: "
                                        + (b.get("lookahead_detail") or ""))
            # discard this best and every best built on it; continue from its parent
            gone = {n}
            for x in rec["bests"][n + 1:]:
                if x["parent"] in gone:
                    gone.add(x["n"])
            for x in rec["bests"]:
                if x["n"] in gone:
                    x["status"] = "discarded" if x["n"] != n else "failed_lookahead"
            for fut, m in list(self._checks.items()):
                if m in gone:
                    fut.cancel()
                    self._checks.pop(fut, None)
            self._current = b["parent"]
            self._reverted = True
            self._set(current_best=self._current)
        self._set(checks_pending=len(self._checks))
        self._save(svc)

    def _drain_checks(self, svc, wait: bool) -> None:
        from concurrent.futures import FIRST_COMPLETED
        from concurrent.futures import wait as fwait
        while wait and self._checks:
            fin, _ = fwait(list(self._checks), return_when=FIRST_COMPLETED)
            for fut in fin:
                n = self._checks.pop(fut, None)
                if n is None:
                    continue
                try:
                    self._on_check(svc, n, fut.result())
                except Exception as exc:                 # noqa: BLE001 - a broken pool while draining
                    if isinstance(exc, R.MyStrategyError):
                        raise
                    self._rec["bests"][n]["lookahead"] = "not_checked"
        for n in self._checks.values():
            self._rec["bests"][n]["lookahead"] = "not_checked"
        self._checks.clear()
        self._set(checks_pending=0)
        self._save(svc)

    # ------------------------------------------------------------------------------------------- finish
    def _final_best(self) -> dict | None:
        rec = self._rec
        live = [b for b in rec["bests"] if b["status"] == "best"]
        n = self._current
        while n is not None and rec["bests"][n]["status"] != "best":
            n = rec["bests"][n]["parent"]
        b = rec["bests"][n] if n is not None else (live[-1] if live else None)
        while b is not None and b["lookahead"] != "passed" and b["parent"] is not None:
            b = rec["bests"][b["parent"]]
        return b

    def _finish(self, svc, lock) -> None:
        rec = self._rec
        fb = self._final_best()
        rec["final"] = None if fb is None else fb["n"]
        stopped = rec.get("stop_reason") == "stopped" or self._stop.is_set()
        if fb is not None and fb["n"] != 0 and fb["lookahead"] == "passed" and not stopped:
            self._set(step="Backtesting the final best normally (trade records, candles, lookahead check)")
            self._save(svc)
            try:
                sm = save_backtest(svc, rec["id"], fb["n"], lock=lock, record=rec)
                rec["final_backtest"] = sm["id"]
            except Exception as exc:                     # noqa: BLE001 - the search result stays; the error is shown
                rec["final_error"] = getattr(exc, "message", None) or str(exc)
        rec["status"] = "stopped" if stopped else "finished"
        rec["finished_at"] = R._now()
        self._set(step="Stopped" if stopped else "Finished")
        self._save(svc)

    def _save(self, svc) -> None:
        rec = getattr(self, "_rec", None)
        if rec is None:
            return
        rec["current"] = getattr(self, "_current", None)
        R._write_json(_run_path(svc, rec["id"]), rec)


def run_of(svc) -> Run:
    r = svc.__dict__.get("_my_optimizer_run")
    if r is None:
        r = svc.__dict__["_my_optimizer_run"] = Run()
        r._reverted = False
    return r


# =============================================================================================== context
def profile_name(p: dict) -> str:
    """'LucidFlex 50K', 'Tradeify Growth 50K' ... from the profile's provider, product and account size."""
    try:
        first = str(p.get("provider") or "").split()[0]
        prod = str(p.get("product") or p["profile_id"])
        size = p["rules"]["account.size"]["value"] if p.get("schema_version") == 3 else p.get("account_size")
        head = prod if prod.lower().startswith(first.lower()) else f"{first} {prod}"
        return f"{head} {int(float(size) // 1000)}K" if size else head
    except Exception:                                    # noqa: BLE001 - a name is display only
        return p["profile_id"]


def run_context(svc) -> dict:
    """Goals, the pass-criteria account and its fees, frozen when a run starts. Refused without an evaluation price."""
    from edgelab.paper.engine import fee_schedule
    from edgelab.prop.service import default_profiles
    prefs = svc.ui_preferences()
    profile = prefs.get("prop_criteria_profile")
    names = {p["profile_id"]: profile_name(p) for p in default_profiles(svc.root)}
    if profile not in names:
        raise R.MyStrategyError("NO_PROP_ACCOUNT", "Pick the pass-criteria account under Settings first.")
    fees_all = {pid: svc.discounted_fees(pid, (prefs.get("prop_fees") or {}).get(pid) or {})[0] for pid in names}
    if fee_schedule(fees_all[profile])["eval_price"] is None:
        raise R.MyStrategyError("PROP_FEES_MISSING", f"Enter the evaluation price of {names[profile]} under Settings -> "
                                                     "Prop account fees first: the autotuner deducts every challenge fee.")
    return {"goals": prefs.get("autotune_goals"), "profile": profile, "profile_name": names[profile],
            "fees": fees_all[profile], "fees_all": fees_all}


def start_summary(svc, start_id: str) -> dict:
    try:
        folder = R._bt_folder(svc, str(start_id))
    except KeyError:
        raise R.MyStrategyError("NO_START", "Pick one of your My strategy backtests to start from.")
    sm = R._read_json(folder / "summary.json") or {}
    if sm.get("kind") != "discovery_backtest":
        raise R.MyStrategyError("NO_START", "The autotuner starts from a discovery backtest (not a holdout report).")
    return sm


# =============================================================================================== read views
def protocol_status(svc) -> dict:
    try:
        parent, mine = ensure_protocol(svc, create=False)
        return {"ready": True, "protocol_id": mine["protocol_id"] if mine else None, "created": mine is not None,
                **R.windows((mine or parent)["material"]),
                "config_ok": svc._config_hash() == (mine or parent)["material"]["config_hash"],
                "trial_budget": TRIAL_BUDGET, "holdout_looks": HOLDOUT_LOOKS,
                "trials_used": svc.store.count_trials(mine["protocol_id"]) if mine else 0}
    except R.MyStrategyError as e:
        return {"ready": False, "problem": e.message}


def _brief(rec: dict) -> dict:
    fb = rec["bests"][rec["final"]] if rec.get("final") is not None and rec["bests"] else None
    first = rec["bests"][0] if rec["bests"] else None
    return {k: rec.get(k) for k in ("id", "created_at", "finished_at", "status", "stop_reason", "tries", "reused",
                                    "failed", "rejected_by_check", "max_tries", "profile", "profile_name",
                                    "final_backtest", "error")} | {
        "start": rec["start"], "bests": len([b for b in rec["bests"] if b["status"] == "best"]),
        "start_full": (first or {}).get("full"), "final_full": (fb or {}).get("full"),
        "final_n": None if fb is None else fb["n"]}


def list_runs(svc) -> list[dict]:
    out = []
    for f in sorted((home(svc) / "runs").glob("OPT_*.json"), reverse=True):
        rec = R._read_json(f)
        if rec:
            out.append(_brief(rec))
    return out


def run_detail(svc, run_id: str) -> dict:
    rec = R._read_json(_run_path(svc, run_id))
    if rec is None:
        raise KeyError(run_id)
    live = run_of(svc).info()
    if live.get("running") and live.get("run_id") == run_id:
        rec["live"] = live
    tries = []
    p = home(svc) / "runs" / f"{run_id}.tries.jsonl"
    if p.exists():
        with open(p, encoding="utf-8") as f:
            for line in f:
                try:
                    tries.append(json.loads(line))
                except ValueError:
                    continue
    rec["tries_log"] = tries
    labels = {d["key"]: d["label"] for d in P.SCHEMA}
    fb = rec["bests"][rec["final"]] if rec.get("final") is not None else \
        next((b for b in reversed(rec["bests"]) if b["status"] == "best"), None)
    if fb is not None and rec["bests"]:
        a, b = P.resolve(rec["bests"][0]["overrides"]), P.resolve(fb["overrides"])
        rec["changes"] = [{"key": k, "label": labels.get(k, k), "from": a[k], "to": b[k]} for k in a if a[k] != b[k]]
    rec["labels"] = {k: labels[k] for k in {b["change"]["key"] for b in rec["bests"] if b.get("change")}
                     | {t["key"] for t in tries if t.get("key")} if k in labels}
    saved = []
    for b in R.list_backtests(svc):
        sm = R._read_json(R.home(svc) / "backtests" / b["id"] / "summary.json") or {}
        if sm.get("optimizer_run") == run_id:
            saved.append({"id": b["id"], "best": sm.get("optimizer_best"), "created_at": b["created_at"]})
    rec["saved"] = saved
    return R.jsonable(rec)


def save_backtest(svc, run_id: str, n: int, *, lock=None, progress=None, record: dict | None = None) -> dict:
    """One best of a run as a normal My strategy backtest (trade records, candles, the lookahead check) under the
    autotuner's protocol: the same trial as the try, never a new one."""
    rec = record or R._read_json(_run_path(svc, run_id))
    if rec is None:
        raise R.MyStrategyError("NO_RUN", f"There is no autotuner run {run_id}.")
    if not (0 <= int(n) < len(rec["bests"])):
        raise R.MyStrategyError("NO_BEST", f"Run {run_id} has no best result #{n}.")
    b = rec["bests"][int(n)]
    if b["status"] == "failed_lookahead":
        raise R.MyStrategyError("LOOKAHEAD", "This result failed the lookahead check.")
    _, mine = ensure_protocol(svc, lock)
    label = f"Autotuner {run_id[4:19]} best #{b['n']}" + (" (final)" if rec.get("final") == b["n"] else "")
    return R.backtest(svc, b["overrides"], label=label, lock=lock, progress=progress, protocol=mine,
                      extra={"optimizer_run": run_id, "optimizer_best": int(b["n"])})


def status(svc) -> dict:
    from edgelab.mystrategy import es as ES
    try:
        es = ES.status(svc.data_root)
    except Exception:                                    # noqa: BLE001 - status only
        es = None
    try:
        ctx = run_context(svc)
        ctx_problem = None
    except R.MyStrategyError as e:
        ctx, ctx_problem = None, {"kind": e.code, "message": e.message}
    starts = [b for b in R.list_backtests(svc) if b.get("kind") == "discovery_backtest"]     # newest first
    starts = [b for b in starts if b.get("favorite")] + [b for b in starts if not b.get("favorite")]
    return R.jsonable({"protocol": protocol_status(svc), "run": run_of(svc).info(), "runs": list_runs(svc)[:50],
                       "starts": starts, "es": es, "cpu_count": os.cpu_count() or 1,
                       "context": None if ctx is None else {k: ctx[k] for k in ("goals", "profile", "profile_name", "fees")},
                       "context_problem": ctx_problem, "default_max_tries": DEFAULT_MAX_TRIES, "batch": BATCH,
                       "train_share": TRAIN_SHARE, "fixed": list(FIXED), "rules_version": RULES_VERSION})
