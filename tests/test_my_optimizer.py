"""Strategy autotuner = step-by-step optimiser (ADR-101) and the prop challenge chain of My strategy reports.

Guarantees tested: the chain's per-challenge re-sizing gives EXACTLY the engine's trades for an account starting at that
time (every column, bit for bit); fees are applied to the stored fee-free chain exactly as paper trading charges them
(evaluation, reset or a new evaluation, activation); a try without the lookahead check has the same trades as with it;
every tweak is valid, new, one setting, never a position-size / flip setting and never a setting that cannot change the
trades; the order is fixed; the score orders goals first, then prop net, then net R; a run goes through the ONE engine,
counts each tried combination once in its own companion protocol (never the governing one), keeps a tweak only when both
parts improve, gives every best the lookahead check, backtests the final best normally under the same trial, reuses
earlier tries in a second run without new tries, and refuses a run without fees or from a holdout report; favourites and
names of reports change no number; the rule-code speed-ups and shared price data give identical results (moved from the
first autotuner's tests). All data is SYNTHETIC."""
import shutil
import tempfile
import time
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.mystrategy import challenge as CH
from edgelab.mystrategy import optimizer as O
from edgelab.mystrategy import params as P
from tests.test_my_strategy import CAL, DISC, HOLD, REPO, random_ds

LOOSE = {"draw.required": False, "eq.enabled": False, "ifg.displacement": False, "day.stop_after_win": False}


def _engine(ds, ov, account=None, check=False):
    from edgelab.engine.backtester import run_backtest
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.instruments import contract_for
    from edgelab.mystrategy.strategy import MyStrategy
    from tests.helpers import CFG
    st = MyStrategy(ov, CAL)
    costs = cost_model_from_config(CFG, ds.instrument.symbol, provider=ds.manifest.provider)
    bt = dict(CFG["backtest"])
    bt["require_causality_check"] = check
    return st, run_backtest(ds, st, costs, bt, sizing=st.sizing, contract=contract_for(CFG, st.sizing), account=account)


