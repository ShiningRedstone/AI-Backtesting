"""ADR-87: the flip scan. SYNTHETIC data only (the Dukascopy-shaped BID/ASK fixture of the holdout tests).

- the mirror: known-answer geometry on the engine's own signal arrays (same signal bars, opposite direction, the
  original target is the mirror's stop and the original stop its target, stop entries become limit entries, signal
  exits swap sides); trailing / no-progress / no-target strategies are refused by name;
- the companion flip protocol: created only for an active parent with recorded trials, its multiplicity family is
  the parent's declared budget plus its own, flipped strategies are governed by it wherever they are evaluated and
  never by the parent (and ordinary strategies never by it); it is retired with its parent;
- the scan: worst clearly-negative-before-costs discovery results only, holdout-tested and flipped strategies
  excluded, duplicates of already-known logic skipped, the cap respected; flipped runs go through the normal search
  path (one trial each in the flip protocol), and flipped survivors reach the holdout gate of the flip protocol."""
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd
import yaml

from edgelab.research import protocol as rp
from edgelab.research.protocol import ProtocolRefusal
from edgelab.services import Services
from edgelab.strategy import mirror as M
from tests.dukascopy_fixture import write_fixture

REPO = Path(__file__).resolve().parents[1]
FX = REPO / "strategies" / "fixtures"
DISC, HOLD = ("2024-03-04", "2024-04-05"), ("2024-04-08", "2024-04-26")
MIRRORABLE = ("ema_crossover.yaml", "atr_breakout.yaml", "fvg_entry.yaml", "mtf_trend_filter.yaml",
              "opening_range_breakout.yaml", "structure_bos.yaml")          # vwap_reclaim: VWAP is refused on Dukascopy volume


def fixture(name):
    return yaml.safe_load((FX / name).read_text())


class FlipBase(unittest.TestCase):
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
        r = svc.import_file(dict(
            file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY",
            asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m", derive_timeframes=["5m"],
            build_features=False, bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
            ask_high_column="ask_high", ask_low_column="ask_low", dataset_name="DUKA_SYN"))
        cls.src1m, cls.d5 = r["dataset_id"], r["derived"][0]
        cls.ids = {name: svc.save_strategy(fixture(name))["strategy_id"] for name in MIRRORABLE + ("rsi_threshold.yaml",)}
        svc.store.close()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def svc(self):
        s = Services(root=self.root)
        self.addCleanup(s.store.close)
        return s


