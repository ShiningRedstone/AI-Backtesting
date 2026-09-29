"""Phase 2: CFD-ready data foundation (import, metadata, hashes, separation, store)."""
import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.data.importer import (ImportFailed, ImportOptions, import_dataset, inspect_file,
                                   load_validated, parse_source_timezone)
from edgelab.data.resample import resample_bars
from edgelab.data.schema import BarArrays, DatasetManifest
from edgelab.data.store import SQLiteStore
from edgelab.data.synthetic import bars_from_ohlc
from edgelab.data.validation import validate_and_freeze, validate_bars
from edgelab.features.cache import FeatureCache
from tests.helpers import CME, NQ, UTC247
from tests.phase2_helpers import CFG, synthetic_canonical, write_generic_utc, write_mt5_like


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.store = SQLiteStore(self.tmp / "s.sqlite")
        self.cache = FeatureCache(self.tmp / "fc")
        self.df = synthetic_canonical("2024-03-06", "2024-03-13", tf=1, seed=31)   # spans US DST

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mt5(self, name="a.csv", **kw):
        write_mt5_like(self.df, self.tmp / name, **kw)
        return ImportOptions(str(self.tmp / name), "NAS100_CFD", "SYNTHBROKER", "CFD", "1m",
                             profile="mt5_export", source_timezone="America/New_York+7h",
                             spread_multiplier=0.01, price_basis="bid", symbol="NAS100")


class TestTimestamps(Base):
    def test_parse_source_timezone(self):
        self.assertEqual(parse_source_timezone("UTC"), ("UTC", pd.Timedelta(0)))
        self.assertEqual(parse_source_timezone("America/New_York+7h"), ("America/New_York", pd.Timedelta(hours=7)))
        self.assertEqual(parse_source_timezone("Europe/Berlin-1:30h"),
                         ("Europe/Berlin", -pd.Timedelta(hours=1, minutes=30)))
        self.assertEqual(parse_source_timezone("Etc/GMT+5")[1], pd.Timedelta(0))   # IANA name, not an offset

    def test_broker_server_time_converts_exactly_across_dst(self):
        r = import_dataset(self.mt5(), CFG, self.store)
        ds = load_validated(self.store, CFG, r.dataset_id)
        np.testing.assert_array_equal(ds.bars.ts_ns, pd.DatetimeIndex(self.df["ts"]).as_unit("ns").asi8)
        np.testing.assert_allclose(ds.bars.close, self.df["close"].round(2))
        np.testing.assert_allclose(ds.bars.spread, 1.5)            # 150 broker points x 0.01

    def test_close_stamped_utc_is_shifted_to_open(self):
        write_generic_utc(self.df, self.tmp / "c.csv", close_stamped=True)
        o = ImportOptions(str(self.tmp / "c.csv"), "NAS100_CFD", "P", "CFD", "1m", source_timezone="UTC",
                          timestamp_convention="close")
        ds = load_validated(self.store, CFG, import_dataset(o, CFG, self.store).dataset_id)
        self.assertEqual(ds.bars.ts[0], self.df["ts"].iloc[0])
        self.assertEqual(ds.manifest.source_timestamp_convention, "close")
        self.assertEqual(ds.manifest.timestamp_convention, "bar_open_utc")

    def test_wrong_convention_is_caught_by_validation(self):
        write_generic_utc(self.df, self.tmp / "c.csv", close_stamped=True)
        o = ImportOptions(str(self.tmp / "c.csv"), "NAS100_CFD", "P", "CFD", "1m", source_timezone="UTC",
                          timestamp_convention="open")                     # WRONG: file is close-stamped
        r = import_dataset(o, CFG, self.store)
        ds = load_validated(self.store, CFG, r.dataset_id)
        self.assertIn(ds.report.get("bars_outside_session").status, ("WARN", "FAIL"))

    def test_timezone_never_guessed(self):
        o = self.mt5()
        o.source_timezone = None
        o.profile = None
        with self.assertRaises(ImportFailed) as cm:
            import_dataset(o, CFG, self.store)
        self.assertIn("source_timezone", str(cm.exception))
        self.assertEqual(self.store.list_datasets(), [])

    def test_profile_required_fields(self):
        o = self.mt5()
        o.spread_multiplier = None
        with self.assertRaises(ImportFailed):
            import_dataset(o, CFG, self.store)

    def test_ambiguous_local_times_refused(self):
        # New York local timestamps inside the repeated 01:00-02:00 hour of Nov 3 2024
        pd.DataFrame({"ts": ["2024-11-03 00:59:00", "2024-11-03 01:00:00", "2024-11-03 01:01:00"],
                      "open": 100, "high": 101, "low": 99, "close": 100}).to_csv(self.tmp / "n.csv", index=False)
        o = ImportOptions(str(self.tmp / "n.csv"), "NAS100_CFD", "P", "CFD", "1m",
                          source_timezone="America/New_York")
        with self.assertRaises(ImportFailed) as cm:
            import_dataset(o, CFG, self.store)
        self.assertIn("localize", str(cm.exception))


