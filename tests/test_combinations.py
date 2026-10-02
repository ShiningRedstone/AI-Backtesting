"""ADR-92: strategy combinations (up to five survivors, one prop account, one position at a time).

- merge: known answers (overlap skipped, the earlier entry wins, an entry at the previous exit is taken, ties by
  member), the merged stream is accepted by the unchanged prop lifecycle;
- statistics: losing months (New York months, like Results by year), trades per week capped for the score, drawdown;
- prop speed: the windowed first-payout replay equals a full ``simulate_lifecycle`` replay for every profile and start;
  the lifecycle's plain-row day groups give byte-identical results to the original pandas groups;
- search: greedy forward selection stops when nothing improves, never exceeds five members, keeps the survivor rule,
  is deterministic (input order, memo, several processes);
- services (SYNTHETIC data): read-only search, the combination protocol (family, one registration, members still
  governed by the parent), the combination holdout test (refusals before the test is spent, one test per
  combination, member runs labelled holdout but never a member's own result, a member's own test refused afterwards)."""
import json
import random
import unittest
from pathlib import Path
from unittest import mock

import numpy as np
import pandas as pd

from edgelab.research import combos as C

REPO = Path(__file__).resolve().parents[1]


def profiles():
    from edgelab.prop.service import default_profiles
    return {p["profile_id"]: p for p in default_profiles(REPO)}


def trades_frame(rows):
    """rows: (entry 'YYYY-MM-DD HH:MM' New York, minutes held, net USD) -> a stored-trades-like frame (risk $200)."""
    out = []
    for k, (e, dur, usd) in enumerate(rows, 1):
        en = pd.Timestamp(e, tz="America/New_York").tz_convert("UTC")
        out.append(dict(trade_no=k, signal_ts=en, entry_ts=en, exit_ts=en + pd.Timedelta(minutes=dur), contracts=2.0,
                        net_usd=float(usd), net_r=usd / 200.0, gross_r=usd / 200.0 + 0.05, cost_r=0.05, risk_usd=200.0,
                        risk_points=50.0, mae_points=10.0, holding_minutes=float(dur), exit_reason="target",
                        conflict_resolution=""))
    return pd.DataFrame(out)


def member(sid, rows=None, frame=None, start="2024-01-01", end="2024-06-30", lh=None):
    t = frame if frame is not None else trades_frame(rows)
    rec = {"dataset": {"start": pd.Timestamp(start, tz="UTC").isoformat(), "end": pd.Timestamp(end, tz="UTC").isoformat()},
           "assumptions": {"execution_contract": {"contract": "MNQ"}}}
    return C.member_from({"strategy_id": sid, "run_id": "RUN_" + sid, "logic_hash": lh or ("L" + sid)}, rec, t)


def daily(sid, hhmm, pnl, dur=30, start="2024-01-01", end="2024-06-30", every=1):
    days = pd.bdate_range(start, end)[::every]
    return member(sid, [(f"{d.date()} {hhmm}", dur, pnl[i % len(pnl)]) for i, d in enumerate(days)], start=start, end=end)


