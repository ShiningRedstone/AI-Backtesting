"""Background paper updates (ADR-81): download completed days, then recompute every running paper account.

Started by the launchers (desktop app, ``python -m edgelab.web``) like the cache warm-up: once at start and then every
CHECK_INTERVAL_S while the app is open (missed days are caught up). "Update now" runs the same step. Never takes the
service lock: the feed and the paper records are files of their own; research reads are not touched.
"""
from __future__ import annotations

import threading
import time
import traceback
from datetime import date, datetime, timezone

CHECK_INTERVAL_S = 30 * 60


class PaperManager:
    def __init__(self, svc):
        self.svc = svc
        self._mu = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self.thread: threading.Thread | None = None
        self.status: dict = {"state": "idle", "last_run": None, "last_error": None, "last_result": None,
                             "next_check": None}
        self.fetcher = None                      # tests inject a fake downloader; None = dukascopy-python
        self._check_requested = False            # "Check against my research data" (ADR-83)

    # ---------------------------------------------------------------- one update
    def run_once(self, now: datetime | None = None) -> dict:
        from edgelab.paper import engine, feed, store
        with self._mu:
            self.status.update(state="running", started=store.now_iso())
            try:
                cfg, root = self.svc.cfg, self.svc.data_root
                running = [a for a in store.list_accounts(root) if a.get("status") == "running"]
                result = {"accounts_updated": 0, "feed": None, "paused": False}
                forced, self._check_requested = self._check_requested, False
                if forced and not running:
                    result["source_check"] = self.check_source(force=True)
                if running:
                    first = min(date.fromisoformat(a["start_date"]) for a in running)
                    feed_start = feed.shift_trading_days(cfg, first, feed.WARMUP_TRADING_DAYS)
                    result["feed"] = feed.update(cfg, root, feed_start, fetcher=self.fetcher, now=now)
                    newest = result["feed"]["newest_day"]
                    if newest:                                  # first data, a changed research dataset, or the button
                        result["source_check"] = self.check_source(force=forced)
                    result["paused"] = is_paused(feed.read_source_check(root))   # then accounts keep their last state
                    if not result["paused"] and newest and newest >= first.isoformat():
                        fd = feed.build_feed(cfg, root, feed_start)
                        for a in running:
                            if newest < a["start_date"]:
                                continue
                            prof = engine.profile_version(self.svc.root, a["profile_id"], a["profile_version"])
                            st = engine.simulate_account(a, fd, cfg, self.svc.sessions, prof)
                            store.save_state(root, a["account_id"], st)
                            if st.get("stopped"):
                                a.update(status="stopped", stopped_at=store.now_iso(), stop_reason=st["stopped"])
                                store.save_account(root, a)
                            result["accounts_updated"] += 1
                self.status.update(state="idle", last_run=store.now_iso(), last_error=None, last_result=result,
                                   paused=result["paused"])
                return result
            except Exception as exc:                     # shown on the page; retried at the next check
                self.status.update(state="error", last_run=store.now_iso(),
                                   last_error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-2000:])
                return {"error": self.status["last_error"]}

    # ---------------------------------------------------------------- research-source check (ADR-83)
    def check_source(self, force: bool = False) -> dict:
        """Compare downloaded days with the user's Dukascopy research dataset (feed.source_check). Runs automatically
        when no check exists for the current research dataset (or the last one could not download), and on request.
        A new check replaces the old one, including any "Continue anyway"."""
        from edgelab.paper import feed, store
        root = self.svc.data_root
        prev = feed.read_source_check(root)
        man, note = self.svc._paper_research_manifest()
        if man is None:
            rec = {"verdict": "no_dataset", "note": note, "checked_at": store.now_iso()}
            if not prev or prev.get("verdict") != "no_dataset" or force:
                feed.write_source_check(root, rec)
            return rec
        same = prev and prev.get("dataset_content_hash") == man["content_hash"]
        if same and not force and prev.get("verdict") in ("match", "mismatch", "no_overlap"):
            return prev                                     # the dataset is loaded only when a check really runs
        self.status["checking_source"] = True
        try:
            rec = feed.source_check(self.svc.cfg, self.svc._paper_load_research(man["dataset_id"]), fetcher=self.fetcher)
        except Exception as exc:                             # reported and retried; never blocks the account updates
            rec = {"verdict": "error", "dataset_id": man["dataset_id"], "dataset_content_hash": man["content_hash"],
                   "checked_at": store.now_iso(), "note": f"{type(exc).__name__}: {exc}", "days": []}
        finally:
            self.status["checking_source"] = False
        feed.write_source_check(root, rec)
        return rec

    def request_check(self) -> None:
        self._check_requested = True
        self.wake()

    # ---------------------------------------------------------------- background loop
    def start(self) -> None:
        if self.thread is not None:
            return
        self.thread = threading.Thread(target=self._loop, name="munyun-paper", daemon=True)
        self.thread.start()

    def wake(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            nxt = time.time() + CHECK_INTERVAL_S
            self.status["next_check"] = datetime.fromtimestamp(nxt, timezone.utc).isoformat()
            self._wake.wait(CHECK_INTERVAL_S)
            self._wake.clear()


def is_paused(rec: dict | None) -> bool:
    """Paper accounts pause while the latest research-source check found different prices, until a later check
    matches or the user chose "Continue anyway" for that check (ADR-83)."""
    return bool(rec) and rec.get("verdict") == "mismatch" and not rec.get("continue_anyway")