class TestExplicitOffsets(Base):
    """Real-data finding (first CFD file): a feed mixing `Z` and `-04:00` offsets was refused by
    pandas ("Mixed timezones detected"). Explicit offsets are absolute instants and convert to
    UTC exactly; naive values still need source_timezone; a mix of the two is refused."""

    def write(self, name, stamps, **extra):
        n = len(stamps)
        base = self.df.iloc[:n]
        pd.DataFrame({"time": stamps, "open": base["open"].to_numpy(), "high": base["high"].to_numpy(),
                      "low": base["low"].to_numpy(), "close": base["close"].to_numpy(),
                      "volume": base["volume"].to_numpy(), **extra}).to_csv(self.tmp / name, index=False)
        return ImportOptions(str(self.tmp / name), "NAS100_CFD", "P", "CFD", "1m", source_timezone="UTC",
                             columns={"ts": "time"})

    def parse(self, stamps, source_timezone="UTC"):
        from edgelab.data.importer import parse_timestamps
        o = ImportOptions("unused.csv", "NAS100_CFD", "P", "CFD", "1m", source_timezone=source_timezone)
        return parse_timestamps(pd.DataFrame({"ts": stamps}), o)

    def test_all_utc_z(self):
        ts = self.parse(["2025-10-02T13:14:00Z", "2025-10-02T13:15:00Z"])
        self.assertEqual(list(ts.astype(str)), ["2025-10-02 13:14:00+00:00", "2025-10-02 13:15:00+00:00"])

    def test_mixed_explicit_offsets_convert_to_exact_utc(self):
        ts = self.parse(["2025-10-02T13:14:00Z", "2026-04-03T09:10:00-04:00", "2026-01-05T09:30:00-05:00",
                         "2026-01-05T15:30:00+0100", "2026-01-05T14:31:00Z"])
        self.assertEqual(list(ts.astype(str)), ["2025-10-02 13:14:00+00:00", "2026-04-03 13:10:00+00:00",
                                                "2026-01-05 14:30:00+00:00", "2026-01-05 14:30:00+00:00",
                                                "2026-01-05 14:31:00+00:00"])
        with self.assertRaises(ImportFailed):                      # explicit offsets + a "+Nh" shift
            self.parse(["2025-10-02T13:14:00Z", "2026-04-03T09:10:00-04:00"], "America/New_York+7h")

    def test_mixed_offsets_import_without_shifting_any_bar(self):
        utc = pd.DatetimeIndex(self.df["ts"])                       # a week of 1m bars across US DST
        ny = utc.tz_convert("America/New_York")
        stamps = [(u.strftime("%Y-%m-%dT%H:%M:%SZ") if i % 2 == 0 else n.isoformat())
                  for i, (u, n) in enumerate(zip(utc, ny))]         # alternate Z / -05:00 / -04:00
        self.assertTrue(any(s.endswith("-05:00") for s in stamps) and any(s.endswith("-04:00") for s in stamps))
        r = import_dataset(self.write("mixed.csv", stamps), CFG, self.store)
        ds = load_validated(self.store, CFG, r.dataset_id)
        np.testing.assert_array_equal(ds.bars.ts_ns, utc.as_unit("ns").asi8)      # identical instants
        np.testing.assert_allclose(ds.bars.close, self.df["close"].to_numpy())
        self.assertEqual(ds.manifest.timestamp_convention, "bar_open_utc")
        insp = inspect_file(self.write("mixed2.csv", stamps), CFG)
        self.assertNotIn("normalize_error", insp)
        self.assertTrue(insp["monotonic"])
        self.assertEqual((insp["duplicate_timestamps"], insp["inferred_bar_minutes"]), (0, 1.0))

    def test_naive_timestamps_still_need_source_timezone(self):
        naive = ["2024-03-06 09:30:00", "2024-03-06 09:31:00"]
        self.assertEqual(list(self.parse(naive, "America/New_York").astype(str)),
                         ["2024-03-06 14:30:00+00:00", "2024-03-06 14:31:00+00:00"])   # declared zone, not UTC
        o = self.write("naive.csv", naive)
        o.source_timezone = None
        with self.assertRaises(ImportFailed) as cm:
            import_dataset(o, CFG, self.store)
        self.assertIn("source_timezone", str(cm.exception))

    def test_explicit_and_naive_mix_is_refused(self):
        for stamps in (["2025-10-02T13:14:00Z", "2025-10-02 13:15:00"],
                       ["2025-10-02 13:14:00", "2025-10-02T13:15:00-04:00"]):
            with self.assertRaises(ImportFailed) as cm:
                self.parse(stamps)
            self.assertIn("refusing to guess", str(cm.exception))

    def test_malformed_timestamps_still_fail(self):
        for stamps in (["2025-10-02T13:14:00Z", "not a time"], ["2025-10-02T25:14:00Z", "2025-10-02T13:15:00Z"],
                       ["2025-10-02T13:14:00+99:00", "2025-10-02T13:15:00Z"]):
            with self.subTest(stamps=stamps):
                with self.assertRaises(ImportFailed):
                    self.parse(stamps)
        self.assertEqual(self.store.list_datasets(), [])


