"""scripts/derive_timeframes.py: add 15m/30m/60m children to an existing BID/ASK root by replaying its recorded import.
SYNTHETIC Dukascopy-shaped fixture; no backtest, no trial."""
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.research import campaign as C
from edgelab.services import Services
from scripts import derive_timeframes as DT
from tests.dukascopy_fixture import write_fixture

REPO = Path(__file__).resolve().parents[1]


def _opts(csv, derive):
    return dict(file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY",
                asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=derive,
                build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
                ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN_BIDASK")


def _workspace(csv_src=None):
    root = Path(tempfile.mkdtemp())
    shutil.copytree(REPO / "configs", root / "configs")
    csv = root / "combined.csv"
    if csv_src is None:
        write_fixture(csv, end="2024-04-05")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
    else:
        shutil.copy(csv_src, csv)
    return root, csv


class TestDeriveTimeframes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root, cls.csv = _workspace()
        svc = Services(root=cls.root)
        r = svc.import_file(_opts(cls.csv, ["5m"]))                     # as the canonical import: 5m only
        cls.root_id, cls.child5 = r["dataset_id"], r["derived"][0]
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def proto(self, src):
        return {"material": {"scope": {"instrument": "NQ_DUKASCOPY", "provider": "DUKASCOPY"},
                             "source_dataset": {"dataset_id": src}}}

    def test_replay_adds_missing_children_to_the_same_root(self):
        s = self.svc()
        tfs = {"1m", "5m", "15m", "30m", "60m"}
        by_tf, probs = C.resolve_datasets(s, self.proto(self.child5), tfs)
        self.assertEqual(sorted(p["timeframe"] for p in probs), ["15m", "30m", "60m"])   # the Windows symptom
        p = DT.plan(s, self.root_id, ["15m", "30m", "60m"])
        self.assertEqual((p["derive"], p["missing"]), (["5m", "15m", "30m", "60m"], ["15m", "30m", "60m"]))
        n0 = len(s.list_datasets())
        self.assertEqual(len(s.list_datasets()), n0)                                     # plan writes nothing
        out = DT.apply(s, p)
        self.assertEqual((out["root"], out["root_created"]), (self.root_id, False))         # same root, kept
        self.assertEqual(out["children"]["5m"], self.child5)                               # existing child unchanged
        self.assertEqual(sorted(out["created"]), ["15m", "30m", "60m"])
        for tf in ("15m", "30m", "60m"):
            m = s.store.get_manifest(out["children"][tf])
            self.assertEqual((m.parent_dataset_id, m.timeframe, bool(m.has_ask_ohlc), m.price_basis),
                             (self.root_id, tf, True, "bid"))
            self.assertIn("ASK OHLC", m.derivation)
        by_tf, probs = C.resolve_datasets(s, self.proto(self.child5), tfs)
        self.assertEqual((probs, sorted(by_tf)), ([], sorted(tfs)))                          # campaign now resolves
        again = DT.plan(s, self.root_id, ["15m", "30m", "60m"])
        self.assertEqual(again["missing"], [])
        # identical to deriving them in the original import
        r2, csv2 = _workspace(self.csv)
        try:
            s2 = Services(root=r2)
            direct = s2.import_file(_opts(csv2, ["5m", "15m", "30m", "60m"]))
            s2.store.close()
            self.assertEqual(direct["dataset_id"], self.root_id)
            self.assertEqual(sorted(direct["derived"]), sorted(out["children"].values()))
        finally:
            shutil.rmtree(r2, ignore_errors=True)

    def test_refusals(self):
        s = self.svc()
        with self.assertRaises(DT.DeriveRefused):
            DT.plan(s, self.child5, ["15m"])                                              # not a root
        with self.assertRaises(DT.DeriveRefused):
            DT.plan(s, self.root_id, ["1m"])                                              # not a HIGHER multiple
        bad = self.root / "changed.csv"
        txt = self.csv.read_text().splitlines()
        bad.write_text("\n".join(txt[:-1]) + "\n")                                         # one row fewer
        with self.assertRaises(DT.DeriveRefused):
            DT.plan(s, self.root_id, ["15m"], source_file=str(bad))
        with self.assertRaises(DT.DeriveRefused):
            DT.plan(s, self.root_id, ["15m"], source_file=str(self.root / "missing.csv"))


if __name__ == "__main__":
    unittest.main()
