"""Data layer: calendars, validation gate, cleaning policy, resampling, synthetic data, providers."""
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.data.calendar import SessionCalendar
from edgelab.data.providers import CSVProvider, VendorAPIProvider
from edgelab.data.resample import map_intrabar, resample_bars
from edgelab.data.schema import BarArrays, DataRequiredError, timeframe_minutes, to_utc_ns
from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import (DataIntegrityError, ValidatedDataset, clean_bars,
                                     validate_and_freeze, validate_bars)
from tests.helpers import CME, NQ, UTC247

ET = "America/New_York"


def cme_bars(start="2024-01-08", end="2024-01-12", tf=1, seed=0, **kw):
    df, _ = generate_bars(CME, start, end, tf_minutes=tf, seed=seed, **kw)
    return df


class TestCalendar(unittest.TestCase):
    def test_trading_date_and_session(self):
        ts = pd.DatetimeIndex([pd.Timestamp(s, tz=ET) for s in (
            "2024-01-07 18:30",   # Sunday evening -> Monday's session, open
            "2024-01-08 16:59",   # Monday, open
            "2024-01-08 17:30",   # daily halt, closed
            "2024-01-13 10:00",   # Saturday, closed
        )]).tz_convert("UTC")
        td = CME.trading_dates(ts)
        self.assertEqual(str(td[0]), "2024-01-08")
        self.assertEqual(list(CME.in_session(ts)), [True, True, False, False])

    def test_bars_per_session(self):
        o, c = CME.session_bounds(pd.Timestamp("2024-01-09").date())
        for tf, n in ((1, 1380), (5, 276), (60, 23)):
            self.assertEqual(len(CME.expected_bar_opens(o, c - pd.Timedelta(minutes=tf), tf)), n)
        o, c = UTC247.session_bounds(pd.Timestamp("2024-01-09").date())
        self.assertEqual(len(UTC247.expected_bar_opens(o, c - pd.Timedelta(minutes=1), 1)), 1440)

    def test_dst_moves_session_open_in_utc(self):
        before = CME.session_bounds(pd.Timestamp("2024-03-04").date())[0].tz_convert("UTC")
        after = CME.session_bounds(pd.Timestamp("2024-03-11").date())[0].tz_convert("UTC")
        self.assertEqual(before.hour, 23)   # 18:00 EST
        self.assertEqual(after.hour, 22)    # 18:00 EDT

    def test_holiday_and_early_close(self):
        cal = SessionCalendar("X", ET, "18:00", "17:00",
                              holidays=frozenset({pd.Timestamp("2024-01-09").date()}),
                              early_closes={pd.Timestamp("2024-01-10").date(): "13:00"})
        idx = cal.expected_bar_opens(pd.Timestamp("2024-01-08 18:00", tz=ET),
                                     pd.Timestamp("2024-01-10 17:00", tz=ET), 60)
        loc = idx.tz_convert(ET)
        self.assertFalse(any((loc >= pd.Timestamp("2024-01-08 18:00", tz=ET)) &
                             (loc < pd.Timestamp("2024-01-09 17:00", tz=ET))))
        self.assertEqual(loc.max(), pd.Timestamp("2024-01-10 12:00", tz=ET))