class TestMirrorGeometry(FlipBase):
    """The mirror on the engine's own arrays: every signal bar the same, every level on the other side."""

    def signals(self, s, defn):
        from edgelab.features.strategy_api import FeatureContext
        from edgelab.strategy.compiler import compile_strategy
        ds = s.load_dataset(self.d5)
        strat = compile_strategy(defn, s.sessions, s._config_hash())
        return strat, strat.bind(FeatureContext(ds, s.sessions, s.cache)).generate_signals(ds.bars)

    def test_known_answer_geometry_of_every_mirrorable_fixture(self):
        s = self.svc()
        any_signals = 0
        for name in MIRRORABLE:
            with self.subTest(name):
                orig = fixture(name)
                mir = M.mirror_definition(orig, parent_strategy_id="STR_TEST")
                co, so = self.signals(s, orig)
                cm, sm = self.signals(s, mir)
                any_signals += int((so.direction != 0).sum())
                np.testing.assert_array_equal(sm.direction, -so.direction)               # same bars, other side
                np.testing.assert_array_equal(sm.entry_price, so.entry_price)            # same order price
                np.testing.assert_allclose(sm.stop_price, so.target_price, rtol=0, atol=1e-9, equal_nan=True)
                np.testing.assert_allclose(sm.target_price, so.stop_price, rtol=0, atol=1e-9, equal_nan=True)
                oo, om = co.compiled.order, cm.compiled.order
                self.assertEqual(om.stop_points, oo.target_points)                        # points: from the fill
                self.assertEqual(om.target_points, oo.stop_points)
                self.assertEqual(om.entry_type, M.ORDER[oo.entry_type])                   # stop <-> limit
                self.assertEqual((om.time_exit_bars, om.max_hold_bars, om.entry_expiry_bars),
                                 (oo.time_exit_bars, oo.max_hold_bars, oo.entry_expiry_bars))
                self.assertEqual(cm.sizing, co.sizing)
                for a, b in ((sm.exit_long, so.exit_short), (sm.exit_short, so.exit_long)):
                    if a is None or b is None:
                        self.assertTrue((a is None or not a.any()) and (b is None or not b.any()))
                    else:
                        np.testing.assert_array_equal(a, b)
        self.assertGreater(any_signals, 0, "the synthetic data must produce signals for the geometry to mean anything")

    def test_risk_reward_with_points_stop_is_resolved(self):
        d = fixture("mtf_trend_filter.yaml")
        d["exit"]["target"] = {"type": "risk_reward", "multiple": 2.5}
        m = M.mirror_definition(d)
        self.assertEqual(m["exit"]["stop"], {"type": "points", "points": 50.0})       # 2.5 x 20
        self.assertEqual(m["exit"]["target"], {"type": "points", "points": 20.0})

    def test_mirror_of_a_mirror_is_the_original_logic(self):
        from edgelab.strategy.compiler import compile_definition
        s = self.svc()
        for name in ("atr_breakout.yaml", "mtf_trend_filter.yaml"):                    # no risk/reward to resolve
            d = fixture(name)
            back = M.mirror_definition(M.mirror_definition(d))
            self.assertEqual(compile_definition(back, s.sessions, s._config_hash()).identity.logic_hash,
                             compile_definition(d, s.sessions, s._config_hash()).identity.logic_hash)

    def test_texts_mark_the_mirror_but_never_the_logic(self):
        d = fixture("atr_breakout.yaml")
        m = M.mirror_definition(d, parent_strategy_id="STR_ABC")
        self.assertTrue(m["name"].endswith("_flipped"))
        self.assertIn("STR_ABC", m["description"])
        self.assertNotIn("parameters", m)                                             # resolved first
        self.assertEqual(m["entry"]["order"], {"type": "limit", "long_price": {"bar": "low"},
                                               "short_price": {"bar": "high"}, "expiry_bars": 2})

    def test_reentry_block_swaps_stop_and_target(self):
        d = fixture("mtf_trend_filter.yaml")
        d["entry"]["reentry"] = {"block_day_after": "stop", "cooldown_bars": 2}
        self.assertEqual(M.mirror_definition(d)["entry"]["reentry"], {"block_day_after": "target", "cooldown_bars": 2})

    def test_refused_by_name(self):
        self.assertEqual(M.refusal_reason(fixture("rsi_threshold.yaml")).code, "MIRROR_NO_TARGET")
        d = fixture("atr_breakout.yaml")
        d["exit"]["trailing"] = {"mode": "breakeven", "breakeven": {"trigger": {"type": "r", "value": 0.5}}}
        with self.assertRaises(M.MirrorRefusal) as cm:
            M.mirror_definition(d)
        self.assertEqual(cm.exception.code, "MIRROR_TRAILING")
        d = fixture("atr_breakout.yaml")
        d["exit"]["no_progress"] = {"bars": 6, "min_progress": {"type": "points", "value": 5}}
        self.assertEqual(M.refusal_reason(d).code, "MIRROR_NO_PROGRESS")
        self.assertIsNone(M.refusal_reason(fixture("atr_breakout.yaml")))



