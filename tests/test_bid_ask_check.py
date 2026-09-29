"""scripts/dukascopy_bid_ask_check.py: read-only BID/ASK alignment + spread report, and a combined file
only on exact alignment. SYNTHETIC fixture data only (temp dirs)."""
import hashlib
import json
import runpy
import shutil
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path

import numpy as np
import pandas as pd

from tests.dukascopy_fixture import write_fixture

REPO = Path(__file__).resolve().parents[1]
MOD = runpy.run_path(str(REPO / "scripts" / "dukascopy_bid_ask_check.py"), run_name="bid_ask_check")


def sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


class TestBidAskCheck(unittest.TestCase):
    def setUp(self):
        self.d = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.d, True)
        self.bid = self.d / "bid.csv"
        write_fixture(self.bid, start="2024-03-03", end="2024-03-16")
        b = pd.read_csv(self.bid, dtype=str)
        rng = np.random.default_rng(3)
        sp = np.round(rng.choice([0.5, 0.75, 1.0, 1.5], len(b)), 3)            # known spreads (points)
        a = b.copy()
        for k in ("open", "high", "low", "close"):
            a[k] = (b[k].astype(float) + sp).map(lambda x: f"{x:.3f}")
        self.spreads, self.ask_df = sp, a

    def write_ask(self, df):
        p = self.d / "ask.csv"
        df.to_csv(p, index=False)
        return p

    def run_main(self, *args):
        out = StringIO()
        with redirect_stdout(out):
            code = MOD["main"]([str(x) for x in args] + ["--root", str(REPO)])
        return code, out.getvalue()

    def test_exact_alignment_report_and_combined_file(self):
        ask = self.write_ask(self.ask_df)
        before = (sha(self.bid), sha(ask))
        comb = self.d / "combined.csv"
        code, text = self.run_main(self.bid, ask, "--out", self.d / "r.json", "--write-combined", comb)
        self.assertEqual(code, 0)
        rep = json.loads((self.d / "r.json").read_text())
        n = len(self.ask_df)
        self.assertEqual((rep["bid"]["rows"], rep["ask"]["rows"], rep["alignment"]["overlap"]), (n, n, n))
        self.assertEqual((rep["alignment"]["bid_only"], rep["alignment"]["ask_only"]), (0, 0))
        self.assertTrue(rep["exactly_aligned"])
        self.assertEqual((rep["bid"]["sha256"], rep["ask"]["sha256"]), before)
        s = rep["spread_points"]["close"]
        self.assertAlmostEqual(s["stats_points"]["median"], float(np.median(self.spreads)), places=6)
        self.assertAlmostEqual(s["stats_points"]["max"], 1.5, places=6)
        self.assertEqual((s["negative"], s["zero"], s["not_computable"]), (0, 0, 0))
        self.assertTrue(rep["bid"]["all_utc_offsets"] and rep["ask"]["all_utc_offsets"])
        self.assertEqual(rep["anomalies"], [])
        self.assertTrue(rep["sources_unchanged"])
        self.assertEqual((sha(self.bid), sha(ask)), before)                       # sources untouched
        c = pd.read_csv(comb, dtype=str)
        pd.testing.assert_frame_equal(c[list(pd.read_csv(self.bid, dtype=str).columns)],
                                      pd.read_csv(self.bid, dtype=str))           # BID values verbatim
        self.assertEqual(list(c["ask_close"]), list(self.ask_df["close"]))
        self.assertIn("EXACTLY ALIGNED: True", text)
        with self.assertRaises(SystemExit):                                         # never overwrites
            self.run_main(self.bid, ask, "--write-combined", comb)

    def test_combined_file_feeds_the_existing_import_path(self):
        """No architecture change: the existing bid/ask-close import options build the per-bar spread."""
        from edgelab.services import Services
        ask = self.write_ask(self.ask_df)
        comb = self.d / "combined.csv"
        self.run_main(self.bid, ask, "--write-combined", comb)
        ws = self.d / "ws"
        shutil.copytree(REPO / "configs", ws / "configs")
        svc = Services(root=ws)
        self.addCleanup(svc.store.close)
        r = svc.import_file(dict(file=str(comb), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY",
                                 provider="DUKASCOPY", asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid",
                                 timeframe="1m", dataset_name="TEST_BIDASK", bid_close_column="close",
                                 ask_close_column="ask_close", build_features=False))
        m = r["manifest"]
        self.assertEqual((m["has_spread"], m["spread_source"], m["has_bid_ask"]), (True, "bid_ask_close", True))
        ds = svc.load_dataset(r["dataset_id"])
        self.assertTrue(np.allclose(np.sort(ds.bars.spread), np.sort(self.spreads)))

    def test_one_sided_rows_are_reported_and_block_the_combined_file(self):
        a = self.ask_df.drop(index=[10, 11, 500])                                 # 3 BID-only minutes
        extra = a.iloc[[-1]].copy()
        extra["timestamp"] = "2024-03-15T20:30:00+00:00"                          # 1 ASK-only minute
        ask = self.write_ask(pd.concat([a, extra], ignore_index=True))
        comb = self.d / "combined.csv"
        with self.assertRaises(SystemExit):
            self.run_main(self.bid, ask, "--write-combined", comb)
        self.assertFalse(comb.exists())
        code, text = self.run_main(self.bid, ask, "--out", self.d / "r.json")
        rep = json.loads((self.d / "r.json").read_text())
        self.assertEqual(code, 2)
        self.assertEqual((rep["alignment"]["bid_only"], rep["alignment"]["ask_only"]), (3, 1))
        self.assertIn("2024-03-15 20:30:00+00:00", rep["alignment"]["ask_only_first"])
        self.assertEqual(rep["alignment"]["overlap"], len(self.ask_df) - 3)
        self.assertFalse(rep["exactly_aligned"])

    def test_anomalies_negative_spread_bad_ohlc_and_non_utc_offsets(self):
        a = self.ask_df.copy()
        a.loc[5, "close"] = f"{float(pd.read_csv(self.bid, dtype=str).loc[5, 'close']) - 2:.3f}"   # ask < bid
        a.loc[5, "low"] = a.loc[5, "close"]
        a.loc[7, "high"] = f"{float(a.loc[7, 'low']) - 1:.3f}"                     # high < low
        t = pd.Timestamp(a.loc[9, "timestamp"]).tz_convert("Etc/GMT-2")
        a.loc[9, "timestamp"] = t.strftime("%Y-%m-%dT%H:%M:%S+02:00")              # same instant, not UTC
        ask = self.write_ask(a)
        code, _ = self.run_main(self.bid, ask, "--out", self.d / "r.json")
        rep = json.loads((self.d / "r.json").read_text())
        self.assertEqual(code, 2)
        self.assertEqual(rep["alignment"]["bid_only"] + rep["alignment"]["ask_only"], 0)   # instant still matches
        self.assertEqual(rep["spread_points"]["close"]["negative"], 1)
        self.assertEqual(rep["ask"]["ohlc_integrity_violations"], 1)
        self.assertEqual(rep["ask"]["non_utc_offset_rows"], 1)
        self.assertEqual(len(rep["anomalies"]), 3)
        self.assertFalse(rep["exactly_aligned"])

    def test_refuses_same_file(self):
        with self.assertRaises(SystemExit):
            self.run_main(self.bid, self.bid)


if __name__ == "__main__":
    unittest.main()
