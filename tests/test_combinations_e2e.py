"""ADR-92 browser flow: Strategies → Combinations (find the best combinations, open one, build your own and save it,
register for holdout tests with the typed confirmation, run the combination holdout test). SYNTHETIC data only; the
survivor labels are pinned (synthetic random data rarely passes a prop payout). Set EDGELAB_SHOTS=<dir> to keep
screenshots (dark and light theme)."""
import os
import threading
import unittest
from pathlib import Path
from unittest import mock

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                        # pragma: no cover
    sync_playwright = None

from edgelab.research import combos as C
from edgelab.research import overview as ov
from tests.test_holdout_backtests import DISC, HOLD, HoldoutBase, fixture


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestCombinationsBrowserFlow(HoldoutBase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from werkzeug.serving import make_server
        from edgelab.web.app import create_app
        cls.app = create_app(cls.root)
        cls.s = cls.app.config["EDGELAB"]["services"]
        extra = [cls.s.save_strategy(fixture(f))["strategy_id"] for f in ("atr_breakout.yaml", "structure_bos.yaml",
                                                                          "opening_range_breakout.yaml")]
        cls.ids = [cls.ema, cls.rsi, *extra]
        real_crit, real_surv = ov.apply_criteria, C.survivor_check
        survivors = set(cls.ids)

        def as_survivor(row, profile):
            out = real_crit(row, profile)
            out["survivor"] = row.get("strategy_id") in survivors and row.get("status") == "IN_SAMPLE"
            return out
        cls.pins = [mock.patch.object(ov, "apply_criteria", as_survivor),
                    mock.patch.object(C, "survivor_check", lambda runner, e: {**real_surv(runner, e), "survivor": True})]
        for p in cls.pins:
            p.start()
        ov._FACET_CACHE.clear()
        cls.s.set_ui_preferences({"research_processes": 1})               # the pins live in this process only
        for p in cls.s.store.list_protocols(status="ACTIVE"):
            cls.s.retire_protocol(p["protocol_id"])
        from edgelab.research.campaign import discovery_period
        p = cls.s.create_protocol(cls.src1m, DISC, HOLD, name="e2e combinations")
        per = discovery_period(p["material"])
        cls.s.run_search({"strategies": {"ids": cls.ids}, "datasets": [cls.d5], "period": {"start": per["start"], "end": per["end"]}})
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
        for close in [lambda: cls.browser.close(), lambda: cls.pw.stop(), cls.srv.shutdown, cls.s.store.close] + [p.stop for p in cls.pins]:
            try:
                close()
            except Exception:
                pass
        ov._FACET_CACHE.clear()
        super().tearDownClass()

    def tid(self, pg, t):
        return pg.locator(f"[data-testid='{t}']")

    def shot(self, pg, name):
        d = os.environ.get("EDGELAB_SHOTS")
        if d:
            Path(d).mkdir(parents=True, exist_ok=True)
            pg.screenshot(path=str(Path(d) / f"{name}.png"), full_page=True)

    def test_find_open_build_register_and_holdout_test(self):
        ctx = self.browser.new_context(viewport={"width": 1500, "height": 1000})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        pg.set_default_timeout(60000)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{self.base}/#/combinations")
        self.tid(pg, "combos-page").wait_for()
        self.assertEqual(self.tid(pg, "subnav-combinations").get_attribute("aria-current"), "page")
        self.tid(pg, "combos-search").click()
        self.tid(pg, "combos-table").wait_for(timeout=180000)
        self.tid(pg, "crow-1").click()
        self.tid(pg, "combo-kpis").wait_for()
        self.tid(pg, "combo-equity-chart").wait_for()
        self.assertIn("Combination", self.tid(pg, "combo-compare").inner_text())
        self.shot(pg, "combinations-dark")

        # build your own, save it
        self.tid(pg, "combos-build").click()
        for sid in self.ids[:2]:
            if not self.tid(pg, f"cpick-{sid}").is_checked():
                self.tid(pg, f"cpick-{sid}").check()
        for sid in self.ids[2:]:
            if self.tid(pg, f"cpick-{sid}").is_checked():
                self.tid(pg, f"cpick-{sid}").uncheck()
        self.tid(pg, "combos-show").click()
        pg.wait_for_function("() => location.hash.includes('c=')")
        self.tid(pg, "combo-name").fill("My pair")
        self.tid(pg, "combo-save").click()
        self.tid(pg, "combos-saved").wait_for()
        self.assertIn("My pair", self.tid(pg, "combos-saved").inner_text())

        # register the best combination (typed confirmation), then its holdout test
        self.tid(pg, "creg-1").check()
        self.tid(pg, "combos-register").click()
        self.tid(pg, "combos-confirm").fill("REGISTER")
        self.tid(pg, "confirm-ok").click()
        self.tid(pg, "combos-registered").wait_for()
        self.tid(pg, "crow-1").click()
        self.tid(pg, "combo-holdout-start").click()
        self.tid(pg, "confirm-ok").click()
        pg.wait_for_function("() => /Criteria (met|not met)/.test(document.querySelector(\"[data-testid='combo-holdout']\")?.innerText || '')",
                             timeout=300000)
        self.assertEqual(self.s.combination_registration()["protocol"]["tests_used"], 1)
        self.shot(pg, "combinations-holdout-dark")
        self.s.set_ui_preferences({"theme": "light"})
        pg.reload()
        self.tid(pg, "combo-kpis").wait_for()
        self.shot(pg, "combinations-light")
        self.s.set_ui_preferences({"theme": "dark"})
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
