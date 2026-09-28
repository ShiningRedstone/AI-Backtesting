"""Phase 3 engine extension: signal exits (SignalSet.exit_long / exit_short).

Contract: a flag at bar k means "at the close of k, exit the open position"; the exit is a
market order at the OPEN of bar k+1 (same timing rule as entries). Stops/targets/time/session
exits that happen earlier win; a gap through the stop/target at that open fills the resting
order (existing gap policy). Absent arrays = Phase 1 behaviour, unchanged."""
import unittest

import numpy as np

from edgelab.engine.backtester import run_backtest
from edgelab.engine.signals import OrderSpec, check_causality
from edgelab.data.schema import BarArrays
from tests.helpers import ZERO_COSTS, Scripted, bt_cfg, dataset, flat_bar


class ScriptedExit(Scripted):
    family = "scripted_exit"

    def __init__(self, order, signals, exit_long=(), exit_short=(), none=False, **kw):
        super().__init__(order, signals, **kw)
        self._xl, self._xs, self._none = set(exit_long), set(exit_short), none

    def generate_signals(self, bars: BarArrays):
        s = super().generate_signals(bars)
        if not self._none:
            n = len(bars)
            s.exit_long = np.isin(np.arange(n), list(self._xl))
            s.exit_short = np.isin(np.arange(n), list(self._xs))
        return s


# bars 0-2 flat at 100; entry signal at bar 2 -> market fill at the open of bar 3 (101)
ROWS = [flat_bar(100)] * 3 + [flat_bar(101), flat_bar(102), flat_bar(103), (105, 105.5, 104.5, 105),
                              flat_bar(105), flat_bar(105), flat_bar(105)]
ORDER = OrderSpec("market", stop_points=20.0)


def run(rows, strat):
    return run_backtest(dataset(rows), strat, ZERO_COSTS, bt_cfg()).trades


class TestSignalExit(unittest.TestCase):
    def test_exit_at_next_open_with_reason_signal(self):
        t = run(ROWS, ScriptedExit(ORDER, {2: 1}, exit_long=[5])).iloc[0]
        self.assertEqual((t["entry_bar"], t["entry_price_theo"]), (3, 101))
        self.assertEqual((t["exit_bar"], t["exit_reason"], t["exit_price_theo"]), (6, "SIGNAL", 105))
        self.assertEqual(t["mfe_points"], 4)            # bars 3..5 plus the exit open, not bar 6's range

    def test_earlier_stop_wins(self):
        rows = list(ROWS)
        rows[4] = (101, 101.5, 80.0, 81.0)              # stop 81 touched on bar 4
        t = run(rows, ScriptedExit(ORDER, {2: 1}, exit_long=[5])).iloc[0]
        self.assertEqual((t["exit_bar"], t["exit_reason"]), (4, "STOP"))

    def test_gap_through_stop_at_the_exit_open(self):
        rows = list(ROWS)
        rows[6] = (70, 70.5, 69.5, 70)                  # opens below the stop (81)
        t = run(rows, ScriptedExit(ORDER, {2: 1}, exit_long=[5])).iloc[0]
        self.assertEqual((t["exit_bar"], t["exit_reason"], t["exit_price_theo"]), (6, "STOP_GAP", 70))

    def test_flag_on_entry_bar_and_flags_before_entry(self):
        t = run(ROWS, ScriptedExit(ORDER, {2: 1}, exit_long=[3])).iloc[0]
        self.assertEqual((t["exit_bar"], t["exit_reason"], t["exit_price_theo"]), (4, "SIGNAL", 102))
        t = run(ROWS, ScriptedExit(ORDER, {2: 1}, exit_long=[1, 2])).iloc[0]    # before the fill
        self.assertEqual(t["exit_reason"], "END_OF_DATA")

    def test_long_and_short_flags_are_separate(self):
        t = run(ROWS, ScriptedExit(ORDER, {2: 1}, exit_short=[4])).iloc[0]
        self.assertEqual(t["exit_reason"], "END_OF_DATA")
        down = [flat_bar(110 - i) for i in range(10)]
        t = run(down, ScriptedExit(ORDER, {2: -1}, exit_short=[4], exit_long=[3])).iloc[0]
        self.assertEqual((t["direction"], t["exit_bar"], t["exit_reason"]), (-1, 5, "SIGNAL"))

    def test_time_stop_before_signal_wins(self):
        o = OrderSpec("market", stop_points=20.0, time_exit_bars=2)
        t = run(ROWS, ScriptedExit(o, {2: 1}, exit_long=[5])).iloc[0]
        self.assertEqual((t["exit_bar"], t["exit_reason"]), (4, "TIME"))

    def test_flag_on_last_bar_is_end_of_data(self):
        t = run(ROWS, ScriptedExit(ORDER, {2: 1}, exit_long=[len(ROWS) - 1])).iloc[0]
        self.assertEqual(t["exit_reason"], "END_OF_DATA")

    def test_absent_or_empty_exit_arrays_change_nothing(self):
        a = run(ROWS, ScriptedExit(ORDER, {2: 1}, none=True))
        b = run(ROWS, ScriptedExit(ORDER, {2: 1}))
        cols = ["entry_bar", "exit_bar", "exit_reason", "exit_price_theo", "net_r"]
        self.assertTrue(a[cols].equals(b[cols]))

    def test_signal_exit_is_a_market_exit_for_costs(self):
        from edgelab.engine.costs import CostModel
        cm = CostModel(slippage_ticks_market=2, slippage_ticks_stop=5)
        t = run_backtest(dataset(ROWS), ScriptedExit(ORDER, {2: 1}, exit_long=[5]), cm, bt_cfg()).trades.iloc[0]
        self.assertAlmostEqual(t["exit_price_theo"] - t["exit_price_eff"], 2 * 0.25)   # market slippage

    def test_leaky_exit_arrays_fail_the_causality_check(self):
        class Leaky(ScriptedExit):
            def generate_signals(self, bars):
                s = super().generate_signals(bars)
                nxt = np.r_[bars.close[1:], np.nan]
                s.exit_long = np.nan_to_num(nxt) > bars.close          # peeks at the next close
                return s
        rows = [flat_bar(100 + (i % 7)) for i in range(200)]
        rep = check_causality(Leaky(ORDER, {2: 1}), dataset(rows).bars, n_cuts=10)
        self.assertFalse(rep.passed)
        self.assertIn("exit_long", rep.detail)


if __name__ == "__main__":
    unittest.main()
