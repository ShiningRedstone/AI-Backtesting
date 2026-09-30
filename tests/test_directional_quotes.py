"""ADR-55 data layer: observed ASK OHLC stored beside the BID primary series (all-or-none, never
inferred). SYNTHETIC data only; every number is a test assumption. No fills/costs change here."""
import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_arrays, hash_obj
from edgelab.data.importer import ImportFailed
from edgelab.data.resample import resample_bars
from edgelab.data.schema import ASK_COLUMNS, BarArrays, DatasetManifest
from edgelab.data.store import SQLiteStore
from edgelab.data.synthetic import bars_from_ohlc
from edgelab.data.validation import DataIntegrityError, validate_and_freeze, validate_bars
from edgelab.services import Services
from tests.dukascopy_fixture import write_fixture
from tests.helpers import INSTRUMENTS, UTC247

CFD = INSTRUMENTS["NAS100_CFD"]
ROWS = [(100.0, 101.0, 99.0, 100.5), (100.5, 102.0, 100.0, 101.5), (101.5, 101.8, 100.2, 100.4),
        (100.4, 100.9, 99.6, 99.8), (99.8, 100.1, 99.1, 100.0), (100.0, 100.7, 99.9, 100.6)]


def with_ask(rows=ROWS, spread=(1.0, 1.25, 1.5, 1.0, 2.0, 1.25)):
    df = bars_from_ohlc(rows)
    s = np.asarray(spread)
    df["spread"] = s
    df["ask_open"], df["ask_high"] = df["open"] + s, df["high"] + s + 0.25
    df["ask_low"], df["ask_close"] = df["low"] + s, df["close"] + s
    return df


def freeze(df, ds_id="ASK"):
    return validate_and_freeze(df, CFD, UTC247, "1m", 1, "test", ds_id, asset_type="CFD")


class TestBarArraysAsk(unittest.TestCase):
    def test_bid_only_hash_formula_unchanged(self):
        b = BarArrays.from_frame(bars_from_ohlc(ROWS), 1)
        self.assertFalse(b.has_ask_ohlc)
        self.assertEqual(b.content_hash(), hash_arrays(b.ts_ns, b.open, b.high, b.low, b.close, b.volume))
        df = bars_from_ohlc(ROWS)
        df["spread"] = 1.0
        s = BarArrays.from_frame(df, 1)
        self.assertEqual(s.content_hash(), hash_arrays(s.ts_ns, s.open, s.high, s.low, s.close, s.volume, s.spread))
        self.assertNotIn("ask_open", s.to_frame().columns)

    def test_all_or_none(self):
        b = BarArrays.from_frame(bars_from_ohlc(ROWS), 1)
        with self.assertRaises(ValueError):
            BarArrays(b.ts_ns, b.open, b.high, b.low, b.close, b.volume, 1, None, ask_open=b.open)
        with self.assertRaises(ValueError):
            BarArrays.from_frame(with_ask().drop(columns=["ask_low"]), 1)

    def test_ask_arrays_hash_head_and_frame(self):
        df = with_ask()
        a = BarArrays.from_frame(df, 1)
        plain = BarArrays.from_frame(df.drop(columns=list(ASK_COLUMNS)), 1)
        self.assertTrue(a.has_ask_ohlc)
        self.assertNotEqual(a.content_hash(), plain.content_hash())
        np.testing.assert_array_equal(a.ask_high, df["ask_high"].to_numpy())
        h = a.head(3)
        self.assertEqual([len(getattr(h, k)) for k in ASK_COLUMNS], [3] * 4)
        self.assertEqual(BarArrays.from_frame(a.to_frame(), 1).content_hash(), a.content_hash())

    def test_manifest_flag_absent_when_false(self):
        import dataclasses
        m = freeze(bars_from_ohlc(ROWS), "BID").manifest
        self.assertNotIn("has_ask_ohlc", m.to_dict())
        pre = {k: v for k, v in dataclasses.asdict(m).items() if k not in ("has_ask_ohlc", "imported_at")}
        self.assertEqual(m.manifest_hash(), hash_obj(pre))                    # pre-ADR-55 manifest hash formula
        self.assertFalse(DatasetManifest.from_dict(m.to_dict()).has_ask_ohlc)
        t = dataclasses.replace(m, has_ask_ohlc=True)
        self.assertTrue(t.to_dict()["has_ask_ohlc"])
        self.assertNotEqual(t.manifest_hash(), m.manifest_hash())


class TestAskValidation(unittest.TestCase):
    def test_valid_ask_passes_and_flags_manifest(self):
        ds = freeze(with_ask())
        self.assertTrue(ds.manifest.has_ask_ohlc)
        self.assertEqual(ds.report.get("ask_not_below_bid").status, "PASS")
        self.assertFalse(freeze(bars_from_ohlc(ROWS), "BID").manifest.has_ask_ohlc)

    def test_bid_only_report_has_no_ask_checks(self):
        names = {c.name for c in validate_bars(bars_from_ohlc(ROWS), CFD, UTC247, 1).checks}
        self.assertFalse({n for n in names if n.startswith("ask_")})

    def test_ask_below_bid_fails_on_each_field(self):
        for k in ("open", "high", "low", "close"):
            df = with_ask()
            df.loc[2, f"ask_{k}"] = df.loc[2, k] - 0.1
            if k == "high":
                df.loc[2, ["ask_open", "ask_close"]] = df.loc[2, "ask_low"]      # keep ASK OHLC self-consistent
            with self.subTest(field=k), self.assertRaises(DataIntegrityError) as cm:
                freeze(df)
            self.assertEqual(cm.exception.report.get("ask_not_below_bid").status, "FAIL")

    def test_missing_or_invalid_ask_value_fails(self):
        for bad in (np.nan, np.inf):
            df = with_ask()
            df.loc[3, "ask_high"] = bad
            with self.assertRaises(DataIntegrityError) as cm:
                freeze(df)
            self.assertEqual(cm.exception.report.get("ask_finite_prices").status, "FAIL")

    def test_partial_ask_columns_fail(self):
        with self.assertRaises(DataIntegrityError) as cm:
            freeze(with_ask().drop(columns=["ask_open"]))
        self.assertEqual(cm.exception.report.get("ask_ohlc_columns").status, "FAIL")

    def test_conflicting_duplicate_differing_only_in_ask_fails(self):
        df = with_ask()
        dup = df.iloc[[1]].copy()
        dup["ask_high"] += 0.5
        with self.assertRaises(DataIntegrityError) as cm:
            freeze(pd.concat([df, dup]))
        self.assertEqual(cm.exception.report.get("conflicting_duplicates").status, "FAIL")


