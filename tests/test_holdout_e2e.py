"""ADR-85 browser flow: Run backtest → Holdout backtest (ranked survivors, selection limited to the tests left, confirm,
live progress, history) and Backtest results → Holdout results (the holdout run with its verdict, the drawer on the
holdout run). SYNTHETIC data only; the survivor label is pinned (synthetic random data rarely passes a prop payout)."""
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                        # pragma: no cover
    sync_playwright = None

from edgelab.research import overview as ov
from tests.test_holdout_backtests import DISC, HOLD, REPO, HoldoutBase  # noqa: F401


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestHoldoutBrowserFlow(HoldoutBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from werkzeug.serving import make_server
        from edgelab.web.app import create_app
        cls.app = create_app(cls.root)
        cls.s = cls.app.config["EDGELAB"]["services"]
        real = ov.apply_criteria
        survivors = {cls.ema, cls.rsi}

        def as_survivor(row, profile):
            out = real(row, profile)
            out["survivor"] = row.get("strategy_id") in survivors and row.get("status") == "IN_SAMPLE"
            return out
        cls.pin = mock.patch.object(ov, "apply_criteria", as_survivor)
        cls.pin.start()
        ov._FACET_CACHE.clear()
        for p in cls.s.store.list_protocols(status="ACTIVE"):
            cls.s.retire_protocol(p["protocol_id"])
        from edgelab.research.campaign import discovery_period
        p = cls.s.create_protocol(cls.src1m, DISC, HOLD, name="e2e", holdout_looks=1)
        per = discovery_period(p["material"])
        cls.s.run_search({"strategies": {"ids": [cls.ema, cls.rsi]}, "datasets": [cls.d5],
                          "period": {"start": per["start"], "end": per["end"]}})
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
                      cls.s.store.close):
            try:
                close()
            except Exception:
                pass
        ov._FACET_CACHE.clear()
        super().tearDownClass()

    def tid(self, pg, t):
        return pg.locator(f"[data-testid='{t}']")

    def wait_text(self, pg, t, text, timeout=30000):
        pg.wait_for_function("([t, x]) => { const e = document.querySelector(`[data-testid='${t}']`);"
                             " return !!e && e.innerText.toLowerCase().includes(x); }", arg=[t, text.lower()], timeout=timeout)
        return self.tid(pg, t).inner_text()

    def test_pick_survivors_run_and_see_holdout_results(self):
        ctx = self.browser.new_context(viewport={"width": 1440, "height": 1000})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        pg.set_default_timeout(30000)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(self.base + "/#/holdout")
        self.wait_text(pg, "holdout-left", "1 of 1")
        self.assertEqual(self.tid(pg, "subnav-holdout").get_attribute("aria-current"), "page")
        self.tid(pg, f"hrow-{self.ema}").wait_for()
        self.tid(pg, f"hrow-{self.rsi}").wait_for()
        self.tid(pg, f"hpick-{self.ema}").check()
        self.tid(pg, f"hpick-{self.rsi}").check()
        self.assertTrue(self.tid(pg, "holdout-start").is_disabled())                 # 2 selected, 1 test left
        self.tid(pg, f"hpick-{self.rsi}").uncheck()
        self.tid(pg, "holdout-start").click()
        self.tid(pg, "confirm-ok").click()
        self.wait_text(pg, "holdout-live", "completed", timeout=600000)
        self.wait_text(pg, "holdout-history", "criteria")
        self.wait_text(pg, "holdout-left", "0 of 1")
        self.assertTrue(self.tid(pg, f"hpick-{self.ema}").is_disabled())              # tested once, forever
        # Backtest results → Holdout results
        pg.goto(self.base + "/#/holdout-results")
        self.tid(pg, f"xrow-{self.ema}").wait_for()
        self.assertEqual(self.tid(pg, "holdout-results").locator("tbody tr").count(), 1)
        self.assertIn("criteria", self.tid(pg, f"verdict-{self.ema}").inner_text().lower())
        self.tid(pg, f"xrow-{self.ema}").click()
        self.tid(pg, "strategy-drawer").wait_for()
        pg.goto(self.base + "/#/explorer")                                            # Strategies stays discovery
        self.tid(pg, "explorer").wait_for()
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