class TestDocumentedLayouts(Base):
    """Every layout shown in DATA_IMPORT.md is exercised here."""

    def expect_same_bars(self, dataset_id, spread=None):
        ds = load_validated(self.store, CFG, dataset_id)
        np.testing.assert_array_equal(ds.bars.ts_ns, pd.DatetimeIndex(self.df["ts"]).as_unit("ns").asi8)
        np.testing.assert_allclose(ds.bars.close, self.df["close"].round(2))
        if spread is not None:
            np.testing.assert_allclose(ds.bars.spread, spread)
        return ds

    def test_dukascopy_profile(self):
        ts = pd.DatetimeIndex(self.df["ts"])
        pd.DataFrame({"Gmt time": ts.strftime("%d.%m.%Y %H:%M:%S.000"), "Open": self.df["open"].round(2),
                      "High": self.df["high"].round(2), "Low": self.df["low"].round(2),
                      "Close": self.df["close"].round(2), "Volume": self.df["volume"] / 1e3}
                     ).to_csv(self.tmp / "d.csv", index=False)
        o = ImportOptions(str(self.tmp / "d.csv"), "NAS100_CFD", "DUKASCOPY", "CFD", "1m",
                          profile="dukascopy_csv", price_basis="bid")
        ds = self.expect_same_bars(import_dataset(o, CFG, self.store).dataset_id)
        self.assertEqual((ds.manifest.volume_type, ds.manifest.source_timezone), ("tick", "UTC"))

    def test_column_mapping_epoch_and_bid_ask_spread(self):
        ts = pd.DatetimeIndex(self.df["ts"])
        pd.DataFrame({"time_ms": ts.asi8 // 1_000_000, "o": self.df["open"].round(2),
                      "h": self.df["high"].round(2), "l": self.df["low"].round(2),
                      "c": self.df["close"].round(2), "bid_c": self.df["close"].round(2),
                      "ask_c": self.df["close"].round(2) + 0.8}).to_csv(self.tmp / "e.csv", index=False)
        o = ImportOptions(str(self.tmp / "e.csv"), "US100_CFD", "VENDORX", "CFD", "1m", source_timezone="UTC",
                          columns={"ts": "time_ms", "open": "o", "high": "h", "low": "l", "close": "c"},
                          epoch_unit="ms", bid_close_column="bid_c", ask_close_column="ask_c",
                          price_basis="bid")
        ds = self.expect_same_bars(import_dataset(o, CFG, self.store).dataset_id, spread=0.8)
        self.assertEqual((ds.manifest.spread_source, ds.manifest.has_bid_ask, ds.manifest.volume_type),
                         ("bid_ask_close", True, "none"))

    def test_futures_through_same_pipeline(self):
        loc = pd.DatetimeIndex(self.df["ts"]).tz_convert("America/Chicago").tz_localize(None)
        out = self.df.assign(ts=loc.strftime("%Y-%m-%d %H:%M:%S"))
        out.to_csv(self.tmp / "f.csv", index=False)
        o = ImportOptions(str(self.tmp / "f.csv"), "NQ", "MYVENDOR", "FUTURE", "1m",
                          source_timezone="America/Chicago", price_basis="last")
        r = import_dataset(o, CFG, self.store)
        self.assertTrue(r.dataset_id.startswith("NQ_FUTURE_MYVENDOR_1M_"))
        ds = load_validated(self.store, CFG, r.dataset_id)
        np.testing.assert_array_equal(ds.bars.ts_ns, pd.DatetimeIndex(self.df["ts"]).as_unit("ns").asi8)


