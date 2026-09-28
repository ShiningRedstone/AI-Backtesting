"""Phase 2: feature identity and cache invalidation.

Rule under test: identical inputs -> identical key -> bit-identical arrays; ANY change to
data, dataset identity, calendar, session definition, parameters, version, implementation
or a dependency -> a different key."""
import dataclasses
import shutil
import tempfile
import unittest
from pathlib import Path

import numpy as np

from edgelab.data.synthetic import generate_bars
from edgelab.data.validation import validate_and_freeze
from edgelab.features.cache import FeatureCache
from edgelab.features.engine import FeatureEngine
from edgelab.features.sessions import SessionWindow
from edgelab.features.spec import REGISTRY, FeatureSpec
from tests.helpers import CME, NQ
from tests.phase2_helpers import SESSIONS


def make_ds(seed=1, ds_id="CACHE", volume_type="synthetic", cal=CME):
    df, _ = generate_bars(cal, "2024-01-08", "2024-01-11", tf_minutes=5, seed=seed)
    return validate_and_freeze(df, NQ, cal, "5m", 5, "synthetic", ds_id, volume_type=volume_type)


class TestSpecIdentity(unittest.TestCase):
    def test_defaults_and_param_order_normalise(self):
        a = FeatureSpec.make("ema", {"period": 20})
        b = FeatureSpec.make("ema", {"slope_bars": 5, "period": 20.0})
        c = FeatureSpec.make("ema")
        self.assertEqual(a, b)
        self.assertEqual(a.spec_hash, c.spec_hash)
        self.assertEqual(a.spec_id, c.spec_id)
        self.assertEqual(FeatureSpec.from_dict(a.to_dict()), a)          # serialisable round trip

    def test_meaningful_changes_change_identity(self):
        base = FeatureSpec.make("atr", {"period": 14})
        self.assertNotEqual(base.spec_hash, FeatureSpec.make("atr", {"period": 15}).spec_hash)
        self.assertNotEqual(base.spec_hash, base.at("15m").spec_hash)
        self.assertEqual(FeatureSpec.make("atr", timeframe=15).timeframe, "15m")
        self.assertEqual(FeatureSpec.make("atr", timeframe="1h").timeframe, "60m")

    def test_spec_is_stable_across_processes(self):
        # hard-coded: if this changes, every cached feature and recorded run changes identity
        self.assertEqual(FeatureSpec.make("atr", {"period": 14}).to_dict(),
                         {"id": "atr", "version": 1, "timeframe": None, "params": {"period": 14}})
        self.assertEqual(FeatureSpec.make("atr", {"period": 14}).label, "atr(period=14)")


