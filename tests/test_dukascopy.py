"""Phase 9 Part A: the Dukascopy source through the EXISTING import pipeline, its provisional identity,
its own (unconfigured) cost profile, gap classification, 1m -> 5m derivation, and the workspace's
Preferred Research Dataset. All data here is a SYNTHETIC Dukascopy-shaped fixture written to temp dirs;
no real Dukascopy file is present in this repository."""
import copy
import hashlib
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.data.importer import ImportFailed
from edgelab.engine.costs import CostConfigError, cost_model_from_config
from edgelab.instruments import InstrumentIdentityError, identity_problem, load_instruments
from edgelab.services import Services
from tests.dukascopy_fixture import session_minutes, write_fixture

REPO = Path(__file__).resolve().parents[1]
OPTS = dict(profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="unspecified",
            timeframe="1m", dataset_name="NQ_DUKASCOPY_2021_2026", derive_timeframes=["5m"], build_features=False)
EMA = REPO / "strategies" / "fixtures" / "ema_crossover.yaml"


def workspace() -> Path:
    root = Path(tempfile.mkdtemp())
    shutil.copytree(REPO / "configs", root / "configs")
    return root


class DukascopyBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = workspace()
        cls.csv = cls.root / "nq_dukascopy_1m.csv"
        cls.facts = write_fixture(cls.csv, end="2024-04-27")
        cls.raw_bytes = cls.csv.read_bytes()
        cls.svc = Services(root=cls.root)
        cls.res = cls.svc.import_file({**OPTS, "file": str(cls.csv)})
        cls.m1 = cls.res["dataset_id"]
        cls.m5 = next(d["dataset_id"] for d in cls.svc.list_datasets() if d["timeframe"] == "5m")

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.root, ignore_errors=True)


class TestImportAndProvenance(DukascopyBase):
    def test_schema_utc_and_source_identity_recorded(self):
        m = self.svc.dataset_detail(self.m1)["manifest"]
        self.assertTrue(self.m1.startswith("NQ_DUKASCOPY_2021_2026_1M_"))
        self.assertEqual((m["provider"], m["instrument"], m["asset_type"], m["symbol"]),
                         ("DUKASCOPY", "NQ_DUKASCOPY", "unspecified", "UNSTATED"))      # symbol NOT invented
        self.assertEqual((m["price_basis"], m["volume_type"]), ("unknown", "unknown"))  # decimal volume != exchange volume
        self.assertEqual((m["source_timezone"], m["source_timestamp_convention"]), ("UTC", "open"))
        self.assertEqual(m["calendar"], "DUKASCOPY_NQ_PROVISIONAL")
        self.assertEqual(m["source_file_sha256"], hashlib.sha256(self.raw_bytes).hexdigest())
        self.assertEqual(self.csv.read_bytes(), self.raw_bytes)                          # source never modified
        self.assertEqual(m["source_detail"]["options"]["profile"], "dukascopy_utc_csv")
        self.assertEqual(m["source_detail"]["file"], str(self.csv))
        ds = self.svc.load_dataset(self.m1)
        first = pd.Timestamp(int(ds.bars.ts_ns[0])).tz_localize("UTC")
        self.assertEqual(first, self.facts["first"])                                   # explicit offsets -> exact UTC
        self.assertEqual(len(ds.bars.ts_ns), self.facts["rows"])
        self.assertTrue(np.all(np.diff(ds.bars.ts_ns) > 0))

    def test_limitations_and_provisional_identity_are_visible(self):
        det = self.svc.dataset_detail(self.m1)
        self.assertTrue(any("volume semantics unknown" in x for x in det["limitations"]))
        self.assertTrue(any("price basis" in x for x in det["limitations"]))
        idn = det["identity"]
        self.assertEqual(idn["identity_status"], "provisional")
        self.assertTrue(idn["research_proxy"])
        self.assertIsNone(idn["source_symbol"])
        self.assertIn("source_symbol", idn["missing_metadata"])
        self.assertIn("asset_class", idn["missing_metadata"])
        self.assertIn("PROVISIONAL", idn["problem"])

    def test_validation_report_covers_the_required_checks(self):
        rep = self.svc.store.get_report(self.m1)
        names = {c["name"]: c for c in rep["checks"]}
        for n in ("timestamp_order", "exact_duplicates", "conflicting_duplicates", "grid_alignment", "tick_alignment",
                  "range_spikes", "positive_prices", "bars_outside_session", "missing_bars", "missing_trading_days"):
            self.assertIn(n, names)
        self.assertEqual(names["bars_outside_session"]["status"], "PASS")               # across the 2024-03-10 DST switch
        self.assertEqual(names["missing_bars"]["count"], len(self.facts["gap"]))        # the real gap, never filled
        self.assertEqual(names["missing_bars"]["status"], "INFO")                       # 0.045% < the 0.1% WARN threshold

    def test_1m_to_5m_derivation_with_lineage(self):
        rows = {d["dataset_id"]: d for d in self.svc.list_datasets()}
        self.assertEqual(rows[self.m5]["parent_dataset_id"], self.m1)
        self.assertEqual(rows[self.m5]["provider"], "DUKASCOPY")
        one, five = self.svc.load_dataset(self.m1), self.svc.load_dataset(self.m5)
        t0 = int(five.bars.ts_ns[0])
        sel = (one.bars.ts_ns >= t0) & (one.bars.ts_ns < t0 + 5 * 60_000_000_000)
        self.assertEqual(five.bars.open[0], one.bars.open[sel][0])
        self.assertEqual(five.bars.high[0], one.bars.high[sel].max())
        self.assertEqual(five.bars.low[0], one.bars.low[sel].min())
        self.assertEqual(five.bars.close[0], one.bars.close[sel][-1])

    def test_gap_classification_is_descriptive(self):
        q = self.svc.dataset_quality(self.m1)
        self.assertEqual(q["summary"]["missing_bars"], len(self.facts["gap"]))
        self.assertEqual(q["summary"]["by_position"], {"intra_session": 1})
        g = q["largest_gaps"][0]
        self.assertEqual((g["missing_bars"], g["length_class"]), (len(self.facts["gap"]), "medium"))
        self.assertEqual(pd.Timestamp(g["start"]), self.facts["gap"][0])
        self.assertEqual(q["coverage"]["missing_trading_days"], [])
        before = self.svc.store.get_manifest(self.m1).to_dict()
        self.assertEqual(self.svc.store.get_manifest(self.m1).to_dict(), before)     # read-only