class TestMerge(unittest.TestCase):
    def test_known_answer(self):
        a = member("STR_A", [("2024-01-02 10:00", 60, 100), ("2024-01-02 12:00", 60, 100), ("2024-01-02 15:00", 10, 5)])
        b = member("STR_B", [("2024-01-02 10:30", 60, -50),          # overlaps A's first trade: skipped
                             ("2024-01-02 11:00", 30, 70),           # enters exactly at A's exit: taken
                             ("2024-01-02 12:30", 10, 9),            # inside A's second trade: skipped
                             ("2024-01-02 15:00", 10, 6)])           # same entry and signal as A: A wins (member order)
        mids, rows = C.merge_select([a, b])
        got = [(["STR_A", "STR_B"][m], int(r)) for m, r in zip(mids, rows)]
        self.assertEqual(got, [("STR_A", 0), ("STR_B", 1), ("STR_A", 1), ("STR_A", 2)])
        self.assertEqual(C.skipped_by_member([a, b], mids), {"STR_A": 0, "STR_B": 3})
        fr = C.merged_frame([a, b], mids, rows)
        self.assertEqual(fr["member"].tolist(), ["STR_A", "STR_B", "STR_A", "STR_A"])
        self.assertEqual(fr["trade_no"].tolist(), [1, 2, 3, 4])
        self.assertEqual(fr["member_trade_no"].tolist(), [1, 2, 2, 3])
        self.assertEqual(fr["net_usd"].tolist(), [100.0, 70.0, 100.0, 5.0])
        from edgelab.prop.lifecycle import prepare
        for p in profiles().values():                                 # no overlap: the unchanged lifecycle accepts it
            self.assertEqual(len(prepare(fr, p)), 4)

    def test_order_of_members_does_not_matter(self):
        a, b, c = daily("STR_A", "10:00", [300, -200]), daily("STR_B", "10:15", [250, -100]), daily("STR_C", "15:00", [100])
        ev1 = C.Evaluator([a, b, c], None)
        ev2 = C.Evaluator([c, a, b], None)
        self.assertEqual(ev1.cheap(ev1.key(["STR_C", "STR_A", "STR_B"])), ev2.cheap(ev2.key(["STR_B", "STR_C", "STR_A"])))
        self.assertEqual(C.combo_id(["L2", "L1"]), C.combo_id(["L1", "L2"]))
        self.assertNotEqual(C.combo_id(["L1", "L2"]), C.combo_id(["L1", "L3"]))
        self.assertRegex(C.combo_id(["L1", "L2"]), r"^CMB_[0-9A-F]{16}$")


class TestStats(unittest.TestCase):
    def test_losing_months_drawdown_and_capped_trades_per_week(self):
        a = member("STR_A", [("2024-01-31 15:00", 120, -100),         # exits 17:00 NY Jan 31: January
                             ("2024-01-31 23:30", 60, 50),            # exits 00:30 NY Feb 1: February
                             ("2024-02-15 10:00", 30, -80),
                             ("2024-03-04 10:00", 30, 10)])
        ms = [a]
        win = C.window_of(ms)
        st = C.cheap_stats(ms, win, cap=0.1)
        self.assertEqual(st["negative_months"], 2)                    # Jan (-100), Feb (+50 - 80); Mar positive
        mids, rows = C.merge_select(ms)
        fr = C.merged_frame(ms, mids, rows)
        self.assertEqual(C.negative_months_of(fr["exit_ts"], fr["net_r"]), 2)
        self.assertEqual(st["max_drawdown_usd"], 130.0)               # 0 -> -100 -> -50 -> -130
        from edgelab.analytics.metrics import trades_per_week
        self.assertAlmostEqual(st["trades_per_week"], trades_per_week(4, *win))
        self.assertEqual(st["tpw_scored"], 0.1)                       # capped for the score
        self.assertEqual(C.cheap_stats(ms, win, cap=50)["tpw_scored"], st["trades_per_week"])

    def test_holdout_negative_months_unchanged(self):
        """holdout.negative_months now uses the shared helper: same answer as its former inline code."""
        t = trades_frame([(f"2024-0{m}-1{d} 10:00", 30, (-1) ** (m + d) * (50 + 7 * d)) for m in range(1, 7) for d in range(4)])
        local = pd.DatetimeIndex(pd.to_datetime(t["exit_ts"], utc=True)).tz_convert("America/New_York")
        want = int((pd.Series(t["net_r"].to_numpy(float)).groupby([local.year, local.month]).sum() < 0).sum())
        self.assertEqual(C.negative_months_of(t["exit_ts"], t["net_r"].to_numpy(float)), want)