class TestValidation(unittest.TestCase):
    def setUp(self):
        self.df = cme_bars()

    def test_clean_data_passes(self):
        rep = validate_bars(self.df, NQ, CME, 1)
        self.assertIn(rep.status, ("PASS", "INFO"))
        self.assertEqual(rep.missing_bars, 0)
        self.assertEqual(rep.get("bars_outside_session").count, 0)

    def test_weekend_and_halt_are_not_missing(self):
        df = cme_bars("2024-01-11", "2024-01-16")   # spans a weekend
        self.assertEqual(validate_bars(df, NQ, CME, 1).missing_bars, 0)

    def test_tz_naive_rejected(self):
        df = self.df.copy()
        df["ts"] = df["ts"].dt.tz_localize(None)
        with self.assertRaises(ValueError):
            validate_bars(df, NQ, CME, 1)

    def test_missing_bars_counted_never_filled(self):
        df = self.df.drop(index=range(1000, 1200)).reset_index(drop=True)
        rep = validate_bars(df, NQ, CME, 1)
        self.assertEqual(rep.missing_bars, 200)
        ds = validate_and_freeze(df, NQ, CME, "1m", 1, "t", "gap")
        self.assertEqual(len(ds.bars), len(df))              # nothing fabricated

    def test_missing_whole_day(self):
        td = CME.trading_dates(pd.DatetimeIndex(self.df["ts"]))
        df = self.df[td != np.datetime64("2024-01-10")].reset_index(drop=True)
        rep = validate_bars(df, NQ, CME, 1)
        self.assertEqual(rep.get("missing_trading_days").count, 1)
        self.assertEqual(rep.get("missing_trading_days").status, "WARN")
        self.assertEqual(rep.status, "FAIL")   # 1 of 4 days missing also exceeds the 5% bar threshold

    def test_exact_duplicates_warn_and_are_cleaned(self):
        df = pd.concat([self.df, self.df.iloc[[5, 6]]], ignore_index=True)
        rep = validate_bars(df, NQ, CME, 1)
        self.assertEqual(rep.get("exact_duplicates").count, 2)
        ds = validate_and_freeze(df, NQ, CME, "1m", 1, "t", "dups")
        self.assertEqual(len(ds.bars), len(self.df))
        self.assertEqual(ds.manifest.duplicate_bars, 2)
        self.assertTrue(any("sorted" in m or "duplicate" in m for m in ds.manifest.source_detail["cleaning"]))

    def test_conflicting_duplicates_fail(self):
        dup = self.df.iloc[[5]].copy()
        dup["close"] += 0.25
        dup["high"] = dup[["high", "close"]].max(axis=1)
        df = pd.concat([self.df, dup], ignore_index=True)
        self.assertEqual(validate_bars(df, NQ, CME, 1).get("conflicting_duplicates").status, "FAIL")
        with self.assertRaises(DataIntegrityError):
            validate_and_freeze(df, NQ, CME, "1m", 1, "t", "conf")

    def test_unsorted_is_reported_then_sorted(self):
        df = self.df.sample(frac=1.0, random_state=1).reset_index(drop=True)
        self.assertEqual(validate_bars(df, NQ, CME, 1).get("timestamp_order").status, "FAIL")
        ds = validate_and_freeze(df, NQ, CME, "1m", 1, "t", "unsorted")
        self.assertTrue((np.diff(ds.bars.ts_ns) > 0).all())

    def test_bad_ohlc_fails(self):
        df = self.df.copy()
        df.loc[10, "high"] = df.loc[10, "low"] - 1
        self.assertEqual(validate_bars(df, NQ, CME, 1).get("ohlc_integrity").count, 1)
        with self.assertRaises(DataIntegrityError):
            validate_and_freeze(df, NQ, CME, "1m", 1, "t", "bad")

    def test_nan_and_nonpositive(self):
        df = self.df.copy()
        df.loc[3, "close"] = np.nan
        df.loc[4, ["open", "high", "low", "close"]] = [-1.0, -1.0, -1.0, -1.0]
        rep = validate_bars(df, NQ, CME, 1)
        self.assertEqual(rep.get("finite_prices").status, "FAIL")
        self.assertEqual(rep.get("positive_prices").status, "FAIL")

    def test_spike_warns(self):
        df = self.df.copy()
        df.loc[50, "high"] += 500
        self.assertEqual(validate_bars(df, NQ, CME, 1).get("range_spikes").status, "WARN")

    def test_off_tick_grid_warns(self):
        df = self.df.copy()
        df.loc[50, "close"] += 0.1
        df.loc[50, ["high", "low"]] = [max(df.loc[50, "high"], df.loc[50, "close"]),
                                       min(df.loc[50, "low"], df.loc[50, "close"])]
        self.assertEqual(validate_bars(df, NQ, CME, 1).get("tick_alignment").status, "WARN")

    def test_dst_timezone_error_detected(self):
        """Vendor data stamped in fixed EST all year (a classic bug) lands in the halt after DST."""
        df = cme_bars("2024-03-06", "2024-03-14", tf=5)
        correct = validate_bars(df, NQ, CME, 5)
        self.assertEqual(correct.get("bars_outside_session").count, 0)
        wrong = df.copy()
        loc = pd.DatetimeIndex(wrong["ts"]).tz_convert(ET)
        after_dst = np.asarray(loc.tz_localize(None) >= pd.Timestamp("2024-03-10 03:00"))
        wrong.loc[after_dst, "ts"] = wrong.loc[after_dst, "ts"] + pd.Timedelta(hours=1)
        rep = validate_bars(wrong, NQ, CME, 5)
        self.assertEqual(rep.get("bars_outside_session").status, "FAIL")
        self.assertGreater(rep.get("bars_outside_session").count, 0)

    def test_close_stamped_bars_misaligned(self):
        df = cme_bars(tf=5)
        df["ts"] = df["ts"] + pd.Timedelta(minutes=1)   # neither open- nor close-stamped grid
        self.assertEqual(validate_bars(df, NQ, CME, 5).get("grid_alignment").status, "FAIL")

    def test_contract_roll_jump_flagged(self):
        df = self.df.copy()
        df["contract"] = np.where(df.index < 3000, "NQH24", "NQM24")
        df.loc[3000:, ["open", "high", "low", "close"]] += 250.0   # unadjusted roll gap
        rep = validate_bars(df, NQ, CME, 1)
        self.assertEqual(rep.get("contract_rolls").status, "WARN")

    def test_validated_dataset_is_immutable_gate(self):
        ds = validate_and_freeze(self.df, NQ, CME, "1m", 1, "t", "imm")
        with self.assertRaises(ValueError):
            ds.bars.close[0] = 1.0
        with self.assertRaises(TypeError):
            ValidatedDataset(ds.bars, NQ, CME, ds.report, ds.manifest)
        ds.verify_unchanged()
        self.assertEqual(ds.manifest.content_hash, ds.bars.content_hash())

    def test_empty_fails(self):
        with self.assertRaises(DataIntegrityError):
            validate_and_freeze(self.df.iloc[:0], NQ, CME, "1m", 1, "t", "empty")