class TestCacheKeys(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ds = make_ds()

    def key(self, spec, ds=None, sessions=None, **over):
        ds = ds or self.ds
        eng = FeatureEngine.for_dataset(ds, sessions or SESSIONS)
        for k, v in over.items():
            setattr(eng, k, v)
        return eng.cache_key(spec)

    def test_same_inputs_same_key(self):
        s = FeatureSpec.make("fvg")
        self.assertEqual(self.key(s), self.key(s))
        self.assertEqual(self.key(s), self.key(FeatureSpec.make("fvg", {"fill_rule": "wick"})))

    def test_every_input_changes_the_key(self):
        s = FeatureSpec.make("vwap", {"anchor": "NY_RTH"})
        k0 = self.key(s)
        variants = {
            "params": self.key(FeatureSpec.make("vwap", {"anchor": "NY_AM"})),
            "timeframe": self.key(s.at("15m")),
            "data": self.key(s, ds=make_ds(seed=2)),
            "dataset_id": self.key(s, ds=make_ds(ds_id="CACHE_OTHER_PROVIDER")),
            "volume_type": self.key(s, volume_type="tick"),
            "session_def": self.key(s, sessions={**SESSIONS, "NY_RTH": SessionWindow(
                "NY_RTH", "America/New_York", "09:30", "16:15")}),
        }
        for name, k in variants.items():
            self.assertNotEqual(k, k0, name)
        self.assertEqual(len(set(variants.values())), len(variants))

    def test_calendar_change_changes_key(self):
        cal2 = dataclasses.replace(CME, holidays=CME.holidays | {__import__("datetime").date(2030, 1, 1)})
        self.assertNotEqual(CME.fingerprint(), cal2.fingerprint())
        s = FeatureSpec.make("atr")
        self.assertNotEqual(self.key(s), self.key(s, calendar=cal2))

    def test_implementation_change_invalidates_feature_and_dependents(self):
        atr_key, rs_key = self.key(FeatureSpec.make("atr")), self.key(FeatureSpec.make("range_stats"))
        original = REGISTRY["atr"]

        def patched_atr(inp, p):   # a "bug fix" without a version bump
            return original.compute(inp, p)
        REGISTRY["atr"] = dataclasses.replace(original, compute=patched_atr)
        try:
            self.assertNotEqual(self.key(FeatureSpec.make("atr")), atr_key)
            self.assertNotEqual(self.key(FeatureSpec.make("range_stats")), rs_key)   # depends on ATR
            self.assertEqual(self.key(FeatureSpec.make("ema")),
                             FeatureEngine.for_dataset(self.ds, SESSIONS).cache_key(FeatureSpec.make("ema")))
        finally:
            REGISTRY["atr"] = original
        self.assertEqual(self.key(FeatureSpec.make("atr")), atr_key)


class TestCacheStorage(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.ds = make_ds()
        self.specs = [FeatureSpec.make("atr"), FeatureSpec.make("fvg"), FeatureSpec.make("ema").at("60m"),
                      FeatureSpec.make("session", {"session": "NY_RTH"})]

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_compute_once_then_memory_then_disk_bit_identical(self):
        c1 = FeatureCache(self.tmp)
        eng = FeatureEngine.for_dataset(self.ds, SESSIONS, c1)
        first = {s: eng.compute(s) for s in self.specs}
        self.assertTrue(all(r.cache_source is None for r in first.values()))
        again = {s: eng.compute(s) for s in self.specs}
        self.assertTrue(all(r.cache_source == "memory" for r in again.values()))
        c2 = FeatureCache(self.tmp)                                   # new process, same disk
        eng2 = FeatureEngine.for_dataset(self.ds, SESSIONS, c2)
        disk = {s: eng2.compute(s) for s in self.specs}
        self.assertTrue(all(r.cache_source == "disk" for r in disk.values()))
        for s in self.specs:
            for k, a in first[s].arrays.items():
                self.assertEqual(a.tobytes(), disk[s].arrays[k].tobytes(), f"{s.label}.{k}")
                self.assertFalse(disk[s].arrays[k].flags.writeable)

    def test_dependencies_are_shared_not_recomputed(self):
        c = FeatureCache(self.tmp)
        eng = FeatureEngine.for_dataset(self.ds, SESSIONS, c)
        eng.compute(FeatureSpec.make("atr", {"period": 14}))
        writes = c.stats["writes"]
        eng.compute(FeatureSpec.make("fvg", {"atr_period": 14}))     # reuses the cached ATR
        self.assertEqual(c.stats["writes"], writes + 1)

    def test_corrupted_entry_is_discarded_and_recomputed(self):
        c1 = FeatureCache(self.tmp)
        eng = FeatureEngine.for_dataset(self.ds, SESSIONS, c1)
        good = eng.compute(self.specs[0]).arrays["atr"].copy()
        npz = next(self.tmp.rglob("*.npz"))
        meta = npz.with_suffix(".json")
        m = __import__("json").loads(meta.read_text())
        m["arrays_sha256"] = "0" * 64                                # tampered / bit rot
        meta.write_text(__import__("json").dumps(m))
        c2 = FeatureCache(self.tmp)
        r = FeatureEngine.for_dataset(self.ds, SESSIONS, c2).compute(self.specs[0])
        self.assertIsNone(r.cache_source)                            # not trusted -> recomputed
        self.assertEqual(c2.stats["corrupt"], 1)
        np.testing.assert_array_equal(r.arrays["atr"], good)
        npz.write_bytes(b"garbage")                                  # truncated file
        c3 = FeatureCache(self.tmp)
        r = FeatureEngine.for_dataset(self.ds, SESSIONS, c3).compute(self.specs[0])
        self.assertEqual(c3.stats["corrupt"], 1)
        np.testing.assert_array_equal(r.arrays["atr"], good)

    def test_datasets_are_namespaced_and_listed(self):
        c = FeatureCache(self.tmp)
        other = make_ds(seed=3, ds_id="CACHE_B")
        FeatureEngine.for_dataset(self.ds, SESSIONS, c).compute(self.specs[0])
        FeatureEngine.for_dataset(other, SESSIONS, c).compute(self.specs[0])
        self.assertEqual(len(c.entries("CACHE")), 1)
        self.assertEqual(len(c.entries("CACHE_B")), 1)
        self.assertEqual(c.entries("CACHE")[0]["dataset_hash"], self.ds.manifest.content_hash)
        c.clear("CACHE")
        self.assertEqual(len(c.entries("CACHE")), 0)
        self.assertEqual(len(c.entries("CACHE_B")), 1)

    def test_memory_lru_bound(self):
        c = FeatureCache(None, memory_entries=2)
        eng = FeatureEngine.for_dataset(self.ds, SESSIONS, c)
        for p in (5, 6, 7):
            eng.compute(FeatureSpec.make("atr", {"period": p}))
        self.assertEqual(len(c._mem), 2)

    def test_reproducible_across_fresh_engines(self):
        a = FeatureEngine.for_dataset(self.ds, SESSIONS).compute(FeatureSpec.make("fvg"))
        b = FeatureEngine.for_dataset(make_ds(), SESSIONS).compute(FeatureSpec.make("fvg"))
        self.assertEqual(a.cache_key, b.cache_key)
        for k in a.arrays:
            self.assertEqual(a.arrays[k].tobytes(), b.arrays[k].tobytes())


if __name__ == "__main__":
    unittest.main()