class TestProp(unittest.TestCase):
    def combos(self):
        a = daily("STR_A", "10:00", [300, -200])
        b = member("STR_B", frame=pd.concat([daily("x", "10:15", [250, -200, 150]).trades,
                                            daily("y", "14:00", [-150, 220]).trades]).sort_values("entry_ts")
                   .assign(trade_no=lambda f: range(1, len(f) + 1)))
        c = daily("STR_C", "15:00", [-100, -100, 150])
        d = daily("STR_D", "09:45", [900, -600, -500, 700], dur=200)          # big swings: breaches and DLL days
        return [[a], [a, b], [a, b, c], [c], [b, c], [d], [a, d]]

    def test_first_payout_equals_full_replay(self):
        from edgelab.prop.lifecycle import simulate_lifecycle
        n = 0
        for prof in profiles().values():
            for ms in self.combos():
                mids, rows = C.merge_select(ms)
                fr = C.merged_frame(ms, mids, rows)
                run = C.PropRunner(fr, prof)
                for s in C.month_starts(C.window_of(ms)) + [None]:
                    sub = fr if s is None else fr[fr["entry_ts"] >= s]
                    self.assertEqual(run.first_payout(s), C._outcome(simulate_lifecycle(sub, prof)),
                                     (prof["profile_id"], [m.strategy_id for m in ms], s))
                    n += 1
        self.assertGreater(n, 150)

    def test_row_groups_byte_identical_to_pandas_groups(self):
        from edgelab.prop import lifecycle as L
        for prof in profiles().values():
            for ms in self.combos():
                mids, rows = C.merge_select(ms)
                fr = C.merged_frame(ms, mids, rows)
                fast = json.dumps(L.simulate_lifecycle(fr, prof), sort_keys=True, default=str)
                with mock.patch.object(L, "_ROW_GROUPS", False):
                    slow = json.dumps(L.simulate_lifecycle(fr, prof), sort_keys=True, default=str)
                self.assertEqual(fast, slow, (prof["profile_id"], [m.strategy_id for m in ms]))

    def test_rolling_starts_known_answer(self):
        """+$1,000 every trading day: LucidFlex needs $3,000 with the best day <= 50% of profit -> passes on its 3rd
        trading day from any start; the first payout needs 5 winning funded days -> 3 + 5 = 8 trading days."""
        prof = profiles()["LUCID_LUCIDFLEX_50K"]
        m = daily("STR_A", "10:00", [1000], start="2024-01-01", end="2024-04-30")
        ev = C.Evaluator([m], prof)
        st = ev.full(("STR_A",))
        self.assertTrue(st["survivor"])
        self.assertEqual((st["starts"], st["failed"], st["undecided"]), (4, 0, 0))
        self.assertEqual((st["pass_pct"], st["median_days_to_pass"], st["median_days_to_payout"]), (100.0, 3.0, 8.0))
        self.assertEqual(C.month_starts(C.window_of([m]))[1], pd.Timestamp("2024-02-01", tz="America/New_York"))
        self.assertEqual(C.month_starts((pd.Timestamp("2024-01-28", tz="UTC"), pd.Timestamp("2024-03-15", tz="UTC")))[1:],
                         [pd.Timestamp("2024-03-01", tz="America/New_York")])      # Feb 1 is within a week of the start

    def test_not_applicable_profile_or_contract(self):
        m = daily("STR_A", "10:00", [1000])
        m.contract = "NQ"
        st = C.Evaluator([m], profiles()["LUCID_LUCIDFLEX_50K"]).full(("STR_A",))
        self.assertFalse(st["survivor"])
        self.assertEqual(st["evaluation"], "NOT_APPLICABLE")