class TestResample(unittest.TestCase):
    def test_1m_to_5m_matches_manual(self):
        df = cme_bars()
        r = resample_bars(df, CME, 5)
        first = df.iloc[:5]
        self.assertEqual(r.loc[0, "open"], first["open"].iloc[0])
        self.assertEqual(r.loc[0, "high"], first["high"].max())
        self.assertEqual(r.loc[0, "low"], first["low"].min())
        self.assertEqual(r.loc[0, "close"], first["close"].iloc[-1])
        self.assertEqual(r.loc[0, "volume"], first["volume"].sum())
        self.assertTrue((r["n_subbars"] == 5).all())
        self.assertIn(validate_bars(r, NQ, CME, 5).status, ("PASS", "INFO"))

    def test_45m_anchored_to_session_open(self):
        r = resample_bars(cme_bars(), CME, 45)
        loc = pd.DatetimeIndex(r["ts"]).tz_convert(ET)
        self.assertIn("18:00", set(loc.strftime("%H:%M")))
        self.assertIn(validate_bars(r, NQ, CME, 45).status, ("PASS", "INFO"))
        self.assertTrue((r["n_subbars"] <= 45).all())
        self.assertIn(30, set(r["n_subbars"]))   # 23h session: last 45m bucket is partial and says so

    def test_intrabar_map_reliability(self):
        df = cme_bars(end="2024-01-09")
        ltf = BarArrays.from_frame(df, 1)
        htf = BarArrays.from_frame(resample_bars(df, CME, 5), 5)
        s, e, ok = map_intrabar(htf, ltf)
        self.assertTrue(ok.all())
        self.assertTrue(((e - s) == 5).all())
        bad = df.copy()
        bad.loc[7, "high"] = bad.loc[5:9, "high"].max() + 0.25   # new max: LTF now disagrees with HTF bar 1
        _, _, ok2 = map_intrabar(htf, BarArrays.from_frame(bad, 1))
        self.assertFalse(ok2[1])
        self.assertEqual(int((~ok2).sum()), 1)


