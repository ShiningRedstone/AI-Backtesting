"""Premarket breakout (ADR-115): settings, a known-answer day, causality of every rule variant, the challenge chain's
size cut, the autotuner's tweaks and the workspace flow (own protocol, backtest, setup review, holdout allowance, API).
Synthetic data only (labelled)."""
from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.engine.backtester import run_backtest
from edgelab.engine.costs import cost_model_from_config
from edgelab.engine.signals import check_causality
from edgelab.instruments import contract_for
from edgelab.pmbreakout import params as P
from edgelab.pmbreakout.strategy import PremarketBreakout
from tests.dukascopy_fixture import session_minutes, write_fixture
from tests.helpers import CFG
from tests.test_my_strategy import CAL, DISC, HOLD, REPO, freeze, random_ds


def known_ds():
    """Flat 18,000 everywhere except 9 Jan 2024 (New York): premarket high 18,010 at 7:00 and low 17,990 at 8:00; the
    9:30-9:35 candle rises from 18,000 to 18,015 (its lowest low 17,998 at 9:30) and closes above the high; from 9:35
    price climbs 5 points a minute to 18,200 and stays there. ASK = BID + 0.5."""
    ts = session_minutes("2024-01-07", "2024-01-11")
    n = len(ts)
    o = np.full(n, 18000.0)
    h, lo, c = o.copy(), o.copy(), o.copy()
    ny = ts.tz_convert("America/New_York")
    day = ny.normalize() == pd.Timestamp("2024-01-09", tz="America/New_York")
    m = ny.hour * 60 + ny.minute

    def at(hh, mm):
        return int(np.flatnonzero(day & (m == hh * 60 + mm))[0])
    h[at(7, 0)] = 18010.0
    lo[at(8, 0)] = 17990.0
    for j, (a, b, hi, low) in enumerate([(18000, 18003, 18004, 17998), (18003, 18006, 18007, 18002),
                                         (18006, 18009, 18010, 18005), (18009, 18012, 18013, 18008),
                                         (18012, 18015, 18016, 18011)]):
        k = at(9, 30 + j)
        o[k], c[k], h[k], lo[k] = a, b, hi, low
    p = 18015.0
    after = np.flatnonzero(day & (m >= 9 * 60 + 35))
    for k in after:
        nxt = min(18200.0, p + 5.0)
        o[k], c[k], h[k], lo[k] = p, nxt, nxt, p
        p = nxt
    df = pd.DataFrame({"ts": ts, "open": o, "high": h, "low": lo, "close": c, "volume": 1.0})
    for k, v in (("ask_open", o), ("ask_high", h), ("ask_low", lo), ("ask_close", c)):
        df[k] = v + 0.5
    return freeze(df, "SYN_PMB"), at


class TestSettings(unittest.TestCase):
    def test_defaults_and_validation(self):
        s = P.resolve()
        self.assertEqual((s["pm.start"], s["pm.end"], s["entry.tf"], s["target.rr"]), ("04:00", "09:30", "5m", 8.0))
        with self.assertRaises(P.SettingsError):
            P.resolve({"nope": 1})
        with self.assertRaises(P.SettingsError):
            P.resolve({"entry.from": "09:00"})                         # before the premarket ends
        with self.assertRaises(P.SettingsError):
            P.resolve({"stop.min_points": 20, "stop.max_points": 10})
        self.assertEqual(P.settings_hash({}), P.settings_hash(P.changed(s)))
        self.assertNotEqual(P.settings_hash({}), P.settings_hash({"target.rr": 6}))
        from edgelab.fairprice import params as FP
        from edgelab.mystrategy import params as MP
        self.assertEqual(len({P.settings_hash({}), FP.settings_hash({}), MP.settings_hash({})}), 3)


