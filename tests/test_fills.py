"""Deterministic fill scenarios. Every expected number below is computed by hand.

Convention in these tests: long signal on bar 0, market fill at open of bar 1 = 100,
stop 10 points (90), target 20 points (120). NQ: 1 point = $20, so 1R = $200.
"""
import math
import unittest

import pandas as pd

from edgelab.data.resample import resample_bars
from edgelab.data.synthetic import bars_from_ohlc, generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import BacktestError, run_backtest
from edgelab.engine.signals import OrderSpec
from tests.helpers import (CME, NQ, UTC247, ZERO_COSTS, Scripted, bt_cfg, dataset, flat_bar)

MKT = OrderSpec("market", stop_points=10, target_points=20)
B0 = flat_bar(100)
B1 = (100, 105, 98, 104)        # fill bar: no touch of 90 / 120


def run(rows, order=MKT, signals=None, cfg=None, ltf=None, tf=1, **kw):
    ds = dataset(rows, tf=tf)
    strat = Scripted(order, signals or {0: 1}, **kw)
    return run_backtest(ds, strat, ZERO_COSTS, cfg or bt_cfg(), ltf=ltf)


def only(res):
    assert len(res.trades) == 1, res.trades
    return res.trades.iloc[0]


class TestMarketEntryAndExits(unittest.TestCase):
    def test_fill_at_next_open_and_target(self):
        t = only(run([B0, B1, (104, 121, 103, 119)]))
        self.assertEqual(t.entry_bar, 1)
        self.assertEqual(t.entry_price_theo, 100)
        self.assertEqual((t.exit_reason, t.exit_price_theo, t.exit_bar), ("TARGET", 120, 2))
        self.assertAlmostEqual(t.gross_r, 2.0)
        self.assertAlmostEqual(t.gross_usd, 400.0)   # 20 pts x $20

    def test_stop(self):
        t = only(run([B0, B1, (104, 105, 89, 90)]))
        self.assertEqual((t.exit_reason, t.exit_price_theo), ("STOP", 90))
        self.assertAlmostEqual(t.gross_r, -1.0)

    def test_short_mirror(self):
        rows = [B0, (100, 102, 95, 96), (96, 97, 79, 81)]
        t = only(run(rows, signals={0: -1}))
        self.assertEqual((t.exit_reason, t.exit_price_theo), ("TARGET", 80))
        self.assertAlmostEqual(t.gross_r, 2.0)

    def test_stop_hit_on_fill_bar(self):
        t = only(run([B0, (100, 101, 89.75, 95)]))
        self.assertEqual((t.exit_reason, t.exit_bar), ("STOP", 1))

    def test_gap_through_stop_fills_at_open(self):
        t = only(run([B0, B1, (85, 86, 84, 85)]))
        self.assertEqual((t.exit_reason, t.exit_price_theo), ("STOP_GAP", 85))
        self.assertAlmostEqual(t.gross_r, -1.5)   # worse than -1R: stops are not guaranteed

    def test_gap_through_target(self):
        rows = [B0, B1, (125, 126, 124, 125)]
        t = only(run(rows))
        self.assertEqual((t.exit_reason, t.exit_price_theo), ("TARGET_GAP", 120))
        cfg = bt_cfg(gap_fill={"target": "open"})
        self.assertEqual(only(run(rows, cfg=cfg)).exit_price_theo, 125)

    def test_time_exit_counts_entry_bar_as_one(self):
        order = OrderSpec("market", stop_points=10, target_points=20, time_exit_bars=2)
        t = only(run([B0, B1, (104, 106, 101, 103), flat_bar(103)], order=order))
        self.assertEqual((t.exit_reason, t.exit_bar, t.exit_price_theo), ("TIME", 2, 103))
        self.assertEqual(t.bars_held, 2)

    def test_no_target_exits_at_end_of_data(self):
        order = OrderSpec("market", stop_points=10)
        t = only(run([B0, B1, flat_bar(104)], order=order))
        self.assertEqual((t.exit_reason, t.exit_price_theo), ("END_OF_DATA", 104))
        self.assertTrue(math.isnan(t.target_price))

    def test_signal_on_last_bar_is_skipped(self):
        res = run([B0, B1], signals={1: 1})
        self.assertEqual(len(res.trades), 0)
        self.assertEqual(res.skipped, {"NO_NEXT_BAR": 1})

    def test_one_position_at_a_time(self):
        res = run([B0, B1, flat_bar(104), (104, 121, 103, 119)], signals={0: 1, 1: 1, 2: 1})
        self.assertEqual(len(res.trades), 1)
        self.assertEqual(res.skipped["BUSY_IN_POSITION_OR_ORDER"], 2)

    def test_new_signal_allowed_on_exit_bar(self):
        rows = [B0, B1, (104, 121, 103, 119), flat_bar(119), flat_bar(119)]
        res = run(rows, signals={0: 1, 2: 1})   # 2nd signal decided at close of exit bar 2
        self.assertEqual(len(res.trades), 2)
        self.assertEqual(res.trades.iloc[1].entry_bar, 3)

    def test_absolute_stop_beyond_fill_is_skipped_and_counted(self):
        order = OrderSpec("market", target_points=20)
        res = run([B0, (89, 95, 88, 94), flat_bar(94)], order=order, stop={0: 90.0})
        self.assertEqual(res.skipped, {"STOP_BEYOND_FILL": 1})

    def test_missing_stop_rejected(self):
        with self.assertRaises(ValueError):
            run([B0, B1], order=OrderSpec("market", target_points=20))


