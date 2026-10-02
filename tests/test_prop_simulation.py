"""Phase 6 prop-account simulation: rule-set validation, account state transitions, daily reset,
simultaneous breaches, intra-trade bound, chronology, multiple accounts, determinism, lineage and
read-only use of stored runs. Synthetic trades and SYNTHETIC TEST-ONLY rule sets only."""
import contextlib
import copy
import io
import json
import shutil
import tempfile
import unittest
from pathlib import Path

import pandas as pd
import yaml

from edgelab.prop.rules import PropConfigError, validate_rules
from edgelab.prop.simulator import (AccountStatus, PropDataError, prepare_trades, simulate_account,
                                    simulate_accounts, simulation_id, trades_fingerprint)

REPO = Path(__file__).resolve().parents[1]
EMA = yaml.safe_load((REPO / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())

# January: America/New_York is UTC-5, so the 17:00 reset is 22:00 UTC.
BASE = {"kind": "edgelab.prop_rules", "schema_version": 1, "id": "T", "name": "synthetic test rules",
        "synthetic_test_only": True, "account": {"starting_balance": 10000},
        "trading_day": {"timezone": "America/New_York", "reset_time": "17:00"}}


def rules(**kw):
    d = copy.deepcopy(BASE)
    d.update(kw)
    return d


def trades(rows):
    """rows: (entry 'YYYY-MM-DD HH:MM' UTC, exit, net_usd[, contracts[, mae_points[, exit_reason]]])."""
    out = []
    for i, r in enumerate(rows, 1):
        entry, exit_, net = r[:3]
        n = r[3] if len(r) > 3 else 1
        mae = r[4] if len(r) > 4 else max(0.0, -net - 2.0)
        out.append({"trade_no": i, "entry_ts": pd.Timestamp(entry, tz="UTC"), "exit_ts": pd.Timestamp(exit_, tz="UTC"),
                    "contracts": n, "net_usd": float(net), "cost_usd": 2.0, "net_r": net / 100.0,
                    "exit_reason": r[5] if len(r) > 5 else "TARGET",
                    "mae_points": float(mae) / n, "risk_points": 100.0, "risk_usd": 100.0 * n})
    return pd.DataFrame(out)


def run(rows, **kw):
    return simulate_account(prepare_trades(trades(rows), ("end_of_trade", "intratrade_bound")), rules(**kw))


def day(d, m=0, net=100.0):
    """One 30-minute trade on 2024-01-<d> entering at 15:<m> UTC (10:<m> New York)."""
    exit_ = f"2024-01-{d:02d} 15:{m + 30:02d}" if m < 30 else f"2024-01-{d:02d} 16:{m - 30:02d}"
    return (f"2024-01-{d:02d} 15:{m:02d}", exit_, net)


class TestRuleValidation(unittest.TestCase):
    def test_shipped_examples_are_valid_and_labelled_synthetic(self):
        files = sorted((REPO / "configs" / "prop").glob("*.yaml"))
        self.assertTrue(files)
        for f in files:
            r = validate_rules(yaml.safe_load(f.read_text()))
            self.assertTrue(r["synthetic_test_only"], f.name)
            self.assertIn("SYNTHETIC / TEST-ONLY", f.read_text())

    def test_errors_are_listed_and_unknown_keys_refused(self):
        bad = rules(target={"profit": -5}, surprise=1, drawdown={"max": 100, "mode": "sideways"})
        del bad["trading_day"]
        with self.assertRaises(PropConfigError) as cm:
            validate_rules(bad)
        text = " | ".join(cm.exception.errors)
        for s in ("target.profit must be > 0", "surprise is not a known field", "drawdown.mode",
                  "trading_day (timezone, reset_time) is required"):
            self.assertIn(s, text)

    def test_unmodellable_rules_refused_by_name(self):
        with self.assertRaisesRegex(PropConfigError, "intratrade_equity_high.*refused.*order of MFE and MAE"):
            validate_rules(rules(drawdown={"max": 100, "mode": "trailing", "trailing_reference": "intratrade_equity_high"}))
        with self.assertRaisesRegex(PropConfigError, "payouts is not supported"):
            validate_rules(rules(target={"profit": 100}, payouts={"split": 0.8}))
        with self.assertRaisesRegex(PropConfigError, "at least one rule"):
            validate_rules(rules())
        with self.assertRaisesRegex(PropConfigError, "currency must be USD"):
            validate_rules(rules(account={"starting_balance": 1, "currency": "EUR"}, target={"profit": 1}))

    def test_hash_ignores_cosmetic_text_but_not_rules(self):
        a = validate_rules(rules(target={"profit": 500}))
        b = validate_rules(rules(target={"profit": 500}, name="renamed", description="words"))
        c = validate_rules(rules(target={"profit": 501}))
        self.assertEqual(a.config_hash, b.config_hash)
        self.assertNotEqual(a.config_hash, c.config_hash)


class TestAccountTransitions(unittest.TestCase):
    def test_target_reached_needs_min_trading_days(self):
        rows = [day(2, net=3000), day(3, net=10), day(4, net=10), day(5, net=10), day(8, net=10), day(9, net=10)]
        s = run(rows, target={"profit": 3000}, min_trading_days=5)
        a = s["summary"]
        self.assertEqual(a["status"], "TARGET_REACHED")
        self.assertTrue(a["profit_target_reached"] and a["profit_target_balance_touched"] and a["survived"])
        self.assertEqual(a["trade_count"], 5)                          # stops on target: trade 6 not processed
        self.assertEqual(a["trades_not_processed"], 1)
        self.assertEqual(a["time_to_target"]["trading_days"], 5)
        self.assertEqual(a["time_to_target"]["at"], "2024-01-08 15:30:00+00:00")
        self.assertEqual([p["status"] for p in s["progression"]][:4], ["ACTIVE"] * 4)
        self.assertAlmostEqual(s["progression"][0]["target_progress"], 1.0)

    def test_survives_without_target_is_incomplete(self):
        a = run([day(2, net=100), day(3, net=-50)], target={"profit": 3000}, drawdown={"max": 500})["summary"]
        self.assertEqual(a["status"], "INCOMPLETE")
        self.assertTrue(a["survived"])
        self.assertFalse(a["drawdown_breach"] or a["daily_loss_breach"])
        self.assertEqual(a["incomplete_reasons"], ["profit target not reached by the end of the period"])
        self.assertAlmostEqual(a["ending_balance"], 10050)
        self.assertEqual(a["trading_days"], 2)

    def test_daily_loss_reaching_the_limit_is_a_breach(self):
        s = run([day(2, 0, net=-600), day(2, 40, net=-400), day(3, net=100)], daily_loss={"max": 1000})
        a = s["summary"]
        self.assertEqual(a["status"], "DAILY_LOSS_VIOLATION")
        self.assertTrue(a["daily_loss_breach"] and not a["survived"])
        self.assertEqual(a["violations"][0]["trade_no"], 2)
        self.assertEqual(a["trades_not_processed"], 1)
        self.assertAlmostEqual(a["max_daily_loss_usd_closed"], 1000)
        self.assertAlmostEqual(s["progression"][0]["daily_loss_headroom"], 400)

    def test_daily_reset_at_configured_local_time(self):
        # 21:00 UTC = 16:00 NY (day ending Jan 2); 22:30 UTC = 17:30 NY -> the day ending Jan 3
        rows = [("2024-01-02 20:00", "2024-01-02 21:00", -600), ("2024-01-02 22:30", "2024-01-02 23:00", -600)]
        a = run(rows, daily_loss={"max": 1000})["summary"]
        self.assertEqual(a["status"], "INCOMPLETE")
        self.assertEqual(a["trading_days"], 2)
        # an exit on the bar ending exactly at the reset belongs to the day that ends then
        s = run([("2024-01-02 20:00", "2024-01-02 22:00", -600)], daily_loss={"max": 1000})
        self.assertEqual(s["progression"][0]["day"], "2024-01-02")
        s = run([("2024-01-02 22:00", "2024-01-02 22:05", 5)], daily_loss={"max": 1000})
        self.assertEqual(s["progression"][0]["day"], "2024-01-03")

    def test_trade_crossing_reset_booked_on_exit_day(self):
        rows = [("2024-01-02 21:00", "2024-01-02 23:00", -700), ("2024-01-03 15:00", "2024-01-03 15:30", -400)]
        s = run(rows, daily_loss={"max": 1000})
        self.assertEqual(s["summary"]["trades_crossing_reset"], 1)
        self.assertEqual(s["progression"][0]["day"], "2024-01-03")
        self.assertEqual(s["summary"]["status"], "DAILY_LOSS_VIOLATION")   # both booked on the Jan 3 day
        with self.assertRaisesRegex(PropDataError, "crosses the daily reset.*maximum adverse excursion"):
            run(rows, daily_loss={"max": 1000}, detection="intratrade_bound")

    def test_static_drawdown_across_days(self):
        rows = [day(2, net=-900), day(3, net=-900), day(4, net=-700), day(5, net=5000)]
        a = run(rows, drawdown={"max": 2500}, daily_loss={"max": 1000})["summary"]
        self.assertEqual(a["status"], "MAX_DRAWDOWN_VIOLATION")
        self.assertEqual(a["time_to_breach"]["trading_days"], 3)
        self.assertAlmostEqual(a["max_drawdown_usd_closed"], 2500)
        self.assertFalse(a["daily_loss_breach"])

    def test_trailing_closed_vs_end_of_day_reference(self):
        rows = [day(2, 0, net=1000), day(2, 40, net=-1000), day(3, net=-500)]
        dd = {"max": 1200, "mode": "trailing"}
        closed = run(rows, drawdown={**dd, "trailing_reference": "closed_balance"})
        self.assertEqual(closed["summary"]["status"], "MAX_DRAWDOWN_VIOLATION")      # floor 9800 after +1000
        self.assertEqual(closed["summary"]["violations"][0]["trade_no"], 3)            # 9500 <= 9800
        eod = run(rows, drawdown={**dd, "trailing_reference": "end_of_day_balance"})
        self.assertEqual(eod["summary"]["status"], "INCOMPLETE")                  # intraday peak not used
        self.assertEqual([p["drawdown_floor"] for p in eod["progression"]], [8800, 8800, 8800])
        locked = run([day(2, net=3000), day(3, net=-1900)],
                     drawdown={**dd, "trailing_reference": "closed_balance", "lock_floor_at": 10000})
        self.assertEqual(locked["progression"][0]["drawdown_floor"], 10000)          # capped at the lock
        self.assertEqual(locked["summary"]["status"], "INCOMPLETE")

    def test_simultaneous_daily_loss_and_drawdown(self):
        a = run([day(2, net=-1200)], drawdown={"max": 1000}, daily_loss={"max": 1000})["summary"]
        self.assertEqual(a["status"], "MAX_DRAWDOWN_VIOLATION")                      # documented precedence
        self.assertTrue(a["drawdown_breach"] and a["daily_loss_breach"])
        self.assertEqual([v["rule"] for v in a["violations"]], ["MAX_DRAWDOWN", "DAILY_LOSS"])

    def test_intratrade_bound_breach_precedes_target_on_same_trade(self):
        # closes +500 (target 500) but its recorded MAE is 1100 points x 1 x 1 -> bound 10000-1100-2
        rows = [(*day(2, net=500), 1, 1100)]
        eot = run(rows, target={"profit": 500}, daily_loss={"max": 1000})["summary"]
        self.assertEqual(eot["status"], "TARGET_REACHED")
        itb = run(rows, target={"profit": 500}, daily_loss={"max": 1000}, detection="intratrade_bound")["summary"]
        self.assertEqual(itb["status"], "DAILY_LOSS_VIOLATION")
        self.assertFalse(itb["profit_target_reached"])
        self.assertEqual(itb["violations"][0]["detection"], "intratrade_bound")
        self.assertTrue(itb["violations"][0]["at"].startswith("within ["))
        self.assertAlmostEqual(itb["max_daily_loss_usd_intratrade_bound"], 1102)

    def test_position_size_violation_is_recorded_never_clipped(self):
        rows = [(*day(2, net=100), 3), day(3, net=100)]
        a = run(rows, position={"max_units": 2}, target={"profit": 5000})["summary"]
        self.assertEqual(a["status"], "RULE_VIOLATION")
        self.assertEqual(a["violations"][0]["rule"], "MAX_POSITION_SIZE")
        self.assertIn("not clipped", a["violations"][0]["detail"])
        self.assertEqual(a["trade_count"], 0)                            # terminated at entry; P&L not booked
        rec = run(rows, position={"max_units": 2, "action": "record"}, target={"profit": 5000})
        self.assertEqual(rec["summary"]["status"], "INCOMPLETE")
        self.assertEqual(rec["progression"][0]["contracts"], 3)          # the real size, unchanged
        self.assertTrue(rec["summary"]["rule_violation"])
        scaled = run([day(2, net=1000), (*day(3, net=100), 3)], target={"profit": 5000},
                     position={"max_units": 2, "scaling": [{"min_profit": 1000, "max_units": 4}]})
        self.assertEqual(scaled["summary"]["violations"], [])

    def test_session_rules(self):
        s = {"timezone": "America/New_York", "entry_start": "09:30", "entry_end": "15:00", "flat_by": "15:55"}
        a = run([("2024-01-02 13:00", "2024-01-02 13:30", 10)], session=s)["summary"]     # 08:00 NY
        self.assertEqual(a["violations"][0]["rule"], "SESSION_ENTRY_WINDOW")
        b = run([("2024-01-02 19:00", "2024-01-02 21:05", 10)], session=s)["summary"]     # held past 15:55 NY
        self.assertEqual(b["violations"][0]["rule"], "SESSION_FLAT_BY")
        ok = run([("2024-01-02 19:00", "2024-01-02 20:55", 10)], session=s)["summary"]    # exits at 15:55
        self.assertEqual(ok["violations"], [])

    def test_pause_day_skips_rest_of_day_then_continues(self):
        rows = [day(2, 0, net=-1000), day(2, 40, net=500), day(3, net=200)]
        s = run(rows, daily_loss={"max": 1000, "action": "pause_day"})
        a = s["summary"]
        self.assertEqual(a["status"], "INCOMPLETE")
        self.assertTrue(a["daily_loss_breach"])
        self.assertEqual(a["trades_skipped_daily_loss_pause"], 1)
        self.assertAlmostEqual(a["ending_balance"], 9200)

    def test_deadline_consistency_payout_and_end_of_data(self):
        a = run([day(2, net=100), day(12, net=100)], target={"profit": 1000}, max_calendar_days=5)["summary"]
        self.assertEqual((a["status"], a["violations"][0]["rule"]), ("RULE_VIOLATION", "EVALUATION_DEADLINE"))
        rows = [day(2, net=900), day(3, net=100)]
        c = run(rows, target={"profit": 1000}, consistency={"max_best_day_share": 0.5},
                payout_eligibility={"min_trading_days": 2, "min_profit": 500})["summary"]
        self.assertEqual(c["status"], "INCOMPLETE")
        self.assertEqual(c["incomplete_reasons"], ["consistency rule not met"])
        self.assertAlmostEqual(c["best_day_share_of_profit"], 0.9)
        self.assertTrue(c["payout_eligible"])
        e = run([(*day(2, net=10), 1, 0, "END_OF_DATA")], target={"profit": 1000})["summary"]
        self.assertEqual(e["trades_end_of_data"], 1)


class TestStreamProperties(unittest.TestCase):
    ROWS = [day(2, 0, net=300), day(2, 40, net=-200), day(3, net=-900), day(4, net=700), day(5, net=-100)]
    RULES = rules(target={"profit": 2000}, drawdown={"max": 1500, "mode": "trailing",
                                                     "trailing_reference": "closed_balance"},
                  daily_loss={"max": 1000})

    def test_chronology_not_input_order_and_no_mutation(self):
        t = trades(self.ROWS)
        shuffled = t.sample(frac=1, random_state=3)
        before = shuffled.copy(deep=True)
        a = simulate_accounts(t, [{"account_id": "A", "rules": self.RULES}])
        b = simulate_accounts(shuffled, [{"account_id": "A", "rules": self.RULES}])
        self.assertEqual(a, b)
        pd.testing.assert_frame_equal(shuffled, before)
        self.assertEqual([p["trade_no"] for p in a["accounts"][0]["progression"]], [1, 2, 3, 4, 5])

    def test_overlapping_positions_and_missing_columns_refused(self):
        with self.assertRaisesRegex(PropDataError, "overlap"):
            prepare_trades(trades([day(2, 0), ("2024-01-02 15:10", "2024-01-02 15:20", 5)]), ("end_of_trade",))
        with self.assertRaisesRegex(PropDataError, "mae_points"):
            prepare_trades(trades(self.ROWS).drop(columns=["mae_points"]), ("intratrade_bound",))

    def test_multiple_accounts_are_independent(self):
        other = rules(id="T2", target={"profit": 500}, daily_loss={"max": 800})
        out = simulate_accounts(trades(self.ROWS), [
            {"account_id": "A1", "rules": self.RULES}, {"account_id": "A2", "rules": self.RULES},
            {"account_id": "A3", "rules": other},
            {"account_id": "A4", "rules": self.RULES, "start": "2024-01-03"}])
        s = {x["summary"]["account_id"]: x["summary"] for x in out["accounts"]}
        self.assertEqual(s["A1"], {**s["A2"], "account_id": "A1"})
        self.assertEqual(s["A3"]["status"], "DAILY_LOSS_VIOLATION")
        self.assertEqual(s["A4"]["trade_count"], 3)                       # starts with the Jan 3 trade
        self.assertEqual(s["A1"]["trade_count"], 5)
        with self.assertRaisesRegex(ValueError, "unique"):
            simulate_accounts(trades(self.ROWS), [{"account_id": "A", "rules": self.RULES}] * 2)

    def test_deterministic_ids(self):
        t = trades(self.ROWS)
        a = simulate_accounts(t, [{"account_id": "A", "rules": self.RULES}])
        src = {"run_id": "RUN_2026_00001", "trades_hash": "x"}
        self.assertEqual(simulation_id(src, a["account_configs"]), simulation_id(src, a["account_configs"]))
        b = simulate_accounts(t, [{"account_id": "A", "rules": rules(target={"profit": 2001})}])
        self.assertNotEqual(simulation_id(src, a["account_configs"]), simulation_id(src, b["account_configs"]))
        self.assertEqual(trades_fingerprint(t), trades_fingerprint(t.copy()))


class TestServiceAndApi(unittest.TestCase):
    """The prop path over a REAL stored run of ema_crossover on a synthetic dataset."""

    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from tests.phase2_helpers import TEST_FEED, TEST_PROXY, add_test_proxy_feed, synthetic_canonical, write_generic_utc
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", cls.root / "configs")
        add_test_proxy_feed(cls.root / "configs")
        write_generic_utc(synthetic_canonical("2024-01-02", "2024-03-28", tf=5, seed=5), cls.root / "h.csv")
        cls.svc = Services(root=cls.root)
        cls.did = cls.svc.import_file(dict(file=str(cls.root / "h.csv"), instrument=TEST_PROXY,
                                           provider=TEST_FEED, asset_type="CFD", timeframe="5m",
                                           source_timezone="UTC", calendar="CME_EQUITY", price_basis="bid",
                                           build_features=False))["dataset_id"]
        cls.run_id = cls.svc.backtest_strategy(EMA, cls.did, True)["run_id"]

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root, ignore_errors=True)

    def snapshot(self):
        rec, tr = self.svc.store.load_run(self.run_id)
        return json.dumps(rec, sort_keys=True, default=str), trades_fingerprint(tr), len(self.svc.store.list_runs())

    def test_lineage_read_only_and_record(self):
        before = self.snapshot()
        accounts = [{"account_id": "A1", "config": "SYNTH_STATIC_EVAL"},
                    {"account_id": "A2", "config": "SYNTH_TRAILING_EVAL", "start": "2024-02-01"}]
        r = self.svc.prop_simulate(self.run_id, accounts, record=True)
        self.assertEqual(self.snapshot(), before)                         # run record, trades, run count
        rec, _ = self.svc.store.load_run(self.run_id)
        L = r["lineage"]
        self.assertEqual((L["source_run_id"], L["strategy_id"], L["dataset_id"]),
                         (self.run_id, rec["strategy"]["strategy_id"], self.did))
        self.assertEqual(L["definition_hash"], rec["strategy"]["dsl"]["definition_hash"])
        from tests.phase2_helpers import TEST_PROXY_PROFILE
        self.assertEqual((L["cost_profile"], L["cost_status"]), (TEST_PROXY_PROFILE, "assumed"))
        self.assertEqual(L["trades_hash"], rec["trades_hash"])
        self.assertTrue(L["trades_hash_verified"])
        self.assertIn("exit_ts", L["ordering"])
        self.assertEqual([c["prop_config_id"] for c in r["account_configs"]], ["SYNTH_STATIC_EVAL", "SYNTH_TRAILING_EVAL"])
        self.assertTrue(all(c["prop_config_hash"] for c in r["account_configs"]))
        self.assertEqual(r["strategy_result"]["net_r"], rec["headline_metrics"]["net_r"])
        self.assertTrue(any("SYNTHETIC TEST-ONLY RULES" in x for x in r["labels"]))
        self.assertIn("not evidence that the strategy is profitable", r["labels"][0])
        self.assertTrue(r["recorded"])
        self.assertEqual(self.svc.get_prop_simulation(r["simulation_id"])["accounts"], r["accounts"])
        self.assertIn(r["simulation_id"], [x["simulation_id"] for x in self.svc.list_prop_simulations()])
        again = self.svc.prop_simulate(self.run_id, accounts)
        self.assertEqual((again["simulation_id"], again["accounts"]), (r["simulation_id"], r["accounts"]))

    def test_tampered_trades_refused(self):
        from unittest import mock
        rec, tr = self.svc.store.load_run(self.run_id)
        tr2 = tr.copy()
        tr2.loc[0, "net_usd"] += 1.0
        with mock.patch.object(self.svc.store, "load_run", return_value=(rec, tr2)):
            with self.assertRaisesRegex(PropDataError, "do not match the run's trades_hash"):
                self.svc.prop_simulate(self.run_id, [{"config": "SYNTH_STATIC_EVAL"}])

    def test_http_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.root).test_client()
        cfgs = c.get("/api/prop/configs").get_json()
        self.assertEqual({x["id"] for x in cfgs}, {"SYNTH_STATIC_EVAL", "SYNTH_TRAILING_EVAL"})
        self.assertFalse(c.post("/api/prop/validate", json={"config": {"kind": "x"}}).get_json()["valid"])
        self.assertTrue(c.post("/api/prop/validate", json={"config": cfgs[0]["text"]}).get_json()["valid"])
        r = c.post("/api/prop/simulate", json={"run_id": self.run_id, "accounts": [{"config": "SYNTH_STATIC_EVAL"}]})
        self.assertEqual(r.status_code, 200, r.get_data(as_text=True)[:300])
        sim = r.get_json()
        json.dumps(sim, allow_nan=False)
        self.assertEqual(c.get(f"/api/prop/simulations/{sim['simulation_id']}").status_code, 200)
        self.assertEqual(c.post("/api/prop/simulate", json={"run_id": "x", "accounts": []}).status_code, 400)
        self.assertEqual(c.post("/api/prop/simulate", json={"run_id": "RUN_2026_09999",
                                                            "accounts": [{"config": "SYNTH_STATIC_EVAL"}]}).status_code, 404)
        bad = c.post("/api/prop/simulate", json={"run_id": self.run_id, "accounts": [{"config": {"kind": "x"}}]})
        self.assertEqual((bad.status_code, bad.get_json()["error"]["kind"]), (422, "prop_config"))
        self.assertEqual(c.get("/api/prop/simulations/PROP_NOPE").status_code, 400)

    def test_cli(self):
        from edgelab.cli import main
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = main(["--root", str(self.root), "--json", "prop", "simulate", self.run_id, "--config",
                         "SYNTH_STATIC_EVAL", "--accounts", "3"])
        self.assertEqual(code, 0)
        out = json.loads(buf.getvalue())
        self.assertEqual([a["summary"]["account_id"] for a in out["accounts"]], ["A1", "A2", "A3"])
        with contextlib.redirect_stderr(io.StringIO()):
            self.assertEqual(main(["--root", str(self.root), "prop", "simulate", self.run_id]), 2)


if __name__ == "__main__":
    unittest.main()
