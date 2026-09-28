"""Lookahead prevention: the causality check must catch strategies that peek forward."""
import unittest

import numpy as np

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.engine.backtester import BacktestError, run_backtest
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy, check_causality
from edgelab.strategies.examples import Breakout, RandomEntry
from tests.helpers import CFG, CME, NQ, ZERO_COSTS

ORDER = OrderSpec("market", stop_points=10, target_points=20)


class PeekNextClose(Strategy):
    """Classic bug: uses close[i+1] to decide at bar i."""
    family = "peek_next_close"

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        nxt = np.r_[bars.close[1:], np.nan]
        with np.errstate(invalid="ignore"):
            s.direction[nxt > bars.close] = 1
        return s


class FullSampleZScore(Strategy):
    """Subtle bug: normalises with the mean/std of the WHOLE series (future included)."""
    family = "full_sample_z"

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        z = (bars.close - bars.close.mean()) / bars.close.std()
        s.direction[z < -1] = 1
        return s


class CentredWindow(Strategy):
    """Centred rolling mean (looks 5 bars ahead)."""
    family = "centred"

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        c = bars.close
        k = np.ones(11) / 11
        m = np.convolve(c, k, mode="same")
        s.direction[5:-5][c[5:-5] < m[5:-5] - 2] = 1
        return s


class TestCausality(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        df, _ = generate_bars(CME, "2024-01-08", "2024-01-12", tf_minutes=5, seed=2)
        cls.ds = validate_and_freeze(df, NQ, CME, "5m", 5, "synthetic", "LA")

    def test_causal_strategies_pass(self):
        for s in (Breakout(ORDER, lookback=10), RandomEntry(ORDER, p=0.05, seed=1),
                  Breakout(ORDER, lookback=5, window=("09:30", "11:00"))):
            self.assertTrue(check_causality(s, self.ds.bars).passed, s.family)

    def test_peeking_strategies_caught(self):
        for s in (PeekNextClose(ORDER), FullSampleZScore(ORDER), CentredWindow(ORDER)):
            rep = check_causality(s, self.ds.bars)
            self.assertFalse(rep.passed, s.family)
            self.assertIn("information from after", rep.detail)

    def test_backtest_refuses_lookahead(self):
        with self.assertRaisesRegex(BacktestError, "LOOKAHEAD"):
            run_backtest(self.ds, PeekNextClose(ORDER), ZERO_COSTS, CFG["backtest"])

    def test_peeking_would_have_looked_great(self):
        """Why the gate matters: the cheating strategy 'wins' when the check is disabled."""
        cfg = {**CFG["backtest"], "require_causality_check": False, "same_bar_policy": "conservative"}
        order = OrderSpec("market", stop_points=50, time_exit_bars=1)
        res = run_backtest(self.ds, PeekNextClose(order), ZERO_COSTS, cfg)
        self.assertGreater((res.trades.gross_r > 0).mean(), 0.75)


if __name__ == "__main__":
    unittest.main()