class TestSameBarConflict(unittest.TestCase):
    ROWS = [B0, B1, (104, 121, 89, 100)]   # bar 2 touches BOTH 90 and 120

    def test_conservative_takes_stop(self):
        t = only(run(self.ROWS, cfg=bt_cfg(same_bar_policy="conservative")))
        self.assertEqual((t.exit_reason, t.conflict_resolution), ("STOP", "CONSERVATIVE"))

    def test_optimistic_takes_target(self):
        t = only(run(self.ROWS, cfg=bt_cfg(same_bar_policy="optimistic")))
        self.assertEqual((t.exit_reason, t.conflict_resolution), ("TARGET", "OPTIMISTIC"))

    def test_intrabar_without_ltf_falls_back_and_says_so(self):
        res = run(self.ROWS, cfg=bt_cfg(same_bar_policy="intrabar"))
        t = only(res)
        self.assertEqual(t.exit_reason, "STOP")
        self.assertEqual(t.conflict_resolution, "INTRABAR_UNAVAILABLE->CONSERVATIVE")
        self.assertIn("unavailable", res.assumptions["same_bar_policy_effective"])

    # --- intrabar with real lower-timeframe data (5m built from 1m) ---------------------
    @staticmethod
    def _htf_ltf(conflict_minutes):
        one = [flat_bar(100)] * 5 + [(100, 104, 99, 104)] + [flat_bar(104)] * 4 + list(conflict_minutes)
        df1 = bars_from_ohlc(one, tf_minutes=1)
        ltf = validate_and_freeze(df1, NQ, UTC247, "1m", 1, "test", "LTF")
        htf = validate_and_freeze(resample_bars(df1, UTC247, 5), NQ, UTC247, "5m", 5, "test", "HTF")
        return htf, ltf

    def _run_ib(self, conflict_minutes):
        htf, ltf = self._htf_ltf(conflict_minutes)
        self.assertTrue((htf.bars.high[2] >= 120) and (htf.bars.low[2] <= 90))  # genuine HTF conflict
        return only(run_backtest(htf, Scripted(MKT, {0: 1}), ZERO_COSTS,
                                 bt_cfg(same_bar_policy="intrabar"), ltf=ltf))

    def test_intrabar_target_first(self):
        t = self._run_ib([(104, 121, 103, 110), (110, 111, 95, 96), (96, 97, 89, 92),
                          flat_bar(92), flat_bar(92)])
        self.assertEqual((t.exit_reason, t.conflict_resolution), ("TARGET", "INTRABAR"))

    def test_intrabar_stop_first(self):
        t = self._run_ib([(104, 105, 89, 95), (95, 121, 94, 110), flat_bar(110),
                          flat_bar(110), flat_bar(110)])
        self.assertEqual((t.exit_reason, t.conflict_resolution), ("STOP", "INTRABAR"))

    def test_intrabar_ambiguous_minute_falls_back(self):
        t = self._run_ib([(104, 121, 89, 100), flat_bar(100), flat_bar(100), flat_bar(100),
                          flat_bar(100)])
        self.assertEqual((t.exit_reason, t.conflict_resolution),
                         ("STOP", "INTRABAR_AMBIGUOUS->CONSERVATIVE"))


