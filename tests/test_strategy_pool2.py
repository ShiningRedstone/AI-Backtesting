"""Strategy pool 2 (ADR-87): pool 1 stays byte-identical; pool 2's frozen design; the two-step workspace flow (generate,
then switch to one 20,000-trial protocol with both pools as sibling campaigns). SYNTHETIC data only."""
from __future__ import annotations

import shutil
import tempfile
import unittest
from collections import Counter
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.research import campaign as C
from edgelab.research import pool2
from edgelab.research import protocol as rp
from edgelab.services import Services
from edgelab.strategy import factory as FX
from edgelab.strategy import factory_space as S
from edgelab.strategy import factory_space_p2 as P2
from tests.dukascopy_fixture import write_fixture
from tests.test_research_protocol import DISC, HOLD

REPO = Path(__file__).resolve().parents[1]
POOL1_MANIFEST = "FM_3B0B01CFC81AB15E"            # the user's frozen 10,000-strategy pool 1 (ADR-67)


class TestPool1Unchanged(unittest.TestCase):
    def test_pool1_regenerates_to_the_frozen_manifest(self):
        res = FX.generate()                            # the full 10,000 (about 1-2 minutes)
        self.assertEqual(res.manifest_id, POOL1_MANIFEST)
        self.assertEqual(len(res.strategies), 10_000)
        self.assertNotIn("pool", res.header["identity"])
        self.assertEqual(S.VARIATION_SPACE_VERSION, "edgelab-dt-space/5")
        self.assertEqual(len(S.FAMILIES), 30)

    def test_explicit_pool1_space_equals_the_default(self):
        q = {"ema_crossover": 3, "ict_liquidity_fvg": 3, "opening_range_breakout": 3}
        self.assertEqual(FX.generate(quotas=q).manifest_id, FX.generate(quotas=q, S=S).manifest_id)


