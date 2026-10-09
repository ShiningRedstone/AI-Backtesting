"""Setup review of My strategy on the DISCOVERY period (ADR-96): the trader judges setups blind, Claude turns the skips
into rules.

1. ``start``: a finished discovery backtest report is the base. A fixed-seed random sample of its trades (150 by default,
   seed derived from the report id, so the same report always gives the same sample) is put in time order.
2. For each setup the trader sees ONLY data up to the signal bar (the chart stops at the entry candle; the candle that
   contains it is rebuilt from the 1-minute bars up to the signal) and decides Take or Skip. A skip carries at least one
   reason tag (and an optional note). Outcomes stay hidden until every setup is decided; the last decision can be undone.
3. When every setup is decided the outcomes are revealed: taken vs skipped and per reason tag.

Nothing here runs the engine, uses a trial or touches the holdout: the trades and their outcomes are the base report's
(recorded by the engine). The reviewed trades are judged one by one; skipping one does not create other trades (unlike
the holdout review, which replays the day). Human decisions are never runs or trials.
"""
from __future__ import annotations

import hashlib
import re
import threading
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.mystrategy import kind as KD
from edgelab.mystrategy import runner as R
from edgelab.mystrategy.records import Charts, jsonable

SAMPLE_SIZE = 150
MAX_NOTE = 300
REASONS = {
    "no_clear_bias": "No clear bias",
    "choppy": "Choppy / no displacement",
    "key_level_not_clean": "Key level not clean",
    "manipulation_not_clear": "Manipulation not clear",
    "target_far_or_blocked": "Target too far / blocked",
    "stop_not_protected": "Stop not protected",
    "against_htf": "Against the higher timeframe",
    "other": "Other",
}
_ID = re.compile(r"SR_[0-9]{8}_[0-9]{6}_[0-9a-f]{4}")
_LOCK = threading.Lock()


def valid_id(x: str) -> bool:
    return bool(_ID.fullmatch(x or ""))


def reasons_of(K=None) -> dict:
    """Skip reasons of a strategy kind (BP Blake: ``REASONS``)."""
    K = KD.of(K)
    return REASONS if K.id == "my" else K.setup_reasons


def _dir(svc, K=None) -> Path:
    p = R.home(svc, K) / "setup_reviews"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _load(svc, sr_id: str, K=None) -> dict:
    if not valid_id(sr_id):
        raise KeyError(sr_id)
    st = R._read_json(_dir(svc, K) / sr_id / "state.json")
    if st is None:
        raise KeyError(sr_id)
    return st


def _save(svc, st: dict, K=None) -> None:
    R._write_json(_dir(svc, K) / st["id"] / "state.json", st)


def list_reviews(svc, K=None) -> list[dict]:
    out = []
    for f in sorted(_dir(svc, K).glob("SR_*/state.json"), reverse=True):
        st = R._read_json(f)
        if st:
            out.append({k: st.get(k) for k in ("id", "status", "created_at", "finished_at", "base_report", "base_label",
                                               "settings_hash", "exported")}
                       | {"size": len(st["order"]), "decided": len(st["decisions"])})
    return out


def in_progress(svc, K=None) -> dict | None:
    return next((r for r in list_reviews(svc, K) if r["status"] == "in_progress"), None)


def sample_of(report_id: str, trade_nos: list[int], size: int) -> list[int]:
    """Fixed-seed random sample (seed from the report id), returned in time order (trade numbers are in time order)."""
    seed = int(hashlib.sha256(f"setup-review:{report_id}".encode()).hexdigest()[:12], 16)
    pick = np.random.default_rng(seed).permutation(np.asarray(trade_nos, dtype=np.int64))[:size]
    return sorted(int(x) for x in pick)


def start(svc, report_id: str, size: int = SAMPLE_SIZE, K=None) -> dict:
    if in_progress(svc, K):
        raise R.MyStrategyError("SETUP_REVIEW_OPEN", "A setup review is already in progress: finish it first.")
    try:
        folder = R._bt_folder(svc, report_id, K)
    except KeyError:
        raise R.MyStrategyError("NO_REPORT", "That backtest was not found.")
    sm = R._read_json(folder / "summary.json") or {}
    if sm.get("kind") != "discovery_backtest":
        raise R.MyStrategyError("DISCOVERY_ONLY", "The setup review uses discovery-period backtests only (the holdout stays "
                                                  "untouched).")
    trades = R.read_gz(folder / "trades.json.gz", [])
    if not trades:
        raise R.MyStrategyError("NO_TRADES", "That backtest has no trades to review.")
    size = max(1, min(int(size), len(trades)))
    order = sample_of(report_id, [int(t["trade_no"]) for t in trades], size)
    st = {"id": R.new_id("SR"), "created_at": R._now(), "status": "in_progress", "base_report": report_id,
          "base_label": sm.get("label") or "", "settings_hash": sm.get("settings_hash"),
          "settings_changed": sm.get("settings_changed") or {}, "window": sm.get("window"),
          "dataset_content_hash": (sm.get("dataset") or {}).get("content_hash"),
          "sample": {"size": size, "of": len(trades), "seed_from": "sha256('setup-review:' + report id)",
                     "order": "time"},
          "order": order, "decisions": {}, "finished_at": None, "exported": None}
    _save(svc, st, K)
    return st


