"""ADR-111 browser flow for the Charts tab, on SYNTHETIC data only (tests.test_charts.fake_fetch stands in for the
Dukascopy download; no network): the chart loads, every drawing tool in every tool group can be drawn without a page
error and is saved, the text dialog opens for text tools, undo / redo, drawings are kept per symbol, a custom timeframe
and the layout are saved, the right-click menu clones a drawing."""
import json
import shutil
import tempfile
import threading
import unittest
import urllib.request
from pathlib import Path

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                        # pragma: no cover
    sync_playwright = None

from edgelab.charts.feed import Feed
from tests.test_charts import NOW, fake_fetch

REPO = Path(__file__).resolve().parents[1]
GROUPS = ["lines", "fib", "patterns", "forecast", "shapes", "text", "icons"]
PTS = [(0.2, 0.6), (0.35, 0.3), (0.5, 0.55), (0.6, 0.35), (0.7, 0.6), (0.78, 0.4), (0.85, 0.55)]


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestChartsBrowserFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        from edgelab.web.app import create_app
        cls.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.tmp / "configs")
        cls.app = create_app(cls.tmp)
        cls.svc = cls.app.config["EDGELAB"]["services"]
        cls.svc.__dict__["_charts_feed_obj"] = Feed(cls.svc.data_root, fetch=fake_fetch(), now=lambda: NOW)
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
        for close in (lambda: cls.browser.close(), lambda: cls.pw.stop(), cls.srv.shutdown):
            try:
                close()
            except Exception:
                pass
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def get(self, path):
        return json.loads(urllib.request.urlopen(self.base + path).read())

    def page(self):
        ctx = self.browser.new_context(viewport={"width": 1500, "height": 900})
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        self.errors = []
        pg.on("pageerror", lambda e: self.errors.append(str(e)))
        self.addCleanup(ctx.close)
        pg.goto(f"{self.base}/#/charts")
        pg.wait_for_selector("[data-testid=ch-chart] canvas")
        pg.wait_for_function("() => /O\\s+[0-9]/.test(document.querySelector(\"[data-testid=ch-legend]\")?.innerText || '')")
        return pg

    def symbol(self, pg, sym, name):
        pg.click("[data-testid=ch-symbol]")
        pg.click(f"[data-testid=ch-sym-{sym}]")
        pg.wait_for_function("(n) => { const t = document.querySelector(\"[data-testid=ch-legend]\")?.innerText || '';"
                             " return t.includes(n) && !t.includes('loading'); }", arg=name)

    def saved(self, symbol, n, timeout=8000):
        """Waits until the workspace holds n drawings for the symbol (saving is debounced)."""
        import time
        end = time.time() + timeout / 1000
        while time.time() < end:
            got = self.get(f"/api/charts/drawings/{symbol}")["drawings"]
            if len(got) == n:
                return got
            time.sleep(0.2)
        self.fail(f"expected {n} saved drawings on {symbol}, have {len(got)}")

    def test_every_tool_draws_and_saves(self):
        pg = self.page()
        self.symbol(pg, "MES", "Micro E-mini S&P")
        box = pg.locator("[data-testid=ch-chart]").bounding_box()
        X = lambda f: box["x"] + box["width"] * f                            # noqa: E731
        Y = lambda f: box["y"] + box["height"] * f                           # noqa: E731
        tools = []
        for g in GROUPS:
            pg.click(f"[data-testid=ch-group-{g}-more]")
            tools += [(g, x.get_attribute("data-testid")[len("ch-tool-"):]) for x in pg.locator("[data-testid^=ch-tool-]").all()]
            pg.keyboard.press("Escape")
            pg.mouse.click(X(0.5), Y(0.02))
            pg.keyboard.press("Escape")
        self.assertGreaterEqual(len(tools), 85)
        for g, tid in tools:
            pg.click(f"[data-testid=ch-group-{g}-more]")
            pg.click(f"[data-testid=ch-tool-{tid}]")
            hint = pg.locator("[data-testid=ch-hint]").inner_text()
            if "press and drag" in hint and "click" not in hint:             # brushes
                pg.mouse.move(X(0.3), Y(0.5)); pg.mouse.down(); pg.mouse.move(X(0.45), Y(0.35), steps=10); pg.mouse.up()
            elif "double-click" in hint:                                     # paths: until a double-click
                for a, b in PTS[:3]:
                    pg.mouse.click(X(a), Y(b))
                pg.mouse.dblclick(X(PTS[3][0]), Y(PTS[3][1]))
            else:
                n = 1 if "click to place" in hint else int(hint.split("click ")[1].split(" ")[0])
                for a, b in PTS[:n]:
                    pg.mouse.click(X(a), Y(b))
            if pg.locator("[data-testid=ch-drawing-dialog]").count():          # text tools ask for their text
                pg.click("[data-testid=ch-dlg-ok]")
            pg.keyboard.press("Escape")
        saved = self.saved("MES", len(tools))
        self.assertEqual(sorted(d["type"] for d in saved), sorted(t for _, t in tools))
        self.assertTrue(all(d["points"] and all(isinstance(p["t"], (int, float)) for p in d["points"]) for d in saved))
        self.assertEqual(self.errors, [])

    def test_editing_undo_symbols_layout(self):
        pg = self.page()
        self.symbol(pg, "ES", "E-mini S&P 500")
        box = pg.locator("[data-testid=ch-chart]").bounding_box()
        X = lambda f: box["x"] + box["width"] * f                            # noqa: E731
        Y = lambda f: box["y"] + box["height"] * f                           # noqa: E731
        pg.click("[data-testid=ch-group-lines]")                              # trend line: click, click
        pg.mouse.click(X(0.3), Y(0.6)); pg.mouse.click(X(0.55), Y(0.3))
        pg.click("[data-testid=ch-group-text]")                               # text: the dialog opens on the text tab
        pg.mouse.click(X(0.15), Y(0.8))
        pg.fill("[data-testid=ch-dlg-text]", "Asia high")
        pg.click("[data-testid=ch-dlg-ok]")
        got = self.saved("ES", 2)
        self.assertEqual([d["type"] for d in got], ["trend_line", "text"])
        self.assertEqual(got[1]["text"], "Asia high")
        pg.mouse.click(X(0.425), Y(0.45), button="right")                     # right-click the trend line -> clone
        pg.click("[data-testid=ch-menu-clone]")
        self.saved("ES", 3)
        pg.click("[data-testid=ch-undo]")
        self.saved("ES", 2)
        pg.click("[data-testid=ch-redo]")
        self.saved("ES", 3)
        pg.mouse.click(X(0.425), Y(0.45))                                     # select a line, Delete removes it
        pg.keyboard.press("Delete")
        self.saved("ES", 2)
        self.assertEqual(self.get("/api/charts/drawings/NQ")["drawings"], [])  # drawings are per symbol
        pg.click("[data-testid=ch-tf-more]")
        pg.fill("[data-testid=ch-tf-input]", "7m")
        pg.keyboard.press("Enter")
        pg.wait_for_function("() => document.querySelector(\"[data-testid=ch-legend]\")?.innerText.includes('7 minutes')")
        import time
        time.sleep(1.0)
        lay = self.get("/api/charts/layout")
        self.assertEqual((lay["symbol"], lay["tf"]), ("ES", 7))
        self.assertEqual(self.errors, [])


if __name__ == "__main__":
    unittest.main()