class TestPendingEntries(unittest.TestCase):
    STOP_ORDER = OrderSpec("stop", stop_points=10, target_points=20)
    LIMIT_ORDER = OrderSpec("limit", stop_points=10, target_points=20)

    def test_buy_stop_fills_at_level(self):
        t = only(run([B0, (100, 105, 98, 104), (104, 123, 103, 121)], order=self.STOP_ORDER,
                     entry={0: 102}))
        self.assertEqual((t.entry_price_theo, t.entry_fill_kind), (102, "level"))
        self.assertEqual((t.stop_price, t.target_price), (92, 122))

    def test_buy_stop_gap_fills_at_open(self):
        t = only(run([B0, (103, 105, 102.5, 104), flat_bar(104)], order=self.STOP_ORDER,
                     entry={0: 102}))
        self.assertEqual((t.entry_price_theo, t.entry_fill_kind), (103, "open"))

    def test_stop_entry_target_on_fill_bar_is_certain(self):
        # open 100 < L 102 < target 122: price passed L on the way to 122 -> target after fill.
        t = only(run([B0, (100, 123, 98, 110), flat_bar(110)], order=self.STOP_ORDER,
                     entry={0: 102}))
        self.assertEqual((t.exit_reason, t.exit_bar, t.conflict_resolution), ("TARGET", 1, ""))

    def test_stop_entry_uncertain_stop_on_fill_bar(self):
        # low 91 <= stop 92, but the dip may have happened BEFORE the fill -> ambiguous.
        rows = [B0, (100, 105, 91, 104), flat_bar(104), flat_bar(104)]
        t = only(run(rows, order=self.STOP_ORDER, entry={0: 102}))
        self.assertEqual((t.exit_reason, t.conflict_resolution), ("STOP", "CONSERVATIVE"))
        t = only(run(rows, order=self.STOP_ORDER, entry={0: 102},
                     cfg=bt_cfg(same_bar_policy="optimistic")))
        self.assertEqual(t.exit_reason, "END_OF_DATA")   # optimistic: stop was pre-fill

    def test_limit_fill_and_price_improvement(self):
        t = only(run([B0, (100, 101, 97, 99), flat_bar(99)], order=self.LIMIT_ORDER, entry={0: 98}))
        self.assertEqual((t.entry_price_theo, t.entry_fill_kind), (98, "level"))
        t = only(run([B0, (97, 99, 96.5, 98), flat_bar(98)], order=self.LIMIT_ORDER, entry={0: 98}))
        self.assertEqual((t.entry_price_theo, t.entry_fill_kind), (97, "open"))

    def test_limit_not_filled_expires(self):
        res = run([B0, (100, 101, 98.5, 99), flat_bar(99)], order=self.LIMIT_ORDER, entry={0: 98})
        self.assertEqual(len(res.trades), 0)
        self.assertEqual(res.skipped, {"ENTRY_NOT_FILLED": 1})

    def test_limit_penetration_requirement(self):
        cfg = bt_cfg(limit_fill={"penetration_ticks": 1})
        res = run([B0, (100, 101, 98, 99), flat_bar(99)], order=self.LIMIT_ORDER, entry={0: 98}, cfg=cfg)
        self.assertEqual(res.skipped, {"ENTRY_NOT_FILLED": 1})     # touch only
        res = run([B0, (100, 101, 97.75, 99), flat_bar(99)], order=self.LIMIT_ORDER, entry={0: 98}, cfg=cfg)
        self.assertEqual(len(res.trades), 1)                        # traded through

    def test_limit_entry_uncertain_target_on_fill_bar(self):
        # high 125 >= target 118 may have printed BEFORE price fell to the 98 limit.
        rows = [B0, (100, 125, 97, 99), flat_bar(99)]
        t = only(run(rows, order=self.LIMIT_ORDER, entry={0: 98}))
        self.assertEqual(t.exit_reason, "END_OF_DATA")               # conservative: no credit
        t = only(run(rows, order=self.LIMIT_ORDER, entry={0: 98}, cfg=bt_cfg(same_bar_policy="optimistic")))
        self.assertEqual(t.exit_reason, "TARGET")

    def test_order_expiry_window(self):
        order = OrderSpec("stop", stop_points=10, target_points=20, entry_expiry_bars=3)
        t = only(run([B0, flat_bar(100), flat_bar(100), (100, 103, 99.5, 102.5), flat_bar(103)],
                     order=order, entry={0: 102}))
        self.assertEqual(t.entry_bar, 3)