# ---------------------------------------------------------------------------------------------- charts before entry
def _base(svc, st: dict, lock=None, K=None):
    """(trade docs by number, Charts, ds) of the base report, remembered per report."""
    key = (KD.of(K).id, st["base_report"])
    memo = svc.__dict__.setdefault("_my_setup_review_memo", {})
    with _LOCK:
        hit = memo.get(key)
    if hit is not None:
        return hit
    folder = R._bt_folder(svc, st["base_report"], K)
    sm = R._read_json(folder / "summary.json") or {}
    docs = {int(t["trade_no"]): t for t in R.read_gz(folder / "trades.json.gz", [])}
    _, mine = R.ensure_protocol(svc, lock, create=False, K=K)
    if mine is None:
        raise R.MyStrategyError("NO_PROTOCOL", f"The {KD.of(K).label} protocol is missing.")
    w = R.windows(mine["material"])
    start, end = R._ts(sm["window"]["start"]), R._ts(sm["window"]["end"])
    data_start = max(R._ts(w["discovery"]["start"]), start - pd.Timedelta(days=R.WARMUP_DAYS))
    ds = svc._cell_dataset(R.dataset_1m(svc, mine), (data_start, end), lock)
    if ds.manifest.content_hash != (sm.get("dataset") or {}).get("content_hash"):
        raise R.MyStrategyError("DATA_CHANGED", "The price data differs from the data this backtest used, so its setups "
                                                "cannot be shown. Run the backtest again and review that one.")
    out = (docs, Charts(ds.bars, ds.calendar, sm["settings"]["models.price_series"]), ds)
    with _LOCK:
        memo.clear()                                       # one base report at a time (a full discovery period is large)
        memo[key] = out
    return out


def _candidate(svc, st: dict, lock=None, K=None) -> dict | None:
    nxt = next((n for n in st["order"] if str(n) not in st["decisions"]), None)
    if nxt is None:
        return None
    docs, charts, ds = _base(svc, st, lock, K)
    doc = docs[nxt]
    sb = int(doc["signal_bar"])
    expl = doc.get("explanation") or {}
    sig_ts = pd.Timestamp(int(ds.bars.ts_ns[sb]), tz="UTC")
    if expl.get("signal_ts") and pd.Timestamp(expl["signal_ts"]) != sig_ts:
        raise R.MyStrategyError("DATA_CHANGED", "The price data does not line up with this backtest's trades.")
    return {"trade_no": nxt, "position": st["order"].index(nxt) + 1, "signal_bar": sb, "signal_ts": sig_ts.isoformat(),
            "explanation": expl, "charts": doc.get("charts") or [],
            "candles": charts.for_trade(doc.get("charts") or [], sb, sb, until=sb)}


def _progress(st: dict) -> dict:
    dec = st["decisions"].values()
    return {"size": len(st["order"]), "decided": len(st["decisions"]), "taken": sum(1 for d in dec if d["take"]),
            "skipped": sum(1 for d in dec if not d["take"])}


# ---------------------------------------------------------------------------------------------- results
def _stats(rows: list[dict]) -> dict:
    r = np.array([x["net_r"] for x in rows], dtype=float)
    if not len(r):
        return {"trades": 0, "wins": 0, "win_rate": None, "net_r": 0.0, "avg_r": None}
    return {"trades": int(len(r)), "wins": int((r > 0).sum()), "win_rate": round(float((r > 0).mean()), 4),
            "net_r": round(float(r.sum()), 4), "avg_r": round(float(r.mean()), 4)}