class TestResearchRefusals(DukascopyBase):
    def test_backtest_refused_while_identity_is_provisional(self):
        with self.assertRaises(InstrumentIdentityError) as cm:
            self.svc.backtest_strategy(EMA.read_text(), self.m5)
        self.assertIn("PROVISIONAL", str(cm.exception))
        row = next(d for d in self.svc.backtest_readiness()["datasets"] if d["dataset_id"] == self.m5)
        self.assertFalse(row["runnable"])
        self.assertTrue(any("provisional" in r for r in row["reasons"]))
        self.assertTrue(any("cost profile is unconfigured" in r for r in row["reasons"]))

    def test_identity_stated_then_cost_refusal_names_the_dukascopy_profile(self):
        cfg = copy.deepcopy(self.svc.cfg)
        cfg["instruments"]["NQ_DUKASCOPY"].update(identity_status="user_specified", source_symbol="TESTSYMBOL",
                                                  asset_class="cfd")
        self.assertIsNone(identity_problem(load_instruments(cfg)["NQ_DUKASCOPY"]))
        with self.assertRaises(CostConfigError) as cm:
            cost_model_from_config(cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")
        self.assertIn("NQ_DUKASCOPY@DUKASCOPY", str(cm.exception))
        svc = Services(cfg=cfg, root=self.root)
        try:
            with self.assertRaises(CostConfigError):
                svc.backtest_strategy(EMA.read_text(), self.m5)
        finally:
            svc.store.close()

    def test_histdata_costs_never_apply_to_dukascopy(self):
        cfg = self.svc.cfg
        hist = cost_model_from_config(cfg, "NAS100_HISTDATA", provider="HISTDATA")           # unchanged: assumed
        self.assertEqual((hist.status, hist.profile), ("assumed", "NAS100_HISTDATA@HISTDATA"))
        for prov in ("DUKASCOPY", "HISTDATA", None):
            with self.assertRaises(CostConfigError):
                cost_model_from_config(cfg, "NQ_DUKASCOPY", provider=prov)

    def test_source_verified_needs_evidence(self):
        cfg = copy.deepcopy(self.svc.cfg)
        cfg["instruments"]["NQ_DUKASCOPY"]["identity_status"] = "source_verified"
        self.assertIn("identity_evidence", identity_problem(load_instruments(cfg)["NQ_DUKASCOPY"]))


class TestImportRefusals(unittest.TestCase):
    def setUp(self):
        self.root = workspace()
        self.svc = Services(root=self.root)
        self.addCleanup(shutil.rmtree, self.root, True)
        self.addCleanup(self.svc.store.close)
        self.csv = self.root / "d.csv"

    def imp(self, **kw):
        return self.svc.import_file({**OPTS, "file": str(self.csv), **kw})

    def test_mixed_offsets_and_added_hours_are_refused(self):
        write_fixture(self.csv, extra_rows=["2024-03-15T21:30:00,18000.000,18001.000,17999.000,18000.500,0.1"])
        with self.assertRaises(ImportFailed) as cm:
            self.imp()
        self.assertIn("no UTC offset", str(cm.exception))
        write_fixture(self.csv)
        with self.assertRaises(ImportFailed) as cm:
            self.imp(source_timezone="UTC+2h")
        self.assertIn("do not add +Nh", str(cm.exception))
        self.assertEqual(self.svc.list_datasets(), [])

    def test_other_explicit_offsets_convert_exactly(self):
        write_fixture(self.csv)
        a = self.imp(derive_timeframes=[])
        # the same instants written with +02:00 offsets import to the SAME content
        df = pd.read_csv(self.csv)
        ts = pd.to_datetime(df["timestamp"], utc=True).dt.tz_convert("Etc/GMT-2")
        df["timestamp"] = ts.dt.strftime("%Y-%m-%dT%H:%M:%S+02:00")
        other = self.root / "e.csv"
        df.to_csv(other, index=False, float_format="%.4f")
        b = self.svc.import_file({**OPTS, "file": str(other), "derive_timeframes": [], "dataset_name": "X"})
        self.assertEqual(a["manifest"]["content_hash"], b["manifest"]["content_hash"])

    def test_wrong_timezone_shows_as_outside_session_and_is_refused(self):
        write_fixture(self.csv)
        df = pd.read_csv(self.csv)
        df["timestamp"] = (pd.to_datetime(df["timestamp"], utc=True) + pd.Timedelta(hours=1)).dt.strftime("%Y-%m-%dT%H:%M:%S+00:00")
        df.to_csv(self.csv, index=False, float_format="%.4f")
        with self.assertRaises(ImportFailed) as cm:
            self.imp()
        self.assertEqual(cm.exception.stage, "validate")
        self.assertEqual(cm.exception.report.get("bars_outside_session").status, "FAIL")      # 1h shift = DST/tz signature
        self.assertEqual(self.svc.list_datasets(), [])

    def test_duplicates_are_cleaned_or_refused_never_guessed(self):
        write_fixture(self.csv)
        lines = self.csv.read_text().splitlines()
        self.csv.write_text("\n".join(lines + [lines[100]]) + "\n")                        # exact duplicate row
        r = self.imp(derive_timeframes=[])
        self.assertEqual(r["manifest"]["source_detail"]["raw_duplicate_bars"], 1)
        t, *_ = lines[100].split(",")
        self.csv.write_text("\n".join(lines + [f"{t},1.000,2.000,0.500,1.500,0.1"]) + "\n")  # conflicting duplicate
        with self.assertRaises(ImportFailed):
            self.imp(derive_timeframes=[], dataset_name="Y")

    def test_coverage_failure_stores_nothing(self):
        allm = session_minutes("2024-03-03", "2024-03-16")
        td = (allm.tz_convert("America/New_York").tz_localize(None) + pd.Timedelta(hours=6)).normalize()
        write_fixture(self.csv, drop=allm[td == pd.Timestamp("2024-03-06")])                # 10% missing in 2 weeks
        with self.assertRaises(ImportFailed):
            self.imp()
        self.assertEqual(self.svc.list_datasets(), [])


class TestPreferredDataset(unittest.TestCase):
    """The preference is a workspace default for NEW research; it never changes stored research."""

    @classmethod
    def setUpClass(cls):
        from tests.test_workspace import make_workspace, tree_fingerprint
        cls.root = Path(tempfile.mkdtemp())
        cls.made = make_workspace(cls.root)                                                  # runnable dataset + a run
        svc = Services(root=cls.root)
        csv = cls.root / "d.csv"
        write_fixture(csv, end="2024-04-27")
        svc.import_file({**OPTS, "file": str(csv)})
        cls.duka5 = next(d["dataset_id"] for d in svc.list_datasets() if d["provider"] == "DUKASCOPY" and d["timeframe"] == "5m")
        svc.store.close()
        cls.fingerprint = staticmethod(tree_fingerprint)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def test_persistence_switching_and_runs_untouched(self):
        s = self.svc()
        self.assertEqual(s.preferred_dataset()["state"], "unset")
        before_runs = json.dumps(s.list_runs(), sort_keys=True, default=str)
        before_tree = self.fingerprint(self.root)
        s.set_preferred_dataset(self.duka5)
        s2 = self.svc()                                                                       # a new process/app
        self.assertEqual(s2.preferred_dataset_id(), self.duka5)
        rd = s2.backtest_readiness(EMA.read_text())
        self.assertEqual(rd["preferred_dataset_id"], self.duka5)
        self.assertTrue(next(d for d in rd["datasets"] if d["dataset_id"] == self.duka5)["preferred"])
        s2.set_preferred_dataset(self.made["dataset_id"])
        s2.clear_preferred_dataset()
        s2.set_preferred_dataset(self.made["dataset_id"])
        p = s2.preferred_dataset()
        self.assertEqual(p["preferred"]["dataset_id"], self.made["dataset_id"])
        self.assertEqual([h["dataset_id"] for h in p["history"]], [self.duka5, self.made["dataset_id"], None, self.made["dataset_id"]])
        # stored research is byte-identical: runs, datasets, strategies (the preference file is not part of it)
        after_tree = {k: v for k, v in self.fingerprint(self.root).items() if not k.endswith("workspace_preferences.json")}
        self.assertEqual(after_tree, {k: v for k, v in before_tree.items() if not k.endswith("workspace_preferences.json")})
        self.assertEqual(json.dumps(s2.list_runs(), sort_keys=True, default=str), before_runs)
        self.assertTrue((self.root / "data" / "workspace_preferences.json").exists())
        # a new backtest is ordinary: it records the dataset it was actually given
        out = s2.backtest_strategy(EMA.read_text(), self.made["dataset_id"], record=True)
        self.assertEqual(s2.get_run(out["run_id"])["record"]["dataset"]["dataset_id"], self.made["dataset_id"])

    def test_only_stored_valid_datasets_are_eligible(self):
        s = self.svc()
        with self.assertRaises(KeyError):
            s.set_preferred_dataset("NOPE_5M_0000000000")
        with self.assertRaises(ValueError):
            s.set_preferred_dataset("")

    def test_web_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        r = c.post("/api/preferences/research-dataset", json={"dataset_id": self.duka5})
        self.assertEqual(r.status_code, 200, r.get_json())
        self.assertEqual(r.get_json()["preferred"]["dataset_id"], self.duka5)
        rows = c.get("/api/datasets").get_json()
        self.assertTrue(next(d for d in rows if d["dataset_id"] == self.duka5)["preferred"])
        self.assertEqual(next(d for d in rows if d["dataset_id"] == self.duka5)["identity"]["identity_status"], "provisional")
        q = c.get(f"/api/datasets/{self.duka5}/quality")
        self.assertEqual(q.status_code, 200)
        self.assertIn("largest_gaps", q.get_json())
        bt = c.post("/api/backtests", json={"strategy": self.made["strategy_id"], "dataset_id": self.duka5})
        self.assertEqual((bt.status_code, bt.get_json()["error"]["kind"]), (409, "instrument_identity"))
        self.assertEqual(c.post("/api/preferences/research-dataset", json={"dataset_id": "../x"}).status_code, 400)
        r = c.post("/api/preferences/research-dataset", json={"dataset_id": None})
        self.assertEqual(r.get_json()["state"], "unset")


if __name__ == "__main__":
    unittest.main()