class TestMetadataAndHashes(Base):
    def test_manifest_fields_preserved_through_store(self):
        r = import_dataset(self.mt5(), CFG, self.store)
        m = self.store.get_manifest(r.dataset_id)
        expect = {"asset_type": "CFD", "provider": "SYNTHBROKER", "instrument": "NAS100_CFD", "symbol": "NAS100",
                  "dataset_name": "NAS100_CFD_SYNTHBROKER", "source_timezone": "America/New_York+7h",
                  "source_timestamp_convention": "open", "timestamp_convention": "bar_open_utc",
                  "timeframe": "1m", "volume_type": "tick", "has_spread": True, "spread_source": "column",
                  "has_bid_ask": False, "price_basis": "bid", "calendar": "CME_EQUITY",
                  "import_version": "edgelab-import/1", "n_bars": len(self.df)}
        for k, v in expect.items():
            self.assertEqual(getattr(m, k), v, k)
        self.assertEqual(m.calendar_fingerprint, CME.fingerprint())
        self.assertEqual(len(m.source_file_sha256), 64)
        self.assertTrue(r.dataset_id.startswith("NAS100_CFD_SYNTHBROKER_1M_"))
        self.assertEqual(r.dataset_id[-10:], m.content_hash[:10].upper())
        self.assertIsNotNone(self.store.get_report(r.dataset_id))

    def test_manifest_hash_deterministic_across_independent_imports(self):
        r1 = import_dataset(self.mt5(), CFG, self.store)
        store2 = SQLiteStore(self.tmp / "other.sqlite")
        r2 = import_dataset(self.mt5(), CFG, store2)
        m1, m2 = self.store.get_manifest(r1.dataset_id), store2.get_manifest(r2.dataset_id)
        self.assertEqual(m1.manifest_hash(), m2.manifest_hash())
        self.assertEqual(m1.content_hash, m2.content_hash)

    def test_old_manifests_still_load(self):
        old = {"dataset_id": "X", "provider": "p", "instrument": "NQ", "timeframe": "1m", "timezone": "UTC",
               "timestamp_convention": "bar_open_utc", "start": "a", "end": "b", "n_bars": 1,
               "content_hash": "h"}
        m = DatasetManifest.from_dict(old)
        self.assertEqual(m.asset_type, "unspecified")
        self.assertEqual(m.volume_type, "unknown")

    def test_spread_hash_semantics(self):
        df = bars_from_ohlc([(100, 101, 99, 100)] * 4)
        a = BarArrays.from_frame(df, 1)
        self.assertEqual(a.content_hash(), BarArrays.from_frame(df, 1).content_hash())
        df["spread"] = 1.0
        b = BarArrays.from_frame(df, 1)
        self.assertNotEqual(a.content_hash(), b.content_hash())       # spread is content when present
        df.loc[2, "spread"] = 1.25
        self.assertNotEqual(b.content_hash(), BarArrays.from_frame(df, 1).content_hash())