def results(svc, st: dict, K=None) -> dict:
    """Outcomes of the reviewed setups (only once every setup is decided)."""
    docs = {int(t["trade_no"]): t for t in R.read_gz(R._bt_folder(svc, st["base_report"], K) / "trades.json.gz", [])}
    rows = []
    for n in st["order"]:
        t, d = docs[n], st["decisions"][str(n)]
        e = t.get("explanation") or {}
        rows.append({"trade_no": n, "entry_ts": t.get("entry_ts"), "direction": t.get("direction"),
                     "model": e.get("model"), "confirmation_tf": (e.get("confirmation") or {}).get("tf"),
                     "quality": e.get("quality"), "r_planned": (e.get("target") or {}).get("r_planned"),
                     "checklist": e.get("checklist"), "take": d["take"], "reasons": d.get("reasons") or [],
                     "note": d.get("note") or "", "net_r": float(t["net_r"]), "net_usd": t.get("net_usd"),
                     "exit_reason": t.get("exit_reason"), "phase": t.get("phase"), "session": e.get("session")})
    taken = [x for x in rows if x["take"]]
    skipped = [x for x in rows if not x["take"]]
    by_reason = []
    for k, label in reasons_of(K).items():
        sel = [x for x in skipped if k in x["reasons"]]
        if sel:
            by_reason.append({"reason": k, "label": label, **_stats(sel)})
    return jsonable({"all": _stats(rows), "taken": _stats(taken), "skipped": _stats(skipped), "by_reason": by_reason,
                     "rows": rows,
                     "note": "Outcomes are the base backtest's engine results for these trades. Skipping a trade here does "
                             "not create other trades (each setup is judged on its own); your decisions are not a run "
                             "and not a trial."})


# ---------------------------------------------------------------------------------------------- views / decisions
def view(svc, sr_id: str, lock=None, K=None) -> dict:
    st = _load(svc, sr_id, K)
    out = {"review": {k: st.get(k) for k in ("id", "status", "created_at", "finished_at", "base_report", "base_label",
                                             "settings_hash", "settings_changed", "window", "sample", "exported")},
           "progress": _progress(st), "reasons": reasons_of(K)}
    if st["status"] == "in_progress":
        out["candidate"] = _candidate(svc, st, lock, K)
        if out["candidate"] is None:                       # every setup decided
            st["status"], st["finished_at"] = "complete", R._now()
            _save(svc, st, K)
            out["review"]["status"], out["review"]["finished_at"] = st["status"], st["finished_at"]
    if st["status"] == "complete":
        out["results"] = results(svc, st, K)
    return jsonable(out)


def decide(svc, sr_id: str, trade_no: int, take: bool, reasons: list | None = None, note: str = "", K=None) -> dict:
    with _LOCK:
        st = _load(svc, sr_id, K)
        if st["status"] != "in_progress":
            raise R.MyStrategyError("REVIEW_DONE", "This setup review is finished.")
        nxt = next((n for n in st["order"] if str(n) not in st["decisions"]), None)
        if nxt is None or int(trade_no) != nxt:
            raise R.MyStrategyError("NOT_CURRENT", "That setup is not the one waiting for a decision (refresh the page).")
        reasons = [str(x) for x in (reasons or [])]
        bad = [x for x in reasons if x not in reasons_of(K)]
        if bad:
            raise R.MyStrategyError("BAD_REASON", f"Unknown reason: {bad[0]}.")
        if take:
            reasons = []
        elif not reasons:
            raise R.MyStrategyError("REASON_REQUIRED", "Pick at least one reason for skipping.")
        st["decisions"][str(nxt)] = {"take": bool(take), "reasons": list(dict.fromkeys(reasons)),
                                     "note": str(note or "")[:MAX_NOTE], "decided_at": R._now()}
        _save(svc, st, K)
    return {"trade_no": nxt, "take": bool(take), "progress": _progress(st)}


def undo(svc, sr_id: str, K=None) -> dict:
    """Removes the most recent decision (outcomes stay hidden; a finished review cannot be reopened)."""
    with _LOCK:
        st = _load(svc, sr_id, K)
        if st["status"] != "in_progress":
            raise R.MyStrategyError("REVIEW_DONE", "This setup review is finished.")
        if not st["decisions"]:
            raise R.MyStrategyError("NOTHING_TO_UNDO", "No decision to undo.")
        last = max(st["decisions"], key=lambda k: (st["decisions"][k]["decided_at"], st["order"].index(int(k))))
        st["decisions"].pop(last)
        _save(svc, st, K)
    return {"trade_no": int(last), "progress": _progress(st)}


def export_files(svc, sr_id: str, K=None) -> dict[str, str]:
    """Files written into the "Save for Claude" ZIP: the review with its outcomes (finished reviews only)."""
    st = _load(svc, sr_id, K)
    if st["status"] != "complete":
        raise R.MyStrategyError("REVIEW_NOT_FINISHED", "Finish the setup review before saving it for Claude.")
    import json
    return {"setup_review.json": json.dumps({**st, "reasons": reasons_of(K), "results": results(svc, st, K)}, indent=1)}


def mark_exported(svc, sr_id: str, mark: dict, K=None) -> None:
    with _LOCK:
        st = _load(svc, sr_id, K)
        st["exported"] = mark
        _save(svc, st, K)