class TestSynthetic(unittest.TestCase):
    def test_deterministic_and_seed_sensitive(self):
        a, b, c = cme_bars(seed=7), cme_bars(seed=7), cme_bars(seed=8)
        pd.testing.assert_frame_equal(a, b)
        self.assertFalse(a["close"].equals(c["close"]))

    def test_gap_free_within_session_unless_requested(self):
        df = cme_bars()
        td = CME.trading_dates(pd.DatetimeIndex(df["ts"]))
        same = td[1:] == td[:-1]
        self.assertTrue(np.allclose(df["open"].to_numpy()[1:][same], df["close"].to_numpy()[:-1][same]))
        g = cme_bars(session_gap_sigma=20.0)
        new = ~same
        jumps = g["open"].to_numpy()[1:][new] - g["close"].to_numpy()[:-1][new]
        self.assertTrue((np.abs(jumps) > 0).any())

    def test_known_drift(self):
        df = cme_bars(sigma_per_bar=0.0001, drift_per_bar=0.25)
        self.assertGreater(df["close"].iloc[-1] - df["close"].iloc[0], 0.24 * (len(df) - 1))


class TestProviders(unittest.TestCase):
    def test_close_stamped_csv_is_shifted_to_open(self):
        df = cme_bars(tf=5, end="2024-01-09")
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bars.csv"
            vendor = df.copy()
            vendor["ts"] = (pd.DatetimeIndex(vendor["ts"]) + pd.Timedelta(minutes=5)) \
                .tz_convert(ET).tz_localize(None)                  # naive, ET, close-stamped
            vendor.to_csv(p, index=False)
            loaded = CSVProvider(p, source_timezone=ET, timestamp_convention="close",
                                 tf_minutes=5).load_bars("NQ", "5m")
            self.assertTrue((pd.DatetimeIndex(loaded["ts"]) == pd.DatetimeIndex(df["ts"])).all())
            naive = CSVProvider(p, source_timezone=ET, timestamp_convention="open").load_bars("NQ", "5m")
            self.assertFalse((pd.DatetimeIndex(naive["ts"]) == pd.DatetimeIndex(df["ts"])).any())

    def test_missing_source_raises_data_required(self):
        with self.assertRaises(DataRequiredError) as cm:
            VendorAPIProvider("Databento", "DATABENTO_API_KEY").load_bars("NQ", "1m", "2020-01-01", "2025-01-01")
        self.assertIn("DATA REQUIRED", str(cm.exception))
        with self.assertRaises(DataRequiredError):
            CSVProvider("/nonexistent.csv").load_bars("NQ", "1m")

    def test_timestamp_helpers(self):
        self.assertEqual(timeframe_minutes("30m"), 30)
        self.assertEqual(timeframe_minutes("4h"), 240)
        self.assertEqual(timeframe_minutes("Daily"), 1440)
        idx = to_utc_ns(pd.Series(pd.to_datetime(["2024-01-02 14:30"], utc=True)))
        self.assertEqual(str(idx.dtype), "datetime64[ns, UTC]")   # pandas 3 defaults to 'us'


if __name__ == "__main__":
    unittest.main()
