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
        self.assertEqual(rep["combined"]["mode"], "exact")
        self.assertEqual(rep["combined"]["output_rows"], n)
        self.assertEqual(rep["combined"]["sha256"], sha(comb))
        with self.assertRaises(SystemExit):                                         # never overwrites
            self.run_main(self.bid, ask, "--out", self.d / "r2.json", "--write-combined", comb)
        with self.assertRaises(SystemExit):                                         # provenance report required
            self.run_main(self.bid, ask, "--write-combined", self.d / "c3.csv")
        for target in (self.bid, ask):                                              # never onto a source
            with self.assertRaises(SystemExit):
                self.run_main(self.bid, ask, "--out", self.d / "r3.json", "--write-combined", target)
        self.assertEqual((sha(self.bid), sha(ask)), before)

    def test_combined_file_feeds_the_existing_import_path(self):
        """No architecture change: the existing bid/ask-close import options build the per-bar spread."""
        from edgelab.services import Services
        ask = self.write_ask(self.ask_df)
        comb = self.d / "combined.csv"
        self.run_main(self.bid, ask, "--out", self.d / "r.json", "--write-combined", comb)
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

    def test_derived_5m_child_keeps_bid_ask_spread_metadata_and_lineage(self):
        from edgelab.services import Services
        ask = self.write_ask(self.ask_df)
        comb = self.d / "combined.csv"
        self.run_main(self.bid, ask, "--out", self.d / "r.json", "--write-combined", comb)
        ws = self.d / "ws"
        shutil.copytree(REPO / "configs", ws / "configs")
        svc = Services(root=ws)
        self.addCleanup(svc.store.close)
        r = svc.import_file(dict(file=str(comb), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY",
                                 provider="DUKASCOPY", asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid",
                                 timeframe="1m", dataset_name="TEST_BIDASK", bid_close_column="close",
                                 ask_close_column="ask_close", derive_timeframes=["5m"], build_features=False,
                                 notes="combined provenance note"))
        p, c = r["manifest"], svc.dataset_detail(r["derived"][0])["manifest"]
        for k in ("has_bid_ask", "has_spread", "spread_source", "source_file_sha256", "provider_notes", "price_basis",
                  "symbol", "calendar", "calendar_fingerprint", "volume_type"):
            self.assertEqual(c[k], p[k], k)
        self.assertEqual((c["has_bid_ask"], c["has_spread"], c["spread_source"]), (True, True, "bid_ask_close"))
        self.assertEqual((c["parent_dataset_id"], c["source_detail"]["derived_from"]), (r["dataset_id"], r["dataset_id"]))
        self.assertEqual(c["derivation"], "session-anchored resample 1m->5m")
        one, five = svc.load_dataset(r["dataset_id"]), svc.load_dataset(r["derived"][0])
        k = len(five.bars) // 2                                          # every 5m spread = mean of its 1m spreads
        sel = (one.bars.ts_ns >= five.bars.ts_ns[k]) & (one.bars.ts_ns < five.bars.ts_ns[k] + 300_000_000_000)
        self.assertAlmostEqual(five.bars.spread[k], one.bars.spread[sel].mean())
        self.assertTrue(np.isfinite(five.bars.spread).all())

    def test_one_sided_rows_are_reported_and_block_the_combined_file(self):
        a = self.ask_df.drop(index=[10, 11, 500])                                 # 3 BID-only minutes
        extra = a.iloc[[-1]].copy()
        extra["timestamp"] = "2024-03-15T20:30:00+00:00"                          # 1 ASK-only minute
        ask = self.write_ask(pd.concat([a, extra], ignore_index=True))
        comb = self.d / "combined.csv"
        with self.assertRaises(SystemExit):
            self.run_main(self.bid, ask, "--out", self.d / "r0.json", "--write-combined", comb)
        with self.assertRaises(SystemExit):                                         # interior gaps: refused
            self.run_main(self.bid, ask, "--out", self.d / "r0.json", "--write-combined", comb, "--intersection")
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

    def shifted_pair(self):
        """The real shape: ASK downloaded one trading date EARLIER than BID (same length)."""
        full = self.d / "full.csv"
        write_fixture(full, start="2024-03-03", end="2024-03-16")
        f = pd.read_csv(full, dtype=str)
        ny = pd.to_datetime(f["timestamp"], utc=True).dt.tz_convert("America/New_York")
        td = (ny.dt.tz_localize(None) + pd.Timedelta(hours=6)).dt.normalize()
        first, last = td.min(), td.max()
        bid = f[td != first].reset_index(drop=True)                               # BID: drops the first date
        a = f[td != last].reset_index(drop=True)                                  # ASK: drops the last date
        sp = np.round(np.random.default_rng(9).choice([3.0, 3.25, 3.5], len(a)), 3)
        for k in ("open", "high", "low", "close"):
            a[k] = (a[k].astype(float) + sp).map(lambda x: f"{x:.3f}")
        bp, ap = self.d / "bid_shift.csv", self.d / "ask_shift.csv"
        bid.to_csv(bp, index=False)
        a.to_csv(ap, index=False)
        return bp, ap, bid, a, int((td == first).sum()), int((td == last).sum())

    def test_boundary_only_difference_writes_the_exact_intersection_with_provenance(self):
        bp, ap, bid, ask, n_first, n_last = self.shifted_pair()
        before = (sha(bp), sha(ap))
        comb = self.d / "combined.csv"
        with self.assertRaises(SystemExit):                                         # not exact without --intersection
            self.run_main(bp, ap, "--out", self.d / "r.json", "--write-combined", comb)
        self.assertFalse(comb.exists())
        code, text = self.run_main(bp, ap, "--out", self.d / "r.json", "--write-combined", comb, "--intersection")
        self.assertEqual(code, 0)
        rep = json.loads((self.d / "r.json").read_text())
        al, c = rep["alignment"], rep["combined"]
        self.assertFalse(rep["exactly_aligned"])
        self.assertTrue(rep["intersection_aligned"])
        self.assertEqual((al["bid_only"], al["ask_only"], al["one_sided_inside_overlap"]), (n_last, n_first, 0))
        overlap = len(bid) - n_last
        self.assertEqual((c["mode"], c["output_rows"], al["overlap"]), ("intersection", overlap, overlap))
        self.assertEqual((c["bid_sha256"], c["ask_sha256"]), before)
        self.assertEqual((c["bid_rows"], c["ask_rows"], c["bid_only"], c["ask_only"]),
                         (len(bid), len(ask), n_last, n_first))
        self.assertLess(pd.Timestamp(c["ask_only_range"][1]), pd.Timestamp(c["overlap_first"]))   # before overlap
        self.assertGreater(pd.Timestamp(c["bid_only_range"][0]), pd.Timestamp(c["overlap_last"]))  # after overlap
        self.assertEqual(c["sha256"], sha(comb))
        out = pd.read_csv(comb, dtype=str)
        self.assertEqual(len(out), overlap)
        exp_bid = bid.iloc[:overlap].reset_index(drop=True)                        # BID rows verbatim, timestamps untouched
        pd.testing.assert_frame_equal(out[list(bid.columns)], exp_bid)
        exp_ask = ask.iloc[n_first:].reset_index(drop=True)
        self.assertEqual(list(out["timestamp"]), list(exp_ask["timestamp"]))
        for k in ("open", "high", "low", "close"):
            self.assertEqual(list(out[f"ask_{k}"]), list(exp_ask[k]))
        self.assertEqual((sha(bp), sha(ap)), before)                               # sources untouched
        self.assertIn("IMPORT NOTES (pass as --notes):", text)
        self.assertIn(before[0], text)
        self.assertIn(before[1], text)

    def test_interior_gap_blocks_the_intersection(self):
        bp, ap, bid, ask, n_first, n_last = self.shifted_pair()
        a = ask.drop(index=[n_first + 100]).reset_index(drop=True)               # one ASK minute missing inside
        a.to_csv(ap, index=False)
        comb = self.d / "combined.csv"
        with self.assertRaises(SystemExit):
            self.run_main(bp, ap, "--out", self.d / "r.json", "--write-combined", comb, "--intersection")
        self.assertFalse(comb.exists())
        self.run_main(bp, ap, "--out", self.d / "r.json")
        rep = json.loads((self.d / "r.json").read_text())
        self.assertEqual(rep["alignment"]["one_sided_inside_overlap"], 1)
        self.assertFalse(rep["intersection_aligned"])

    def test_refuses_same_file(self):
        with self.assertRaises(SystemExit):
            self.run_main(self.bid, self.bid)


if __name__ == "__main__":
    unittest.main()
