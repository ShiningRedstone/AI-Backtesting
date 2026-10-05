"""Strategy autotuner (ADR-97): the 10,000 reasoned settings combinations of My strategy, tested on the discovery period.

* **Protocol.** A companion of the active research protocol (role ``my_autotune``, scope ``<inst>@<prov>#my_autotune``):
  same data, windows, execution and config hash; its own budget of 10,000 tries (one per combination) and ONE holdout
  look. My strategy's own protocol (300 tries) is untouched. Its exposure statement says that the base (test 37) was
  chosen with discovery-period backtests.
* **Design.** ``autotune_space.manifest()`` frozen into ``<data>/my_strategy/autotune/manifest.json`` on the first
  start (the frozen file is what runs; its hash is checked against the code's design).
* **Run.** Every combination goes through the ONE engine exactly like a My strategy backtest of the whole discovery
  period: the empirical lookahead check, BID/ASK costs, MNQ sizing, then the prop audit of its trades. Worker
  processes (spawned) compute; the parent alone writes: one trial event per combination in the protocol's ledger and one
  line in ``results.jsonl`` (numbers only, no trades / candles: the user's choice). Stop keeps what finished; Start
  resumes with the rest (failed ones run again; a try is counted once per trial key).
* **Rerun.** One combination can be run again as a normal My strategy backtest (trade records + candles) under the
  autotuner protocol: the same trial key, so not a new try.

Human judgement and the holdout stay separate: nothing here looks at the holdout window.
"""
from __future__ import annotations

import json
import os
import threading
import time
import traceback
from contextlib import nullcontext
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

from edgelab.mystrategy import autotune_space as A
from edgelab.mystrategy import params as P
from edgelab.mystrategy import runner as R
from edgelab.core.fsutil import atomic_write_text
from edgelab.mystrategy.logic import RULES_VERSION

TRIAL_BUDGET = A.TOTAL
HOLDOUT_LOOKS = 1
ENTRY_POINT = "my_autotune"
METRIC_KEYS = ("trade_count", "win_rate", "expectancy_r", "net_r", "net_usd", "trades_per_week", "profit_factor",
               "max_drawdown_r", "max_drawdown_usd", "months_losing", "months_total", "months_winning",
               "avg_planned_rr", "avg_win_r", "max_loss_streak")
_FILE_LOCK = threading.Lock()


def home(svc) -> Path:
    p = R.home(svc) / "autotune"
    p.mkdir(parents=True, exist_ok=True)
    return p


# =============================================================================================== protocol
def _scope(parent: dict) -> str:
    from edgelab.research.protocol import AUTOTUNE_SCOPE_SUFFIX
    sc = parent["material"]["scope"]
    return R.svc_scope(sc["instrument"], sc["provider"]) + AUTOTUNE_SCOPE_SUFFIX


