"""Strategy autotuner (ADR-97): the 10,000 reasoned combinations of My strategy and their run.

Guarantees tested: the design is deterministic (known-answer fingerprint), has exactly 10,000 unique combinations (by
settings hash) that all resolve, starts from test 37, never changes a setting twice or to an inert value, keeps every
target at 1R or more, and every change carries a reason; the speed-ups of the rule code give exactly the earlier values
(range min / max, recent swing lows); the run goes through the ONE engine with the lookahead check, a combination's
result equals a normal My strategy backtest of the same settings (same trades fingerprint), every combination is one try
in the autotuner's own protocol (a companion that never governs research), a rerun is not a new try, SMT combinations
are refused up front without ES data, and the API refuses bad input. All data is SYNTHETIC."""
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np

from edgelab.core.identity import hash_obj
from edgelab.mystrategy import autotune_space as A
from edgelab.mystrategy import params as P
from tests.test_my_strategy import DISC, HOLD, REPO

DESIGN_HASH = "8d25af78c52d731d"          # known answer: change it only together with AUTOTUNE_VERSION


class TestDesign(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = A.manifest()

    def test_known_answer_and_determinism(self):
        self.assertEqual(self.m["manifest_hash"], DESIGN_HASH)
        self.assertEqual(A.manifest()["manifest_hash"], DESIGN_HASH)
        self.assertEqual(self.m["total"], 10_000)
        self.assertEqual(self.m["counts"], {"base": 1, "single": 173, "pair": 3600, "triple": 3100, "quad": 3126})

    def test_every_combination_is_valid_unique_and_reasoned(self):
        rows = self.m["rows"]
        self.assertEqual(len({r["settings_hash"] for r in rows}), 10_000)
        self.assertEqual(rows[0]["stage"], "base")
        self.assertEqual(rows[0]["overrides"], P.changed(P.resolve(A.BASE)))      # test 37
        by = A.BY_ID
        for r in rows:
            s = P.resolve(r["overrides"])
            self.assertEqual(P.settings_hash(s), r["settings_hash"])
            self.assertLessEqual(len(r["options"]), 4)
            themes = [by[o].theme for o in r["options"]]
            self.assertEqual(len(themes), len(set(themes)))                   # never two changes of one theme
            for o in r["options"]:
                self.assertTrue(by[o].reason)
                for k in by[o].changes:
                    if s[k] != A.BASE_RESOLVED[k]:
                        self.assertTrue(A.INERT_UNLESS.get(k, lambda _s: True)(s), (r["n"], k))
            # the user's minimum: targets of at least 1R (fixed or the liquidity minimum)
            self.assertGreaterEqual(s["target.fixed_r"] if s["target.mode"] == "fixed_r" else s["target.min_r"], 1.0)
        used = {o for r in rows for o in r["options"]}
        self.assertEqual(used, set(by))                                        # every change is tested
        flips = sum(1 for r in rows if r["overrides"].get("models.flip"))
        self.assertGreater(flips, 500)

    def test_option_keys_belong_to_one_theme(self):
        owner = {}
        for o in A.OPTIONS:
            for k in o.changes:
                self.assertEqual(owner.setdefault(k, o.theme), o.theme, k)


class TestSpeedUpsAreIdentical(unittest.TestCase):
    def test_range_queries(self):
        from edgelab.mystrategy.frames import RangeQ
        rng = np.random.default_rng(5)
        x = rng.normal(0, 10, 5000).cumsum()
        q = RangeQ(x)
        for _ in range(3000):
            a = int(rng.integers(0, 5000))
            b = int(rng.integers(a, min(5000, a + int(rng.choice([5, 300, 3000])))))
            self.assertEqual(q.min(a, b), float(np.min(x[a:b + 1])))
            self.assertEqual(q.max(a, b), float(np.max(x[a:b + 1])))
        self.assertTrue(np.isnan(q.min(5, 4)))

    def test_recent_swing_lows_match_the_full_scan(self):
        from edgelab.mystrategy.frames import swings
        from edgelab.mystrategy.logic import Rules
        from tests.test_my_strategy import CAL, random_ds
        ds = random_ds("2024-01-07", "2024-02-20", seed=4)
        r = Rules(ds.bars, CAL, P.resolve(A.BASE))
        for d in (1, -1):
            v = r.views[d]
            for tf in (1, 5, 15):
                t = v.tf[tf]
                sw = swings(t, 2)["lo"]
                for start in range(3000, r.n, 997):
                    old = np.flatnonzero((sw[2] < start) & (t.end[sw[0]] >= start - 2880))[::-1][:30].tolist()
                    self.assertEqual(r._recent_swing_lows(v, t, start), old)


class TestAutotuneRun(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import numpy as np
        import pandas as pd

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
        cls._orig = staticmethod(A.manifest)

    @classmethod
    def tearDownClass(cls):
        A.manifest = cls._orig
        shutil.rmtree(cls.root, ignore_errors=True)

    @staticmethod
    def small(rows_of):
        def m():
            full = TestAutotuneRun._orig()
            rows = rows_of(full["rows"])
            full.update(rows=rows, total=len(rows), manifest_hash=hash_obj([r["settings_hash"] for r in rows], 16))
            return full
        return m

    def test_run_record_rerun(self):
        from edgelab.mystrategy import autotune as AT
        from edgelab.mystrategy import runner as R
        from edgelab.research import campaign
        from edgelab.services import Services
        svc = Services(root=self.root)
        # SMT combinations without ES data: refused before anything runs
        A.manifest = self.small(lambda rows: [r for r in rows if r["overrides"].get("filters.smt")][:1])
        AT.run_of(svc).start(svc, 1, svc.lock)
        while AT.run_of(svc).info()["running"]:
            time.sleep(0.2)
        self.assertEqual(AT.run_of(svc).info()["error"]["kind"], "ES_DATA_REQUIRED")
        self.assertEqual(AT.status(svc)["protocol"]["trials_used"], 0)
        shutil.rmtree(AT.home(svc))
        # three combinations (base, longs only, flip) on two worker processes
        A.manifest = self.small(lambda rows: [rows[0], rows[1], rows[5]])
        AT.run_of(svc).start(svc, 2, svc.lock)
        while AT.run_of(svc).info()["running"]:
            time.sleep(0.5)
        info = AT.run_of(svc).info()
        self.assertIsNone(info["error"], info)
        st = AT.status(svc)
        self.assertEqual((st["done"], st["failed"], st["protocol"]["trials_used"]), (3, 0, 3))
        _, mine = AT.ensure_protocol(svc, create=False)
        self.assertEqual(mine["material"]["role"], "my_autotune")
        self.assertEqual(mine["material"]["trial_budget"]["max_unique_trials"], 10_000)
        self.assertEqual(mine["material"]["holdout_budget"]["max_unique_candidate_evaluations"], 1)
        self.assertEqual(campaign.governing_protocol(svc)["protocol_id"], self.parent["protocol_id"])   # never governs
        res = AT.read_results(svc)
        self.assertTrue(all(r["causality_passed"] for r in res.values()))
        pts = AT.points(svc, "LUCID_LUCIDFLEX_50K")["points"]
        self.assertEqual(len(pts), 3)
        self.assertIn("prop_payouts", pts[0])
        # a combination's result = a normal My strategy backtest of the same settings; the rerun is not a new try
        sm = AT.rerun(svc, 1, lock=svc.lock)
        self.assertEqual(sm["autotune_n"], 1)
        self.assertEqual(svc.store.load_run(sm["run_id"])[0]["trades_hash"], res[1]["trades_hash"])
        self.assertEqual(sm["trade_count"], res[1]["metrics"]["trade_count"])
        self.assertEqual(AT.status(svc)["protocol"]["trials_used"], 3)
        self.assertEqual(R.protocol_info(svc)["trials_used"], 0)                 # My strategy's own 300 tries untouched
        self.assertEqual(AT.detail(svc, 1)["reruns"][0]["id"], sm["id"])
        # continue: nothing left to do, nothing counted twice
        AT.run_of(svc).start(svc, 1, svc.lock)
        while AT.run_of(svc).info()["running"]:
            time.sleep(0.2)
        self.assertEqual(AT.status(svc)["protocol"]["trials_used"], 3)
        # ADR-100: flipped reruns. #1's flip is combination #6 ("Flip every trade"): linked, never a new try
        from edgelab.mystrategy import autotune_flips as AF
        rec = AF.run_flip(svc, 1, lock=svc.lock)
        self.assertEqual((rec["kind"], rec["design_n"]), ("design", 6))
        self.assertEqual(AF.status(svc)["used"], 0)
        # #2 (longs only) flipped: a new strategy, counted in the flip protocol only, through the same engine
        rec = AF.run_flip(svc, 2, lock=svc.lock)
        self.assertEqual(rec["kind"], "flip")
        self.assertTrue(rec["causality_passed"])
        self.assertEqual(AF.status(svc)["used"], 1)
        self.assertEqual(AT.status(svc)["protocol"]["trials_used"], 3)          # the autotuner's budget untouched
        _, fp = AF.ensure_protocol(svc, create=False)
        self.assertEqual(fp["material"]["role"], "my_autotune_flip")
        self.assertEqual(fp["material"]["trial_budget"]["max_unique_trials"], 500)
        self.assertEqual(campaign.governing_protocol(svc)["protocol_id"], self.parent["protocol_id"])
        ov = AF.flipped(AT.frozen_manifest(svc)["rows"][1]["overrides"])[0]
        self.assertTrue(P.resolve(ov)["models.flip"])
        ds, es, s0, e0, td = AT.load_inputs(svc, fp, svc.lock)
        direct = AT.evaluate(svc.cfg, ds, es, str(svc.root), (s0, e0), td, ov)
        self.assertEqual(direct["trades_hash"], rec["trades_hash"])               # = the engine on the flipped settings
        AF.run_flip(svc, 2, lock=svc.lock)
        self.assertEqual(AF.status(svc)["used"], 1)                               # the same flip is never a new try
        pts = AT.points(svc, "LUCID_LUCIDFLEX_50K")
        self.assertEqual({p["flip_of"] for p in pts["flips"]}, {1, 2})
        d = AF.detail(svc, 2)
        self.assertEqual((d["flip_of"], d["row"]["changes"][-1]["option"]), (2, "flip"))
        sm = AF.rerun(svc, 2, lock=svc.lock)                                      # trades + charts, not a new try
        self.assertEqual(sm["autotune_flip_of"], 2)
        self.assertEqual(AF.status(svc)["used"], 1)
        svc.store.close()

    def test_flipped_settings(self):
        """The flip toggles models.flip; breakeven, trailing and limit entries are switched off (no exact mirror); a
        flipped combination flips back to its unflipped version."""
        from edgelab.mystrategy import autotune_flips as AF
        ov, off = AF.flipped({**A.BASE})
        self.assertTrue(P.resolve(ov)["models.flip"])
        self.assertEqual(off, [])                                                  # test 37: breakeven already off
        ov, off = AF.flipped({**A.BASE, "manage.be": "leg_swing", "manage.trail": "swing_1m", "entry.type": "limit_gap"})
        s = P.resolve(ov)
        self.assertEqual((s["manage.be"], s["manage.trail"], s["entry.type"]), ("off", "off", "market"))
        self.assertEqual(len(off), 3)
        back, off2 = AF.flipped(AF.flipped({**A.BASE})[0])
        self.assertEqual(P.settings_hash(back), P.settings_hash(A.BASE))
        self.assertEqual(off2, [])

    def test_api(self):
        from edgelab.web.app import create_app
        A.manifest = self._orig
        c = create_app(self.root).test_client()
        st = c.get("/api/my/autotune")
        self.assertEqual(st.status_code, 200, st.json)
        self.assertEqual(st.json["design"]["total"], 10_000)
        self.assertEqual(c.get("/api/my/autotune/points?profile=../x").status_code, 400)
        self.assertEqual(c.post("/api/my/autotune/start", json={"processes": 0}).status_code, 400)
        self.assertEqual(c.post("/api/my/autotune/start", json={"processes": "x"}).status_code, 400)
        self.assertEqual(c.get("/api/my/autotune/combos/99999999").status_code, 404)


if __name__ == "__main__":
    unittest.main()


class TestAdr98(unittest.TestCase):
    """ADR-98: the cached-gap fix, results of the old rules set aside, and the speed-ups giving identical results."""

    @classmethod
    def setUpClass(cls):
        from tests.test_my_strategy import CAL, random_ds
        cls.cal = CAL
        cls.ds = random_ds("2024-01-07", "2024-03-15", seed=4)

    def test_cached_gap_results_belong_to_their_list(self):
        from edgelab.mystrategy.frames import first_where, fvgs
        from edgelab.mystrategy.logic import Rules
        r = Rules(self.ds.bars, self.cal, P.resolve(A.BASE))
        v = r.views[1]
        t = v.tf[60]
        lists = [fvgs(t, "bear", 1.0), fvgs(t, "bear", 2.0), fvgs(t, "bear", 5.0)]
        self.assertNotEqual(len(lists[0].k), len(lists[2].k))
        for g in lists + lists[::-1]:                       # mixed order: a cached value never leaks between lists
            for gi in range(len(g.k)):
                want = first_where(v.h, int(g.known[gi]) + 1, r.n - 1, above=float(g.bottom[gi]))
                got = r._first_touch(v, 60, "bear", gi, g, below=False)
                self.assertTrue(got == want or (want < 0 and got > r.n))

    def test_reuse_between_combinations_is_identical(self):
        from edgelab.engine.signals import check_causality
        from edgelab.mystrategy import logic as L
        from edgelab.mystrategy.strategy import MyStrategy
        rows = [r for r in A.manifest()["rows"][::997] if not P.smt_used(P.resolve(r["overrides"]))][:6]

        def fp(over):
            st = MyStrategy(over, self.cal)
            sig = st.generate_signals(self.ds.bars)
            rep = check_causality(st, self.ds.bars, n_cuts=6)
            return (sig.direction.tobytes(), sig.stop_price.tobytes(), sig.target_price.tobytes(), rep.passed,
                    repr(sorted(st.explanations.items())))
        fresh = [fp(r["overrides"]) for r in rows]
        try:
            L.enable_shared_memo(5_000_000)
            reused = [fp(r["overrides"]) for r in rows]
            self.assertGreater(sum(len(e[1]) + len(e[-1]) for e in L._SHARED_MEMO.values()), 0)
        finally:
            L.enable_shared_memo(0)
        self.assertEqual(fresh, reused)
        self.assertTrue(all(f[3] for f in fresh))

    def test_shared_price_data(self):
        from edgelab.mystrategy import autotune as AT
        shm, payload = AT.share_dataset(self.ds)
        self.assertIsNotNone(shm)
        try:
            ds2, h = AT._attach(payload)
            self.assertEqual(ds2.bars.content_hash(), self.ds.bars.content_hash())
            self.assertFalse(ds2.bars.close.flags.writeable)
            self.assertEqual(ds2.manifest.content_hash, self.ds.manifest.content_hash)
            del ds2
            h.close()
        finally:
            shm.close()
            shm.unlink()

    def test_worker_plan(self):
        from edgelab.mystrategy import autotune as AT

        gb = 2 ** 30
        pl = AT.plan_workers(31, self.ds, True, available=int(19.3 * gb))
        self.assertTrue(pl["limited_by_memory"])
        self.assertGreater(pl["processes"], 6)                   # the old plan's 6
        self.assertEqual(pl["memo_entries"], 0)                  # memory-bound: no reuse memory
        roomy = AT.plan_workers(4, self.ds, True, available=64 * gb)
        self.assertEqual(roomy["processes"], 4)
        self.assertGreater(roomy["memo_entries"], 0)             # core-bound: spare memory reused
        self.assertLessEqual(AT.plan_workers(31, self.ds, False, available=int(19.3 * gb))["processes"], pl["processes"])

    def test_old_results_are_set_aside(self):
        import json

        from edgelab.mystrategy import autotune as AT
        root = Path(tempfile.mkdtemp())
        try:
            class S:
                data_root = root
            svc = S()
            AT.home(svc)
            p = AT._results_path(svc)
            p.write_text(json.dumps({"n": 1, "metrics": {}}) + "\n" +
                         json.dumps({"n": 2, "metrics": {}, "rules_version": AT.RULES_VERSION}) + "\n")
            self.assertEqual(set(AT.read_results(svc)), {2})        # old-rules result not counted as done
            self.assertEqual(AT._old_lines(svc), 1)
            self.assertEqual(AT.set_aside_old(svc), 1)
            self.assertEqual(AT.set_aside_count(svc), 1)
            self.assertEqual(AT._old_lines(svc), 0)
            self.assertEqual(set(AT.read_results(svc)), {2})
        finally:
            shutil.rmtree(root, ignore_errors=True)


class TestGoals(unittest.TestCase):
    """ADR-99: the autotuner goals are workspace preferences: every rule on / off with its value, validated, and still
    there after a restart (the desktop app opens on a new port each time, so browser storage did not survive)."""

    def test_saved_validated_and_kept(self):
        from edgelab.services import Services
        root = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(REPO / "configs", root / "configs")
            svc = Services(root=root)
            g = svc.ui_preferences()["autotune_goals"]
            self.assertEqual(g["win_rate"], {"on": False, "value": 70.0})          # defaults: today's rules, all visible
            self.assertTrue(g["prop"]["on"] and g["profit"]["on"] and g["rr"]["on"] and g["trades_per_week"]["on"])
            g = {**g, "win_rate": {"on": True, "value": 70}, "losing_months": {"on": True, "value": 4}}
            svc.set_ui_preferences({"autotune_goals": g})
            again = Services(root=root).ui_preferences()["autotune_goals"]            # a restart
            self.assertEqual(again["win_rate"], {"on": True, "value": 70.0})
            self.assertEqual(again["losing_months"], {"on": True, "value": 4.0})
            for bad in ({"win_rate": {"on": 1}}, {"x": {"on": True}}, {"win_rate": {"on": True, "value": 101}},
                        {"rr": {"on": True, "value": "a"}}, "nope"):
                with self.assertRaises(ValueError):
                    svc.set_ui_preferences({"autotune_goals": bad})
            self.assertEqual(Services(root=root).ui_preferences()["autotune_goals"]["win_rate"]["value"], 70.0)
        finally:
            shutil.rmtree(root, ignore_errors=True)
