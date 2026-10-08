"""Live charts (ADR-111). Dukascopy cannot be reached from the build environment: every test uses a deterministic
stand-in downloader (random-walk minutes, hours = those minutes aggregated), labelled synthetic.

Guarantees tested:
* bars of every timeframe are anchored at the 18:00 New York session open (4h buckets 18-22-02-06-10-14, the daily bar is
  the trading date, the weekly bar starts on Monday, the monthly bar on the 1st); bars built from hourly data equal bars
  built from the minutes;
* nothing is invented: every bar's open / high / low / close comes from the source minutes of its bucket;
* complete days are cached and not downloaded again; the open day is downloaded again; a failed download is reported;
* paging: ``to`` returns only bars before it, ``count`` limits them; live polling merges the newest minutes;
* drawings and layout are stored as data; bad symbols / timeframes are refused; the API answers.
"""
import shutil
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.charts import feed as CF
from edgelab.charts import store as CS


def fake_fetch(calls=None):
    def day(code, d):                                                    # one UTC day, the same whatever range is asked
        idx = pd.date_range(d, d + pd.Timedelta(days=1), freq="1min", inclusive="left")
        ny = idx.tz_convert("America/New_York")
        closed = (ny.weekday == 5) | ((ny.weekday == 4) & (ny.hour >= 17)) | ((ny.weekday == 6) & (ny.hour < 18)) | (ny.hour == 17)
        idx = idx[~closed]
        rng = np.random.default_rng(int(d.value // 86_400_000_000_000) + (7 if "SandP" in code else 0))
        c = (18000.0 if "NQ" in code else 4500.0) + np.cumsum(rng.normal(0, 2.0, len(idx)))
        o = np.r_[c[0], c[:-1]] if len(c) else c
        return pd.DataFrame({"open": o, "high": np.maximum(o, c) + 0.5, "low": np.minimum(o, c) - 0.5, "close": c, "volume": 1.0}, index=idx)

    def minutes(code, start, end):
        s, e = pd.Timestamp(start), pd.Timestamp(end)
        days = pd.date_range(s.floor("D"), e.ceil("D"), freq="1D", inclusive="left")
        df = pd.concat([day(code, d) for d in days]) if len(days) else day(code, s.floor("D")).iloc[:0]
        return df[(df.index >= s) & (df.index < e)]

    def fetch(code, interval, start, end):
        if calls is not None:
            calls.append((code, interval, start, end))
        if interval == "1m":
            return minutes(code, start, end)
        parts = []
        d = pd.Timestamp(start).floor("D")
        while d < pd.Timestamp(end):
            parts.append(minutes(code, d.to_pydatetime(), (d + pd.Timedelta(days=1)).to_pydatetime()))
            d += pd.Timedelta(days=1)
        m = pd.concat(parts)
        h = m.resample("1h").agg({"open": "first", "high": "max", "low": "min", "close": "last", "volume": "sum"}).dropna()
        return h[(h.index >= pd.Timestamp(start)) & (h.index < pd.Timestamp(end))]
    return fetch


NOW = datetime(2024, 3, 20, 15, 7, tzinfo=timezone.utc)


class TestResample(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())
        self.feed = CF.Feed(self.root, fetch=fake_fetch(), now=lambda: NOW)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_anchoring_and_truth(self):
        res = self.feed.bars("NQ", 240, count=60)
        ny = pd.to_datetime([b["time"] for b in res["bars"]], unit="s", utc=True).tz_convert("America/New_York")
        self.assertTrue(set(ny.hour) <= {18, 22, 2, 6, 10, 14}, set(ny.hour))
        # every 15m bar = its minutes
        m = self.feed.minutes("E_NQ-100", NOW - timedelta(days=2), NOW)
        r15 = self.feed.bars("NQ", 15, count=50, live=False)["bars"]
        for b in r15[-10:-1]:                                            # the last bar is still forming
            s = m[(m.index >= pd.Timestamp(b["time"], unit="s", tz="UTC")) & (m.index < pd.Timestamp(b["time"] + 900, unit="s", tz="UTC"))]
            self.assertAlmostEqual(b["open"], s["open"].iloc[0])
            self.assertAlmostEqual(b["high"], s["high"].max())
            self.assertAlmostEqual(b["low"], s["low"].min())
            self.assertAlmostEqual(b["close"], s["close"].iloc[-1])

    def test_hourly_source_equals_minutes(self):
        m = self.feed.minutes("E_NQ-100", datetime(2024, 3, 11, tzinfo=timezone.utc), datetime(2024, 3, 16, tzinfo=timezone.utc))
        a = {b["time"]: b for b in CF.resample(m, 120)}
        h = self.feed.hours("E_NQ-100", datetime(2024, 3, 11, tzinfo=timezone.utc), datetime(2024, 3, 16, tzinfo=timezone.utc))
        b = {x["time"]: x for x in CF.resample(h, 120)}
        common = sorted(set(a) & set(b))[2:-2]
        self.assertGreater(len(common), 20)
        for t in common:
            for k in ("open", "high", "low", "close"):
                self.assertAlmostEqual(a[t][k], b[t][k], places=6)

    def test_daily_weekly_monthly(self):
        d = self.feed.bars("ES", CF.parse_tf("1D"), count=30)["bars"]
        days = pd.to_datetime([x["time"] for x in d], unit="s")
        self.assertTrue((days.hour == 0).all())
        self.assertTrue((days.weekday < 5).all())                        # trading dates, no weekend bars
        w = self.feed.bars("ES", CF.parse_tf("1W"), count=8)["bars"]
        self.assertTrue((pd.to_datetime([x["time"] for x in w], unit="s").weekday == 0).all())
        mo = self.feed.bars("ES", CF.parse_tf("1M"), count=3)["bars"]
        self.assertTrue((pd.to_datetime([x["time"] for x in mo], unit="s").day == 1).all())

    def test_parse_and_refusals(self):
        self.assertEqual(CF.parse_tf("2h"), 120)
        self.assertEqual(CF.parse_tf("45m"), 45)
        self.assertEqual(CF.parse_tf("1W"), CF.WEEK)
        with self.assertRaises(CF.ChartError):
            CF.parse_tf("0")
        with self.assertRaises(CF.ChartError):
            self.feed.bars("XYZ", 5)


class TestCacheLivePaging(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp())

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def test_cache_paging_live_and_failure(self):
        calls = []
        feed = CF.Feed(self.root, fetch=fake_fetch(calls), now=lambda: NOW)
        a = feed.bars("MNQ", 5, count=400)
        n1 = len(calls)
        b = feed.bars("MNQ", 5, count=400)
        self.assertEqual(a["bars"][:-5], b["bars"][:-5])
        self.assertLessEqual(len(calls) - n1, 1)                          # only the open day is fetched again
        cached = sorted((self.root / "charts" / "cache" / "E_NQ-100" / "1m").glob("*.csv.gz"))
        self.assertGreater(len(cached), 2)                                # complete days cached one file per day
        self.assertLess(n1, len(cached))                                  # ... downloaded in batches (fewer requests than days)
        self.assertTrue(any(c[3] - c[2] > timedelta(days=1) for c in calls))
        first = a["bars"][0]["time"]
        older = feed.bars("MNQ", 5, to_ns=first * CF.NS, count=100)["bars"]
        self.assertTrue(older and all(x["time"] < first for x in older))
        self.assertLessEqual(len(older), 100)
        feed._poll_once("E_NQ-100")                                       # live: the newest minutes merged
        lv = feed.live_bars("MNQ", 5, a["bars"][-1]["time"])
        self.assertTrue(lv["bars"])
        self.assertIsNone(lv["status"]["error"])

        def broken(code, interval, start, end):
            raise ConnectionError("offline")
        feed2 = CF.Feed(Path(tempfile.mkdtemp()), fetch=broken, now=lambda: NOW)
        with self.assertRaises(CF.ChartError) as e:
            feed2.bars("ES", 1)
        self.assertEqual(e.exception.code, "DOWNLOAD_FAILED")
        self.assertIn("offline", feed2.status["E_SandP-500"]["error"])

    def test_store_and_api(self):
        self.assertEqual(CS.save_drawings(self.root, "NQ", [{"type": "trend_line", "points": []}])["saved"], 1)
        self.assertEqual(CS.drawings(self.root, "NQ")[0]["type"], "trend_line")
        with self.assertRaises(CF.ChartError):
            CS.save_drawings(self.root, "NQ", [{"no": "type"}])
        from edgelab.web.app import create_app
        from tests.test_my_strategy import REPO
        ws = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", ws / "configs")
        c = create_app(ws).test_client()
        meta = c.get("/api/charts").get_json()
        self.assertEqual([s["symbol"] for s in meta["symbols"]], ["NQ", "MNQ", "ES", "MES"])
        self.assertEqual(c.get("/api/charts/bars?symbol=XX&tf=5").status_code, 400)
        self.assertEqual(c.get("/api/charts/bars?symbol=NQ&tf=5x").status_code, 400)
        self.assertEqual(c.put("/api/charts/drawings/NQ", json={"drawings": [{"type": "rectangle", "points": []}]}).status_code, 200)
        self.assertEqual(c.get("/api/charts/drawings/NQ").get_json()["drawings"][0]["type"], "rectangle")
        self.assertEqual(c.put("/api/charts/layout", json={"tf": "5"}).status_code, 200)
        self.assertEqual(c.get("/api/charts/layout").get_json()["tf"], "5")
        shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