class ScanBase(FlipBase):
    """A fresh ACTIVE protocol on the 1-minute source and a discovery search of the fixtures on 5m. The synthetic data
    has no real edge either way, so the "clearly negative before costs" threshold is loosened IN THESE TESTS ONLY
    (every result with trades qualifies); the rule itself is tested on known numbers in TestSelectionRule."""
    ORIGINALS = ("ema_crossover.yaml", "atr_breakout.yaml", "mtf_trend_filter.yaml", "structure_bos.yaml",
                 "fvg_entry.yaml", "rsi_threshold.yaml")

    def setUp(self):
        from edgelab.research import flips as F
        for k, v in (("Z_CLEAR", -1e12), ("MIN_TRADES", 1)):
            pt = mock.patch.object(F, k, v)
            pt.start()
            self.addCleanup(pt.stop)
        self.F = F
        self.work = Path(tempfile.mkdtemp())                     # each test: its own copy of the workspace
        self.addCleanup(shutil.rmtree, self.work, True)
        shutil.copytree(self.root, self.work, dirs_exist_ok=True)

    def svc(self):
        s = Services(root=self.work)
        self.addCleanup(s.store.close)
        return s

    def protocol(self, s, name):
        from edgelab.research.campaign import discovery_period
        for p in s.store.list_protocols(status="ACTIVE"):
            s.retire_protocol(p["protocol_id"])
        p = s.create_protocol(self.src1m, DISC, HOLD, name=name)
        per = discovery_period(p["material"])
        srch = s.run_search({"strategies": {"ids": [self.ids[n] for n in self.ORIGINALS]}, "datasets": [self.d5],
                             "period": {"start": per["start"], "end": per["end"]}})
        return p, srch["search_id"], per

    def refused(self, code, fn, *a, **kw):
        with self.assertRaises((ProtocolRefusal, self.F.FlipError)) as cm:
            fn(*a, **kw)
        self.assertEqual(cm.exception.code, code, cm.exception.to_dict())
        return cm.exception