class TestKnownAnswer(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds, at = known_ds()
        cls.at = staticmethod(at)
        cls.costs = cost_model_from_config(CFG, cls.ds.instrument.symbol, provider="DUKASCOPY")

    def run_(self, over=None):
        st = PremarketBreakout(over or {}, CAL)
        res = run_backtest(self.ds, st, self.costs, CFG["backtest"], sizing=st.sizing, contract=contract_for(CFG, st.sizing))
        return st, res

    def test_breakout_long_stop_at_wick_target_8r(self):
        i = self.at(9, 34)                                        # the 9:30-9:35 candle is complete at its last minute
        st, res = self.run_()
        self.assertTrue(res.causality.passed)
        sig = st.last_signals
        self.assertEqual(list(np.flatnonzero(sig.direction)), [i])
        self.assertEqual(sig.direction[i], 1)
        self.assertAlmostEqual(sig.stop_price[i], 17998.0)        # the candle's wick low
        risk = 18015.5 - 17998.0                                  # planned entry = ASK close (buys on ASK)
        self.assertAlmostEqual(sig.target_price[i], 18015.5 + 8 * risk)
        e = st.explanations[i]
        self.assertEqual((e["premarket"]["high"], e["premarket"]["low"], e["level"]), (18010.0, 17990.0, 18010.0))
        self.assertEqual(e["candle"]["c"], 18015.0)
        self.assertEqual(len(res.trades), 1)
        t = res.trades.iloc[0]
        self.assertEqual(t["exit_reason"], "TARGET")
        self.assertEqual(int(t["contracts"]), 14)                 # $500 / (17.5 points x $2) = 14.3 -> 14

    def test_settings_change_the_answer(self):
        _, none = self.run_({"models.direction": "short_only"})
        self.assertEqual(len(none.trades), 0)
        st, _ = self.run_({"entry.buffer_points": 10})            # 18,015 is not 10 points beyond 18,010
        self.assertNotIn(self.at(9, 34), set(np.flatnonzero(st.last_signals.direction)))
        st, _ = self.run_({"stop.max_points": 10})                # a 17.5-point stop is too wide
        self.assertEqual(int(np.count_nonzero(st.last_signals.direction)), 0)
        self.assertEqual(st.stats["skipped_stop_size"], 1)
        st, res = self.run_({"target.rr": 2, "stop.buffer_points": 2})
        i = self.at(9, 34)
        self.assertAlmostEqual(st.last_signals.stop_price[i], 17996.0)
        self.assertAlmostEqual(st.last_signals.target_price[i], 18015.5 + 2 * 19.5)
        st, _ = self.run_({"pm.max_range_points": 15})            # the 20-point range is too big
        self.assertEqual(int(np.count_nonzero(st.last_signals.direction)), 0)
        st, _ = self.run_({"entry.tf": "1m"})                     # the 9:32 minute closes 18,009 < 18,010; 9:33 breaks
        self.assertEqual(list(np.flatnonzero(st.last_signals.direction)), [self.at(9, 33)])
        self.assertAlmostEqual(st.last_signals.stop_price[self.at(9, 33)], 18008.0)

    def test_declined_signal_is_not_traded(self):
        st, res = PremarketBreakout({}, CAL, skip={self.at(9, 34)}), None
        st.generate_signals(self.ds.bars)
        self.assertEqual(int(np.count_nonzero(st.last_signals.direction)), 0)
        self.assertEqual(st.stats["declined"], 1)


class TestCausality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = random_ds()

    def test_every_rule_variant_is_causal(self):
        for over in ({}, {"entry.tf": "1m", "day.max_trades": 3, "entry.first_break_only": False},
                     {"entry.tf": "15m", "models.price_series": "mid", "stop.buffer_points": 2, "target.rr": 3,
                      "pm.min_range_points": 20, "entry.until": "15:00", "target.force_exit": "15:30"},
                     {"entry.tf": "3m", "risk.mode": "equity_pct", "models.direction": "long_only", "stop.min_points": 5,
                      "day.skip_monday": True, "pm.start": "08:00", "pm.min_minutes": 30}):
            st = PremarketBreakout(over, CAL)
            st.generate_signals(self.ds.bars)
            self.assertGreater(int(np.count_nonzero(st.last_signals.direction)), 0, over)
            rep = check_causality(PremarketBreakout(over, CAL), self.ds.bars, n_cuts=10)
            self.assertTrue(rep.passed, f"{over}: {rep.detail}")


class TestChainCut(unittest.TestCase):
    def test_oversized_trades_are_cut_not_ending_the_chain(self):
        """A $2,000 risk on tight stops asks for more micros than LucidFlex funded allows (20 at the start): the chain
        cuts them to the limit (the user's rule from ADR-114) and goes on; without the cut it stops INCOMPATIBLE."""
        from edgelab.mystrategy import challenge as CH
        from edgelab.prop.service import default_profiles
        ds = random_ds()
        costs = cost_model_from_config(CFG, ds.instrument.symbol, provider="DUKASCOPY")
        st = PremarketBreakout({"risk.usd": 2000, "risk.max_contracts": 400, "entry.tf": "1m", "target.rr": 1,
                                "day.max_trades": 3, "entry.first_break_only": False}, CAL)
        res = run_backtest(ds, st, costs, CFG["backtest"], sizing=st.sizing, contract=contract_for(CFG, st.sizing))
        self.assertGreater(int(res.trades["contracts"].max()), 40)
        prof = next(p for p in default_profiles(REPO) if p["profile_id"] == "LUCID_LUCIDFLEX_50K")
        rs = CH.resizer_for(CFG, ds, st, res.trades)
        start = pd.Timestamp(int(ds.bars.ts_ns[0]), tz="UTC")
        plain = CH.chain(rs, prof, start)
        cut = CH.chain(rs, prof, start, fit=True)
        self.assertEqual(plain["attempts"][-1]["status"], "incompatible")
        self.assertFalse(any(a["status"] == "incompatible" for a in cut["attempts"]))
        self.assertGreater(sum(a["trades"] for a in cut["attempts"]), sum(a["trades"] for a in plain["attempts"]))


class TestOptimizerTweaks(unittest.TestCase):
    def test_neighbours_keep_fixed_settings(self):
        from edgelab.mystrategy import kind as KD, optimizer as O
        K = KD.get("pmb")
        nbs = O.neighbours(P.resolve(), False, K)
        self.assertGreater(len(nbs), 15)
        self.assertFalse({n["key"] for n in nbs} & set(K.optimizer["fixed"]))
        self.assertIn("target.rr", {n["key"] for n in nbs})
        self.assertEqual(len({n["settings_hash"] for n in nbs}), len(nbs))


class TestWorkspaceFlow(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        cls.root = Path(tempfile.mkdtemp())
        cls._env = os.environ.get("EDGELAB_EXPORT_DIR")
        os.environ["EDGELAB_EXPORT_DIR"] = str(cls.root / "exports")
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
        if cls._env is None:
            os.environ.pop("EDGELAB_EXPORT_DIR", None)
        else:
            os.environ["EDGELAB_EXPORT_DIR"] = cls._env

    def test_flow(self):
        from edgelab.mystrategy import kind as KD, review as RV, runner as R, setup_review as SR
        from edgelab.research import campaign
        from edgelab.services import Services
        svc = Services(root=self.root)
        K = KD.get("pmb")
        sm = R.backtest(svc, {"entry.tf": "1m", "day.max_trades": 2}, label="pmb", lock=svc.lock, K=K)
        self.assertEqual(R.protocol_info(svc, K)["trials_used"], 1)
        self.assertEqual(R.protocol_info(svc)["trials_used"], 0)            # My strategy's protocol untouched
        self.assertEqual(R.protocol_info(svc, KD.get("fair"))["trials_used"], 0)
        mine = svc.store.get_protocol(R.protocol_info(svc, K)["protocol_id"])
        self.assertEqual(mine["material"]["role"], "pm_breakout")
        self.assertEqual(campaign.governing_protocol(svc)["protocol_id"], self.parent["protocol_id"])
        self.assertTrue(sm["causality_passed"])
        self.assertTrue(svc.store.has_run(sm["run_id"]))
        self.assertEqual(R.list_backtests(svc), [])                          # nothing in My strategy's folder
        self.assertGreater(sm["trade_count"], 0)
        doc = R.get_trade(svc, sm["id"], 1, K)
        self.assertIn("premarket", doc["explanation"])
        self.assertIn("LUCID_LUCIDFLEX_50K", R.get_backtest(svc, sm["id"], K)["challenge"]["profiles"])
        R.backtest(svc, {"entry.tf": "1m", "day.max_trades": 2}, label="again", lock=svc.lock, K=K)
        self.assertEqual(R.protocol_info(svc, K)["trials_used"], 1)          # same settings: not a new trial
        st = SR.start(svc, sm["id"], 3, K)
        self.assertIn("fake_breakout", SR.view(svc, st["id"], svc.lock, K)["reasons"])
        cands = RV.candidates(svc, K)
        slot = RV.start_automatic(svc, cands[0]["ref"], lock=svc.lock, K=K)
        RV.start_manual(svc, slot["n"], lock=svc.lock, K=K)
        k = 0
        while True:
            v = RV.allowance_view(svc, svc.lock, K)
            if not v.get("candidate"):
                break
            RV.decide(svc, v["candidate"]["signal_bar"], k % 2 == 0, svc.lock, K)
            k += 1
        self.assertEqual(v["review"]["status"], "complete")
        _, hp = RV.holdout_protocol(svc, create=False, K=K)
        self.assertEqual(hp["material"]["role"], "pm_holdout")
        self.assertEqual(len([a for a in svc.store.list_holdout_access(hp["protocol_id"]) if a["status"] != "refused"]), 2)
        _, my_hp = RV.holdout_protocol(svc, create=False)
        self.assertIsNone(my_hp)                                              # My strategy's allowance untouched
        svc.store.close()

    def test_optimizer_try(self):
        """One autotuner try of this kind through the shared evaluate(): numbers per part and every profile's chain."""
        from edgelab.mystrategy import kind as KD, optimizer as O, runner as R
        from edgelab.services import Services
        svc = Services(root=self.root)
        K = KD.get("pmb")
        _, mine = R.ensure_protocol(svc, svc.lock, K=K)
        ds, extra, start, end, td_from = O.load_inputs(svc, mine, svc.lock, K)
        split, _ = O.split_point(ds, start, end)
        out = O.evaluate(svc.cfg, ds, extra, svc.root, (start, end), split, td_from, {"entry.tf": "1m"}, None,
                         check=True, K=K)
        self.assertTrue(out["causality_passed"])
        self.assertEqual(set(out["parts"]), {"train", "check", "full"})
        self.assertIn("LUCID_LUCIDFLEX_50K", out["parts"]["full"]["chains"])
        self.assertEqual(out["settings_hash"], P.settings_hash(P.resolve({"entry.tf": "1m"})))
        svc.store.close()

    def test_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        ov = c.get("/api/pmb").get_json()
        self.assertEqual(ov["strategy_kind"], "pmb")
        st = c.get("/api/pmb/settings").get_json()
        self.assertIn("target.rr", st["resolved"])
        r = c.post("/api/pmb/settings", json={"overrides": {"target.rr": 6}})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["overrides"], {"target.rr": 6.0})
        self.assertEqual(c.post("/api/pmb/settings", json={"overrides": {"ifg.tf_1m": True}}).status_code, 422)
        self.assertNotIn("target.rr", c.get("/api/my/settings").get_json()["resolved"])
        self.assertNotIn("target.rr", c.get("/api/fair/settings").get_json()["resolved"])
        self.assertEqual(c.get("/api/pmb/es").status_code, 404)
        self.assertEqual(c.get("/api/pmb/autotune").status_code, 200)
        self.assertEqual(c.get("/api/pmb/holdout").status_code, 200)
        c.post("/api/pmb/settings", json={"overrides": {}})


if __name__ == "__main__":
    unittest.main()