class TestAskResample(unittest.TestCase):
    def setUp(self):
        rows = [(100 + i, 101 + i + (i % 3), 99 + i - (i % 2), 100.5 + i) for i in range(10)]
        self.df = with_ask(rows, spread=[1.0 + 0.25 * (i % 4) for i in range(10)])
        self.df = self.df.drop(index=[6, 7]).reset_index(drop=True)          # bucket 2 is partial (3 of 5)

    def test_first_max_min_last_of_present_rows_and_bid_spread_unchanged(self):
        out = resample_bars(self.df, UTC247, 5)
        plain = resample_bars(self.df.drop(columns=list(ASK_COLUMNS)), UTC247, 5)
        pd.testing.assert_frame_equal(out.drop(columns=list(ASK_COLUMNS)), plain)   # BID + spread identical
        for b, rows in enumerate((self.df.iloc[0:5], self.df.iloc[5:8])):
            self.assertEqual(out.loc[b, "n_subbars"], len(rows))
            self.assertEqual(out.loc[b, "ask_open"], rows["ask_open"].iloc[0])
            self.assertEqual(out.loc[b, "ask_high"], rows["ask_high"].max())
            self.assertEqual(out.loc[b, "ask_low"], rows["ask_low"].min())
            self.assertEqual(out.loc[b, "ask_close"], rows["ask_close"].iloc[-1])
            self.assertAlmostEqual(out.loc[b, "spread"], rows["spread"].mean())
        self.assertEqual(len(out), 2)                                             # no bucket invented

    def test_missing_ask_in_a_present_row_blanks_that_buckets_ask_side(self):
        df = self.df.copy()
        df.loc[4, "ask_open"] = np.nan                                            # last row of bucket 0
        out = resample_bars(df, UTC247, 5)
        self.assertTrue(out.loc[0, list(ASK_COLUMNS)].isna().all())               # not first()/last() of the rest
        self.assertTrue(np.isfinite(out.loc[1, list(ASK_COLUMNS)].to_numpy(float)).all())