class TestSeparationAndIdempotency(Base):
    def test_reimport_is_idempotent(self):
        r1 = import_dataset(self.mt5(), CFG, self.store)
        r2 = import_dataset(self.mt5(), CFG, self.store)
        self.assertTrue(r1.created)
        self.assertFalse(r2.created)
        self.assertEqual(r1.dataset_id, r2.dataset_id)
        self.assertEqual(len(self.store.list_datasets()), 1)

    def test_providers_and_symbols_never_merge(self):
        ids = set()
        for inst, prov in (("NAS100_CFD", "PROVA"), ("NAS100_CFD", "PROVB"), ("US100_CFD", "PROVA"),
                           ("NQ_CFD", "PROVA")):
            o = self.mt5()
            o.instrument, o.provider = inst, prov
            r = import_dataset(o, CFG, self.store)
            ids.add(r.dataset_id)
            if len(ids) > 1:
                self.assertTrue(any("IDENTICAL" in w.upper() or "identical" in w for w in r.warnings))
        self.assertEqual(len(ids), 4)
        names = {m["dataset_name"] for m in self.store.list_datasets()}
        self.assertEqual(names, {"NAS100_CFD_PROVA", "NAS100_CFD_PROVB", "US100_CFD_PROVA", "NQ_CFD_PROVA"})

    def test_new_file_same_name_gets_new_id_never_overwrites(self):
        r1 = import_dataset(self.mt5(), CFG, self.store)
        self.df.loc[100, ["high"]] += 5.0
        r2 = import_dataset(self.mt5(name="b.csv"), CFG, self.store)
        self.assertNotEqual(r1.dataset_id, r2.dataset_id)
        self.assertEqual(len(self.store.list_datasets()), 2)
        self.assertEqual(load_validated(self.store, CFG, r1.dataset_id).manifest.content_hash,
                         self.store.get_manifest(r1.dataset_id).content_hash)


class TestFailuresAndGaps(Base):
    def test_impossible_ohlc_fails_and_stores_nothing(self):
        self.df.loc[50, "high"] = self.df.loc[50, "low"] - 1
        with self.assertRaises(ImportFailed) as cm:
            import_dataset(self.mt5(), CFG, self.store, reports_dir=self.tmp / "rep")
        self.assertEqual(cm.exception.stage, "validate")
        self.assertEqual(self.store.list_datasets(), [])
        self.assertTrue(list((self.tmp / "rep").glob("FAILED_*_quality.txt")))

    def test_conflicting_duplicates_fail(self):
        dup = pd.concat([self.df, self.df.iloc[[10]].assign(close=self.df.loc[10, "close"] + 0.25)])
        dup.loc[dup.index[-1], "high"] = max(dup.iloc[-1]["high"], dup.iloc[-1]["close"])
        write_generic_utc(dup, self.tmp / "d.csv")
        o = ImportOptions(str(self.tmp / "d.csv"), "NAS100_CFD", "P", "CFD", "1m", source_timezone="UTC")
        with self.assertRaises(ImportFailed):
            import_dataset(o, CFG, self.store)

    def test_missing_bars_are_reported_not_filled(self):
        write_generic_utc(self.df.drop(index=range(200, 230)), self.tmp / "g.csv")
        o = ImportOptions(str(self.tmp / "g.csv"), "NAS100_CFD", "P", "CFD", "1m", source_timezone="UTC")
        r = import_dataset(o, CFG, self.store)
        ds = load_validated(self.store, CFG, r.dataset_id)
        self.assertEqual(len(ds.bars), len(self.df) - 30)
        self.assertEqual(ds.manifest.missing_bars, 30)

    def test_no_volume_dataset(self):
        write_generic_utc(self.df, self.tmp / "nv.csv", with_volume=False)
        o = ImportOptions(str(self.tmp / "nv.csv"), "NAS100_CFD", "P", "CFD", "1m", source_timezone="UTC",
                          derive_timeframes=["5m"])
        r = import_dataset(o, CFG, self.store, self.cache)
        ds = load_validated(self.store, CFG, r.dataset_id)
        self.assertEqual(ds.manifest.volume_type, "none")
        self.assertTrue(np.isnan(ds.bars.volume).all())               # unknown, never 0
        d5 = load_validated(self.store, CFG, r.derived[0])
        self.assertTrue(np.isnan(d5.bars.volume).all())               # resample keeps it unknown
        self.assertIn("vwap(anchor=trading_day)", r.features[r.dataset_id]["skipped"])
        self.assertEqual(ds.report.get("volume_availability").status, "INFO")

    def test_spread_checks(self):
        df = bars_from_ohlc([(100, 101, 99, 100)] * 5)
        df["spread"] = [1.0, np.nan, 1.0, 1.0, 1.0]
        self.assertEqual(validate_bars(df, NQ, UTC247, 1).get("spread_availability").status, "WARN")
        df["spread"] = [1.0, -0.5, 1.0, 1.0, 1.0]
        self.assertEqual(validate_bars(df, NQ, UTC247, 1).get("spread_non_negative").status, "FAIL")

    def test_resample_volume_bug_fix(self):
        df = bars_from_ohlc([(100, 101, 99, 100)] * 10)
        df.loc[3, "volume"] = np.nan
        r = resample_bars(df, UTC247, 5)
        self.assertTrue(np.isnan(r["volume"].iloc[0]))                # partial -> unknown, not an under-count
        self.assertFalse(np.isnan(r["volume"].iloc[1]))


