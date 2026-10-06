"""My strategy holdout allowance (ADR-102): one strategy, an AUTOMATIC look and then a MANUAL look.

Guarantees tested: the allowance is a companion protocol of its own (2 looks; never the governing protocol) whose family is
the source protocols' budgets; only settings counted as a discovery try of My strategy or the autotuner can be tested, from
a backtest or an autotuner result; the automatic look runs the ONE engine with the lookahead check and records an
OUT_OF_SAMPLE run; the manual look needs the automatic one first, tests the SAME settings, starts from its report, and its
"with your decisions" result keeps every taken trade identical to the automatic one; each look can be used once, and
nothing is spent when a start is refused; the looks of the earlier protocols stay recorded. All data is SYNTHETIC."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.mystrategy import optimizer as O
from edgelab.mystrategy import review as RV
from edgelab.mystrategy import runner as R
from tests.test_my_strategy import DISC, HOLD, REPO

LOOSE = {"draw.required": False, "eq.enabled": False, "ifg.displacement": False, "day.stop_after_win": False}


class TestHoldoutAllowance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from tests.dukascopy_fixture import write_fixture
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, end="2024-04-27")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        svc = Services(root=cls.root)
        imp = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY", asset_type="CFD",
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m", "15m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        cls.parent = svc.create_protocol(imp["derived"][0], DISC, HOLD, name="parent", exposure_statement="none",
                                         trial_budget=20)
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_automatic_then_manual(self):
        from edgelab.research import campaign
        from edgelab.services import Services
        svc = Services(root=self.root)
        sm = R.backtest(svc, LOOSE, label="start", lock=svc.lock)
        # an autotuner result to pick from (a short run)
        svc.set_ui_preferences({"prop_fees": {"LUCID_LUCIDFLEX_50K": {"eval_price": 100}}})
        svc.my_autotune_start(sm["id"], 1, 6)
        while O.run_of(svc).info()["running"]:
            time.sleep(0.3)
        self.assertIsNone(O.run_of(svc).info()["error"])
        cands = RV.candidates(svc)
        self.assertEqual(cands[0]["ref"], f"bt:{sm['id']}")
        self.assertEqual(len({c["settings_hash"] for c in cands}), len(cands))
        opt = [c for c in cands if c["source"] == "autotuner"]
        ref = opt[0]["ref"] if opt else f"bt:{sm['id']}"
        sm2 = R.backtest(svc, {**LOOSE, "stop.buffer": 2.0}, label="second", lock=svc.lock)
        refs = [ref, f"bt:{sm2['id']}"]                    # an autotuner result (when it found one) and a backtest
        from edgelab.mystrategy import params as P
        from edgelab.research import protocol as rp
        # an allowance of the first version (ADR-102: 1 strategy, 2 looks) with a look already spent: the reset
        parent = R._parent(svc)
        old = RV.holdout_material(svc, parent)
        old.pop("allowance_version")
        old["holdout_budget"] = {"max_unique_candidate_evaluations": 2, "per_candidate": 2, "looks": ["automatic", "manual"]}
        rec = rp.make_record(old, {"code_version": "test"})
        svc.store.save_protocol(rec, RV._hp_scope(parent))
        svc.store.add_holdout_access({"access_id": "HA_AUTOMATIC_OLD", "protocol_id": rec["protocol_id"], "strategy_id": "x",
                                      "logic_hash": "x", "definition_hash": "x", "frozen_hash": "x", "search_id": None,
                                      "status": "completed", "reason_code": None, "reason": None, "run_id": None,
                                      "result_json": None, "created_at": R._now(), "completed_at": R._now()})
        R._write_json(RV._allowance_path(svc), {"protocol_id": rec["protocol_id"], "automatic": {"report": "HO_x"}})
        self.assertIsNone(RV.holdout_protocol(svc, create=False)[1])                 # a page read changes nothing
        self.assertEqual(RV.allowance_view(svc)["strategies"], [])
        # refusals spend nothing: the manual look first, an unknown backtest
        with self.assertRaises(R.MyStrategyError) as e:
            RV.start_manual(svc, 1, lock=svc.lock)
        self.assertEqual(e.exception.code, "AUTOMATIC_FIRST")
        with self.assertRaises(R.MyStrategyError):
            RV.start_automatic(svc, "bt:BT_20200101_000000_abcd", lock=svc.lock)
        _, hp = RV.holdout_protocol(svc, svc.lock)                                   # the reset happens here
        self.assertNotEqual(hp["protocol_id"], rec["protocol_id"])
        self.assertEqual(svc.store.get_protocol(rec["protocol_id"])["status"], "RETIRED")
        self.assertEqual(len(svc.store.list_holdout_access(rec["protocol_id"])), 1)  # the old look stays recorded
        self.assertIn("spent before (source protocols and earlier allowances): 1", hp["material"]["pre_protocol_exposure"]["statement"])
        self.assertEqual(svc.store.list_holdout_access(hp["protocol_id"]), [])
        mat = hp["material"]
        self.assertEqual(mat["role"], "my_holdout")
        self.assertEqual(mat["holdout_budget"]["max_unique_candidate_evaluations"], 4)
        self.assertEqual(mat["trial_budget"]["max_unique_trials"], 300 + 5000)      # the family the strategies came from
        self.assertEqual(campaign.governing_protocol(svc)["protocol_id"], self.parent["protocol_id"])
        my_looks = R.protocol_info(svc)["holdout_looks_used"]
        for k, ref in enumerate(refs, start=1):
            s_ref, _ = RV._resolve_ref(svc, ref)
            # look 1 of this strategy: automatic
            slot = RV.start_automatic(svc, ref, lock=svc.lock)
            self.assertEqual((slot["n"], slot["settings_hash"]), (k, P.settings_hash(s_ref)))
            au = svc.my_strategy_backtest(slot["automatic"]["report"])
            self.assertEqual(au["kind"], "holdout_mechanical")
            self.assertTrue(au["causality_passed"])
            self.assertEqual(svc.store.load_run(au["run_id"])[0]["status"], "OUT_OF_SAMPLE")
            self.assertGreaterEqual(pd.Timestamp(au["window"]["start"]), pd.Timestamp(HOLD[0], tz="UTC") - pd.Timedelta(days=1))
            with self.assertRaises(R.MyStrategyError) as e:
                RV.start_automatic(svc, ref, lock=svc.lock)
            self.assertEqual(e.exception.code, "HOLDOUT_LOOK_USED" if k == 1 else "HOLDOUT_STRATEGIES_USED")
            # look 2: manual, same settings, from the automatic report
            st = RV.start_manual(svc, k, lock=svc.lock)
            self.assertEqual((st["settings_hash"], st["mechanical_report"]), (slot["settings_hash"], slot["automatic"]["report"]))
            decided = 0
            while True:
                v = RV.allowance_view(svc)
                if v["review"]["status"] == "complete":
                    break
                take = decided % 2 == 0
                d = RV.decide(svc, v["candidate"]["signal_bar"], take)
                self.assertEqual(d["take"], take)
                if not take and "outcome" in d:              # a skip shows what it would have done
                    self.assertEqual(d["outcome"]["signal_bar"], v["candidate"]["signal_bar"])
                decided += 1
            self.assertGreater(decided, 0)
            fin = svc.my_strategy_backtest(v["review"]["final_report"])
            self.assertEqual(fin["kind"], "holdout_with_decisions")
            taken = {int(x) for x, dd in fin["decisions"].items() if dd["take"]}
            mine = R.read_gz(R.home(svc) / "holdout" / fin["id"] / "trades.json.gz", [])
            self.assertLessEqual({int(t["signal_bar"]) for t in mine}, taken)      # only the setups you took
            with self.assertRaises(R.MyStrategyError):
                RV.start_manual(svc, k, lock=svc.lock)
        v = RV.allowance_view(svc)
        self.assertEqual([x["review_status"] for x in v["strategies"]], ["complete"] * len(refs))
        acc = svc.store.list_holdout_access(hp["protocol_id"])
        self.assertEqual(sorted(x["access_id"].split("_")[1] for x in acc), ["AUTOMATIC", "AUTOMATIC", "MANUAL", "MANUAL"])
        self.assertEqual(v["candidates"], [])                # a third strategy is refused
        with self.assertRaises(R.MyStrategyError) as e:
            RV._spend(svc, hp, "automatic", "another", svc.lock)
        self.assertEqual(e.exception.code, "HOLDOUT_STRATEGIES_USED")
        self.assertEqual(R.protocol_info(svc)["holdout_looks_used"], my_looks)
        svc.store.close()

    def test_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        self.assertEqual(c.get("/api/my/holdout").status_code, 200)
        self.assertEqual(c.post("/api/my/holdout/automatic", json={"ref": "../x"}).status_code, 400)
        self.assertEqual(c.post("/api/my/holdout/automatic", json={}).status_code, 400)
        self.assertEqual(c.post("/api/my/holdout/manual", json={"strategy": 3}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
