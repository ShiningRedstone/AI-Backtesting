"""ADR-112 browser flow for simulated trading on the Charts tab, on SYNTHETIC data only (no network): create a LucidFlex
account, buy at market with a bracket, place a limit from the chart's right-click menu, see the working orders, cancel,
exit everything; the stored attempt has the fills and one closed trade; no page error."""
import shutil
import tempfile
import threading
import time
import unittest
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd

try:
    from playwright.sync_api import sync_playwright
except ImportError:                                        # pragma: no cover
    sync_playwright = None

from edgelab.charts.feed import Feed
from edgelab.charts.ticks import TickSource
from tests.test_charts import fake_fetch

REPO = Path(__file__).resolve().parents[1]


def tick_stand_in(ff):
    """Synthetic bid / ask every 500 ms, walking through the synthetic minute bars (ask = bid + 0.5)."""
    def ticks(code, start, end):
        m = ff(code, "1m", start - timedelta(minutes=1), end)
        idx = pd.date_range(pd.Timestamp(start).ceil("500ms"), end, freq="500ms")
        if not len(m) or not len(idx):
            return pd.DataFrame({"bidPrice": [], "askPrice": []})
        mi = m.index.searchsorted(idx, side="right") - 1
        idx, mi = idx[mi >= 0], mi[mi >= 0]
        frac = ((idx - m.index[mi]) / pd.Timedelta(minutes=1)).to_numpy()
        o, c = m["open"].to_numpy()[mi], m["close"].to_numpy()[mi]
        bid = np.round(o + (c - o) * frac, 2)
        return pd.DataFrame({"bidPrice": bid, "askPrice": bid + 0.5}, index=idx)
    return ticks


@unittest.skipIf(sync_playwright is None, "playwright not installed")
class TestTradingBrowserFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from werkzeug.serving import make_server
        from edgelab.web.app import create_app
        cls.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.tmp / "configs")
        cls.app = create_app(cls.tmp)
        cls.svc = cls.app.config["EDGELAB"]["services"]
        ff = fake_fetch()
        cls.svc.__dict__["_charts_feed_obj"] = Feed(cls.svc.data_root, fetch=ff)
        cls.svc._sim().ticks = TickSource(tick_stand_in(ff))
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

    def wait(self, cond, timeout=20):
        end = time.time() + timeout
        while time.time() < end:
            v = cond()
            if v:
                return v
            time.sleep(0.25)
        self.fail("condition not met in time")

    def test_trade_flow(self):
        ctx = self.browser.new_context(viewport={"width": 1600, "height": 1000})
        self.addCleanup(ctx.close)
        pg = ctx.new_page()
        pg.set_default_timeout(20000)
        errors = []
        pg.on("pageerror", lambda e: errors.append(str(e)))
        pg.goto(f"{self.base}/#/charts")
        pg.wait_for_selector("[data-testid=ch-chart] canvas")
        pg.click("[data-testid=ch-trade]")
        pg.click("[data-testid=tr-new-first]")
        pg.fill("[data-testid=tr-new-balance]", "50000")
        pg.click("[data-testid=tr-new-ok]")
        pg.wait_for_selector("[data-testid=tr-status]")
        self.assertIn("EVALUATION", pg.locator("[data-testid=tr-stage]").inner_text())
        m = self.svc._sim()
        aid = next(iter(m.accounts))
        self.wait(lambda: m.last.get("E_NQ-100"))                            # quotes are flowing
        pg.click("[data-testid=tr-tp-on]")
        pg.click("[data-testid=tr-sl-on]")
        pg.click("[data-testid=tr-buy]")
        att = lambda: m.accounts[aid]["attempts"][-1]                          # noqa: E731
        self.wait(lambda: att()["positions"].get("MNQ", {}).get("qty") == 1)
        self.wait(lambda: sum(o["status"] == "working" and o["role"] in ("tp", "sl") for o in att()["orders"]) == 2)
        box = pg.locator("[data-testid=ch-chart]").bounding_box()
        pg.mouse.click(box["x"] + box["width"] * 0.6, box["y"] + box["height"] * 0.9, button="right")
        label = pg.locator("[data-testid=ch-menu-buy]").inner_text()
        self.assertTrue(label.startswith("Buy 1 MNQ "))
        pg.click("[data-testid=ch-menu-buy]")
        self.wait(lambda: sum(o["status"] == "working" for o in att()["orders"]) == 3)
        pg.click("[data-testid=tr-tab-working]")
        pg.wait_for_function("() => document.querySelector('[data-testid=tr-bottom]').innerText.includes('take profit')")
        pg.click("[data-testid=tr-flatten-all]")
        self.wait(lambda: not att()["positions"]["MNQ"]["qty"] and not [o for o in att()["orders"] if o["status"] == "working"])
        self.assertEqual(len(att()["episodes"]), 1)
        self.assertGreaterEqual(len(att()["fills"]), 2)
        self.assertEqual(att()["state"], "active")
        self.assertEqual(errors, [])


if __name__ == "__main__":
    unittest.main()
