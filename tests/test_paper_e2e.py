"""ADR-81/83 browser flow for the "Prop & paper" tab, on SYNTHETIC data only (tests/paper_fixture.py stands in for the
Dukascopy downloader; no network): fees in Settings, batch start with a readable refusal for a non-MNQ strategy, the
accounts table and detail drawer, stop/resume, and the research-source check (match, mismatch pause, continue anyway).

The account start date is pinned to 2024-03-06 so the synthetic archive can play the role of "future" days."""
import functools
import shutil
import tempfile
import threading
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                        # pragma: no cover
    sync_playwright = None

from edgelab.paper import feed
from tests.paper_fixture import fake_fetcher
from tests.test_paper_trading import PROFILE, import_research_dataset, mnq_strategy, shifted_fetcher

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
NOW = datetime(2024, 4, 10, 23, 0, tzinfo=UTC)


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestPaperBrowserFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        from edgelab.web.app import create_app
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.app = create_app(cls.tmp / "demo", demo=True)
        svc = cls.svc = cls.app.config["EDGELAB"]["services"]
        svc.paper.fetcher = fake_fetcher
        import_research_dataset(svc, cls.tmp, date(2024, 1, 2), datetime(2024, 3, 2, 3, tzinfo=UTC),
                                "NQ_DUKASCOPY_BIDASK_OHLC_E2E", derive=["5m"])
        cls.mnq = svc.save_strategy(mnq_strategy("e2e_paper_mnq"))["strategy_id"]
        d5 = next(d["dataset_id"] for d in svc.backtest_readiness(cls.mnq)["datasets"]
                  if d["runnable"] and "DUKASCOPY" in d["dataset_id"])
        svc.backtest_strategy(cls.mnq, d5, record=True)                     # a tested MNQ strategy
        cls.nq = svc.library.list()[0]["strategy_id"]
        if cls.nq == cls.mnq:
            cls.nq = svc.library.list()[1]["strategy_id"]
        dn = next(d["dataset_id"] for d in svc.backtest_readiness(cls.nq)["datasets"] if d["runnable"])
        svc.backtest_strategy(cls.nq, dn, record=True)                      # a tested NQ-sized strategy
        cls.pin = mock.patch.object(feed, "first_unstarted_date", lambda cfg, now=None: date(2024, 3, 6))
        cls.pin.start()
        cls.srv = make_server("127.0.0.1", 0, cls.app, threaded=True)
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:                                             # pragma: no cover
            cls.pw.stop()
            cls.tearDownClass()
            raise unittest.SkipTest(f"chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        for close in (lambda: cls.browser.close(), lambda: cls.pw.stop(), cls.srv.shutdown, cls.pin.stop,
                      cls.svc.store.close):
            try:
                close()
            except Exception:
                pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def page(self):
        ctx = self.browser.new_context(viewport={"width": 1400, "height": 950})
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(ctx.close)
        return pg

    def tid(self, pg, t):
        return pg.locator(f"[data-testid='{t}']")

    def wait_text(self, pg, t, text, timeout=20000):
        """Pages show their previous answer at once while the fresh one loads (ADR-77): wait for the fresh text."""
        pg.wait_for_function("([t, x]) => { const e = document.querySelector(`[data-testid='${t}']`);"
                             " return !!e && e.innerText.toLowerCase().includes(x); }", arg=[t, text.lower()], timeout=timeout)
        return self.tid(pg, t).inner_text()

    def test_fees_batch_start_accounts_and_source_check(self):
        svc, pg = self.svc, self.page()
        # 1. fees in Settings
        pg.goto(self.base + "/#/settings")
        for k, v in (("eval_price", "100"), ("reset_fee", "50"), ("activation_fee", "30")):
            self.tid(pg, f"fee-{PROFILE}-{k}").fill(v)
        self.tid(pg, "fees-save").click()
        pg.wait_for_function("() => document.querySelector(\"[data-testid='fees-save']\").disabled")   # saved, nothing pending
        self.assertEqual(svc.ui_preferences()["prop_fees"][PROFILE],
                         {"eval_price": 100.0, "reset_fee": 50.0, "activation_fee": 30.0})
        # 2. batch start: the NQ-sized strategy is refused in plain words, the MNQ one starts
        pg.goto(self.base + "/#/paper/new")
        self.tid(pg, "paper-profile").select_option(PROFILE)
        self.tid(pg, "paper-view-all").click()
        self.tid(pg, f"paper-pick-{self.nq}").check()
        self.tid(pg, "paper-start").click()
        self.wait_text(pg, "paper-start-error", "not sized in MNQ")
        self.tid(pg, f"paper-pick-{self.nq}").uncheck()
        self.tid(pg, f"paper-pick-{self.mnq}").check()
        self.wait_text(pg, "paper-settings", "2024-03-06")
        self.tid(pg, "paper-start").click()
        self.tid(pg, "paper-page").wait_for()
        aid = svc.paper_accounts()[0]["account_id"]
        # 3. a daily update (feed download, research-source check, recompute)
        out = svc.paper.run_once(now=NOW)
        self.assertEqual((out["source_check"]["verdict"], out["accounts_updated"]), ("match", 1))
        # 4. accounts table, drawer, stop / resume
        pg.reload()                                                          # already on #/paper after the start
        self.wait_text(pg, "paper-source-status", "same prices")
        self.tid(pg, f"paper-row-{aid}").click()
        self.tid(pg, "paper-attempts").locator("tbody tr").first.wait_for()  # the drawer loads its rows asynchronously
        self.assertGreater(self.tid(pg, "paper-attempts").locator("tbody tr").count(), 0)
        self.tid(pg, "paper-stop").click()
        self.tid(pg, "paper-resume").wait_for()
        self.assertEqual(svc.paper_accounts()[0]["status"], "stopped")
        self.tid(pg, "paper-resume").click()
        self.tid(pg, "paper-stop").wait_for()
        self.tid(pg, "drawer-close").click()
        # 5. a different source: the button runs the check, accounts pause, "Continue anyway" resumes them
        svc.paper.fetcher = shifted_fetcher
        with mock.patch.object(svc.paper, "run_once", functools.partial(type(svc.paper).run_once, svc.paper, now=NOW)):
            self.tid(pg, "paper-check").click()
            self.tid(pg, "paper-source-mismatch").wait_for(timeout=60000)
        banner = self.tid(pg, "paper-source-mismatch").inner_text()
        self.assertIn("does not match your research data", banner)
        self.assertIn("paused", banner)
        self.assertIn("0.250", banner)                                       # largest price gap shown
        self.assertTrue(svc.paper_feed_status()["paused"])
        self.tid(pg, "paper-continue").click()
        self.tid(pg, "confirm-ok").click()
        self.wait_text(pg, "paper-source-mismatch", "you chose to continue anyway")
        self.assertFalse(svc.paper_feed_status()["paused"])
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