class TestChallengeChain(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds("2024-01-07", "2024-06-30", seed=11)

    def test_resizing_equals_the_engine(self):
        """Each challenge is sized from its own starting balance: identical to the engine with account.equity_from_ts."""
        from tests.helpers import CFG
        for ov in ({**LOOSE, "risk.pct": 3.0}, {**LOOSE, "risk.pct": 0.5, "risk.max_contracts": 400}):
            st, res = _engine(self.ds, ov)
            tr = res.trades
            self.assertGreater(len(tr), 20)
            rs = CH.resizer_for(CFG, self.ds, st, tr)
            for k in (0, len(tr) // 3, len(tr) // 2, len(tr) - 3):
                t = pd.Timestamp(tr["entry_ts"].iloc[k])
                for bal in (50_000.0, 47_250.5):
                    mine = rs.from_(t, bal).reset_index(drop=True)
                    _, r2 = _engine(self.ds, ov, account={"starting_equity": bal, "equity_from_ts": t.isoformat()})
                    eng = r2.trades[pd.to_datetime(r2.trades["entry_ts"], utc=True) >= t].reset_index(drop=True)
                    self.assertEqual(list(mine.columns), list(eng.columns))
                    for c in eng.columns:
                        self.assertTrue(mine[c].equals(eng[c]), (ov, k, bal, c))
            self.assertEqual(rs.dropped, 0)
        # fixed-dollar sizing: the trades unchanged
        st, res = _engine(self.ds, {**LOOSE, "risk.mode": "fixed_usd"})
        rs = CH.resizer_for(CFG, self.ds, st, res.trades)
        t = pd.Timestamp(res.trades["entry_ts"].iloc[5])
        self.assertTrue(rs.from_(t, 1.0).equals(res.trades[pd.to_datetime(res.trades["entry_ts"], utc=True) >= t]))

    def test_chain_and_fees(self):
        from tests.helpers import CFG
        st, res = _engine(self.ds, {**LOOSE, "risk.pct": 3.0})
        raw = CH.chains(CFG, REPO, self.ds, st, res.trades, pd.Timestamp("2024-01-08", tz="UTC"))
        self.assertEqual(set(raw), {"LUCID_LUCIDFLEX_50K", "TRADEIFY_GROWTH_50K", "TRADEIFY_SELECT_DAILY_50K",
                                    "TRADEIFY_SELECT_FLEX_50K"})
        for pid, c in raw.items():
            atts = c["attempts"]
            self.assertTrue(atts and all(a["trades"] > 0 for a in atts))
            self.assertEqual(sum(a["trades"] for a in atts if a["status"] != "in_progress" and a["status"] != "funded")
                             + (atts[-1]["trades"] if atts[-1]["status"] in ("in_progress", "funded") else 0),
                             sum(a["trades"] for a in atts))
            for a, b in zip(atts, atts[1:]):                       # one challenge after another, never overlapping
                self.assertLess(pd.Timestamp(a["end"]), pd.Timestamp(b["start"]))
        # fees: known answer on a hand-made chain
        raw = {"attempts": [{"status": "failed", "passed": False, "payouts": 0, "trader": 0.0},
                            {"status": "failed", "passed": False, "payouts": 0, "trader": 0.0},
                            {"status": "funded_lost", "passed": True, "payouts": 2, "trader": 1800.0},
                            {"status": "funded", "passed": True, "payouts": 1, "trader": 900.0}], "stopped": None}
        s = CH.summary(raw, {"eval_price": 100, "reset_fee": 80, "activation_fee": 50})
        self.assertEqual((s["challenges"], s["fails"], s["passes"], s["funded_lost"], s["payouts"]), (4, 2, 2, 1, 3))
        self.assertEqual(s["fees_total"], 100 + 80 + 80 + 50 + 100 + 50)          # eval, reset, reset+act, new eval+act
        self.assertEqual((s["trader_payouts"], s["avg_payout_per_pass"], s["net"]), (2700.0, 1350.0, 2700.0 - 460))
        self.assertTrue(s["open_at_end"])
        s = CH.summary(raw, {"eval_price": 100})                                     # no reset fee: a new evaluation
        self.assertEqual(s["fees_total"], 400.0)
        s = CH.summary(raw, {})
        self.assertIsNone(s["net"])
        self.assertFalse(s["fees_complete"])


class TestTries(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds("2024-01-07", "2024-03-15", seed=4)

    def test_without_the_check_the_trades_are_identical(self):
        for ov in (LOOSE, {**LOOSE, "manage.be": "r", "target.mode": "fixed_r"}, {}):
            _, a = _engine(self.ds, ov, check=False)
            _, b = _engine(self.ds, ov, check=True)
            self.assertIsNone(a.causality)
            self.assertTrue(b.causality.passed)
            self.assertEqual(a.trades_hash, b.trades_hash)

    def test_tweaks(self):
        missing = [p["key"] for p in P.SCHEMA if p["type"] in ("int", "float") and p["key"] not in O.FIXED
                   and p["key"] not in O.STEPS]
        self.assertEqual(missing, [])
        self.assertTrue(set(O.INERT_UNLESS) <= set(P.BY_KEY))
        for start in ({}, LOOSE, {"target.mode": "fixed_r", "manage.be": "off", "models.judas": False}):
            s = P.resolve(start)
            nbs = O.neighbours(s, es_ok=False)
            self.assertGreater(len(nbs), 100)
            hashes = [n["settings_hash"] for n in nbs]
            self.assertEqual(len(hashes), len(set(hashes)))
            self.assertNotIn(P.settings_hash(s), hashes)
            for nb in nbs:
                t = P.resolve(nb["overrides"])
                self.assertEqual(P.settings_hash(t), nb["settings_hash"])
                diff = {k for k in s if s[k] != t[k]}
                self.assertIn(nb["key"], diff)
                self.assertTrue(diff <= {nb["key"], "filters.smt_in_score"}, diff)
                self.assertNotIn(nb["key"], O.FIXED)
                self.assertTrue(O.INERT_UNLESS.get(nb["key"], lambda _s: True)(s))
                self.assertFalse(P.smt_used(t))                         # no ES data: never an SMT setting
            a = O.ordered(nbs, "0123456789abcdef", {"key": "stop.buffer", "dir": 1})
            self.assertEqual([n["settings_hash"] for n in a],                      # a fixed order
                             [n["settings_hash"] for n in O.ordered(nbs, "0123456789abcdef", {"key": "stop.buffer", "dir": 1})])
            self.assertEqual(sorted(n["settings_hash"] for n in a), sorted(hashes))
            if any(n["key"] == "stop.buffer" and n["dir"] == 1 for n in nbs):
                self.assertEqual((a[0]["key"], a[0]["dir"]), ("stop.buffer", 1))   # the last good change continues first
        # numbers stay inside their range
        s = P.resolve({"stop.buffer": 0.0})
        self.assertEqual([n["to"] for n in O.neighbours(s, False) if n["key"] == "stop.buffer"], [0.5])

    def test_score_order(self):
        goals = {"win_rate": {"on": True, "value": 50.0}, "trades_per_week": {"on": True, "value": 2.0},
                 "losing_months": {"on": True, "value": 4.0}, "profit": {"on": True}, "rr": {"on": False, "value": 1.0},
                 "prop": {"on": True, "value": 2.0}}
        ctx = {"goals": goals, "profile": "P", "fees": {"eval_price": 100, "reset_fee": 100}}

        def part(wr, tpw, lm, net_r, payouts, passes=1, trader=0.0):
            atts = [{"status": "funded" if passes else "failed", "passed": bool(passes), "payouts": payouts,
                     "trader": trader}]
            return {"metrics": {"win_rate": wr, "trades_per_week": tpw, "months_losing": lm, "net_r": net_r,
                                "expectancy_r": net_r / 10}, "chains": {"P": {"attempts": atts, "stopped": None}}}
        a = O.score(part(0.6, 3, 1, 5, 2, trader=2000), ctx, 1.0)
        self.assertEqual((a["met"], a["goals"], a["short"], a["prop_net"]), (5, 5, 0.0, 1900.0))
        b = O.score(part(0.6, 3, 1, 5, 2, trader=3000), ctx, 1.0)
        self.assertTrue(O.better(b, a))                                  # all goals met: more prop net wins
        c = O.score(part(0.45, 3, 1, 50, 2, trader=9000), ctx, 1.0)
        self.assertTrue(O.better(a, c))                                  # a goal missed: worse, whatever the money
        d = O.score(part(0.40, 3, 1, 50, 2), ctx, 1.0)
        self.assertTrue(O.better(c, d))                                  # closer to the missed goal is better
        # count goals are scaled to the part: 2 losing months allowed on a 30 % part of a 4-month limit? no (1.2)
        self.assertFalse([r for r in O.score(part(0.6, 3, 2, 5, 2), ctx, 0.3)["rows"] if r["goal"] == "losing_months"][0]["ok"])
        self.assertTrue([r for r in O.score(part(0.6, 3, 1, 5, 1), ctx, 0.3)["rows"] if r["goal"] == "prop"][0]["ok"])


class TestOptimizerRun(unittest.TestCase):
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

    @staticmethod
    def wait(svc):
        while O.run_of(svc).info()["running"]:
            time.sleep(0.3)
        return O.run_of(svc).info()

    def test_run(self):
        from edgelab.mystrategy import runner as R
        from edgelab.research import campaign
        from edgelab.services import Services
        svc = Services(root=self.root)
        sm = R.backtest(svc, LOOSE, label="start", lock=svc.lock)
        self.assertTrue(sm["challenge"]["profiles"])                       # every new report carries its chain
        # refusals before anything runs: no fees, a holdout report, unknown id
        with self.assertRaises(R.MyStrategyError) as e:
            svc.my_autotune_start(sm["id"], 1, 10)
        self.assertEqual(e.exception.code, "PROP_FEES_MISSING")
        svc.set_ui_preferences({"prop_fees": {"LUCID_LUCIDFLEX_50K": {"eval_price": 100, "reset_fee": 80}}})
        with self.assertRaises(R.MyStrategyError):
            svc.my_autotune_start("BT_20200101_000000_abcd", 1, 10)
        # a run of 24 tries on two worker processes
        svc.my_autotune_start(sm["id"], 2, 24)
        info = self.wait(svc)
        self.assertIsNone(info["error"], info)
        st = svc.my_autotune_status()
        run = svc.my_autotune_run(st["runs"][0]["id"])
        self.assertEqual(run["status"], "finished")
        self.assertEqual(run["stop_reason"], "limit")
        self.assertEqual(run["tries"], 24)
        _, mine = O.ensure_protocol(svc, create=False)
        self.assertEqual(mine["material"]["role"], "my_optimizer")
        self.assertEqual(mine["material"]["trial_budget"]["max_unique_trials"], 5000)
        self.assertEqual(mine["material"]["holdout_budget"]["max_unique_candidate_evaluations"], 1)
        self.assertEqual(campaign.governing_protocol(svc)["protocol_id"], self.parent["protocol_id"])   # never governs
        self.assertEqual(st["protocol"]["trials_used"], 24)
        self.assertEqual(R.protocol_info(svc)["trials_used"], 1)          # My strategy's own tries: only the start
        bests = [b for b in run["bests"] if b["status"] == "best"]
        self.assertEqual(bests[0]["overrides"], P.changed(P.resolve(LOOSE)))
        for b in bests:
            self.assertEqual(b["lookahead"], "passed")
            if b["parent"] is not None:                                     # better on BOTH parts than its parent
                par = run["bests"][b["parent"]]
                self.assertGreater(tuple(b["train"]["key"]), tuple(par["train"]["key"]))
                self.assertGreater(tuple(b["check"]["key"]), tuple(par["check"]["key"]))
                self.assertEqual(len(O.neighbours(P.resolve(par["overrides"]), False)) > 0, True)
        accepted = [t for t in run["tries_log"] if t["accepted"]]
        self.assertEqual(len(accepted), len(bests) - 1)
        # the start's whole-period numbers = the start backtest
        self.assertEqual(bests[0]["trades_hash"], svc.store.load_run(sm["run_id"])[0]["trades_hash"])
        self.assertAlmostEqual(bests[0]["full"]["metrics"]["net_r"], sm["metrics"]["net_r"], places=4)
        if len(bests) > 1:                                                 # the final best: a normal backtest, same trial
            fb = svc.my_strategy_backtest(run["final_backtest"])
            self.assertEqual(svc.store.load_run(fb["run_id"])[0]["trades_hash"], bests[-1]["trades_hash"])
            self.assertTrue(fb["causality_passed"])
            self.assertEqual(fb["optimizer_run"], run["id"])
            self.assertEqual(svc.my_autotune_status()["protocol"]["trials_used"], 24)
        # the same run on one core, without the stored tries: the same path (never depends on the cores) and the same
        # trial keys, so no new try
        (O.home(svc) / "tries.jsonl").rename(O.home(svc) / "tries_run1.jsonl")
        svc.my_autotune_start(sm["id"], 1, 24)
        info = self.wait(svc)
        self.assertIsNone(info["error"], info)
        again = svc.my_autotune_run(svc.my_autotune_status()["runs"][0]["id"])
        self.assertEqual((again["tries"], again["reused"]), (24, 0))
        self.assertEqual([b["settings_hash"] for b in again["bests"]], [b["settings_hash"] for b in run["bests"]])
        self.assertEqual([b["train"]["key"] for b in again["bests"]], [b["train"]["key"] for b in run["bests"]])
        self.assertEqual(svc.my_autotune_status()["protocol"]["trials_used"], 24)
        # a second run from the same start reuses every earlier try: no new tries for those
        svc.my_autotune_start(sm["id"], 1, 24)
        info = self.wait(svc)
        self.assertIsNone(info["error"], info)
        run2 = svc.my_autotune_run(svc.my_autotune_status()["runs"][0]["id"])
        self.assertGreater(run2["reused"], 0)
        self.assertEqual(svc.my_autotune_status()["protocol"]["trials_used"], 24 + run2["tries"])
        self.assertEqual(run2["bests"][0]["settings_hash"], bests[0]["settings_hash"])
        self.assertGreaterEqual(run2["reused"], 23)        # the first run's tries (its limit cut its batch; this one goes on)
        # favourite + rename: display only
        before = svc.my_strategy_backtest(sm["id"])
        svc.my_strategy_set_meta(sm["id"], True, "  my   start ")
        after = svc.my_strategy_backtest(sm["id"])
        self.assertEqual((after["favorite"], after["label"], after["label_original"]), (True, "my start", "start"))
        self.assertEqual(after["metrics"], before["metrics"])
        self.assertEqual(svc.my_autotune_status()["starts"][0]["id"], sm["id"])    # favourites first
        ch = after["challenge"]["profiles"]["LUCID_LUCIDFLEX_50K"]
        self.assertTrue(ch["fees_complete"])
        self.assertEqual(after["challenge"]["names"]["LUCID_LUCIDFLEX_50K"], "LucidFlex 50K")
        svc.store.close()

    def test_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        self.assertEqual(c.get("/api/my/autotune").status_code, 200)
        self.assertEqual(c.post("/api/my/autotune/start", json={"start_id": "x"}).status_code, 400)
        self.assertEqual(c.post("/api/my/autotune/start", json={"start_id": "BT_20240101_000000_abcd", "max_tries": 0}).status_code, 400)
        self.assertEqual(c.post("/api/my/autotune/start", json={"start_id": "HO_20240101_000000_abcd"}).status_code, 400)
        self.assertEqual(c.get("/api/my/autotune/runs/OPT_20240101_000000_abcd").status_code, 404)
        self.assertEqual(c.get("/api/my/autotune/runs/../x").status_code, 404)
        self.assertEqual(c.post("/api/my/reports/BT_20240101_000000_abcd/meta", json={"favorite": True}).status_code, 404)
        self.assertEqual(c.post("/api/my/reports/BT_20240101_000000_abcd/meta", json={"favorite": "yes"}).status_code, 400)
        self.assertEqual(c.post("/api/my/reports/BT_20240101_000000_abcd/meta", json={}).status_code, 400)


class TestSpeedUpsAreIdentical(unittest.TestCase):
    """ADR-98 guarantees, kept from the first autotuner: the rule-code speed-ups and shared price data change nothing."""

    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds("2024-01-07", "2024-03-15", seed=4)

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
        ds = random_ds("2024-01-07", "2024-02-20", seed=4)
        r = Rules(ds.bars, CAL, P.resolve(LOOSE))
        for d in (1, -1):
            v = r.views[d]
            for tf in (1, 5, 15):
                t = v.tf[tf]
                sw = swings(t, 2)["lo"]
                for start in range(3000, r.n, 997):
                    old = np.flatnonzero((sw[2] < start) & (t.end[sw[0]] >= start - 2880))[::-1][:30].tolist()
                    self.assertEqual(r._recent_swing_lows(v, t, start), old)

    def test_cached_gap_results_belong_to_their_list(self):
        from edgelab.mystrategy.frames import first_where, fvgs
        from edgelab.mystrategy.logic import Rules
        r = Rules(self.ds.bars, CAL, P.resolve(LOOSE))
        v = r.views[1]
        t = v.tf[60]
        lists = [fvgs(t, "bear", 1.0), fvgs(t, "bear", 2.0), fvgs(t, "bear", 5.0)]
        self.assertNotEqual(len(lists[0].k), len(lists[2].k))
        for g in lists + lists[::-1]:
            for gi in range(len(g.k)):
                want = first_where(v.h, int(g.known[gi]) + 1, r.n - 1, above=float(g.bottom[gi]))
                got = r._first_touch(v, 60, "bear", gi, g, below=False)
                self.assertTrue(got == want or (want < 0 and got > r.n))

    def test_reuse_between_tries_is_identical(self):
        from edgelab.engine.signals import check_causality
        from edgelab.mystrategy import logic as L
        from edgelab.mystrategy.strategy import MyStrategy
        nbs = O.neighbours(P.resolve(LOOSE), False)
        rows = [P.changed(P.resolve(LOOSE))] + [n["overrides"] for n in nbs[::23]][:5]

        def fp(over):
            st = MyStrategy(over, CAL)
            sig = st.generate_signals(self.ds.bars)
            rep = check_causality(st, self.ds.bars, n_cuts=6)
            return (sig.direction.tobytes(), sig.stop_price.tobytes(), sig.target_price.tobytes(), rep.passed,
                    repr(sorted(st.explanations.items())))
        fresh = [fp(r) for r in rows]
        try:
            L.enable_shared_memo(5_000_000)
            reused = [fp(r) for r in rows]
        finally:
            L.enable_shared_memo(0)
        self.assertEqual(fresh, reused)
        self.assertTrue(all(f[3] for f in fresh))

    def test_shared_price_data(self):
        shm, payload = O.share_dataset(self.ds)
        self.assertIsNotNone(shm)
        try:
            ds2, h = O._attach(payload)
            self.assertEqual(ds2.bars.content_hash(), self.ds.bars.content_hash())
            self.assertFalse(ds2.bars.close.flags.writeable)
            self.assertEqual(ds2.manifest.content_hash, self.ds.manifest.content_hash)
            del ds2
            h.close()
        finally:
            shm.close()
            shm.unlink()

    def test_worker_plan(self):
        gb = 2 ** 30
        pl = O.plan_workers(31, self.ds, True, available=int(19.3 * gb))
        self.assertTrue(pl["limited_by_memory"])
        self.assertGreater(pl["processes"], 6)
        self.assertEqual(pl["memo_entries"], 0)
        roomy = O.plan_workers(4, self.ds, True, available=64 * gb)
        self.assertEqual(roomy["processes"], 4)
        self.assertGreater(roomy["memo_entries"], 0)


class TestGoals(unittest.TestCase):
    """ADR-99: the goals are workspace preferences (the optimiser scores with them), validated and kept on restart."""

    def test_saved_validated_and_kept(self):
        from edgelab.services import Services
        root = Path(tempfile.mkdtemp())
        try:
            shutil.copytree(REPO / "configs", root / "configs")
            svc = Services(root=root)
            g = svc.ui_preferences()["autotune_goals"]
            self.assertEqual(g["win_rate"], {"on": False, "value": 70.0})
            g = {**g, "win_rate": {"on": True, "value": 70}, "losing_months": {"on": True, "value": 4}}
            svc.set_ui_preferences({"autotune_goals": g})
            again = Services(root=root).ui_preferences()["autotune_goals"]
            self.assertEqual(again["win_rate"], {"on": True, "value": 70.0})
            for bad in ({"win_rate": {"on": 1}}, {"x": {"on": True}}, {"win_rate": {"on": True, "value": 101}}, "nope"):
                with self.assertRaises(ValueError):
                    svc.set_ui_preferences({"autotune_goals": bad})
        finally:
            shutil.rmtree(root, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