class TestStoreAndLoad(Base):
    def test_derived_timeframes_link_to_parent(self):
        o = self.mt5()
        o.derive_timeframes = ["5m", "15m"]
        r = import_dataset(o, CFG, self.store)
        for did, tf in zip(r.derived, ("5m", "15m")):
            m = self.store.get_manifest(did)
            self.assertEqual((m.parent_dataset_id, m.timeframe, m.asset_type, m.provider),
                             (r.dataset_id, tf, "CFD", "SYNTHBROKER"))
            self.assertIn("resample", m.derivation)
            self.assertTrue(load_validated(self.store, CFG, did).bars.spread is not None)

    def test_reload_revalidates_and_detects_calendar_change(self):
        r = import_dataset(self.mt5(), CFG, self.store)
        cfg2 = __import__("copy").deepcopy(CFG)
        cfg2["calendars"]["CME_EQUITY"]["holidays"] = list(cfg2["calendars"]["CME_EQUITY"].get("holidays", [])) + ["2030-01-02"]
        with self.assertRaises(ImportFailed):
            load_validated(self.store, cfg2, r.dataset_id)
        ds = load_validated(self.store, cfg2, r.dataset_id, allow_calendar_change=True)
        self.assertEqual(ds.manifest.content_hash, self.store.get_manifest(r.dataset_id).content_hash)

    def test_tampered_bars_detected_on_load(self):
        r = import_dataset(self.mt5(), CFG, self.store)
        self.store.con.execute("UPDATE bars SET spread = spread + 1 WHERE rowid = 5")
        self.store.con.commit()
        with self.assertRaises(Exception):
            load_validated(self.store, CFG, r.dataset_id)

    def test_trades_table_schema_evolves(self):
        self.store._append_table("trades", pd.DataFrame({"run_id": ["a"], "net_r": [1.0]}))
        self.store._append_table("trades", pd.DataFrame({"run_id": ["b"], "net_r": [2.0], "financing_usd": [0.5]}))
        rows = self.store.con.execute("SELECT run_id, financing_usd FROM trades ORDER BY run_id").fetchall()
        self.assertEqual(rows, [("a", None), ("b", 0.5)])

    def test_inspect_is_read_only(self):
        info = inspect_file(self.mt5(), CFG)
        self.assertEqual(info["inferred_bar_minutes"], 1.0)
        self.assertTrue(info["spread_present"])
        self.assertEqual(self.store.list_datasets(), [])


if __name__ == "__main__":
    unittest.main()