def build_material(parent: dict) -> dict:
    from edgelab.research import protocol as rp
    mat = R.build_material(parent, budget=TRIAL_BUDGET, looks=HOLDOUT_LOOKS)
    mat.update({
        "role": rp.AUTOTUNE_ROLE, "name": "Strategy autotuner",
        "search_constraints": {
            "strategies": f"only the {A.TOTAL:,} frozen combinations of the 'My strategy' settings (autotuner design "
                          f"version {A.AUTOTUNE_VERSION}); one trial = one settings combination on one evaluated window",
            "evaluation_windows": "the whole discovery window (earlier discovery bars may be warm-up history, never "
                                  "traded)",
            "stages": {"discovery": "autotuner combinations and their reruns", "holdout": "one holdout look"}},
    })
    mat["pre_protocol_exposure"] = {
        "statement": "The rules were written from BP Blake's public videos. The base of every combination ('test 37') was "
                     "chosen by the user with 37 discovery-period backtests of the My strategy protocol, so the base is "
                     "already tuned on the discovery period. The videos show trades from May-August 2026, which may lie "
                     "inside the holdout window.",
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


# =============================================================================================== design (frozen)
def frozen_manifest(svc, create: bool = False) -> dict | None:
    """The frozen design. Created from the code on the first start; afterwards the file is what runs."""
    path = home(svc) / "manifest.json"
    m = R._read_json(path)
    if m is None and create:
        m = A.manifest()
        m["frozen_at"] = R._now()
        R._write_json(path, m)
    return m


def design(svc) -> dict:
    """The design without its 10,000 rows (summary for the page)."""
    m = frozen_manifest(svc) or A.manifest()
    return {k: m[k] for k in ("autotune_version", "manifest_hash", "total", "base", "base_label", "themes", "options",
                              "counts")} | {"frozen": "frozen_at" in m, "frozen_at": m.get("frozen_at")}


# =============================================================================================== results file
def _results_path(svc) -> Path:
    return home(svc) / "results.jsonl"


def _version_of(r: dict) -> int:
    return int(r.get("rules_version") or 1)            # lines written before ADR-98 carry no version: rules 1


def read_results(svc) -> dict[int, dict]:
    """Latest result per combination number (a later line for the same number replaces an earlier failed one). Only
    results of the CURRENT rule code count (ADR-98): older ones were computed with the cached-gap bug."""
    out: dict[int, dict] = {}
    p = _results_path(svc)
    if not p.exists():
        return out
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                r = json.loads(line)
            except ValueError:                  # a line being written right now
                continue
            if _version_of(r) == RULES_VERSION:
                out[int(r["n"])] = r
    return out


def set_aside_old(svc) -> int:
    """Move results of older rule code out of results.jsonl into results_rules_v<k>.jsonl (kept, never shown).
    Returns how many lines were moved. Called by the run before it starts (the parent is the only writer)."""
    p = _results_path(svc)
    if not p.exists():
        return 0
    keep, old = [], {}
    with _FILE_LOCK:
        for line in p.read_text(encoding="utf-8").splitlines():
            try:
                r = json.loads(line)
            except ValueError:
                continue
            v = _version_of(r)
            (keep if v == RULES_VERSION else old.setdefault(v, [])).append(line)
        if not old:
            return 0
        for v, lines in old.items():
            with open(home(svc) / f"results_rules_v{v}.jsonl", "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        atomic_write_text(p, "".join(x + "\n" for x in keep))
    return sum(len(x) for x in old.values())


def _old_lines(svc) -> int:
    """Results of older rule code still in results.jsonl (until the next start sets them aside)."""
    p = _results_path(svc)
    if not p.exists():
        return 0
    n = 0
    with open(p, encoding="utf-8") as f:
        for line in f:
            try:
                n += _version_of(json.loads(line)) != RULES_VERSION
            except ValueError:
                continue
    return n


def set_aside_count(svc) -> int:
    n = 0
    for f in home(svc).glob("results_rules_v*.jsonl"):
        with open(f, encoding="utf-8") as fh:
            n += sum(1 for line in fh if line.strip())
    return n


def _append(svc, row: dict) -> None:
    line = json.dumps(R.jsonable(row), separators=(",", ":"), allow_nan=False)
    with _FILE_LOCK, open(_results_path(svc), "a", encoding="utf-8") as f:
        f.write(line + "\n")
        f.flush()
        os.fsync(f.fileno())


# =============================================================================================== worker processes
_W: dict = {}

# ADR-98 speed. Measured on 4 years of 1-minute data (1.39 M bars, 106 MB): one worker peaks at about 970 MB = about
# 820 MB for the program + its own copy of the bars + working memory. Shared bars (one copy for every worker) leave
# about half a dataset per worker. Reusing zone results between combinations (logic.enable_shared_memo) needs about
# 200 bytes per result and about 1.1 KB per bar for a whole combination's set; it is worth it only when CPU cores,
# not memory, limit the run (it makes a combination ~1.5x faster but needs ~1.5 GB more per worker).
WORKER_BASE_MB = 820
USABLE_SHARE = 0.80                  # of the memory free at the start (the user chose maximum speed)
PARENT_RESERVE_MB = 1024
MEMO_BYTES_PER_ENTRY = 200
MEMO_MB_PER_BAR = 0.0011
HIGH_PRIORITY_CLASS = 0x00000080     # Windows


def plan_workers(requested: int, ds, shared: bool, available: int | None = None) -> dict:
    """How many worker processes fit, and whether each keeps zone results between combinations."""
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
        extra.append("reusing work between combinations")
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


def _worker_init(cfg: dict, data, es, root: str, win: tuple, td_from: int, memo_entries: int = 0,
                 high_priority: bool = False) -> None:
    from edgelab.mystrategy import logic as L
    ds, shm = _attach(data) if isinstance(data, dict) else (data, None)
    if memo_entries:
        L.enable_shared_memo(memo_entries)
    if high_priority:
        _raise_priority()
    _W.update(cfg=cfg, ds=ds, es=es, root=root, win=win, td_from=td_from, shm=shm)


def evaluate(cfg: dict, ds, es, root, win: tuple, td_from: int, overrides: dict) -> dict:
    """One combination through the engine (lookahead check on) and the prop audit. Pure: no store, no files."""
    from edgelab.engine.backtester import run_backtest
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.instruments import check_identity, contract_for
    from edgelab.mystrategy.records import report_numbers
    from edgelab.mystrategy.strategy import MyStrategy
    from edgelab.prop.service import outcomes
    s = P.resolve(overrides)
    use_es = es if (es is not None) else None
    if P.smt_used(s) and use_es is None:
        raise R.MyStrategyError("ES_DATA_REQUIRED", "This combination uses SMT divergence, which needs ES data covering "
                                                    "the discovery period.")
    strat = MyStrategy(s, ds.calendar, trade_from_td=td_from, es=use_es)
    check_identity(ds.instrument)
    costs = cost_model_from_config(cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    res = run_backtest(ds, strat, costs, cfg["backtest"], sizing=strat.sizing, contract=contract_for(cfg, strat.sizing))
    nums = report_numbers(res.trades, win[0], win[1], cfg.get("sample_size"))
    met = nums["metrics"]
    prop = outcomes(root, res.trades, assumptions=res.assumptions)
    briefs = {}
    for p in prop.get("profiles", []):
        pid = (p.get("profile") or {}).get("profile_id")
        if pid:
            briefs[pid] = {"status": p.get("status"), "evaluation": (p.get("evaluation") or {}).get("status"),
                           "payouts": (p.get("totals") or {}).get("n_payouts"),
                           "trader_payout": (p.get("totals") or {}).get("trader_payout")}
    return {"settings_hash": P.settings_hash(s), "strategy_id": res.strategy_id, "trades_hash": res.trades_hash,
            "causality_passed": None if res.causality is None else bool(res.causality.passed),
            "n_signals": res.n_signals, "metrics": {k: met.get(k) for k in METRIC_KEYS},
            "weekly": met.get("weekly"), "monthly": nums["monthly"], "prop": briefs,
            "evaluated_content_hash": R.evaluated_hash(ds, s, use_es)}


def _worker(n: int, overrides: dict) -> dict:
    t0 = time.perf_counter()
    try:
        out = evaluate(_W["cfg"], _W["ds"], _W["es"], _W["root"], _W["win"], _W["td_from"], overrides)
        out["n"] = n
    except Exception as exc:                             # noqa: BLE001 - recorded as a failed combination
        code = getattr(exc, "code", None)
        out = {"n": n, "error": {"kind": code or type(exc).__name__, "message": getattr(exc, "message", None) or str(exc),
                                 **({} if code else {"trace": traceback.format_exc()[-1500:]})}}
    out["duration_s"] = round(time.perf_counter() - t0, 2)
    out["pid"] = os.getpid()
    return out


# =============================================================================================== the run
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


class Run:
    """The one autotuner run of this process (background thread + worker processes)."""

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

    def start(self, svc, processes: int, lock=None) -> dict:
        with self._lock:
            if self.state.get("running"):
                raise R.MyStrategyError("AUTOTUNE_RUNNING", "The autotuner is already running.")
            self._stop.clear()
            self.state = {"running": True, "stopping": False, "step": "Starting", "started_at": R._now(),
                          "processes": processes, "done_now": 0, "failed_now": 0, "error": None, "finished_at": None}
        self._thread = threading.Thread(target=self._work, args=(svc, processes, lock), daemon=True, name="my-autotune")
        self._thread.start()
        return self.info()

    def stop(self) -> dict:
        self._stop.set()
        self._set(stopping=True, step="Stopping: finishing the combinations already running")
        return self.info()

    def _work(self, svc, processes: int, lock) -> None:
        try:
            self._run(svc, processes, lock)
            self._set(step="Stopped" if self._stop.is_set() else "Finished")
        except Exception as exc:                         # noqa: BLE001 - shown on the page, never swallowed
            msg = getattr(exc, "message", None) or f"{type(exc).__name__}: {exc}"
            self._set(error={"kind": getattr(exc, "code", type(exc).__name__), "message": msg,
                             **({} if hasattr(exc, "code") else {"trace": traceback.format_exc()[-2000:]})},
                      step="Failed")
        finally:
            self._set(running=False, stopping=False, finished_at=R._now())

    def _run(self, svc, processes: int, lock) -> None:
        from edgelab.research import protocol as rp
        guard = lock if lock is not None else nullcontext()
        self._set(step="Checking the protocol and the design")
        _, mine = ensure_protocol(svc, lock)
        pid, mat = mine["protocol_id"], mine["material"]
        if svc._config_hash() != mat["config_hash"]:
            raise R.MyStrategyError("PROTOCOL_CONFIG_CHANGED", "The research settings (costs, fills, sessions) differ "
                                                               "from the protocol's. Restore them first.")
        man = frozen_manifest(svc, create=True)
        if man["manifest_hash"] != A.manifest()["manifest_hash"]:
            raise R.MyStrategyError("DESIGN_CHANGED", "The frozen design differs from this app version's design; it "
                                                      "cannot be continued with this version.")
        moved = set_aside_old(svc)                       # ADR-98: results of the old rule code start again
        if moved:
            self._set(set_aside_now=moved)
        done = {n for n, r in read_results(svc).items() if "error" not in r}
        todo = [r for r in man["rows"] if r["n"] not in done]
        self._set(total=man["total"], done_before=len(done))
        if not todo:
            return
        self._set(step="Loading and checking the price data")
        ds, es, start, end, td_from = load_inputs(svc, mine, lock)
        if es is None and any(P.smt_used(P.resolve(r["overrides"])) for r in todo):
            raise R.MyStrategyError("ES_DATA_REQUIRED", "Some combinations use SMT divergence with ES. Import ES data that covers "
                                                        "the whole discovery period first (My strategy -> Settings -> ES data for "
                                                        "SMT); nothing was run.")
        keys = [rp.trial_key(pid, r["settings_hash"], self._content(ds, r["overrides"], es), mat["config_hash"])
                for r in todo]
        with guard:                                      # refuse BEFORE computing when the budget cannot hold the run
            used = svc.store.count_trials(pid)
            new = sum(1 for k in keys if not svc.store.trial_counted(pid, k))
            if used + new > mat["trial_budget"]["max_unique_trials"]:
                raise R.MyStrategyError("TRIAL_BUDGET_EXHAUSTED", f"{new} new tries would exceed the autotuner's budget "
                                                                  f"({used} of {mat['trial_budget']['max_unique_trials']} used).")
        shm, payload = share_dataset(ds)
        shared = shm is not None
        pl = plan_workers(processes, ds, shared)
        n_workers, memo = pl["processes"], pl["memo_entries"]
        self._set(processes=n_workers, memory_note=plan_note(pl, shared), memory_plan=pl, step="Running")
        try:
            self._pool_loop(svc, mine, todo, ds, es, payload, n_workers, memo, start, end, td_from, lock)
        finally:
            if shm is not None:
                shm.close()
                try:
                    shm.unlink()
                except FileNotFoundError:
                    pass

    def _pool_loop(self, svc, mine, todo, ds, es, payload, n_workers, memo, start, end, td_from, lock) -> None:
        import multiprocessing
        from collections import deque
        from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait
        from concurrent.futures.process import BrokenProcessPool
        win = (start, end)
        queue = deque(todo)
        restarts = 0
        while queue and not self._stop.is_set():
            pool = ProcessPoolExecutor(max_workers=n_workers, mp_context=multiprocessing.get_context("spawn"),
                                       initializer=_worker_init,
                                       initargs=(dict(svc.cfg), payload, es, str(svc.root), win, td_from, memo, True))
            inflight: dict = {}
            broken = False
            try:
                while (queue or inflight) and not broken:
                    while queue and len(inflight) < n_workers and not self._stop.is_set():
                        row = queue.popleft()
                        inflight[pool.submit(_worker, row["n"], row["overrides"])] = row
                    if not inflight:
                        break
                    fin, _ = wait(list(inflight), return_when=FIRST_COMPLETED)
                    for fut in fin:
                        row = inflight.pop(fut)
                        try:
                            out = fut.result()
                        except BrokenProcessPool:        # a worker died (e.g. out of memory): retry, fewer workers
                            queue.appendleft(row)
                            broken = True
                            continue
                        self._record(svc, mine, row, out, ds, es, lock)
            finally:
                if broken:
                    for fut, row in inflight.items():
                        queue.appendleft(row)
                pool.shutdown(wait=True, cancel_futures=True)
            if broken:
                restarts += 1
                n_workers, memo = max(1, n_workers // 2), 0      # fewer workers, no reuse memory
                self._set(processes=n_workers, restarts=restarts,
                          memory_note=f"A worker process stopped; continuing with {n_workers} core(s).")
                if restarts > 4:
                    raise R.MyStrategyError("WORKERS_FAILED", "Worker processes kept stopping (memory?).")

    @staticmethod
    def _content(ds, overrides: dict, es) -> str:
        return R.evaluated_hash(ds, P.resolve(overrides), es)

    def _record(self, svc, mine: dict, row: dict, out: dict, ds, es, lock) -> None:
        from edgelab.research import protocol as rp
        guard = lock if lock is not None else nullcontext()
        pid, mat = mine["protocol_id"], mine["material"]
        h = row["settings_hash"]
        content = out.get("evaluated_content_hash") or self._content(ds, row["overrides"], es)
        key = rp.trial_key(pid, h, content, mat["config_hash"])
        failed = "error" in out
        with guard:
            svc.store.add_trial_event({
                "protocol_id": pid, "trial_id": "TR_" + key[:12].upper(), "trial_key": key,
                "status": "failed" if failed else "completed", "entry_point": ENTRY_POINT,
                "strategy_id": out.get("strategy_id"), "logic_hash": h, "definition_hash": h, "family": "my_strategy",
                "dataset_id": ds.manifest.dataset_id, "source_dataset_id": ds.manifest.dataset_id,
                "evaluated_content_hash": content, "window_start": str(R.windows(mat)["discovery"]["start"]),
                "window_end": str(R.windows(mat)["discovery"]["end"]), "config_hash": mat["config_hash"],
                "cost_scenario": mat["execution"].get("cost_scenario"), "proposal_id": None, "search_id": None,
                "run_id": None, "error": None if not failed else json.dumps(out["error"])[:500], "created_at": R._now()})
        out.pop("pid", None)
        _append(svc, {**out, "n": row["n"], "settings_hash": h, "finished_at": R._now(), "rules_version": RULES_VERSION,
                      "trial_id": "TR_" + key[:12].upper(), "app_version": R._code_version()})
        with self._lock:
            self.state["failed_now" if failed else "done_now"] = self.state.get("failed_now" if failed else "done_now",
                                                                                0) + 1
            self.state["last_duration_s"] = out.get("duration_s")


def run_of(svc) -> Run:
    r = svc.__dict__.get("_my_autotune_run")
    if r is None:
        r = svc.__dict__["_my_autotune_run"] = Run()
    return r


# =============================================================================================== read views
GOOD_DEFAULTS = {"min_trades_per_week": 3.0, "min_rr": 1.0}


def status(svc) -> dict:
    try:
        parent, mine = ensure_protocol(svc, create=False)
        proto = {"ready": True, "protocol_id": mine["protocol_id"] if mine else None, "created": mine is not None,
                 **R.windows((mine or parent)["material"]),
                 "config_ok": svc._config_hash() == (mine or parent)["material"]["config_hash"],
                 "trial_budget": TRIAL_BUDGET, "holdout_looks": HOLDOUT_LOOKS,
                 "trials_used": svc.store.count_trials(mine["protocol_id"]) if mine else 0}
    except R.MyStrategyError as e:
        proto = {"ready": False, "problem": e.message}
    res = read_results(svc)
    ok = [r for r in res.values() if "error" not in r]
    dur = sorted(r["duration_s"] for r in ok if r.get("duration_s"))[-200:]
    from edgelab.mystrategy import es as ES
    try:
        es = ES.status(svc.data_root)
    except Exception:                                    # noqa: BLE001 - status only
        es = None
    return R.jsonable({"protocol": proto, "design": design(svc), "done": len(ok),
                       "failed": len(res) - len(ok), "run": run_of(svc).info(),
                       "median_seconds": dur[len(dur) // 2] if dur else None, "es": es,
                       "rules_version": RULES_VERSION, "set_aside": set_aside_count(svc) + _old_lines(svc),
                       "cpu_count": os.cpu_count() or 1})


def points(svc, profile: str | None = None) -> dict:
    """Every finished combination with its numbers (the scatter), plus the 'good' flags under the criteria account."""
    man = frozen_manifest(svc) or A.manifest()
    rows = {r["n"]: r for r in man["rows"]}
    res = read_results(svc)
    out = []
    for n, r in sorted(res.items()):
        m = rows.get(n)
        if m is None or "error" in r:
            continue
        met = r["metrics"]
        pr = (r.get("prop") or {}).get(profile) if profile else None
        out.append({"n": n, "label": m["label"], "stage": m["stage"], "options": m["options"], **met,
                    "prop_evaluation": pr.get("evaluation") if pr else None,
                    "prop_payouts": pr.get("payouts") if pr else None,
                    "prop_trader_payout": pr.get("trader_payout") if pr else None})
    failed = [{"n": n, "label": rows[n]["label"] if n in rows else "", "error": r["error"]}
              for n, r in sorted(res.items()) if "error" in r][:200]
    from edgelab.mystrategy import autotune_flips as AF            # ADR-100: the pink bubbles
    return R.jsonable({"profile": profile, "points": out, "failed": failed, "total": man["total"],
                       "flips": AF.points(svc, profile, rows, res), "flip_budget": AF.status(svc)})


def detail(svc, n: int) -> dict:
    man = frozen_manifest(svc) or A.manifest()
    row = next((r for r in man["rows"] if r["n"] == int(n)), None)
    if row is None:
        raise KeyError(f"combination {n}")
    res = read_results(svc).get(int(n))
    reruns = [b for b in R.list_backtests(svc)
              if (R._read_json(R.home(svc) / "backtests" / b["id"] / "summary.json") or {}).get("autotune_n") == int(n)]
    return R.jsonable({"row": row, "result": res, "base": man["base"], "base_label": man["base_label"],
                       "reruns": reruns})


def rerun(svc, n: int, *, lock=None, progress: Callable | None = None) -> dict:
    """The combination again as a normal My strategy backtest (trade records + candles) under the autotuner protocol:
    same trial key, so never a new try."""
    man = frozen_manifest(svc)
    if man is None:
        raise R.MyStrategyError("NOT_STARTED", "Start the autotuner first.")
    row = next((r for r in man["rows"] if r["n"] == int(n)), None)
    if row is None:
        raise R.MyStrategyError("NO_COMBINATION", f"There is no combination {n}.")
    _, mine = ensure_protocol(svc, lock)
    return R.backtest(svc, row["overrides"], label=f"Autotuner #{n}: {row['label']}"[:160], lock=lock,
                      progress=progress, protocol=mine, extra={"autotune_n": int(n),
                                                               "autotune_manifest": man["manifest_hash"]})


def window_of(svc) -> tuple[pd.Timestamp, pd.Timestamp] | None:
    try:
        _, mine = ensure_protocol(svc, create=False)
    except R.MyStrategyError:
        return None
    if mine is None:
        return None
    w = R.windows(mine["material"])
    return R._ts(w["discovery"]["start"]), R._ts(w["discovery"]["end"])
