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

    # ---------------------------------------------------------------- one update
    def run_once(self, now: datetime | None = None) -> dict:
        from edgelab.paper import engine, feed, store
        with self._mu:
            self.status.update(state="running", started=store.now_iso())
            try:
                cfg, root = self.svc.cfg, self.svc.data_root
                running = [a for a in store.list_accounts(root) if a.get("status") == "running"]
                result = {"accounts_updated": 0, "feed": None}
                if running:
                    first = min(date.fromisoformat(a["start_date"]) for a in running)
                    feed_start = feed.shift_trading_days(cfg, first, feed.WARMUP_TRADING_DAYS)
                    result["feed"] = feed.update(cfg, root, feed_start, fetcher=self.fetcher, now=now)
                    newest = result["feed"]["newest_day"]
                    if newest and newest >= first.isoformat():
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
                self.status.update(state="idle", last_run=store.now_iso(), last_error=None, last_result=result)
                return result
            except Exception as exc:                     # shown on the page; retried at the next check
                self.status.update(state="error", last_run=store.now_iso(),
                                   last_error=f"{type(exc).__name__}: {exc}", trace=traceback.format_exc()[-2000:])
                return {"error": self.status["last_error"]}

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
