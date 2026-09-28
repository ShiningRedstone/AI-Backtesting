"""Phase 3.5 end-to-end: a real server + headless Chromium driving the UI.

Skipped when Playwright or a Chromium build is unavailable. Set PLAYWRIGHT_BROWSERS_PATH if
the browser lives outside Playwright's default location (the test tries /opt/pw-browsers).
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if Path("/opt/pw-browsers").is_dir():
    os.environ.setdefault("PLAYWRIGHT_BROWSERS_PATH", "/opt/pw-browsers")
try:
    from playwright.sync_api import sync_playwright
except ImportError:                                            # pragma: no cover
    sync_playwright = None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _get(url):
    with urllib.request.urlopen(url, timeout=10) as r:
        return json.loads(r.read())


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestBrowserFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp())
        cls.port = _free_port()
        cls.base = f"http://127.0.0.1:{cls.port}"
        cls.proc = subprocess.Popen([sys.executable, "-m", "edgelab.web", "--demo", "--root", str(cls.tmp / "demo"),
                                     "--port", str(cls.port)], cwd=REPO,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(120):
            try:
                _get(cls.base + "/api/health")
                break
            except OSError:
                time.sleep(0.25)
        else:
            cls.tearDownClass()
            raise RuntimeError("web server did not start")
        cls.pw = sync_playwright().start()
        try:
            cls.browser = cls.pw.chromium.launch()
        except Exception as exc:                               # pragma: no cover
            cls.pw.stop()
            cls.proc.terminate()
            raise unittest.SkipTest(f"chromium unavailable: {exc}")

    @classmethod
    def tearDownClass(cls):
        for close in (lambda: cls.browser.close(), lambda: cls.pw.stop()):
            try:
                close()
            except Exception:
                pass
        cls.proc.terminate()
        cls.proc.wait(10)
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def page(self, width=1400, height=900):
        ctx = self.browser.new_context(viewport={"width": width, "height": height})
        pg = ctx.new_page()
        pg.set_default_timeout(15000)
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        pg.on("console", lambda m: m.type == "error" and self.errors.append(m.text))
        self.addCleanup(ctx.close)
        return pg

    def tid(self, pg, t):
        return pg.locator(f"[data-testid='{t}']")

    def wait_valid(self, pg):
        pg.wait_for_function("() => { const s = document.querySelector(\"[data-testid='live-status']\");"
                             " return s && /valid STR_/.test(s.innerText); }")
        return self.tid(pg, "live-status").inner_text().split()[1]

    def build_strategy(self, pg) -> str:
        pg.goto(self.base + "/#/builder?new=1")
        self.tid(pg, "f-name").fill("e2e_ema_trend")
        self.tid(pg, "f-family-id").fill("e2e_trend")
        self.tid(pg, "f-family-name").fill("E2E trend")
        self.tid(pg, "f-hypothesis").fill("Closes above a rising EMA tend to continue (test fixture).")
        self.tid(pg, "tab-parameters").click()
        for name, ptype in (("ema_period", "integer"), ("stop_pts", "float"), ("use_filter", "boolean"),
                            ("regime", "choice"), ("htf", "timeframe")):
            self.tid(pg, "new-param-name").fill(name)
            self.tid(pg, "new-param-type").select_option(ptype)
            self.tid(pg, "add-param").click()
            self.tid(pg, f"param-{name}").wait_for()
        self.tid(pg, "param-stop_pts-value").fill("2")
        self.tid(pg, "tab-entry").click()
        # ALL[ close > ema($ema_period),  ANY[ NOT( close > ema(20)@60m ) ] when $use_filter ]
        self.tid(pg, "cond-long-0-right-kind").select_option("feature")
        self.tid(pg, "cond-long-0-right-p-period-mode").select_option("param")
        self.assertEqual(self.tid(pg, "cond-long-0-right-p-period").input_value(), "ema_period")
        self.tid(pg, "cond-long-addgroup").click()
        self.assertEqual(self.tid(pg, "cond-long-1-kind").input_value(), "any")
        self.tid(pg, "cond-long-1-0-right-kind").select_option("feature")
        self.tid(pg, "cond-long-1-0-right-tf").select_option("60m")
        self.tid(pg, "cond-long-1-0-not").click()
        self.tid(pg, "cond-long-1-0-n").wait_for()
        self.tid(pg, "cond-long-1-enabled").select_option("$use_filter")
        self.tid(pg, "tab-exit").click()
        self.tid(pg, "stop-points-mode").select_option("param")
        self.tid(pg, "stop-points").select_option("stop_pts")
        self.tid(pg, "target-type").select_option("risk_reward")
        return self.wait_valid(pg)

    # ------------------------------------------------------------------ tests
    def test_1_create_validate_save_reload(self):
        pg = self.page()
        live_id = self.build_strategy(pg)
        text = self.tid(pg, "dsl-text").inner_text()
        for fragment in ("period: $ema_period", "timeframe: 60m", "not:", "any:", "enabled: $use_filter", "points: $stop_pts"):
            self.assertIn(fragment, text)                     # backend-rendered DSL of the visual draft
        self.tid(pg, "tab-review").click()
        self.tid(pg, "validate").click()
        res = self.tid(pg, "validation-result")
        res.wait_for()
        self.assertIn("Strategy valid", res.inner_text())
        self.assertIn("never used", self.tid(pg, "validation-issues").inner_text())    # $regime warning
        self.tid(pg, "explain").click()
        self.assertIn("ema(period=10", self.tid(pg, "explain-result").inner_text())
        self.tid(pg, "save").click()
        saved = self.tid(pg, "save-result")
        saved.wait_for()
        self.assertIn(live_id, saved.inner_text())
        stored = _get(f"{self.base}/api/strategies/{live_id}")
        self.assertEqual(stored["family_id"], "e2e_trend")
        self.assertEqual(set(stored["definition"]["parameters"]),
                         {"ema_period", "stop_pts", "use_filter", "regime", "htf"})
        pg.goto(f"{self.base}/#/builder/{live_id}")               # reload: same canonical identity
        self.assertEqual(self.wait_valid(pg), live_id)
        self.__class__.saved_id = live_id
        self.assertEqual(self.errors, [])

    def test_2_invalid_strategy_shows_backend_errors(self):
        pg = self.page()
        self.build_strategy(pg)
        self.tid(pg, "tab-exit").click()
        self.tid(pg, "stop-points-mode").select_option("fixed")
        self.tid(pg, "stop-points").fill("-5")
        pg.wait_for_function("() => /validation issue/.test(document.querySelector(\"[data-testid='live-status']\").innerText)")
        self.tid(pg, "tab-review").click()
        self.tid(pg, "validate").click()
        issues = self.tid(pg, "validation-issues").inner_text().lower()      # paths are shown upper-case
        self.assertIn("exit.stop.points", issues)
        self.assertIn("points must be > 0", issues)
        self.tid(pg, "save").click()
        self.assertIn("not saved", self.tid(pg, "save-error").inner_text())

    def test_3_variations_and_lineage(self):
        sid = getattr(self, "saved_id", None) or self.build_and_save()
        pg = self.page()
        pg.goto(f"{self.base}/#/strategies/{sid}?tab=variations")
        self.tid(pg, "dim-ema_period-explore").check()
        self.tid(pg, "dim-ema_period-min").fill("10")
        self.tid(pg, "dim-ema_period-max").fill("20")
        self.tid(pg, "dim-ema_period-step").fill("5")
        self.tid(pg, "dim-use_filter-explore").check()
        pg.wait_for_function("() => document.querySelector(\"[data-testid='var-count']\")?.innerText === '6'")
        self.tid(pg, "generate").click()
        self.tid(pg, "variation-results").wait_for()
        self.assertEqual(self.tid(pg, "stat-combinations").inner_text(), "6")
        self.assertEqual(self.tid(pg, "stat-unique").inner_text(), "5")
        self.assertEqual(self.tid(pg, "stat-same").inner_text(), "1")
        child = self.tid(pg, "variants-table").locator("tbody tr").first.locator("code").first.inner_text()
        pg.goto(f"{self.base}/#/strategies/{child}?tab=lineage")
        self.tid(pg, "lineage-view").wait_for()
        self.tid(pg, f"tree-{sid}").wait_for()                     # parent from lineage records
        self.assertIn(sid, self.tid(pg, "lineage-table").inner_text())
        lin = _get(f"{self.base}/api/strategies/{child}/lineage")
        self.assertEqual(lin["records"][0]["parent_strategy_id"], sid)
        self.assertEqual(lin["records"][0]["generation_method"], "mode_a_variation")
        # the cap is enforced by the backend: a tighter max is refused, never truncated
        pg.goto(f"{self.base}/#/strategies/{sid}?tab=variations")
        self.tid(pg, "dim-ema_period-explore").check()
        self.tid(pg, "var-max").fill("2")
        pg.wait_for_function("() => /exceed max_variants/.test(document.querySelector(\"[data-testid='variation-preview']\").innerText)")
        self.assertTrue(self.tid(pg, "generate").is_disabled())

    def build_and_save(self) -> str:
        pg = self.page()
        sid = self.build_strategy(pg)
        self.tid(pg, "save").click()
        self.tid(pg, "save-result").wait_for()
        return sid

    def test_4_synthetic_backtest_and_cfd_refusal(self):
        sid = _get(self.base + "/api/strategies")[0]["strategy_id"]
        pg = self.page()
        pg.goto(f"{self.base}/#/strategies/{sid}?tab=backtest")
        self.tid(pg, "dataset-select").wait_for()
        cfd_row = pg.locator("tr[data-testid^='ds-NAS100_CFD']")
        self.assertTrue(cfd_row.locator("input[type=radio]").is_disabled())
        self.assertIn("cost profile is unconfigured", cfd_row.inner_text())
        self.assertIn("CFD backtest unavailable", self.tid(pg, "cfd-unavailable").inner_text())
        pg.locator("tr[data-testid^='ds-NQ_FUTURE'] input[type=radio]").check()
        self.tid(pg, "run-backtest").click()
        result = self.tid(pg, "backtest-result")
        result.wait_for(timeout=60000)
        self.assertIn("not evidence of trading performance", self.tid(pg, "synthetic-banner").inner_text())
        self.assertIn("trade_count", self.tid(pg, "metrics").inner_text())
        run_id = result.locator("a[href^='#/results/']").inner_text()
        pg.goto(f"{self.base}/#/results")
        self.assertIn(run_id, self.tid(pg, "runs-demo").inner_text())     # kept apart from research runs
        self.assertEqual(self.errors, [])

    def test_5_mobile_navigation(self):
        pg = self.page(390, 844)
        pg.goto(self.base + "/#/")
        self.assertFalse(self.tid(pg, "nav-datasets").is_visible())
        self.tid(pg, "menu-toggle").click()
        self.tid(pg, "nav-datasets").click()
        pg.wait_for_function("() => document.querySelector('h1')?.innerText === 'Datasets'")
        self.tid(pg, "nav-datasets").wait_for(state="hidden", timeout=3000)   # drawer closes after navigating
        width = pg.evaluate("document.documentElement.scrollWidth")
        self.assertLessEqual(width, 390 + 1)                                # no sideways page scroll
        self.tid(pg, "demo-banner").wait_for()


if __name__ == "__main__":
    unittest.main()