class TestSessionRules(unittest.TestCase):
    def setUp(self):
        df, _ = generate_bars(CME, "2024-01-08", "2024-01-10", tf_minutes=5, sigma_per_bar=0.5, seed=3)
        self.ds = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "SESS")
        self.local = self.ds.bars.ts.tz_convert("America/New_York")

    def _idx(self, hhmm, day=9):
        return int(((self.local.day == day) & (self.local.strftime("%H:%M") == hhmm)).argmax())

    def test_flatten_at_session_time(self):
        cfg = bt_cfg(session={"flatten_daily": True, "flatten_time": "16:00", "hold_overnight": False})
        i = self._idx("15:30")
        res = run_backtest(self.ds, Scripted(OrderSpec("market", stop_points=500), {i: 1}),
                           ZERO_COSTS, cfg)
        t = only(res)
        self.assertEqual(t.exit_reason, "SESSION_CLOSE")
        self.assertEqual(t.exit_ts.tz_convert("America/New_York").strftime("%H:%M"), "16:00")

    def test_no_entry_on_flatten_bar_or_into_next_session(self):
        cfg = bt_cfg(session={"flatten_daily": True, "flatten_time": "16:00", "hold_overnight": False})
        i = self._idx("15:50")   # entry bar would be 15:55-16:00, the flatten bar
        res = run_backtest(self.ds, Scripted(OrderSpec("market", stop_points=500), {i: 1}), ZERO_COSTS, cfg)
        self.assertEqual(res.skipped, {"ENTRY_BLOCKED": 1})
        j = self._idx("16:55")   # last bar of trading day; next bar is a new session
        res = run_backtest(self.ds, Scripted(OrderSpec("market", stop_points=500), {j: 1}), ZERO_COSTS, cfg)
        self.assertEqual(res.skipped, {"NEXT_BAR_NEW_SESSION": 1})

    def test_max_trades_per_day(self):
        cfg = bt_cfg(max_trades_per_day=1)
        order = OrderSpec("market", stop_points=500, time_exit_bars=1)
        sig = {self._idx("10:00"): 1, self._idx("10:30"): 1, self._idx("11:00"): 1}
        res = run_backtest(self.ds, Scripted(order, sig), ZERO_COSTS, cfg)
        self.assertEqual(len(res.trades), 1)
        self.assertEqual(res.skipped, {"MAX_TRADES_PER_DAY": 2})


class TestGates(unittest.TestCase):
    def test_rejects_raw_dataframe(self):
        with self.assertRaises(BacktestError):
            run_backtest(bars_from_ohlc([B0, B1]), Scripted(MKT, {0: 1}), ZERO_COSTS, bt_cfg())

    def test_entries_never_on_signal_bar(self):
        df, _ = generate_bars(UTC247, "2024-01-01", "2024-01-03", seed=5)
        ds = validate_and_freeze(df, NQ, UTC247, "1m", 1, "synthetic", "G")
        sig = {i: (1 if i % 2 else -1) for i in range(10, 2000, 37)}
        res = run_backtest(ds, Scripted(OrderSpec("market", stop_points=5, target_points=5), sig),
                           ZERO_COSTS, bt_cfg())
        self.assertGreater(len(res.trades), 10)
        self.assertTrue((res.trades.entry_bar > res.trades.signal_bar).all())
        self.assertTrue((res.trades.exit_bar >= res.trades.entry_bar).all())
        self.assertTrue((res.trades.entry_ts >= res.trades.signal_ts).all())


if __name__ == "__main__":
    unittest.main()