class TestPool2Design(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.res = FX.generate(P2.DEFAULT_SEED, quotas={f.fid: 3 for f in P2.FAMILIES}, S=P2)

    def test_allocation_is_6000_new_and_4000_existing_equal_shares(self):
        a = P2.allocate()
        self.assertEqual(sum(a.values()), 10_000)
        self.assertEqual(len(P2.NEW_FAMILIES), 25)
        self.assertEqual({a[f.fid] for f in P2.NEW_FAMILIES}, {240})
        old = [a[f.fid] for f in sorted(P2.EXISTING_FAMILIES, key=lambda f: f.num)]
        self.assertEqual(sum(old), 4_000)
        self.assertEqual(old, [134] * 10 + [133] * 20)
        self.assertEqual(len({f.fid for f in P2.FAMILIES}), 55)

    def test_every_family_generates_valid_variants(self):
        got = Counter(r["family_id"] for r in self.res.strategies)
        self.assertEqual(set(got), {f.fid for f in P2.FAMILIES})
        self.assertEqual(set(got.values()), {3})

    def test_existing_family_variants_use_a_new_value_and_sizing_is_risk_based(self):
        for r in self.res.strategies:
            fam = P2.FAMILY_BY_ID[r["family_id"]]
            if fam.fid not in P2.NEW_FAMILY_IDS:
                self.assertTrue(P2.uses_new_value(fam, r["variation"]), r["strategy_id"])
            self.assertIn(r["variation"]["sizing"], P2.SIZING)
            self.assertIn(r["definition"]["sizing"]["mode"], ("risk", "equity_risk"))
            self.assertEqual(r["definition"]["sizing"]["max_quantity"], 40)
            self.assertEqual(r["definition"]["sizing"]["contract"], "MNQ")

    def test_identity_records_the_pool_and_the_excluded_pool(self):
        idn = self.res.header["identity"]
        self.assertEqual(idn["pool"]["pool"], 2)
        self.assertEqual(idn["factory_version"], P2.FACTORY_VERSION)
        self.assertEqual(idn["variation_space_version"], P2.VARIATION_SPACE_VERSION)
        excl = {"manifest_id": "FM_X", "logic_hashes": [self.res.strategies[0]["logic_hash"]]}
        res2 = FX.generate(P2.DEFAULT_SEED, quotas={f.fid: 3 for f in P2.FAMILIES}, S=P2, exclude=excl)
        self.assertNotEqual(res2.manifest_id, self.res.manifest_id)
        self.assertNotIn(excl["logic_hashes"][0], {r["logic_hash"] for r in res2.strategies})
        self.assertTrue(any(str(d["duplicate_of"]).startswith("earlier pool FM_X") for d in res2.duplicates))
        self.assertEqual(len(res2.strategies), len(self.res.strategies))

    def test_contradictory_orders_and_window_bound_filters_are_refused(self):
        fam = P2.FAMILY_BY_ID["pivot_points"]
        base = FX.sample_choice(fam, 1, 0, P2)
        ch = {**base, "family_params": {**base["family_params"], "mode": "fade"}, "order": {"type": "stop", "expiry_bars": 1},
              "entry_delay": 0}
        with self.assertRaises(S.Reject) as cm:
            P2.extra_checks(fam, ch)
        self.assertEqual(cm.exception.code, "ORDER_TYPE_NOT_APPLICABLE")
        ema = P2.FAMILY_BY_ID["ema_crossover"]
        b = FX.sample_choice(ema, 1, 0, P2)
        asia = {**b, "session": "asia", "regime": "gap_large", "order": {"type": "market"}, "entry_delay": 0}
        with self.assertRaises(S.Reject) as cm:
            P2.extra_checks(ema, asia)
        self.assertEqual(cm.exception.code, "FILTER_NEEDS_RTH_ENTRY")
        plain = {**b, "session": "ny_open", "regime": "none", "order": {"type": "market"}, "entry_delay": 0,
                 "stop": {"type": "atr", "multiple": 1.0}, "target": {"type": "rr", "multiple": 2.0}, "time_exit": 60,
                 "mtf": None}
        with self.assertRaises(S.Reject) as cm:
            P2.extra_checks(ema, plain)
        self.assertEqual(cm.exception.code, "POOL2_NO_NEW_VARIABLE")

    def test_holdout_exposure_marker(self):
        rec = {"material": {"pre_protocol_exposure": {"runs": [
            {"run_id": "R1", "strategy_id": "STR_A", "note": "holdout look under RP_OLD (access HA_1, logic x)"},
            {"run_id": "R2", "strategy_id": "STR_B", "note": "an earlier exploratory run"}]}}}
        self.assertEqual(rp.holdout_exposed(rec), {"STR_A"})


class TestPool2Workspace(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
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
            symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m", "15m", "30m", "60m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        src = imp["derived"][0]
        res = FX.generate(seed=4, quotas={f.fid: 1 for f in S.FAMILIES})          # a small SYNTHETIC "pool 1"
        cls.p1 = res.manifest_id
        FX.write_manifest(res, svc.factory_dir / cls.p1)
        p = svc.create_protocol(src, DISC, HOLD, name="pool 1", exposure_statement="none", trial_budget=len(res.strategies))
        cls.old_pid = p["protocol_id"]
        cls.old_cid = svc.campaign_freeze(cls.p1)["campaign_id"]
        ids = sorted(r["strategy_id"] for r in res.strategies)[:2]
        C.run_scope(svc, cls.old_cid, strategy_ids=ids)                           # the old protocol is now USED
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s

    def test_two_step_flow(self):
        s = self.svc()
        st = pool2.status(s)
        self.assertEqual(st["pool1"]["manifest_id"], self.p1)
        self.assertIsNone(st["pool2"])
        self.assertIn("Create strategy pool 2 first.", st["blockers"])
        self.assertEqual(st["old_campaign"]["completed"], 2)
        # ---- step 1: generate (no protocol / ledger change)
        before = len(s.store.list_trial_events(self.old_pid))
        g = pool2.generate(s, quotas={f.fid: 2 for f in P2.FAMILIES})
        self.assertEqual(g["pool1_manifest_id"], self.p1)
        self.assertEqual(g["n_strategies"], 110)
        self.assertEqual(len(s.store.list_trial_events(self.old_pid)), before)
        self.assertTrue(pool2.verify(s, g["manifest_id"])["reproducible"])
        self.assertTrue(s.factory_verify(g["manifest_id"])["reproducible"])
        st = pool2.status(s)
        self.assertEqual(st["pool2"]["manifest_id"], g["manifest_id"])
        self.assertEqual(st["blockers"], [])
        self.assertTrue(any("28 strategies without a result" in w for w in st["warnings"]))
        # a holdout look under the old protocol (recorded directly; the gate itself is tested elsewhere)
        ev = next(e for e in s.store.list_trial_events(self.old_pid) if e["counted"])
        run_id = ev["run_id"]
        self.assertTrue(s.store.has_run(run_id))
        s.store.add_holdout_access({"access_id": "HA_TEST", "protocol_id": self.old_pid, "strategy_id": ev["strategy_id"],
                                    "logic_hash": ev["logic_hash"], "definition_hash": "d", "frozen_hash": "f",
                                    "search_id": ev["search_id"], "status": "completed", "run_id": run_id,
                                    "created_at": "2026-10-02T00:00:00+00:00"})
        # ---- step 2: switch
        with self.assertRaises(pool2.Pool2Error):
            pool2.switch(s, "switch")
        out = pool2.switch(s, pool2.CONFIRM_WORD)
        new = s.get_protocol(out["protocol_id"])
        old = s.get_protocol(self.old_pid)
        self.assertEqual(old["status"], "RETIRED")
        self.assertEqual(new["status"], "ACTIVE")
        self.assertEqual(new["material"]["trial_budget"]["max_unique_trials"], 20_000)
        self.assertEqual(new["material"]["holdout_budget"]["max_unique_candidate_evaluations"],
                         old["material"]["holdout_budget"]["max_unique_candidate_evaluations"] - 1)     # carried over
        for k in ("windows", "execution", "config_hash", "source_dataset", "scope"):
            self.assertEqual(new["material"][k], old["material"][k], k)
        self.assertIn(self.old_pid, new["material"]["pre_protocol_exposure"]["statement"])
        self.assertEqual(rp.holdout_exposed(new), {ev["strategy_id"]})
        cids = {v["campaign_id"] for v in out["campaigns"].values()}
        self.assertEqual(len(cids), 2)
        specs = {cid: C.load(s, cid) for cid in cids}
        for cid, spec in specs.items():
            self.assertEqual(spec["protocol"]["protocol_id"], out["protocol_id"])
            other = [x for x in specs.values() if x is not spec][0]
            self.assertEqual(spec["protocol_siblings"], [{"manifest_id": other["manifest"]["manifest_id"],
                                                          "search_id": other["search"]["search_id"],
                                                          "n_strategies": other["manifest"]["n_strategies"]}])
            rep = C.check(s, cid)
            self.assertTrue(rep["ready"], [c for c in rep["checks"] if not c["ok"]])
        # run one pool-2 strategy: pool 1's preflight still passes (sibling trial, not foreign)
        c2 = next(cid for cid, sp in specs.items() if sp["manifest"]["manifest_id"] == g["manifest_id"])
        c1 = next(cid for cid in cids if cid != c2)
        C.run_scope(s, c2, strategy_ids=[specs[c2]["search"]["spec"]["strategies"]["ids"][0]])
        led = C.ledger(s, specs[c1])
        self.assertEqual((led["sibling_trials"], led["foreign_trials"], led["campaign_trials"]), (1, 0, 0))
        self.assertTrue(C.check(s, c1)["ready"])
        self.assertFalse(C.check(s, self.old_cid)["ready"])                # the retired protocol's campaign
        # idempotent, labelled
        self.assertTrue(pool2.switch(s, pool2.CONFIRM_WORD)["already_switched"])
        self.assertTrue(pool2.status(s)["switched"])
        labels = pool2.run_names(s)
        self.assertEqual(sorted(labels.values()), ["Strategy pool 1", "Strategy pool 1 (retired protocol)", "Strategy pool 2"])
        # the web API
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        r = c.get("/api/pool2")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["switched"])
        self.assertEqual(c.post("/api/pool2/switch", json={"confirm": "nope"}).status_code, 400)
        rows = c.get("/api/campaigns").get_json()
        self.assertEqual(sorted(x["label"] for x in rows), sorted(labels.values()))


if __name__ == "__main__":
    unittest.main()