class TestAskImportAndStore(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree("configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, end="2024-03-09")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        cls.csv, cls.src = csv, f
        cls.svc = Services(root=cls.root)
        base = dict(file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY",
                    asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m",
                    derive_timeframes=["5m"], build_features=False, bid_close_column="close",
                    ask_close_column="ask_close")
        cls.base = base
        cls.close_only = cls.svc.import_file({**base, "dataset_name": "CLOSEONLY"})
        cls.ohlc = cls.svc.import_file({**base, "dataset_name": "ASKOHLC", "ask_open_column": "ask_open",
                                        "ask_high_column": "ask_high", "ask_low_column": "ask_low"})

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.root, ignore_errors=True)

    def pair(self, i):
        a = self.svc.load_dataset(([self.close_only["dataset_id"]] + self.close_only["derived"])[i])
        b = self.svc.load_dataset(([self.ohlc["dataset_id"]] + self.ohlc["derived"])[i])
        return a, b

    def test_bid_and_spread_identical_to_close_only_import(self):
        for i in (0, 1):                                                          # 1m and derived 5m
            a, b = self.pair(i)
            for k in ("ts_ns", "open", "high", "low", "close", "volume", "spread"):
                np.testing.assert_array_equal(getattr(a.bars, k), getattr(b.bars, k), err_msg=k)
            self.assertFalse(a.manifest.has_ask_ohlc)
            self.assertNotIn("has_ask_ohlc", a.manifest.to_dict())
            self.assertTrue(b.manifest.has_ask_ohlc)
            self.assertTrue(b.bars.has_ask_ohlc)
            self.assertEqual(a.manifest.content_hash, hash_arrays(*(getattr(a.bars, k) for k in (
                "ts_ns", "open", "high", "low", "close", "volume", "spread"))))

    def test_ask_values_survive_import_and_store_exactly(self):
        _, b = self.pair(0)                                                       # load re-checks the hash
        for k in ASK_COLUMNS:
            np.testing.assert_array_equal(getattr(b.bars, k), self.src[k].astype(float).to_numpy(), err_msg=k)

    def test_derived_5m_provenance_names_ask_aggregation(self):
        a, b = self.pair(1)
        self.assertIn("observed ASK OHLC", b.manifest.derivation)
        self.assertNotIn("ASK", a.manifest.derivation)
        self.assertEqual(b.manifest.parent_dataset_id, self.ohlc["dataset_id"])

    def test_option_refusals(self):
        ask = {"ask_open_column": "ask_open", "ask_high_column": "ask_high", "ask_low_column": "ask_low"}
        bad = [{"ask_open_column": "ask_open"},                                  # partial ASK columns
               {**ask, "price_basis": "ask"},
               {**ask, "bid_close_column": "ask_close"},                          # primary must be BID close
               {**ask, "ask_close_column": None}]
        for over in bad:
            with self.subTest(over=over), self.assertRaises(ImportFailed):
                self.svc.import_file({**self.base, "dataset_name": "BAD", **over})

    def test_row_with_missing_or_crossed_ask_refuses_import(self):
        for k, v in (("ask_high", ""), ("ask_close", "abc"), ("ask_low", "1.000")):  # "1.000" < bid low
            f = self.src.copy()
            f.loc[10, k] = v
            p = self.root / f"bad_{k}.csv"
            f.to_csv(p, index=False)
            with self.subTest(field=k), self.assertRaises(ImportFailed):
                self.svc.import_file({**self.base, "file": str(p), "dataset_name": f"BAD{k}",
                                      "ask_open_column": "ask_open", "ask_high_column": "ask_high",
                                      "ask_low_column": "ask_low"})


class TestStoreMigration(unittest.TestCase):
    def test_pre_ask_database_migrates_and_loads_identically(self):
        tmp = Path(tempfile.mkdtemp())
        try:
            db = tmp / "old.sqlite"
            bid = freeze(bars_from_ohlc(ROWS), "BIDONLY")
            st = SQLiteStore(db)
            st.save_dataset(bid.manifest, bid.bars, bid.report)
            st.close()
            con = sqlite3.connect(db)                                            # simulate a pre-ADR-55 database
            for k in ASK_COLUMNS:
                con.execute(f"ALTER TABLE bars DROP COLUMN {k}")
            con.commit()
            con.close()
            st = SQLiteStore(db)                                                 # migration re-adds NULL columns
            m, bars = st.load_dataset("BIDONLY", 1)
            self.assertEqual(bars.content_hash(), bid.manifest.content_hash)
            self.assertFalse(bars.has_ask_ohlc)
            ask = freeze(with_ask(), "WITHASK")
            st.save_dataset(ask.manifest, ask.bars, ask.report)
            m2, b2 = st.load_dataset("WITHASK", 1)
            self.assertTrue(m2.has_ask_ohlc)
            for k in ASK_COLUMNS:
                np.testing.assert_array_equal(getattr(b2, k), getattr(ask.bars, k))
            self.assertEqual(st.load_dataset("BIDONLY", 1)[1].content_hash(), bid.manifest.content_hash)
            st.close()
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()


# =============================================================================================
# ADR-55 engine: directional BID/ASK execution (cost spread_source: quotes). SYNTHETIC data only.
# =============================================================================================
import copy                                                                       # noqa: E402

from edgelab.engine.backtester import BacktestError, _check_quotes, run_backtest  # noqa: E402
from edgelab.engine.costs import CostConfigError, CostModel, cost_model_from_config  # noqa: E402
from edgelab.engine.fills import (Entry, FillPolicy, IntrabarData, MarketArrays, _resolve_intrabar,  # noqa: E402
                                  resolve_bar)
from edgelab.engine.signals import OrderSpec, SignalSet, Strategy                 # noqa: E402
from tests.helpers import CFG, Scripted, bt_cfg                                   # noqa: E402

FLAT = (100.0, 100.5, 99.5, 100.0)
QUOTES = CostModel(spread_source="quotes", commission_mode="notional", commission_per_million=30.15,
                   slippage_unit="points", slippage_ticks_market=0.5, slippage_ticks_stop=0.5)
SINGLE = CostModel(commission_mode="notional", commission_per_million=30.15,
                   slippage_unit="points", slippage_ticks_market=0.5, slippage_ticks_stop=0.5)


def qds(n=12, bid=None, ask=None, ds_id="Q", with_ask_cols=True):
    """Flat BID bars with ASK = BID + 1 unless overridden per bar: bid/ask = {bar: (o, h, l, c)}."""
    b = [(bid or {}).get(i, FLAT) for i in range(n)]
    a = [(ask or {}).get(i, tuple(x + 1.0 for x in b[i])) for i in range(n)]
    df = bars_from_ohlc(b)
    if with_ask_cols:
        for j, k in enumerate(ASK_COLUMNS):
            df[k] = [r[j] for r in a]
        df["spread"] = df["ask_close"] - df["close"]
    return freeze(df, ds_id)


class ScriptedExit(Scripted):
    def __init__(self, order, signals, exits, **kw):
        super().__init__(order, signals, **kw)
        self._x = exits

    def generate_signals(self, bars):
        s = super().generate_signals(bars)
        s.exit_long, s.exit_short = np.zeros(len(bars), bool), np.zeros(len(bars), bool)
        for i, d in self._x.items():
            (s.exit_long if d > 0 else s.exit_short)[i] = True
        return s


def one(ds, strat, costs=QUOTES, **cfg):
    res = run_backtest(ds, strat, costs, bt_cfg(**cfg), sizing={"mode": "fixed", "contracts": 1})
    return res, (res.trades.iloc[0] if len(res.trades) else None)


def mkt(o, h, l, c):
    return MarketArrays(*(np.array([x]) for x in (o, h, l, c)), np.array([False]), np.array([True]),
                        np.array([0], np.int64))


class TestDirectionalFills(unittest.TestCase):
    """Known answers. Signal at bar 2 -> first possible fill on bar 3."""

    def check(self, t, reason, entry, exit_, e_bar, x_bar, sides):
        self.assertEqual((t["exit_reason"], t["entry_price_theo"], t["exit_price_theo"], t["entry_bar"],
                          t["exit_bar"], t["entry_quote_side"], t["exit_quote_side"]),
                         (reason, entry, exit_, e_bar, x_bar) + sides)

    def test_market_entries_and_close_exits(self):
        o = OrderSpec("market", stop_points=5.0, time_exit_bars=3)
        _, t = one(qds(), Scripted(o, {2: 1}))
        self.check(t, "TIME", 101.0, 100.0, 3, 5, ("ask", "bid"))                 # buy ASK open, sell BID close
        self.assertAlmostEqual(t["gross_usd"], -1.0 * CFD.point_value)             # spread is inside gross
        _, t = one(qds(), Scripted(o, {2: -1}))
        self.check(t, "TIME", 100.0, 101.0, 3, 5, ("bid", "ask"))                 # sell BID open, buy ASK close

    def test_stop_entries_trigger_on_entry_side(self):
        o = OrderSpec("stop", stop_points=5.0, time_exit_bars=2)
        ds = qds(bid={3: (100, 101.9, 99.5, 100)}, ask={3: (101, 102.9, 100.5, 101)})
        _, t = one(ds, Scripted(o, {2: 1}, entry={2: 102.0}))                     # ASK high 102.9 >= 102
        self.assertEqual((t["entry_bar"], t["entry_price_theo"], t["entry_fill_kind"]), (3, 102.0, "level"))
        res, t = one(ds, Scripted(o, {2: 1}, entry={2: 102.0}), SINGLE)          # BID high 101.9: no fill
        self.assertIsNone(t)
        ds = qds(bid={3: (100, 100.5, 98.9, 100)}, ask={3: (101, 101.5, 99.9, 101)})
        _, t = one(ds, Scripted(o, {2: -1}, entry={2: 99.0}))                     # BID low 98.9 <= 99
        self.assertEqual((t["entry_bar"], t["entry_price_theo"], t["entry_quote_side"]), (3, 99.0, "bid"))

    def test_limit_entries_trigger_on_entry_side(self):
        o = OrderSpec("limit", stop_points=5.0, time_exit_bars=2, entry_expiry_bars=3)
        ds = qds(bid={3: (100, 100.5, 99.0, 100), 4: (100, 100.5, 98.4, 100)},
                 ask={3: (101, 101.5, 100.0, 101), 4: (101, 101.5, 99.4, 101)})
        _, t = one(ds, Scripted(o, {2: 1}, entry={2: 99.5}))                      # ASK low 100.0 on bar 3: no
        self.assertEqual((t["entry_bar"], t["entry_price_theo"]), (4, 99.5))
        ds = qds(bid={4: (100, 101.1, 99.5, 100)}, ask={3: (101, 101.5, 100.5, 101), 4: (101, 102.1, 100.5, 101)})
        _, t = one(ds, Scripted(o, {2: -1}, entry={2: 101.0}))                    # ASK high 101.5 on bar 3 ignored
        self.assertEqual((t["entry_bar"], t["entry_price_theo"]), (4, 101.0))

    def test_long_exits_on_bid(self):
        o = OrderSpec("market", stop_points=2.0, time_exit_bars=8)
        ds = qds(bid={5: (100, 100.5, 98.9, 100)}, ask={5: (101, 101.5, 99.9, 101)})
        _, t = one(ds, Scripted(o, {2: 1}))                                       # stop 99: BID low 98.9
        self.check(t, "STOP", 101.0, 99.0, 3, 5, ("ask", "bid"))
        o = OrderSpec("market", stop_points=5.0, target_points=2.0, time_exit_bars=8)
        ds = qds(bid={5: (100, 102.5, 99.5, 100), 7: (100, 103.2, 99.5, 100)},
                 ask={5: (101, 103.5, 100.5, 101), 7: (101, 104.2, 100.5, 101)})
        _, t = one(ds, Scripted(o, {2: 1}))                                       # target 103: BID high, not ASK
        self.check(t, "TARGET", 101.0, 103.0, 3, 7, ("ask", "bid"))

    def test_short_exits_on_ask(self):
        o = OrderSpec("market", stop_points=2.0, time_exit_bars=8)
        ds = qds(bid={5: (100, 101.5, 99.5, 100)}, ask={5: (101, 102.5, 100.5, 101)})
        _, t = one(ds, Scripted(o, {2: -1}))                                      # stop 102: ASK high 102.5
        self.check(t, "STOP", 100.0, 102.0, 3, 5, ("bid", "ask"))
        _, t1 = one(ds, Scripted(o, {2: -1}), SINGLE)                             # single BID series misses it
        self.assertNotEqual(t1["exit_reason"], "STOP")
        o = OrderSpec("market", stop_points=5.0, target_points=2.0, time_exit_bars=8)
        ds = qds(bid={5: (100, 100.5, 97.9, 100), 7: (100, 100.5, 96.9, 100)},
                 ask={5: (101, 101.5, 98.9, 101), 7: (101, 101.5, 97.9, 101)})
        _, t = one(ds, Scripted(o, {2: -1}))                                      # target 98: ASK low, not BID
        self.check(t, "TARGET", 100.0, 98.0, 3, 7, ("bid", "ask"))

    def test_gaps_use_exit_side_open(self):
        o = OrderSpec("market", stop_points=2.0, target_points=10.0, time_exit_bars=8)
        _, t = one(qds(bid={5: (98.5, 100, 98, 99)}, ask={5: (99.5, 101, 99, 100)}), Scripted(o, {2: 1}))
        self.check(t, "STOP_GAP", 101.0, 98.5, 3, 5, ("ask", "bid"))
        _, t = one(qds(bid={5: (101.5, 102, 101, 101.5)}, ask={5: (102.5, 103, 102, 102.5)}), Scripted(o, {2: -1}))
        self.check(t, "STOP_GAP", 100.0, 102.5, 3, 5, ("bid", "ask"))
        o = OrderSpec("market", stop_points=10.0, target_points=2.0, time_exit_bars=8)
        _, t = one(qds(bid={5: (96.5, 97, 96, 96.5)}, ask={5: (97.5, 98, 97, 97.5)}), Scripted(o, {2: -1}))
        self.check(t, "TARGET_GAP", 100.0, 98.0, 3, 5, ("bid", "ask"))

    def test_signal_exits_at_exit_side_open(self):
        o = OrderSpec("market", stop_points=5.0, time_exit_bars=9)
        _, t = one(qds(), ScriptedExit(o, {2: 1}, {5: 1}))
        self.check(t, "SIGNAL", 101.0, 100.0, 3, 6, ("ask", "bid"))
        _, t = one(qds(), ScriptedExit(o, {2: -1}, {5: -1}))
        self.check(t, "SIGNAL", 100.0, 101.0, 3, 6, ("bid", "ask"))


class TestEntryBarCertainty(unittest.TestCase):
    """resolve_bar on the fill bar: A = exit side, ent = entry side (two-sided rule)."""
    CONS, OPT = FillPolicy("conservative"), FillPolicy("optimistic")

    def test_limit_fill_bar_stop_touch_becomes_a_conflict(self):
        e = Entry(True, "", 0, 100.0, "level", "limit", 0)                        # long buy limit at 100
        single = mkt(101, 101.5, 98.8, 100.5)
        for pol in (self.CONS, self.OPT):                                         # old rule: provably after fill
            self.assertEqual(resolve_bar(single, None, 0, 1, 99.0, 110.0, pol, e, "limit", 100.0)[:3],
                             (1, 99.0, "STOP"))
        ask, bid = mkt(101, 101.5, 99.8, 100.5), mkt(100, 100.5, 98.8, 99.5)
        self.assertEqual(resolve_bar(bid, None, 0, 1, 99.0, 110.0, self.CONS, e, "limit", 100.0, ask),
                         (1, 99.0, "STOP", "CONSERVATIVE"))                     # same outcome, now a policy call
        self.assertEqual(resolve_bar(bid, None, 0, 1, 99.0, 110.0, self.OPT, e, "limit", 100.0, ask)[2:],
                         ("", "OPTIMISTIC"))

    def test_stop_fill_bar_target_certain_stop_not(self):
        e = Entry(True, "", 0, 102.0, "level", "stop", 0)                         # long buy stop at 102
        ask, bid = mkt(101, 103.5, 100.5, 103), mkt(100, 102.6, 99.5, 102)
        self.assertEqual(resolve_bar(bid, None, 0, 1, 99.0, 102.5, self.OPT, e, "stop", 102.0, ask),
                         (2, 102.5, "TARGET", ""))                                # BID>=T => ASK>=T, passed L
        self.assertEqual(resolve_bar(bid, None, 0, 1, 99.6, 110.0, self.OPT, e, "stop", 102.0, ask)[2:],
                         ("", "OPTIMISTIC"))                                      # stop touch never provable
        e = Entry(True, "", 0, 100.0, "level", "stop", 0)                         # short sell stop at 100
        bid, ask = mkt(101, 101.5, 96.9, 99.5), mkt(102, 102.5, 97.9, 100.5)
        self.assertEqual(resolve_bar(ask, None, 0, -1, 110.0, 98.0, self.CONS, e, "stop", 100.0, bid),
                         (2, 98.0, "TARGET", ""))                                 # ASK<=T => BID<=T, passed L

    def test_market_fill_touches_certain(self):
        e = Entry(True, "", 0, 101.0, "open", "market", 0)
        ask, bid = mkt(101, 101.5, 99.8, 100.5), mkt(100, 100.5, 98.8, 99.5)
        self.assertEqual(resolve_bar(bid, None, 0, 1, 99.0, 110.0, self.OPT, e, "market", np.nan, ask),
                         (1, 99.0, "STOP", ""))


class TestDirectionalIntrabar(unittest.TestCase):
    def ib(self, rows):
        a = np.array(rows, float)
        return IntrabarData(a[:, 0], a[:, 1], a[:, 2], a[:, 3], np.array([0]), np.array([len(rows)]), np.array([True]))

    def test_entry_replay_uses_entry_side(self):
        ask = self.ib([(101, 102.5, 100.5, 101.5), (101.5, 102.9, 99.8, 100.5), (100.5, 106.2, 100.4, 106)])
        bid = self.ib([(100, 101.5, 99.5, 100.5), (100.5, 101.9, 98.8, 99.5), (99.5, 105.2, 99.4, 105)])
        pol = FillPolicy("intrabar")
        self.assertEqual(_resolve_intrabar(bid, 0, 1, 99.0, 105.0, ("stop", 102.0), pol, ask),
                         (1, 99.0, "STOP"))              # filled on ASK at minute 0, BID stop at minute 1
        self.assertEqual(_resolve_intrabar(bid, 0, 1, 99.0, 105.0, ("stop", 102.0), pol),
                         (2, 105.0, "TARGET"))           # a BID-triggered buy stop would fill later

    def frames(self):
        n = 40
        bid = [FLAT] * n
        ask = [tuple(x + 1 for x in FLAT)] * n
        bid[20], ask[20] = FLAT, (101, 104.5, 100.5, 101)                        # ASK reaches target first
        bid[21], ask[21] = (100, 100.5, 97.5, 100), (101, 101.5, 98.5, 101)      # BID stop
        bid[22], ask[22] = (100, 104.2, 99.5, 100), (101, 105.2, 100.5, 101)     # BID target
        df = bars_from_ohlc(bid)
        for j, k in enumerate(ASK_COLUMNS):
            df[k] = [r[j] for r in ask]
        return df

    def run5(self, df1, df5=None):
        ltf = validate_and_freeze(df1, CFD, UTC247, "1m", 1, "test", "L1", asset_type="CFD")
        df5 = resample_bars(self.frames(), UTC247, 5).drop(columns=["n_subbars"]) if df5 is None else df5
        htf = validate_and_freeze(df5, CFD, UTC247, "5m", 5, "test", "H5", asset_type="CFD")
        o = OrderSpec("market", stop_points=3.0, target_points=3.0, time_exit_bars=6)
        res = run_backtest(htf, Scripted(o, {1: 1}), QUOTES, bt_cfg(same_bar_policy="intrabar"),
                           sizing={"mode": "fixed", "contracts": 1}, ltf=ltf)
        return res, res.trades.iloc[0]

    def test_exit_replay_uses_exit_side(self):
        res, t = self.run5(self.frames())
        self.assertEqual((t["exit_reason"], t["exit_price_theo"], t["exit_bar"], t["conflict_resolution"]),
                         ("STOP", 98.0, 4, "INTRABAR"))  # BID minutes: stop (m21) before target (m22)
        self.assertTrue(res.intrabar["ltf_has_ask_ohlc"])

    def test_ltf_without_ask_refuses_replay(self):
        res, t = self.run5(self.frames().drop(columns=list(ASK_COLUMNS)))
        self.assertFalse(res.intrabar["available"])
        self.assertIn("ASK OHLC", res.intrabar["reason"])
        self.assertEqual(t["conflict_resolution"], "INTRABAR_UNAVAILABLE->CONSERVATIVE")

    def test_both_sides_must_reproduce_the_bar(self):
        df5 = resample_bars(self.frames(), UTC247, 5).drop(columns=["n_subbars"])
        df5.loc[4, "ask_high"] += 0.5                    # ASK side of bar 4 not reproduced by the 1m ASK
        res, t = self.run5(self.frames(), df5)
        self.assertLess(res.intrabar["reliable_bar_fraction"], 1.0)
        self.assertEqual(t["conflict_resolution"], "INTRABAR_UNAVAILABLE->CONSERVATIVE")


class TestQuotesCosts(unittest.TestCase):
    def test_no_separate_spread_commission_on_quote_fills_slippage_once(self):
        pv = CFD.point_value
        o = OrderSpec("market", stop_points=2.0, time_exit_bars=8)
        _, t = one(qds(bid={5: (100, 100.5, 98.9, 100)}, ask={5: (101, 101.5, 99.9, 101)}), Scripted(o, {2: 1}))
        self.assertEqual(t["spread_usd"], 0.0)
        self.assertAlmostEqual(t["commission_usd"], 30.15e-6 * (101.0 + 99.0) * pv)   # ASK entry, BID exit
        self.assertAlmostEqual(t["slippage_usd"], (0.5 + 0.5) * pv)                    # market + stop, once each
        self.assertAlmostEqual(t["gross_usd"], (99.0 - 101.0) * pv)
        self.assertAlmostEqual(t["net_usd"], t["gross_usd"] - t["commission_usd"] - t["slippage_usd"])
        self.assertAlmostEqual(t["entry_price_eff"] - t["entry_price_theo"], 0.5)      # display only
        o = OrderSpec("market", stop_points=5.0, target_points=2.0, time_exit_bars=8)
        _, t = one(qds(bid={5: (100, 100.5, 97.9, 100), 7: (100, 100.5, 96.9, 100)},
                       ask={5: (101, 101.5, 98.9, 101), 7: (101, 101.5, 97.9, 101)}), Scripted(o, {2: -1}))
        self.assertAlmostEqual(t["slippage_usd"], 0.5 * pv)                            # target = limit: 0
        self.assertAlmostEqual(t["commission_usd"], 30.15e-6 * (100.0 + 98.0) * pv)   # BID entry, ASK exit
        self.assertEqual(t["spread_usd"], 0.0)

    def test_quotes_never_charges_spread_twice(self):
        with self.assertRaises(ValueError):
            CostModel(spread_source="quotes", spread_points=1.0)
        with self.assertRaises(ValueError):
            QUOTES.round_trip_base("market", "market", 1, CFD, spread_points=1.0, entry_price=1, exit_price=1)
        self.assertEqual(QUOTES.round_trip_base("market", "market", 1, CFD, entry_price=1, exit_price=1)["spread_usd"], 0)

    def test_config_resolution(self):
        cfg = copy.deepcopy(CFG)
        prof = cfg["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"]
        self.assertEqual((prof["spread_source"], prof["scenario"]),                 # canonical live profile (ADR-55)
                         ("quotes", "dukascopy_directional_cost_assumption_v1"))
        cm = cost_model_from_config(cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")
        self.assertEqual((cm.to_dict()["spread_source"], cm.spread_points), ("quotes", 0.0))
        legacy = copy.deepcopy(cfg)                                                 # legacy single-series still resolves
        legacy["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"].update(
            spread_source="dataset", scenario="dukascopy_central_cost_assumption_v1")
        self.assertEqual(cost_model_from_config(legacy, "NQ_DUKASCOPY", provider="DUKASCOPY").spread_source, "dataset")
        prof["spread_points"] = 1.0
        with self.assertRaises(CostConfigError):
            cost_model_from_config(cfg, "NQ_DUKASCOPY", provider="DUKASCOPY")


class TestQuotesRefusalAndProvenance(unittest.TestCase):
    O = OrderSpec("market", stop_points=5.0, time_exit_bars=3)

    def test_refuses_without_complete_finite_ask(self):
        with self.assertRaises(BacktestError):
            one(qds(with_ask_cols=False), Scripted(self.O, {2: 1}))                # BID-only
        df = bars_from_ohlc([FLAT] * 12)
        df["spread"] = 1.0
        with self.assertRaises(BacktestError):
            one(freeze(df, "SPR"), Scripted(self.O, {2: 1}))                        # spread but no ASK OHLC
        b = BarArrays.from_frame(with_ask(), 1)
        bad = BarArrays(b.ts_ns, b.open, b.high, b.low, b.close, b.volume, 1, None,
                        b.ask_open, b.ask_high, np.r_[b.ask_low[:-1], np.nan], b.ask_close)
        with self.assertRaises(BacktestError):
            _check_quotes(bad)                                                      # invalid ASK value
        with self.assertRaises(ValueError):
            BarArrays(b.ts_ns, b.open, b.high, b.low, b.close, b.volume, 1, None, ask_open=b.ask_open)

    def test_assumptions_and_trade_provenance(self):
        res, t = one(qds(), Scripted(self.O, {2: 1}))
        a = res.assumptions
        self.assertEqual((a["quote_model"], a["execution_sides"], a["spread_treatment"], a["dataset_has_ask_ohlc"],
                          a["costs"]["spread_source"]),
                         ("directional_bid_ask", {"buy": "ask", "sell": "bid"}, "embedded_in_quotes", True, "quotes"))
        self.assertIn("ASK", a["market_entry"])
        self.assertIn("AFTER the bid/ask spread", a["gross_pnl"])
        self.assertTrue(res.dataset["has_ask_ohlc"])
        res, t = one(qds(), Scripted(self.O, {2: 1}), SINGLE)                       # ASK present but not used
        a = res.assumptions
        self.assertEqual((a["quote_model"], a["execution_sides"], a["spread_treatment"], a["dataset_has_ask_ohlc"]),
                         ("single_series", {"buy": "unknown", "sell": "unknown"}, "none", True))
        self.assertEqual((t["entry_quote_side"], t["exit_quote_side"]), ("unknown", "unknown"))


def _walk(n=1500, seed=3):
    rng = np.random.default_rng(seed)
    c = np.round(100 + np.cumsum(rng.normal(0, 0.6, n)), 2)
    o = np.r_[c[0], c[:-1]]
    h = np.round(np.maximum(o, c) + rng.uniform(0, 0.8, n), 2)
    l = np.round(np.minimum(o, c) - rng.uniform(0, 0.8, n), 2)
    return list(zip(o, h, l, c))


class TestEquivalenceAskEqualsBid(unittest.TestCase):
    """ASK == BID: directional execution must reproduce the single-series result exactly."""

    def test_same_trades_hash(self):
        rows = _walk()
        df = bars_from_ohlc(rows)
        for src, k in zip(PRICE_COLS, ASK_COLUMNS):
            df[k] = df[src]
        ds = freeze(df, "EQ")
        sig = {i: (1 if (i // 12) % 2 else -1) for i in range(20, 1480, 12)}
        close = np.array([r[3] for r in rows])
        cases = [(OrderSpec("market", stop_points=3.0, target_points=4.0, time_exit_bars=30), {}, ("conservative", "optimistic")),
                 (OrderSpec("stop", stop_points=3.0, target_points=4.0, time_exit_bars=30, entry_expiry_bars=3),
                  {i: close[i] + 0.5 * d for i, d in sig.items()}, ("conservative", "optimistic")),
                 (OrderSpec("limit", stop_points=3.0, target_points=4.0, time_exit_bars=30, entry_expiry_bars=3),
                  {i: close[i] - 0.5 * d for i, d in sig.items()}, ("conservative",))]
        for order, entry, policies in cases:
            for pol in policies:
                with self.subTest(entry=order.entry_type, policy=pol):
                    r1 = run_backtest(ds, Scripted(order, sig, entry=entry), SINGLE, bt_cfg(same_bar_policy=pol))
                    r2 = run_backtest(ds, Scripted(order, sig, entry=entry), QUOTES, bt_cfg(same_bar_policy=pol))
                    self.assertGreater(len(r1.trades), 30)
                    self.assertEqual(r1.trades_hash, r2.trades_hash)
                    np.testing.assert_array_equal(r1.trades["net_usd"], r2.trades["net_usd"])
                    self.assertEqual(r1.skipped, r2.skipped)


PRICE_COLS = ("open", "high", "low", "close")


class PeekNextAsk(Strategy):
    family = "peek_next_ask"

    def generate_signals(self, bars):
        s = SignalSet.empty(len(bars))
        nxt = np.r_[bars.ask_close[1:], np.nan]
        with np.errstate(invalid="ignore"):
            s.direction[nxt > bars.ask_close + 0.5] = 1
        return s


class TestCausalityWithAsk(unittest.TestCase):
    def test_truncation_check_covers_ask_arrays(self):
        rows = _walk(400)
        df = bars_from_ohlc(rows)
        rng = np.random.default_rng(1)
        s = np.round(rng.uniform(0.5, 1.5, len(df)), 2)
        for src, k in zip(PRICE_COLS, ASK_COLUMNS):
            df[k] = df[src] + s
        ds = freeze(df, "CAUS")
        cfg = bt_cfg(require_causality_check=True)
        with self.assertRaises(BacktestError) as cm:
            run_backtest(ds, PeekNextAsk(OrderSpec("market", stop_points=3.0)), QUOTES, cfg)
        self.assertIn("LOOKAHEAD", str(cm.exception))
        ok = run_backtest(ds, Scripted(OrderSpec("market", stop_points=3.0, time_exit_bars=5), {50: 1}), QUOTES, cfg)
        self.assertTrue(ok.causality.passed)


class TestQuotesThroughServices(unittest.TestCase):
    """Eligibility + backtester refusal + recorded provenance under the CANONICAL Dukascopy profile
    (spread_source: quotes, ADR-55), on a temp copy of the repository configs; synthetic data only."""

    @classmethod
    def setUpClass(cls):
        cls.root = Path(tempfile.mkdtemp())
        shutil.copytree("configs", cls.root / "configs")
        csv = cls.root / "combined.csv"
        write_fixture(csv, end="2024-03-09")
        f = pd.read_csv(csv, dtype=str)
        s = 1.0 + 0.25 * (np.arange(len(f)) % 7)
        for k, extra in (("open", 0.0), ("high", 0.5), ("low", 0.0), ("close", 0.0)):
            f[f"ask_{k}"] = (f[k].astype(float) + s + extra).map(lambda x: f"{x:.3f}")
        f.to_csv(csv, index=False)
        cls.svc = Services(root=cls.root)
        base = dict(file=str(csv), profile="dukascopy_utc_csv", instrument="NQ_DUKASCOPY", provider="DUKASCOPY",
                    asset_type="CFD", symbol="USATECH.IDX/USD", price_basis="bid", timeframe="1m",
                    derive_timeframes=["5m"], build_features=False, bid_close_column="close",
                    ask_close_column="ask_close")
        cls.close_only = cls.svc.import_file({**base, "dataset_name": "CLOSEONLY"})["derived"][0]
        cls.ohlc = cls.svc.import_file({**base, "dataset_name": "ASKOHLC", "ask_open_column": "ask_open",
                                        "ask_high_column": "ask_high", "ask_low_column": "ask_low"})["derived"][0]
        cls.yaml = Path("strategies/fixtures/ema_crossover.yaml").read_text()

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.root, ignore_errors=True)

    def test_eligibility_requires_ask_ohlc(self):
        rows = {d["dataset_id"]: d for d in self.svc.backtest_readiness()["datasets"]}
        self.assertTrue(any("no ASK OHLC" in r for r in rows[self.close_only]["reasons"]))
        self.assertEqual(rows[self.close_only]["reason_codes"], ["ASK_OHLC_REQUIRED"])     # machine-readable
        self.assertEqual((rows[self.close_only]["runnable"], rows[self.close_only]["has_ask_ohlc"]), (False, False))
        self.assertFalse(any("ASK OHLC" in r for r in rows[self.ohlc]["reasons"]))
        self.assertEqual((rows[self.ohlc]["reason_codes"], rows[self.ohlc]["has_ask_ohlc"]), ([], True))
        self.assertEqual((rows[self.ohlc]["cost"]["spread_source"], rows[self.ohlc]["cost"]["quote_model"]),
                         ("quotes", "directional_bid_ask"))

    def test_legacy_single_series_profile_keeps_its_old_eligibility(self):
        cfg = copy.deepcopy(self.svc.cfg)
        cfg["costs"]["symbols"]["NQ_DUKASCOPY"]["providers"]["DUKASCOPY"].update(
            spread_source="dataset", scenario="dukascopy_central_cost_assumption_v1")
        svc = Services(cfg=cfg, root=self.root)
        rows = {d["dataset_id"]: d for d in svc.backtest_readiness()["datasets"]}
        self.assertEqual((rows[self.close_only]["reason_codes"], rows[self.close_only]["cost"]["quote_model"]),
                         ([], "single_series"))                                     # BID+spread dataset eligible again
        self.assertFalse(any("ASK OHLC" in r for r in rows[self.close_only]["reasons"]))

    def test_backtester_refuses_and_records_provenance(self):
        with self.assertRaises(Exception) as cm:
            self.svc.backtest_strategy(self.yaml, self.close_only, record=False)
        self.assertIn("ASK OHLC", str(cm.exception))
        out = self.svc.backtest_strategy(self.yaml, self.ohlc, record=True)
        rec, trades = self.svc.store.load_run(out["run_id"])
        a = rec["assumptions"]
        self.assertEqual((a["quote_model"], a["spread_treatment"], a["costs"]["spread_source"],
                          a["costs"]["scenario"], a["dataset_has_ask_ohlc"]),
                         ("directional_bid_ask", "embedded_in_quotes", "quotes",
                          "dukascopy_directional_cost_assumption_v1", True))
        self.assertEqual((rec["dataset"]["dataset_id"], rec["dataset"]["has_ask_ohlc"]), (self.ohlc, True))
        self.assertEqual(rec["strategy"]["strategy_id"], out["strategy_id"])
        self.assertGreater(len(trades), 0)
        self.assertEqual(set(trades["spread_usd"]), {0.0})
        long_ = trades["direction"] > 0
        self.assertTrue((trades.loc[long_, "entry_quote_side"] == "ask").all())
        self.assertTrue((trades.loc[~long_, "entry_quote_side"] == "bid").all())