class TestSearch(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prof = profiles()["LUCID_LUCIDFLEX_50K"]
        # A: steady; B: complements A (afternoon, positive); C: loses money (never improves anything); D/E/F/G: more
        # positive afternoon/evening strategies at other times (more trades per week, small edge)
        cls.ms = [daily("STR_A", "10:00", [400, -150]), daily("STR_B", "13:00", [300, -100]),
                  daily("STR_C", "15:00", [-200, 100]), daily("STR_D", "11:00", [250, -120], every=2),
                  daily("STR_E", "12:00", [260, -130], every=2), daily("STR_F", "14:00", [240, -110], every=3),
                  daily("STR_G", "09:40", [220, -100], every=3)]

    def test_greedy_rules(self):
        ev = C.Evaluator(self.ms, self.prof)
        pool = sorted(ev.members)
        final, path = C.greedy_from(ev, "STR_A", pool)
        self.assertLessEqual(len(final), 5)
        self.assertNotIn("STR_C", final)                              # a losing member never improves the score
        self.assertEqual(path[0], "STR_A")
        self.assertEqual(sorted(path), list(final))
        self.assertTrue(ev.full(final)["survivor"])
        # the stopping rule: with five members, or the final set ranks first against its best extensions
        if len(final) < 5:
            cands = [ev.key(final + (o,)) for o in pool if o not in final]
            top = [tuple(r["members"]) for r in C.rank([ev.cheap(c) for c in cands], C.CHEAP_CRITERIA)
                   if ev.survivor(tuple(r["members"]))["survivor"]][:C.PROP_TOP_K]
            self.assertEqual(C.rank([ev.full(final)] + [ev.full(k) for k in top])[0]["members"], list(final))
        small, _ = C.greedy_from(ev, "STR_A", pool, max_members=2)
        self.assertEqual(len(small), 2)

    def test_search_is_deterministic(self):
        r1 = C.search(self.ms, self.prof)
        shuffled = list(self.ms)
        random.Random(3).shuffle(shuffled)
        r2 = C.search(shuffled, self.prof)
        self.assertEqual([x["members"] for x in r1["rows"]], [x["members"] for x in r2["rows"]])
        self.assertEqual([x["score"] for x in r1["rows"]], [x["score"] for x in r2["rows"]])
        self.assertTrue(r1["rows"])
        self.assertTrue(all(2 <= len(x["members"]) <= 5 and x["survivor"] for x in r1["rows"]))
        self.assertEqual(len({tuple(x["members"]) for x in r1["rows"]}), len(r1["rows"]))          # distinct
        self.assertEqual([x["position"] for x in r1["rows"]], list(range(1, len(r1["rows"]) + 1)))
        self.assertGreaterEqual(r1["n_evaluated"], len(r1["rows"]))
        # memo off (a fresh evaluator per combination) gives the same numbers
        for x in r1["rows"][:3]:
            fresh = C.Evaluator(self.ms, self.prof).full(tuple(x["members"]))
            self.assertEqual({k: fresh[k] for k, *_ in C.CRITERIA}, {k: x[k] for k, *_ in C.CRITERIA})

    def test_parallel_equals_sequential(self):
        r1 = C.search(self.ms, self.prof)
        r2 = C._search_parallel(self.ms, self.prof, C.DEFAULT_TPW_CAP, 2, None, None)
        strip = lambda rows: [{k: v for k, v in x.items()} for x in rows]                     # noqa: E731
        self.assertEqual(strip(r1["rows"]), strip(r2["rows"]))
        self.assertEqual((r1["n_evaluated"] <= r2["n_evaluated"], r1["n_alone"]), (True, r2["n_alone"]))

    def test_rank_known_answer(self):
        rows = [{"members": ["A"], "negative_months": 1, "max_drawdown_usd": 100, "expectancy_r": 0.2, "tpw_scored": 5,
                 "pass_pct": 90, "median_days_to_pass": 10, "median_days_to_payout": 20},
                {"members": ["B"], "negative_months": 3, "max_drawdown_usd": 100, "expectancy_r": 0.3, "tpw_scored": 2,
                 "pass_pct": None, "median_days_to_pass": 12, "median_days_to_payout": 25}]
        out = C.rank(rows)
        # A ranks: 1, 1.5, 2, 1, 1, 1, 1 -> (2*1 + 2*1.5 + 2 + 1 + 1 + 1 + 1) / 9 = 11 / 9
        self.assertEqual(out[0]["members"], ["A"])
        self.assertAlmostEqual(out[0]["score"], round(11 / 9, 6))
        self.assertAlmostEqual(out[1]["score"], round((2 * 2 + 2 * 1.5 + 1 + 2 + 2 + 2 + 2) / 9, 6))


if __name__ == "__main__":
    unittest.main()


# ============================================================================== services (SYNTHETIC data)
from tests.test_holdout_backtests import HoldoutBase  # noqa: E402


class TestServicesFlow(HoldoutBase):
    def pins(self, survivors):
        from edgelab.research import overview as ov
        real_crit, real_surv = ov.apply_criteria, C.survivor_check

        def as_survivor(row, profile):                 # SYNTHETIC data rarely passes a prop payout: pin the labels
            out = real_crit(row, profile)
            out["survivor"] = row.get("strategy_id") in survivors and row.get("status") == "IN_SAMPLE"
            return out

        def combo_survivor(runner, e):
            return {**real_surv(runner, e), "survivor": True}
        ov._FACET_CACHE.clear()
        for p in (mock.patch.object(ov, "apply_criteria", as_survivor), mock.patch.object(C, "survivor_check", combo_survivor)):
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(ov._FACET_CACHE.clear)

    def counts(self, s, pid):
        return (len(s.store.list_runs()), len(s.store.list_trial_events(pid)), s._config_hash())

    def test_search_registration_and_combination_holdout(self):
        from edgelab.research import overview as ov
        from edgelab.research import protocol as rp
        s = self.svc()
        p, sid = self.protocol(s, "combo")
        pid = p["protocol_id"]
        self.pins({self.ema, self.rsi})
        rows = s.combination_survivors()["rows"]
        self.assertEqual({r["strategy_id"] for r in rows}, {self.ema, self.rsi})
        self.assertTrue(all(r["protocol_id"] == pid and r["registrable"] for r in rows))

        # read-only: a panel and a search write no runs, trials or settings
        before = self.counts(s, pid)
        panel = s.combination_panel([self.rsi, self.ema])
        self.assertEqual(panel["member_ids"], sorted([self.ema, self.rsi]))
        self.assertEqual(len(panel["members"]), 2)
        self.assertEqual(sum(m["kept"] for m in panel["members"]), panel["kpis"]["trade_count"])
        self.assertEqual(panel["skipped_total"], sum(m["skipped"] for m in panel["members"]))
        self.assertFalse(panel["registered"])
        self.assertIn(panel["best_single"], panel["member_ids"])
        doc = C.run_search(s)
        self.assertEqual((doc["protocol_id"], doc["n_survivors"]), (pid, 2))
        self.assertGreaterEqual(doc["n_evaluated"], 1)
        self.assertEqual(s.latest_combination_search()["search"]["search_id"], doc["search_id"])
        self.assertEqual(self.counts(s, pid), before)
        self.assertEqual(C.ledger_count(s.data_root, pid), 1)                     # the one pair, counted once

        # registration: typed confirmation, one per research protocol, members still governed by the parent
        with self.assertRaises(ValueError):
            s.register_combinations([[self.ema, self.rsi]], "register")
        with self.assertRaises(ValueError):
            s.register_combinations([[self.ema]], "REGISTER")                    # a combination has 2..5 members
        out = s.register_combinations([[self.ema, self.rsi]], "REGISTER")
        cp = s.store.get_protocol(out["combo_protocol_id"])
        self.assertTrue(rp.is_combo(cp) and rp.is_companion(cp))
        budget = p["material"]["trial_budget"]["max_unique_trials"]
        self.assertEqual(out["family_size"], budget + 1)
        self.assertEqual(rp.family_size(cp["material"], 0), budget + 1)
        self.assertEqual(out["holdout_tests"], 10)
        st = s.protocol_status(cp["protocol_id"])                                 # other pages read it without errors
        self.assertEqual(st["protocol_id"], cp["protocol_id"])
        self.assertIn("combo_companion", {r["role"] for r in s.list_protocols()})
        with self.assertRaises(ValueError) as cm:
            s.register_combinations([[self.ema, self.rsi]], "REGISTER")
        self.assertIn("COMBO_EXISTS", str(cm.exception))
        sc = p["material"]["scope"]
        lh = s.library.load(self.ema)["logic_hash"]
        self.assertEqual(s._governing_protocol(sc["instrument"], sc["provider"], logic_hash=lh)["protocol_id"], pid)
        from edgelab.research import campaign
        self.assertEqual(campaign.governing_protocol(s)["protocol_id"], pid)
        self.assertEqual(self.counts(s, pid), before)

        # the holdout test: refusals before it is spent, then exactly one test
        cid = cp["material"]["combo_set"][0]["combo_id"]
        with self.assertRaises(rp.ProtocolRefusal) as cm:
            s.evaluate_combination_holdout(cp["protocol_id"], "CMB_0000000000000000")
        self.assertEqual(cm.exception.code, "COMBO_NOT_REGISTERED")
        with mock.patch.object(type(s), "_config_hash", return_value="changed"):
            with self.assertRaises(rp.ProtocolRefusal) as cm:
                s.evaluate_combination_holdout(cp["protocol_id"], cid)
        self.assertEqual(cm.exception.code, "PROTOCOL_CONFIG_CHANGED")
        self.assertEqual([a["status"] for a in s.store.list_holdout_access(cp["protocol_id"])], ["refused", "refused"])
        res = s.evaluate_combination_holdout(cp["protocol_id"], cid)
        self.assertIn(res["outcome"], ("HOLDOUT_CRITERIA_MET", "HOLDOUT_CRITERIA_NOT_MET"))
        self.assertEqual(res["holdout_looks_used"], 1)
        self.assertEqual(res["multiple_testing"]["family_size"], budget + 1)
        self.assertEqual(res["criteria"]["random_control"]["n_controls"],
                         p["material"]["acceptance_criteria"]["random_control"]["n_controls"])
        member_runs = {m["run_id"] for m in res["members"]}
        self.assertEqual(len(member_runs), 2)
        h = p["material"]["windows"]["holdout"]
        for rid in member_runs:
            rec, _ = s.store.load_run(rid)
            self.assertEqual(rec["status"], "OUT_OF_SAMPLE")
            self.assertEqual(pd.Timestamp(rec["dataset"]["start"]), pd.Timestamp(h["first_bar"]))
        ov._FACET_CACHE.clear()
        self.assertLessEqual(member_runs, ov.holdout_run_ids(s))                # labelled holdout, never OOS
        self.assertEqual(ov.combination_run_ids(s), member_runs)
        mine = [r for r in ov.run_records(s) if r["strategy_id"] == self.ema]
        self.assertEqual(ov.scoped_runs(mine, ov.HOLDOUT_VIEW), [])            # not the member's own holdout result
        self.assertEqual([r["run_id"] for r in ov.scoped_runs(mine, "oos")], [])
        with self.assertRaises(rp.ProtocolRefusal) as cm:
            s.evaluate_combination_holdout(cp["protocol_id"], cid)
        self.assertEqual(cm.exception.code, "COMBO_ALREADY_EVALUATED")

        # a member's own holdout test is refused afterwards (its holdout was seen inside the combination)
        s.select_shortlist(sid, [self.ema])
        with self.assertRaises(rp.ProtocolRefusal) as cm:
            s.evaluate_holdout(pid, sid, self.ema)
        self.assertEqual(cm.exception.code, "HOLDOUT_ALREADY_EVALUATED")
        cand = {r["strategy_id"]: r for r in s.holdout_candidates()["rows"]}
        self.assertFalse(cand[self.ema]["eligible"])
        self.assertIn("combination", cand[self.ema]["reason"])
        self.assertEqual(s.protocol_status(pid)["holdout"]["looks_used"], 0)    # the parent's own tests are untouched

        reg = s.combination_registration()
        self.assertEqual(reg["protocol"]["tests_used"], 1)
        self.assertEqual(reg["combos"][0]["holdout"]["outcome"], res["outcome"])
        panel = s.combination_panel([self.ema, self.rsi])
        self.assertTrue(panel["registered"])
        self.assertEqual(panel["holdout_view"]["trades"], res["trade_count"])

        # retiring the research protocol retires its combination protocol
        s.retire_protocol(pid)
        self.assertEqual(s.store.get_protocol(cp["protocol_id"])["status"], "RETIRED")

    def test_random_entry_control_output_unchanged_by_trades_option(self):
        s = self.svc()
        p, _ = self.protocol(s, "control")
        from edgelab.research.campaign import discovery_period
        per = discovery_period(p["material"])
        period = (pd.Timestamp(per["start"]), pd.Timestamp(per["end"]))
        a = s.random_entry_control(self.ema, self.d5, n_controls=3, seed=5, period=period)
        tr: list = []
        b = s.random_entry_control(self.ema, self.d5, n_controls=3, seed=5, period=period, trades_out=tr)
        self.assertEqual(a["realizations"], b["realizations"])
        self.assertEqual(a["validation_id"], b["validation_id"])
        self.assertEqual(len(tr), 3)
        from edgelab.engine.backtester import BacktestResult
        from types import SimpleNamespace
        self.assertEqual([BacktestResult.trades_hash.fget(SimpleNamespace(trades=t)) for t in tr],
                         [r["trades_hash"] for r in b["realizations"]])
