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


def _post(url, body):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
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

    def test_10_prop_simulation_and_dataset_eligibility(self):
        """Phase 6: choose a stored run + two accounts with different rule sets, run, inspect."""
        sid = _get(self.base + "/api/strategies")[0]["strategy_id"]
        fut = next(d for d in _get(self.base + "/api/datasets") if d["asset_type"] == "FUTURE")
        run_id = _post(self.base + "/api/backtests", {"strategy": sid, "dataset_id": fut["dataset_id"]})["run_id"]
        before = _get(f"{self.base}/api/results/{run_id}")
        pg = self.page()
        pg.goto(self.base + "/#/prop")
        self.tid(pg, "prop-run").select_option(run_id)
        self.tid(pg, "prop-config-0").select_option("SYNTH_STATIC_EVAL")
        self.tid(pg, "prop-add-account").click()
        self.tid(pg, "prop-config-1").select_option("SYNTH_TRAILING_EVAL")
        self.tid(pg, "prop-run-btn").click()
        self.tid(pg, "prop-result").wait_for(timeout=60000)
        self.assertIn(run_id, self.tid(pg, "prop-strategy-result").inner_text())
        table = self.tid(pg, "prop-account-results").inner_text()
        self.assertIn("SYNTH_STATIC_EVAL", table)
        self.assertIn("SYNTH_TRAILING_EVAL", table)
        self.assertIn("not evidence that the strategy is profitable", self.tid(pg, "prop-result").inner_text())
        self.tid(pg, "prop-detail-A1").click()
        self.tid(pg, "prop-progression-A1").wait_for()
        self.assertEqual(_get(f"{self.base}/api/results/{run_id}"), before)          # source run untouched
        self.tid(pg, "prop-sims").wait_for()
        self.assertEqual(self.errors, [])
        pg.goto(self.base + "/#/datasets")
        cfd = pg.locator("[data-testid^='eligible-NAS100_CFD']").first
        self.assertIn("not eligible", cfd.inner_text())
        self.assertIn("eligible", pg.locator("[data-testid^='eligible-NQ_FUTURE']").first.inner_text())

    def _ema_id(self):
        return next(r["strategy_id"] for r in _get(self.base + "/api/strategies") if r["name"] == "ema_crossover")

    def test_11_strategy_lab_full_workflow(self):
        """Phase 8: Lab -> Duplicate ema_crossover -> fast 9->8, slow 21->20 (+ slow step 1: 20 is off the
        declared 15+3k grid) -> validate -> save new version -> choose dataset -> backtest -> result."""
        ema = self._ema_id()
        parent = _get(f"{self.base}/api/strategies/{ema}")
        pg = self.page()
        pg.goto(f"{self.base}/#/strategies/{ema}")
        self.tid(pg, "lab-hub").wait_for()
        self.tid(pg, "lab-provenance").wait_for()
        self.tid(pg, "lab-duplicate").click()
        self.tid(pg, "tab-parameters").click()
        self.tid(pg, "param-fast-value").fill("8")
        self.tid(pg, "param-slow-value").fill("20")
        pg.wait_for_function("() => /validation issue/.test(document.querySelector(\"[data-testid='live-status']\").innerText)")
        self.tid(pg, "param-slow-step").fill("1")
        new_id = self.wait_valid(pg)
        self.assertNotEqual(new_id, ema)
        self.tid(pg, "save").click()
        self.tid(pg, "save-result").wait_for()
        self.assertIn(new_id, self.tid(pg, "save-result").inner_text())
        rec = _get(f"{self.base}/api/strategies/{new_id}/research")
        self.assertEqual((rec["lineage"]["parent_strategy_id"], rec["lineage"]["generation_method"]), (ema, "duplicate"))
        self.assertEqual((rec["strategy"]["parameters"]["fast"], rec["strategy"]["parameters"]["slow"]), (8, 20))
        self.assertEqual(_get(f"{self.base}/api/strategies/{ema}")["definition_hash"], parent["definition_hash"])
        self.tid(pg, "goto-backtest").click()
        self.tid(pg, "dataset-select").wait_for()
        pg.locator("tr[data-testid^='ds-NQ_FUTURE'] input[type=radio]").check()
        self.tid(pg, "run-backtest").click()
        res = self.tid(pg, "backtest-result")
        res.wait_for(timeout=60000)
        run_id = res.locator("a[href^='#/results/']").inner_text()
        self.assertEqual(_get(f"{self.base}/api/results/{run_id}")["record"]["strategy"]["strategy_id"], new_id)
        res.locator("a[href^='#/results/']").click()
        self.tid(pg, "equity-chart").wait_for()
        self.tid(pg, "run-analytics").wait_for()
        self.assertIn("In-sample", pg.locator(".subtitle").first.inner_text())
        pg.goto(f"{self.base}/#/strategies/{new_id}?tab=research")
        self.tid(pg, "lab-runs").wait_for()
        self.assertIn(run_id, self.tid(pg, "lab-runs").inner_text())
        self.assertIn(ema, self.tid(pg, "lab-provenance").inner_text())
        self.assertEqual(self.errors, [])

    def test_12_variations_batch_compare_validate_prop(self):
        """Phase 8: bounded grid (exact combinations shown) -> batch job on a dataset -> comparison
        (sortable, scoped) -> OOS + OOS-window control for one selection -> prop on the OOS run."""
        ema = self._ema_id()
        pg = self.page()
        pg.goto(f"{self.base}/#/strategies/{ema}?tab=variations")
        self.tid(pg, "dim-fast-explore").click()
        self.tid(pg, "dim-fast-min").fill("8")
        self.tid(pg, "dim-fast-max").fill("10")
        pg.wait_for_function("() => { const c = document.querySelector(\"[data-testid='var-count']\"); return c && c.innerText.trim() === '3'; }")
        combos = self.tid(pg, "var-combos")
        combos.wait_for()
        self.assertEqual(combos.locator("tbody tr").count(), 3)
        self.tid(pg, "generate").click()
        self.tid(pg, "var-run-batch").click()
        self.tid(pg, "lab-batch").wait_for()
        self.assertTrue(self.tid(pg, "lab-batch-select").input_value().startswith("VB_"))
        cfd = pg.locator("[data-testid^='lab-batch-ds-NAS100_CFD'] input").first
        self.assertTrue(cfd.is_disabled())                                      # ineligible: cannot be selected
        pg.locator("[data-testid^='lab-batch-ds-NQ_FUTURE'] input").first.check()
        self.tid(pg, "lab-batch-start").click()
        pg.wait_for_function("() => { const s = document.querySelector(\"[data-testid='rs-job-state']\"); return s && /completed/.test(s.innerText); }",
                             timeout=180000)
        self.tid(pg, "rs-open-compare").click()
        table = self.tid(pg, "compare-table")
        table.wait_for()
        rows = table.locator("tbody tr")
        self.assertGreaterEqual(rows.count(), 3)                                # base + 2 unique variants
        self.assertIn("In-sample", table.inner_text())
        self.tid(pg, "cmp-sort-net_r").click()
        vals = [float(x) for x in table.locator("tbody tr td:nth-child(9)").all_inner_texts() if x.strip() not in ("", "—")]
        self.tid(pg, "cmp-sort-run_id").click()
        rows.first.locator("input[type=checkbox]").check()
        self.tid(pg, "cmp-validate").click()
        self.tid(pg, "lab-validate").wait_for()
        self.tid(pg, "lab-val-split").fill("2024-03-01")
        pg.locator("[data-testid^='lab-val-ds-NQ_FUTURE'] input").first.check()
        self.tid(pg, "lab-val-run").click()
        result = self.tid(pg, "lab-val-result")
        result.wait_for(timeout=120000)
        self.assertIn("Out-of-sample", result.inner_text())
        oos_run = result.locator("tbody tr").nth(1).locator("a[href^='#/results/']").inner_text()
        self.assertEqual(_get(f"{self.base}/api/results/{oos_run}")["record"]["status"], "OUT_OF_SAMPLE")
        self.tid(pg, "lab-val-control").click()
        self.tid(pg, "lab-val-n").fill("3")
        self.tid(pg, "lab-val-run").click()
        ctl = self.tid(pg, "lab-ctl-result")
        ctl.wait_for(timeout=120000)
        self.assertIn("Out-of-sample", ctl.inner_text())
        self.assertIn("never runs", ctl.inner_text())
        pg.goto(f"{self.base}/#/prop?run={oos_run}")
        self.assertEqual(self.tid(pg, "prop-run").input_value(), oos_run)
        self.tid(pg, "prop-config-0").select_option("SYNTH_STATIC_EVAL")
        self.tid(pg, "prop-run-btn").click()
        self.tid(pg, "prop-result").wait_for(timeout=60000)
        self.assertEqual(self.errors, [])
        self.assertTrue(vals == sorted(vals) or not vals)

    def test_9z_preferred_dataset_ai_discovery_to_backtest(self):
        """Phase 9: set the Preferred Research Dataset -> AI Discovery preselects it -> mock proposals through the
        gate -> accept + save -> open in the Strategy Lab (preferred dataset preselected) -> backtest."""
        rows = _get(self.base + "/api/datasets")
        did = next(d["dataset_id"] for d in rows if d["runnable"] and d["provider"] != "SYNTHETIC_CFD")
        pg = self.page()
        try:
            pg.goto(f"{self.base}/#/datasets")
            self.tid(pg, f"prefer-{did}").click()
            self.tid(pg, "preferred-strip").wait_for()
            self.assertIn(did, self.tid(pg, "preferred-strip").inner_text())
            self.assertEqual(_get(self.base + "/api/preferences/research-dataset")["preferred"]["dataset_id"], did)
            pg.goto(f"{self.base}/#/discovery")
            self.tid(pg, "ai-offline").wait_for()                              # no external AI in tests: says so
            pg.wait_for_function(f"() => document.querySelector(\"[data-testid='ai-dataset']\")?.value === '{did}'")
            self.assertIn(did, self.tid(pg, "research-dataset-strip").inner_text())
            self.tid(pg, "ai-mode-template").click()
            self.tid(pg, "ai-template").select_option("rsi_reversion")
            self.tid(pg, "ai-n").fill("2")
            self.tid(pg, "ai-generate").click()
            card = self.tid(pg, "proposal-0")
            card.wait_for(timeout=30000)
            self.assertIn("passed the gate", card.inner_text().lower())
            self.assertIn("rejected by the gate", self.tid(pg, "proposal-1").inner_text().lower())
            self.assertIn("contains code", self.tid(pg, "reasons-1").inner_text())
            self.tid(pg, "inspect-0").click()
            self.tid(pg, "inspect-body-0").wait_for()
            self.tid(pg, "accept-0").click()
            self.tid(pg, "save-0").click()
            self.tid(pg, "backtest-0").click()
            self.tid(pg, "dataset-select").wait_for()
            radio = pg.locator(f"tr[data-testid='ds-{did}'] input[type=radio]")
            pg.wait_for_function(f"() => document.querySelector(\"tr[data-testid='ds-{did}'] input[type=radio]\")?.checked === true")
            self.assertTrue(radio.is_checked())                                  # preferred dataset preselected
            self.tid(pg, "run-backtest").click()
            res = self.tid(pg, "backtest-result")
            res.wait_for(timeout=60000)
            run_id = res.locator("a[href^='#/results/']").inner_text()
            rec = _get(f"{self.base}/api/results/{run_id}")["record"]
            gens = _get(self.base + "/api/ai/generations")
            gen = _get(f"{self.base}/api/ai/generations/{gens[0]['generation_id']}")
            saved = gen["proposals"][0]["decision"]["saved_strategy_id"]
            self.assertEqual(rec["strategy"]["strategy_id"], saved)
            self.assertEqual(rec["dataset"]["dataset_id"], did)
            lin = _get(f"{self.base}/api/ai/proposals/{gen['proposals'][0]['proposal_id']}/lineage")
            self.assertIn(run_id, [r["run_id"] for r in lin["runs"]])
            self.assertEqual(self.errors, [])
        finally:
            _post(self.base + "/api/preferences/research-dataset", {"dataset_id": None})

    def test_5_mobile_navigation(self):
        pg = self.page(390, 844)
        pg.goto(self.base + "/#/")
        self.assertFalse(self.tid(pg, "nav-settings").is_visible())
        self.tid(pg, "menu-toggle").click()
        self.tid(pg, "nav-settings").click()                                # Data is a view of the Settings tab
        self.tid(pg, "nav-settings").wait_for(state="hidden", timeout=3000)   # drawer closes after navigating
        self.tid(pg, "subnav-datasets").click()
        pg.wait_for_function("() => document.querySelector('h1')?.innerText === 'Datasets'")
        width = pg.evaluate("document.documentElement.scrollWidth")
        self.assertLessEqual(width, 390 + 1)                                # no sideways page scroll
        self.tid(pg, "demo-banner").wait_for()

    # ------------------------------------------------------------------ Phase 4 research page
    def _research_ids(self):
        strategies = {s["name"]: s["strategy_id"] for s in _get(self.base + "/api/strategies")}
        datasets = {d["instrument"]: d["dataset_id"] for d in _get(self.base + "/api/datasets")}
        return strategies, datasets

    def _setup_search(self, pg, names=("ema_crossover", "rsi_threshold"), instruments=("NQ", "NAS100_CFD")):
        strategies, datasets = self._research_ids()
        pg.goto(self.base + "/#/research")
        self.tid(pg, "search-setup").wait_for()
        for n in names:
            self.tid(pg, f"rs-ids-{strategies[n]}").check()
        for i in instruments:
            self.tid(pg, f"rs-datasets-{datasets[i]}").check()
        return strategies, datasets

    def _unexpected_errors(self):
        """Page errors, ignoring the browser's own log line for an HTTP error the test asked for."""
        return [e for e in self.errors if "Failed to load resource" not in e]

    def test_6_research_setup_check_plan_and_refusals(self):
        pg = self.page()
        self._setup_search(pg)
        self.assertNotIn("planned", self.tid(pg, "nav-run").inner_text())         # Experiments live in Run backtest
        self.assertIn("active", self.tid(pg, "subnav-research").get_attribute("class"))
        self.assertIn("NOT VALIDATED", self.tid(pg, "research-in-sample").inner_text())
        self.tid(pg, "rs-validate").click()
        self.assertIn("well formed", self.tid(pg, "rs-validation").inner_text())
        self.tid(pg, "rs-period").select_option("explicit")                  # naive time: refused, never guessed
        self.tid(pg, "rs-start").fill("2024-01-02")
        self.tid(pg, "rs-end").fill("2024-02-01T00:00:00Z")
        self.tid(pg, "rs-validate").click()
        pg.wait_for_function("() => /problem/.test(document.querySelector(\"[data-testid='rs-validation']\")?.innerText)")
        self.assertIn("never guessed", self.tid(pg, "rs-validation-issues").inner_text())
        self.tid(pg, "rs-period").select_option("none")
        self.tid(pg, "rs-max-cells").fill("1")                              # 2 eligible cells > 1: refused (422)
        self.tid(pg, "rs-plan").click()
        err = self.tid(pg, "rs-error")
        err.wait_for()
        self.assertIn("never truncated", err.inner_text())
        self.tid(pg, "rs-max-cells").fill("100")
        self.tid(pg, "rs-plan").click()
        plan = self.tid(pg, "rs-plan-result")
        plan.wait_for()
        self.assertIn("4 planned · 2 eligible · 2 ineligible", plan.inner_text())
        self.assertIn("cost profile is unconfigured", self.tid(pg, "rs-plan-cells").inner_text())
        self.assertEqual(self.tid(pg, "rs-error").count(), 0)
        self.assertEqual(self._unexpected_errors(), [])

    def test_7_research_job_results_ranking_and_shortlist(self):
        pg = self.page()
        strategies, _ = self._setup_search(pg)
        self.tid(pg, "rs-seed").fill("7")                                    # a search of its own
        self.tid(pg, "rs-start-job").click()
        pg.wait_for_url("**/#/research?job=JOB_*")
        pg.wait_for_function("() => document.querySelector(\"[data-testid='rs-job-state']\")?.innerText === 'completed'",
                             timeout=120000)                                  # reached through polling
        self.assertIn("Trials", self.tid(pg, "rs-progress").inner_text())
        sid = self.tid(pg, "rs-job-search").inner_text().strip()
        self.assertIn(sid, self.tid(pg, "rs-searches").inner_text())         # the list refreshed
        self.tid(pg, "rs-open-results").click()
        self.tid(pg, "rs-search-page").wait_for()
        self.assertIn("NOT VALIDATED", self.tid(pg, "rs-search-in-sample").inner_text())
        statuses = pg.locator("[data-testid='rs-cells'] tbody tr").evaluate_all("rs => rs.map(r => r.dataset.status)")
        self.assertEqual(sorted(statuses), ["completed", "completed", "ineligible", "ineligible"])
        self.assertIn("None.", self.tid(pg, "rs-historical").inner_text())
        self.assertEqual(self.tid(pg, "rs-trials").inner_text(), "2")
        self.tid(pg, "rk-min-sample").select_option("LOW SAMPLE SIZE")
        pg.wait_for_function("() => /LOW SAMPLE SIZE/.test(document.querySelector(\"[data-testid='rk-meta']\")?.innerText)")
        self.assertIn("NOT VALIDATED", self.tid(pg, "rk-label").inner_text())
        self.assertIn("2 trial(s)", self.tid(pg, "rk-meta").inner_text())
        ema = strategies["ema_crossover"]
        self.tid(pg, f"sl-{ema}").check()
        self.tid(pg, "sl-save").click()
        pg.wait_for_function(f"() => document.querySelector(\"[data-testid='sl-current']\")?.innerText.includes('{ema}')")
        self.assertIn("implies no validation", self.tid(pg, "rs-shortlist").inner_text())
        detail = _get(f"{self.base}/api/research/searches/{sid}")
        self.assertEqual(detail["shortlist"]["strategy_ids"], [ema])
        runs = [c["run_id"] for c in detail["cells"] if c["run_id"]]
        self.assertTrue(all(r["status"] == "IN_SAMPLE" for r in _get(self.base + "/api/results") if r["run_id"] in runs))
        self.assertEqual(self._unexpected_errors(), [])

    def test_8_research_polling_and_cancel(self):
        """The job endpoints are intercepted so the running state lasts exactly as long as the test needs."""
        pg = self.page()
        job_id, sid = "JOB_AAAAAAAAAAAA", "SRCH_AAAAAAAAAAAA"
        state = {"polls": 0, "cancel": False}

        def job(st, cancel_requested=False, evaluated=1):
            return {"job_id": job_id, "search_id": sid, "state": st, "history": ["queued", st], "created_at": "2024-01-01T00:00:00",
                    "started_at": "2024-01-01T00:00:01", "finished_at": None if st == "running" else "2024-01-01T00:00:09",
                    "error": None, "cancel_requested": cancel_requested,
                    "progress": {"stored": True, "batch_status": st, "planned": 4, "eligible": 3, "ineligible": 1,
                                 "evaluated": evaluated, "failed": 0, "skipped_resume": 0, "cancelled": 2 if st == "cancelled" else 0,
                                 "trials": evaluated, "pending": 0 if st == "cancelled" else 3 - evaluated,
                                 "cell_status": {}, "fraction_done": evaluated / 3}}

        def start(route):
            if route.request.method != "POST":
                return route.fallback()
            route.fulfill(status=202, content_type="application/json", body=json.dumps({**job("queued", evaluated=0)}))

        def status(route):
            state["polls"] += 1
            body = job("cancelled") if state["cancel"] else job("running")
            route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

        def cancel(route):
            state["cancel"] = True
            route.fulfill(status=200, content_type="application/json", body=json.dumps(job("running", cancel_requested=True)))
        pg.route("**/api/research/jobs", start)
        pg.route(f"**/api/research/jobs/{job_id}", status)
        pg.route(f"**/api/research/jobs/{job_id}/cancel", cancel)
        self._setup_search(pg, names=("ema_crossover",), instruments=("NQ",))
        self.tid(pg, "rs-start-job").click()
        pg.wait_for_url(f"**/#/research?job={job_id}")
        pg.wait_for_function("() => document.querySelector(\"[data-testid='rs-job-state']\")?.innerText === 'running'")
        self.assertIn("Evaluated", self.tid(pg, "rs-progress").inner_text())
        deadline = time.monotonic() + 20
        while state["polls"] < 2:                                             # polling continues while running
            self.assertLess(time.monotonic(), deadline, "the page did not poll the running job")
            pg.wait_for_timeout(250)
        self.tid(pg, "rs-cancel").click()
        pg.wait_for_function("() => document.querySelector(\"[data-testid='rs-job-state']\")?.innerText === 'cancelled'")
        self.assertEqual(self.tid(pg, "rs-cancel").count(), 0)                # no cancel once final
        self.tid(pg, "rs-open-results").wait_for()
        polls = state["polls"]
        pg.wait_for_timeout(4500)                                             # > 2 poll intervals
        self.assertEqual(state["polls"], polls)                               # polling stopped at the final state
        self.assertEqual(self._unexpected_errors(), [])

    def test_9_research_error_display_and_historical_cells(self):
        pg = self.page()
        conflict = {"error": {"kind": "job_conflict", "message": "Another search job is still active; one runs at a time.",
                              "reason": "search job JOB_BBBBBBBBBBBB (SRCH_BBBBBBBBBBBB) is still running"}}
        pg.route("**/api/research/jobs", lambda r: r.fulfill(status=409, content_type="application/json", body=json.dumps(conflict))
                 if r.request.method == "POST" else r.fallback())
        self._setup_search(pg, names=("ema_crossover",), instruments=("NQ",))
        self.tid(pg, "rs-start-job").click()
        err = self.tid(pg, "rs-error")
        err.wait_for()
        self.assertIn("one runs at a time", err.inner_text())
        self.assertIn("still running", err.inner_text())
        pg.goto(self.base + "/#/research/SRCH_000000000000")                  # unknown search: 404 from the backend
        self.assertIn("Could not load search", self.tid(pg, "rs-search-error").inner_text())
        pg.goto(self.base + "/#/research?job=JOB_000000000000")               # unknown job: 404, polling stops
        self.assertIn("not known to the running server", self.tid(pg, "rs-job-error").inner_text())

        sid = "SRCH_CCCCCCCCCCCC"                                            # current vs historical cells
        cell = lambda cid, st, cur, rid=None: {"search_id": sid, "cell_id": cid, "plan_index": int(cid[-1]),  # noqa: E731
                                              "strategy_id": f"STR_{cid[-1] * 12}", "dataset_id": "DS_X", "dataset_content_hash": "h",
                                              "status": st, "run_id": rid, "trades_hash": "t" if rid else None,
                                              "reasons": ["no cost profile"] if st == "ineligible" else [], "error": None,
                                              "headline": {"trade_count": 150, "sample_label": "MODERATE SAMPLE", "expectancy_r": 0.1}
                                              if rid else None, "current": cur}
        detail = {"search_id": sid, "search_hash": "a" * 64, "config_hash": "b" * 64, "created_at": "2024-01-01T00:00:00",
                  "finished_at": "2024-01-01T00:01:00", "status": "completed", "spec": {"strategies": {}, "datasets": ["DS_X"]},
                  "shortlist": None, "warnings": [], "n_planned": 2, "n_eligible": 1, "n_ineligible": 1, "n_evaluated": 0,
                  "n_skipped_resume": 1, "n_failed": 0, "n_cancelled": 0, "n_trials": 0,
                  "cells": [cell("CELL_1", "completed", True, "RUN_2024_00001"), cell("CELL_2", "ineligible", True)],
                  "historical_cells": [cell("CELL_3", "completed", False, "RUN_2024_00002")],
                  "cumulative": {"completed": 1, "failed": 0, "pending": 0, "ineligible": 1, "cancelled": 0, "trials": 1},
                  "note": "in-sample research results under the stated assumptions; nothing is validated"}
        ranking = {"search_id": sid, "search_status": "completed", "metric": "expectancy_r", "direction": "descending",
                   "min_sample_label": "MODERATE SAMPLE", "n_trials": 1, "n_current_cells": 2, "n_ranked": 1,
                   "excluded": {"historical": 1, "ineligible": 1}, "in_sample": True, "status": "IN_SAMPLE", "validated": False,
                   "ranked": [{"rank": 1, "strategy_id": "STR_111111111111", "dataset_id": "DS_X", "cell_id": "CELL_1",
                               "run_id": "RUN_2024_00001", "trades_hash": "t", "value": 0.1, "value_infinite": False,
                               "metrics": {"trade_count": 150, "sample_label": "MODERATE SAMPLE", "expectancy_r": 0.1}}],
                   "label": "IN-SAMPLE ranking of 1 result(s) drawn from 1 trial(s) - NOT VALIDATED", "note": "note"}
        pg.route(f"**/api/research/searches/{sid}", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                          body=json.dumps(detail)))
        pg.route(f"**/api/research/searches/{sid}/ranking*", lambda r: r.fulfill(status=200, content_type="application/json",
                                                                                   body=json.dumps(ranking)))
        pg.goto(f"{self.base}/#/research/{sid}")
        self.tid(pg, "rs-search-page").wait_for()
        cur = self.tid(pg, "rs-cells").inner_text()
        hist = self.tid(pg, "rs-historical-cells").inner_text()
        self.assertIn("STR_111111111111", cur)
        self.assertIn("no cost profile", cur)                                 # ineligible stays visible
        self.assertNotIn("STR_333333333333", cur)
        self.assertIn("STR_333333333333", hist)
        self.assertIn("not counted", self.tid(pg, "rs-historical").inner_text())
        self.tid(pg, "rk-table").wait_for()
        self.assertNotIn("STR_333333333333", self.tid(pg, "rk-table").inner_text())
        self.assertEqual(self._unexpected_errors(), [])


if __name__ == "__main__":
    unittest.main()