class TestFlipScan(ScanBase):
    def test_preview_create_run_and_route(self):
        s = self.svc()
        p, sid, per = self.protocol(s, "flip-e2e")
        pid = p["protocol_id"]
        parent_trials = s.protocol_status(pid)["trials"]["unique_numerical_trials"]
        pv = s.flip_scan(cap=3)
        self.assertEqual(pv["state"], "preview")
        sel = pv["selection"]
        self.assertEqual(sel["n_selected"], 3)
        rows = {r["strategy_id"]: r for r in sel["rows"]}
        rsi = rows.get(self.ids["rsi_threshold.yaml"])
        if rsi is not None and rsi["skip"] is not None:
            self.assertEqual(rsi["skip"], "MIRROR_NO_TARGET")
        self.assertEqual(s.protocol_status(pid)["trials"]["unique_numerical_trials"], parent_trials)   # read-only
        self.assertEqual(len(s.list_protocols()), len([x for x in s.store.list_protocols()]))

        out = s.create_flip_scan(cap=3)
        fid = out["flip_protocol_id"]
        flip = s.get_protocol(fid)
        mat = flip["material"]
        self.assertTrue(rp.is_flip(flip))
        self.assertEqual(mat["parent"]["protocol_id"], pid)
        self.assertEqual(mat["trial_budget"]["max_unique_trials"], 3)
        self.assertEqual(rp.family_size(mat, 0), rp.DEFAULT_TRIAL_BUDGET + 3)               # parent budget + flips
        self.assertEqual(mat["acceptance_criteria"]["oos_confidence"]["bootstrap"]["replicates"],
                         rp.bootstrap_replicates_for(rp.DEFAULT_TRIAL_BUDGET + 3))
        self.assertEqual((mat["windows"], mat["config_hash"]), (p["material"]["windows"], p["material"]["config_hash"]))
        self.refused("FLIP_EXISTS", s.create_flip_scan, cap=3)                              # one per protocol
        from edgelab.research import campaign as C
        self.assertEqual(C.governing_protocol(s)["protocol_id"], pid)                       # never the flip protocol

        flips = [m["strategy_id"] for m in mat["mirror_set"]]
        for m in mat["mirror_set"]:
            doc = s.library.load(m["strategy_id"])
            self.assertEqual(doc["logic_hash"], m["logic_hash"])
            lin = doc["lineage"][0]
            self.assertEqual((lin["generation_method"], lin["parent_strategy_id"]), ("mirror", m["mirror_of"]["strategy_id"]))
        from edgelab.research import overview as ov
        fac = {f["strategy_id"]: f for f in ov.library_facets(s)}
        self.assertTrue(all(fac[x]["display_name"].startswith("Flipped · ") for x in flips))
        self.assertTrue(all(fac[x]["mirror_of"] for x in flips))

        r = self.F.run(s, fid)
        b = s.store.get_search_batch(r["search_id"])
        self.assertEqual(b["protocol_id"], fid)                                             # governed by the flip protocol
        self.assertEqual(s.protocol_status(fid)["trials"]["unique_numerical_trials"], 3)    # one trial per flip
        self.assertEqual(s.protocol_status(pid)["trials"]["unique_numerical_trials"], parent_trials)
        res = s.flip_scan()
        self.assertEqual(res["state"], "created")
        self.assertEqual(res["flip"]["counts"]["completed"], 3)
        for row in res["flip"]["rows"]:
            self.assertEqual(row["flip"]["status"], "completed")
            self.assertIsNotNone(row["flip"]["run_id"])
            self.assertIsNotNone(row["original"]["run_id"])
            rec, _ = s.store.load_run(row["flip"]["run_id"])
            self.assertEqual(rec["status"], "IN_SAMPLE")
        again = self.F.run(s, fid)                                                          # resumable, no new trial
        self.assertEqual(s.protocol_status(fid)["trials"]["unique_numerical_trials"], 3)
        self.assertEqual(again["search_id"], r["search_id"])

        # a flip evaluated anywhere is governed by the flip protocol; an original by the parent
        s.backtest_strategy(flips[0], self.d5, False, (per["start"], per["end"]))
        ev = s.store.list_trial_events(fid)
        self.assertEqual(ev[-1]["strategy_id"], flips[0])
        self.assertEqual(sum(e["counted"] for e in ev), 3)                                  # a duplicate, not a trial
        n_parent_events = len(s.store.list_trial_events(pid))
        s.backtest_strategy(self.ids["atr_breakout.yaml"], self.d5, False, (per["start"], per["end"]))
        self.assertEqual(len(s.store.list_trial_events(pid)), n_parent_events + 1)
        self.refused("PROTOCOL_MISMATCH", s.run_search, {"strategies": {"ids": [flips[0], self.ids["atr_breakout.yaml"]]},
                                                         "datasets": [self.d5],
                                                         "period": {"start": per["start"], "end": per["end"]}})

        # the holdout gate of the flip protocol: its own looks, its family
        from edgelab.research import holdout as H
        ref = {"instrument": "NQ_DUKASCOPY", "provider": "DUKASCOPY"}
        self.assertEqual(H._protocol_of(s, ref, mat["mirror_set"][0]["logic_hash"])["protocol_id"], fid)
        self.assertEqual(H._protocol_of(s, ref, None)["protocol_id"], pid)
        s.select_shortlist(r["search_id"], [flips[0]])
        self.refused("HOLDOUT_SEARCH_NOT_IN_PROTOCOL", s.evaluate_holdout, pid, r["search_id"], flips[0])
        hv = s.evaluate_holdout(fid, r["search_id"], flips[0])
        self.assertIn(hv["outcome"], ("HOLDOUT_CRITERIA_MET", "HOLDOUT_CRITERIA_NOT_MET"))
        self.assertEqual(hv["multiple_testing"]["family_size"], rp.DEFAULT_TRIAL_BUDGET + 3)
        self.assertEqual([a["status"] for a in s.store.list_holdout_access(fid)][-1], "completed")
        self.assertFalse([a for a in s.store.list_holdout_access(pid) if a["status"] != "refused"])
        self.assertEqual(s.flip_scan()["flip"]["holdout"]["looks_used"], 1)

        # retiring the parent retires its flip protocol
        s.retire_protocol(pid)
        self.assertEqual(s.get_protocol(fid)["status"], "RETIRED")

    def test_holdout_tested_and_duplicate_logic_are_skipped(self):
        s = self.svc()
        p, sid, per = self.protocol(s, "flip-skips")
        pid = p["protocol_id"]
        atr = self.ids["atr_breakout.yaml"]
        s.select_shortlist(sid, [atr])
        s.evaluate_holdout(pid, sid, atr)                                                   # an original is holdout-tested
        mtf = fixture("mtf_trend_filter.yaml")
        dup = s.save_strategy(M.mirror_definition(mtf))["strategy_id"]                       # its mirror already exists
        sel = s.flip_scan(cap=50)["selection"]
        rows = {r["strategy_id"]: r for r in sel["rows"]}
        self.assertEqual(rows[atr]["skip"], "HOLDOUT_TESTED")
        self.assertEqual((rows[self.ids["mtf_trend_filter.yaml"]]["skip"], rows[self.ids["mtf_trend_filter.yaml"]]["duplicate_of"]),
                         ("DUPLICATE", dup))
        self.assertFalse(any(m["mirror_of"]["strategy_id"] in (atr, self.ids["mtf_trend_filter.yaml"]) for m in sel["selected"]))

    def test_background_job_one_at_a_time(self):
        s = self.svc()
        self.protocol(s, "flip-job")
        self.refused("NO_FLIP_SCAN", s.start_flip_job)
        s.create_flip_scan(cap=2)
        j = s.start_flip_job()
        self.assertEqual(j["kind"], "flip")
        from edgelab.research.jobs import JobConflict
        if s.jobs.active()["state"] in ("queued", "running"):
            with self.assertRaises(JobConflict):
                s.start_flip_job()
        self.assertTrue(s.jobs.join(600))
        done = s.flip_job(j["job_id"])
        self.assertEqual(done["state"], "completed", done.get("error"))
        self.assertEqual(done["live"]["completed"] + done["live"]["skipped"], 2)
        self.assertEqual(s.flip_scan()["flip"]["counts"]["completed"], 2)


class TestFlipApi(ScanBase):
    """The HTTP routes are thin: data-only inputs, refusals with machine-readable codes."""

    def test_routes(self):
        from edgelab.web.app import create_app
        s = self.svc()
        self.protocol(s, "flip-api")
        s.store.close()
        app = create_app(self.work)
        c = app.test_client()
        self.addCleanup(lambda: app.config["EDGELAB"]["services"].store.close())
        v = c.get("/api/flips?cap=2").get_json()
        self.assertEqual((v["state"], v["selection"]["n_selected"]), ("preview", 2))
        self.assertEqual(c.get("/api/flips?cap=abc").status_code, 400)
        self.assertEqual(c.post("/api/flips", json={"cap": 0}).status_code, 422)            # CAP_INVALID
        self.assertEqual(c.post("/api/flips", json={"cap": 2, "holdout_looks": 0}).status_code, 400)
        r = c.post("/api/flips", json={"cap": 2})
        self.assertEqual(r.status_code, 201, r.get_json())
        dup = c.post("/api/flips", json={"cap": 2})
        self.assertEqual((dup.status_code, dup.get_json()["error"]["code"]), (409, "FLIP_EXISTS"))
        j = c.post("/api/flips/jobs", json={})
        self.assertEqual(j.status_code, 202, j.get_json())
        jid = j.get_json()["job_id"]
        self.assertTrue(app.config["EDGELAB"]["services"].jobs.join(600))
        self.assertEqual(c.get(f"/api/flips/jobs/{jid}").get_json()["state"], "completed")
        self.assertEqual(c.get("/api/flips").get_json()["flip"]["counts"]["completed"], 2)
        self.assertEqual(c.get("/api/flips/jobs/nope").status_code, 400)

    def test_no_protocol_is_a_state_not_an_error(self):
        s = self.svc()
        for p in s.store.list_protocols(status="ACTIVE"):
            s.retire_protocol(p["protocol_id"])
        self.assertEqual(s.flip_scan()["state"], "no_protocol")

class TestSelectionRule(unittest.TestCase):
    """The "clearly negative before costs" rule on known numbers (no data, no engine)."""

    def test_upper_bound_known_answers(self):
        from edgelab.research import flips as F
        self.assertAlmostEqual(F.upper_bound({"n": 100, "mean": -0.2, "sd": 1.0}), -0.2 + 0.16448536269514722)
        self.assertGreater(F.upper_bound({"n": 100, "mean": -0.1, "sd": 1.0}), 0)          # not clearly negative
        self.assertIsNone(F.upper_bound({"n": 1, "mean": -1.0, "sd": float("nan")}))

    def test_family_size_rule(self):
        mat = {"multiple_testing": {"family_size_rule": rp.FLIP_FAMILY_RULE},
               "trial_budget": {"max_unique_trials": 200}, "parent": {"trial_budget": 10_000}}
        self.assertEqual(rp.family_size(mat, 0), 10_200)
        self.assertEqual(rp.family_size(mat, 10_500), 10_500)                               # never below the counted

if __name__ == "__main__":
    unittest.main()
