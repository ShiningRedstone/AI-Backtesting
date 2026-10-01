"""ADR-81 paper trading: forward-only accounts on a validated feed, prop attempts with fees, kept out of research.

SYNTHETIC data only (tests/paper_fixture.py stands in for the Dukascopy downloader; no network)."""
import shutil
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest import mock

import pandas as pd
import yaml

from edgelab.paper import engine, feed, store
from tests.paper_fixture import fake_fetcher

REPO = Path(__file__).resolve().parents[1]
UTC = timezone.utc
CREATED = datetime(2024, 3, 5, 15, 0, tzinfo=UTC)        # Tue 10:00 NY -> starts Wed 2024-03-06
FEES = {"eval_price": 100.0, "reset_fee": 50.0, "activation_fee": 30.0}
PROFILE = "TRADEIFY_GROWTH_50K"


def mnq_strategy(name, sizing=None):
    d = yaml.safe_load((REPO / "strategies" / "fixtures" / "ema_crossover.yaml").read_text())
    d["name"] = name
    d["sizing"] = sizing or {"mode": "risk", "risk_usd": 500, "max_quantity": 40, "contract": "MNQ"}
    return d


class PaperBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        cls.tmp = Path(tempfile.mkdtemp())
        create_demo_workspace(cls.tmp / "demo", REPO)
        cls.svc = Services(root=cls.tmp / "demo")
        cls.svc.paper.fetcher = fake_fetcher
        cls.sid = cls.svc.save_strategy(mnq_strategy("paper_test_ema"))["strategy_id"]
        cls.svc.set_ui_preferences({"prop_fees": {PROFILE: FEES}})

    @classmethod
    def tearDownClass(cls):
        cls.svc.store.close()
        shutil.rmtree(cls.tmp, ignore_errors=True)


class TestPaperAccounts(PaperBase):
    def test_forward_only_attempts_fees_and_separation_from_research(self):
        svc = self.svc
        n_ds, n_runs = len(svc.store.list_datasets()), len(svc.store.list_runs())
        r = svc.paper_start([self.sid], PROFILE, now=CREATED)
        self.assertEqual(r["start_date"], "2024-03-06")
        aid = r["accounts"][0]
        out = svc.paper.run_once(now=datetime(2024, 5, 30, 23, 0, tzinfo=UTC))
        self.assertEqual(out.get("accounts_updated"), 1, svc.paper.status)
        st = svc.paper_account(aid)["state"]
        start = pd.Timestamp(svc.paper_account(aid)["account"]["start_ts"])
        self.assertEqual(start, pd.Timestamp("2024-03-05T23:00:00Z"))                 # 18:00 NY, session of 03-06
        trades = st["trades"]
        self.assertTrue(trades)
        self.assertTrue(all(pd.Timestamp(t["entry_ts"]) >= start for t in trades))     # never a pre-start trade
        att = st["attempts"]
        self.assertGreaterEqual(len(att), 2)
        for a, b in zip(att, att[1:]):                                                 # attempts follow each other
            self.assertIsNotNone(a["end"])
            self.assertGreater(pd.Timestamp(b["start"]), pd.Timestamp(a["end"]))
            self.assertIn(a["status"], ("failed", "funded_lost", "funded_completed"))
        self.assertIn(att[-1]["status"], ("in_progress", "funded"))
        self.assertEqual(sum(a["n_trades"] for a in att), len(trades))                 # every trade in one attempt
        self.assertEqual(len({t["entry_ts"] for t in trades}), len(trades))
        for a in att[:-1]:
            self.assertTrue(all(pd.Timestamp(t["entry_ts"]) <= pd.Timestamp(a["end"]) for t in trades
                                if pd.Timestamp(a["start"]) <= pd.Timestamp(t["entry_ts"]) <= pd.Timestamp(a["end"])))
        # fee rules: first = eval price; after an evaluation failure = reset fee; after a funded cycle = eval price;
        # activation fee on every pass; totals and net are exact
        kinds = [f for f in st["fees"] if f["kind"] != "activation_fee"]
        self.assertEqual(kinds[0]["kind"], "eval_price")
        for prev, nxt in zip(att, kinds[1:]):
            self.assertEqual(nxt["kind"], "reset_fee" if prev["status"] == "failed" else "eval_price")
        self.assertEqual(sum(1 for f in st["fees"] if f["kind"] == "activation_fee"), st["passes"])
        self.assertAlmostEqual(st["fees_total"], sum(f["amount"] for f in st["fees"]))
        self.assertAlmostEqual(st["net"], round(st["trader_payouts"] - st["fees_total"], 2))
        self.assertAlmostEqual(st["trader_payouts"], sum(p["trader_share"] for p in st["payouts"]))
        # nothing reached research: no dataset, no run, no trial
        self.assertEqual((len(svc.store.list_datasets()), len(svc.store.list_runs())), (n_ds, n_runs))
        self.assertIn("PAPER", st["label"])
        # adding days never changes what already happened (forward feed, causal engine)
        early = svc.paper.run_once(now=datetime(2024, 4, 10, 23, 0, tzinfo=UTC))
        self.assertEqual(early.get("accounts_updated"), 1)
        st_early = svc.paper_account(aid)["state"]
        later = {(t["entry_ts"], t["net_usd"]) for t in trades}
        self.assertTrue({(t["entry_ts"], t["net_usd"]) for t in st_early["trades"]} <= later)
        rows = svc.paper_accounts()
        self.assertEqual(rows[0]["account_id"], aid)
        svc.paper_set_status(aid, "delete")

    def test_refusals(self):
        svc = self.svc
        with self.assertRaises(ValueError):
            svc.paper_start([self.sid], "NOPE", now=CREATED)
        with self.assertRaises(ValueError):
            svc.paper_start([], PROFILE, now=CREATED)
        with self.assertRaises(KeyError):
            svc.paper_start(["STR_000000000000"], PROFILE, now=CREATED)
        plain = svc.save_strategy(mnq_strategy("paper_test_fixed", {"mode": "fixed", "quantity": 1}))["strategy_id"]
        with self.assertRaises(ValueError) as cm:
            svc.paper_start([plain], PROFILE, now=CREATED)
        self.assertIn("not sized in MNQ", str(cm.exception))
        with mock.patch.object(svc, "ui_preferences", return_value={**svc.ui_preferences(), "prop_fees": {}}):
            with self.assertRaises(ValueError) as cm:
                svc.paper_start([self.sid], PROFILE, now=CREATED)
        self.assertIn("evaluation price", str(cm.exception))
        with self.assertRaises(ValueError):
            svc.set_ui_preferences({"prop_fees": {PROFILE: {"eval_price": -5}}})

    def test_stop_resume_and_reset_removes_paper_records(self):
        svc = self.svc
        aid = svc.paper_start([self.sid], PROFILE, now=CREATED)["accounts"][0]
        self.assertEqual(svc.paper_set_status(aid, "stop")["status"], "stopped")
        self.assertEqual(svc.paper.run_once(now=datetime(2024, 3, 20, 23, 0, tzinfo=UTC)).get("accounts_updated"), 0)
        self.assertEqual(svc.paper_set_status(aid, "resume")["status"], "running")
        self.assertEqual(store.delete_records(svc.data_root), 1)
        self.assertEqual(svc.paper_accounts(), [])
        self.assertTrue(feed.status(svc.data_root)["n_days"] > 0)                       # the feed (price data) is kept


class TestAttemptRules(unittest.TestCase):
    """Known answers for the attempt wrapper with a controlled lifecycle (no data needed)."""

    def test_fail_reset_pass_funded_loss_new_evaluation(self):
        seq = iter([
            {"evaluation": {"status": "FAIL", "failure_reason": "MAX_LOSS_LIMIT", "breach": {"ts": "2024-03-07T15:00:00Z",
             "reason": "MAX_LOSS_LIMIT", "detail": "x"}, "profit": -2000, "trading_days": 2, "balance": 48000, "pass": None},
             "funded": {"started": False, "status": "NOT_APPLICABLE"}, "payouts": [], "totals": {"trader_payout": 0}},
            {"evaluation": {"status": "PASS", "failure_reason": None, "breach": None, "profit": 3000, "trading_days": 3,
             "balance": 53000, "pass": {"ts": "2024-03-12T15:00:00Z", "day_end_ts": "2024-03-12T20:15:00Z"}},
             "funded": {"started": True, "status": "FAIL", "outcome": "BREACH", "balance": 47000,
                        "breach": {"ts": "2024-03-20T15:00:00Z", "reason": "MAX_LOSS_LIMIT", "detail": "y"}},
             "payouts": [{"n": 1, "date": "2024-03-18", "gross": 1000, "trader_share": 900, "firm_share": 100,
                          "balance_after": 51000, "eligibility_ts": "2024-03-18T19:00:00Z"}],
             "totals": {"trader_payout": 900}},
            {"evaluation": {"status": "FAIL", "failure_reason": "NOT_PASSED_BY_END_OF_DATA", "breach": None, "profit": 100,
             "trading_days": 1, "balance": 50100, "pass": None},
             "funded": {"started": False, "status": "NOT_APPLICABLE"}, "payouts": [], "totals": {"trader_payout": 0}}])
        one = pd.DataFrame({"entry_ts": [pd.Timestamp("2025-01-01", tz="UTC")]})
        with mock.patch("edgelab.prop.lifecycle.simulate_lifecycle", side_effect=lambda *_: next(seq)), \
                mock.patch("edgelab.prop.lifecycle.compact", side_effect=lambda r: {"evaluation": r["evaluation"],
                                                                                   "funded": r["funded"]}):
            res = engine.run_attempts(lambda t: one, {}, FEES, pd.Timestamp("2024-03-06", tz="UTC"))
        self.assertEqual([a["status"] for a in res["attempts"]], ["failed", "funded_lost", "in_progress"])
        self.assertEqual([f["kind"] for f in res["fees"]], ["eval_price", "reset_fee", "activation_fee", "eval_price"])
        self.assertEqual(res["fees_total"], 100 + 50 + 30 + 100)
        self.assertEqual((res["trader_payouts"], res["net"], res["passes"]), (900, 900 - 280, 1))

    def test_no_reset_fee_charges_a_new_evaluation_and_incompatible_stops(self):
        seq = iter([
            {"evaluation": {"status": "FAIL", "failure_reason": "MAX_LOSS_LIMIT", "breach": {"ts": "2024-03-07T15:00:00Z",
             "reason": "MAX_LOSS_LIMIT", "detail": "x"}, "profit": -2000, "trading_days": 2, "balance": 48000, "pass": None},
             "funded": {"started": False, "status": "NOT_APPLICABLE"}, "payouts": [], "totals": {"trader_payout": 0}},
            {"evaluation": {"status": "INCOMPATIBLE", "failure_reason": "MAX_CONTRACTS", "breach": {"ts": "2024-03-08T15:00:00Z",
             "reason": "MAX_CONTRACTS", "detail": "41 micros > 40"}, "profit": 0, "trading_days": 0, "balance": 50000,
             "pass": None}, "funded": {"started": False, "status": "NOT_APPLICABLE"}, "payouts": [],
             "totals": {"trader_payout": 0}}])
        one = pd.DataFrame({"entry_ts": [pd.Timestamp("2025-01-01", tz="UTC")]})
        with mock.patch("edgelab.prop.lifecycle.simulate_lifecycle", side_effect=lambda *_: next(seq)), \
                mock.patch("edgelab.prop.lifecycle.compact", side_effect=lambda r: {"evaluation": r["evaluation"],
                                                                                   "funded": r["funded"]}):
            res = engine.run_attempts(lambda t: one, {}, {"eval_price": 80}, pd.Timestamp("2024-03-06", tz="UTC"))
        self.assertEqual([f["kind"] for f in res["fees"]], ["eval_price", "eval_price"])
        self.assertEqual(res["fees_total"], 160)
        self.assertIn("41 micros", res["stopped"])


class TestFeed(unittest.TestCase):
    def setUp(self):
        from edgelab.core.config import load_config
        self.cfg = load_config(REPO / "configs")
        self.dir = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.dir, True)

    def test_completed_days_only_holidays_skipped_never_overwritten(self):
        now = datetime(2024, 3, 13, 18, 0, tzinfo=UTC)                       # Wed 14:00 NY: Wed not complete
        hol = lambda s, a, b: fake_fetcher(s, a, b, holidays={date(2024, 3, 8)})    # noqa: E731
        # the same validation limits as research data: one holiday in ~7 weeks stays under the 5% missing-bar limit
        r = feed.update(self.cfg, self.dir, date(2024, 1, 22), fetcher=hol, now=now)
        self.assertEqual(r["last_completed_date"], "2024-03-12")
        self.assertIn("2024-03-08", r["skipped"])
        self.assertNotIn("2024-03-13", r["downloaded"])
        p = feed.feed_dir(self.dir) / "days" / "2024-03-11.csv"
        before = p.read_bytes()
        again = feed.update(self.cfg, self.dir, date(2024, 1, 22), fetcher=fake_fetcher, now=now)
        self.assertEqual(again["downloaded"], [])
        self.assertEqual(p.read_bytes(), before)
        f = feed.build_feed(self.cfg, self.dir, date(2024, 1, 22))
        self.assertEqual(sorted(f.datasets), ["15m", "1m", "30m", "5m", "60m"])
        self.assertTrue(f.datasets["1m"].bars.has_ask_ohlc)
        p.write_text(p.read_text().replace("18", "19", 1))                     # tampered after download
        with self.assertRaises(feed.FeedError):
            feed.build_feed(self.cfg, self.dir, date(2024, 1, 22))

    def test_a_failed_day_never_leaves_a_hole(self):
        now = datetime(2024, 3, 13, 18, 0, tzinfo=UTC)

        def flaky(side, a, b):
            if a.date() == date(2024, 3, 6):                                     # session of 2024-03-07 (opens 03-06)
                raise ConnectionError("offline")
            return fake_fetcher(side, a, b)
        r = feed.update(self.cfg, self.dir, date(2024, 3, 4), fetcher=flaky, now=now)
        self.assertEqual(r["downloaded"], ["2024-03-04", "2024-03-05", "2024-03-06"])
        self.assertIn("download failed", r["errors"]["2024-03-07"])
        self.assertEqual(r["newest_day"], "2024-03-06")                           # 03-08.. wait for 03-07
        r = feed.update(self.cfg, self.dir, date(2024, 3, 4), fetcher=fake_fetcher, now=now)
        self.assertEqual(r["downloaded"], ["2024-03-07", "2024-03-08", "2024-03-11", "2024-03-12"])
        self.assertEqual(feed.status(self.dir)["errors"], {})

    def test_combine_refuses_inconsistent_sides_and_identity_check(self):
        s, e = feed._window(self.cfg, date(2024, 3, 6))
        bid, ask = fake_fetcher("BID", s, e), fake_fetcher("ASK", s, e)
        frame = feed.combine_day(bid, ask, s, e)
        self.assertEqual(len(frame), len(bid))
        with self.assertRaises(feed.FeedError):
            feed.combine_day(bid, ask.drop(ask.index[100]), s, e)               # a gap on one side inside the day
        bad = ask.copy()
        bad.iloc[5, bad.columns.get_loc("close")] = bid.iloc[5]["close"] - 1
        with self.assertRaises(feed.FeedError):
            feed.combine_day(bid, bad, s, e)                                      # negative spread
        self.assertIsNone(feed.combine_day(bid.iloc[:0], ask.iloc[:0], s, e))
        feed.update(self.cfg, self.dir, date(2024, 3, 4), fetcher=fake_fetcher,
                    now=datetime(2024, 3, 8, 23, 0, tzinfo=UTC))
        ds = feed.build_feed(self.cfg, self.dir, date(2024, 3, 4)).datasets["1m"]
        self.assertTrue(feed.verify_against(ds, frame)["identical"])
        moved = frame.copy()
        moved.loc[10, "close"] += 0.5
        self.assertFalse(feed.verify_against(ds, moved)["identical"])


class TestEquityFromSwitch(unittest.TestCase):
    """The optional engine switch: absent = byte-identical backtests; present = equity counted from that time only."""

    def test_default_unchanged_and_equity_counted_from_the_given_time(self):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        create_demo_workspace(tmp / "demo", REPO)
        svc = Services(root=tmp / "demo")
        self.addCleanup(svc.store.close)
        defn = mnq_strategy("eq_test", {"mode": "equity_risk", "risk_pct": 1.0, "max_quantity": 40})   # demo series = NQ
        did = next(d["dataset_id"] for d in svc.backtest_readiness(defn)["datasets"] if d["runnable"])
        from edgelab.engine import backtester
        real = backtester.run_backtest
        a = svc._run_cell(defn, did)["result"]
        seen = {}

        def with_account(*args, **kw):
            seen["account"] = kw.get("account")
            return real(*args, **{**kw, "account": {"name": "x", "starting_equity": 50_000.0}})
        with mock.patch.object(backtester, "run_backtest", with_account):
            b = svc._run_cell(defn, did)["result"]
        self.assertEqual(a.trades_hash, b.trades_hash)                              # explicit default = default
        t = a.trades
        cut = pd.Timestamp(t["entry_ts"].iloc[len(t) // 2])

        def from_cut(*args, **kw):
            return real(*args, **{**kw, "account": {"name": "x", "starting_equity": 50_000.0, "equity_from_ts": cut}})
        with mock.patch.object(backtester, "run_backtest", from_cut):
            c = svc._run_cell(defn, did)["result"]
        tc = c.trades
        before = tc[pd.to_datetime(tc["entry_ts"], utc=True) < cut]
        self.assertTrue((before["equity_before"] == 50_000.0).all())               # earlier trades never count
        first_after = tc[pd.to_datetime(tc["entry_ts"], utc=True) >= cut].iloc[0]
        self.assertEqual(float(first_after["equity_before"]), 50_000.0)              # equity starts at the cut
        self.assertEqual(c.assumptions["equity_sizing"]["equity_from_ts"], cut.isoformat())


class TestPaperHttp(PaperBase):
    def test_routes(self):
        from edgelab.web.app import create_app
        app = create_app(self.svc.root, demo=True)
        s2 = app.config["EDGELAB"]["services"]
        s2.paper.fetcher = fake_fetcher
        c = app.test_client()
        try:
            lib = s2.library.list()[0]["strategy_id"]                                   # a tested (NQ-sized) strategy
            did = next(d["dataset_id"] for d in s2.backtest_readiness(lib)["datasets"] if d["runnable"])
            s2.backtest_strategy(lib, did, record=True)
            r = c.get(f"/api/paper/candidates?profile_id={PROFILE}&show_all=1")
            self.assertEqual(r.status_code, 200)
            got = {x["strategy_id"]: x for x in r.get_json()["strategies"]}
            self.assertIn(lib, got)
            only = c.get(f"/api/paper/candidates?profile_id={PROFILE}&show_all=0").get_json()["strategies"]
            self.assertTrue(all(x["survivor"] for x in only))                            # survivors only by default
            self.assertEqual({x["strategy_id"] for x in only}, {k for k, v in got.items() if v["survivor"]})
            self.assertEqual(c.get("/api/paper/candidates?profile_id=bad id").status_code, 400)
            r = c.post("/api/paper/batches", json={"strategy_ids": [self.sid], "profile_id": PROFILE})
            self.assertEqual(r.status_code, 201, r.get_json())
            aid = r.get_json()["accounts"][0]
            rows = c.get("/api/paper/accounts").get_json()
            self.assertIn(aid, [x["account_id"] for x in rows])
            self.assertEqual(c.get(f"/api/paper/accounts/{aid}").status_code, 200)
            self.assertEqual(c.get("/api/paper/accounts/PA_000000000000").status_code, 404)
            self.assertEqual(c.post(f"/api/paper/accounts/{aid}/explode").status_code, 400)
            self.assertEqual(c.post(f"/api/paper/accounts/{aid}/stop").get_json()["status"], "stopped")
            self.assertEqual(c.get("/api/paper/feed").status_code, 200)
            self.assertEqual(c.post("/api/paper/batches", json={"strategy_ids": [], "profile_id": PROFILE}).status_code, 400)
            self.assertEqual(c.post(f"/api/paper/accounts/{aid}/delete").status_code, 200)
        finally:
            s2.store.close()


def shifted_fetcher(side, start, end):
    """A DIFFERENT source (synthetic): every price 0.25 higher than the stand-in archive."""
    df = fake_fetcher(side, start, end)
    for c in ("open", "high", "low", "close"):
        df[c] = df[c] + 0.25
    return df


def import_research_dataset(svc, tmp: Path, start: date, now: datetime, name: str, derive=()) -> str:
    """A stored 1-minute NQ_DUKASCOPY BID/ASK 'research' dataset built from the same SYNTHETIC archive."""
    from edgelab.data.importer import ImportOptions, import_dataset
    d = tmp / f"research_{name}"
    feed.update(svc.cfg, d, start, fetcher=fake_fetcher, now=now)
    days = sorted((feed.feed_dir(d) / "days").glob("*.csv"))
    out = d / "research.csv"
    with open(out, "w", encoding="utf-8", newline="") as fh:
        for i, p in enumerate(days):
            lines = p.read_text(encoding="utf-8").splitlines(keepends=True)
            fh.writelines(lines if i == 0 else lines[1:])
    opts = ImportOptions(file=str(out), instrument=feed.INSTRUMENT, provider=feed.PROVIDER, asset_type="CFD",
                         timeframe="1m", profile="dukascopy_utc_csv", symbol="USATECH.IDX/USD", price_basis="bid",
                         bid_close_column="close", ask_close_column="ask_close", ask_open_column="ask_open",
                         ask_high_column="ask_high", ask_low_column="ask_low", dataset_name=name,
                         derive_timeframes=list(derive), build_features=False, notes="SYNTHETIC test research dataset")
    return import_dataset(opts, svc.cfg, svc.writer_store).dataset_id


class TestResearchSourceCheck(unittest.TestCase):
    """ADR-83: downloaded days are compared bar by bar with the research dataset; a different source pauses accounts."""

    def setUp(self):
        from edgelab.services import Services
        from edgelab.web.demo import create_demo_workspace
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        create_demo_workspace(self.tmp / "demo", REPO)
        self.svc = Services(root=self.tmp / "demo")
        self.addCleanup(self.svc.store.close)
        self.svc.paper.fetcher = fake_fetcher
        sid = self.svc.save_strategy(mnq_strategy("paper_src_ema"))["strategy_id"]
        self.svc.set_ui_preferences({"prop_fees": {PROFILE: FEES}})
        self.aid = self.svc.paper_start([sid], PROFILE, now=CREATED)["accounts"][0]

    def computed_at(self):
        return (self.svc.paper_account(self.aid)["state"] or {}).get("computed_at")

    def test_match_mismatch_pause_continue_and_rematch(self):
        svc = self.svc
        did = import_research_dataset(svc, self.tmp, date(2024, 1, 15), datetime(2024, 3, 2, 3, tzinfo=UTC),
                                      "NQ_DUKASCOPY_BIDASK_OHLC_TEST")
        out = svc.paper.run_once(now=datetime(2024, 3, 20, 23, tzinfo=UTC))
        chk = out["source_check"]
        self.assertEqual((chk["verdict"], chk["dataset_id"]), ("match", did))
        self.assertEqual([d["date"] for d in chk["days"]], ["2024-02-28", "2024-02-29", "2024-03-01"])
        self.assertGreater(chk["compared"], 3000)
        self.assertTrue(all(d["ask_compared"] for d in chk["days"]))
        self.assertEqual((out["paused"], out["accounts_updated"]), (False, 1))
        first = self.computed_at()
        again = svc.paper.run_once(now=datetime(2024, 3, 20, 23, tzinfo=UTC))
        self.assertEqual(again["source_check"]["checked_at"], chk["checked_at"])     # automatic: once per dataset
        # a different source: the check says so, accounts keep their last state (not stopped)
        svc.paper.fetcher = shifted_fetcher
        svc.paper.request_check()
        st0 = self.computed_at()
        out = svc.paper.run_once(now=datetime(2024, 3, 20, 23, tzinfo=UTC))
        chk = out["source_check"]
        self.assertEqual(chk["verdict"], "mismatch")
        self.assertAlmostEqual(chk["max_abs_diff"]["close"], 0.25, places=6)
        self.assertAlmostEqual(chk["max_abs_diff"]["ask_close"], 0.25, places=6)   # both sides shifted
        self.assertEqual(chk["bars_different"], chk["compared"])
        self.assertEqual((out["paused"], out["accounts_updated"]), (True, 0))
        self.assertEqual(self.computed_at(), st0)
        self.assertEqual(svc.paper_accounts()[0]["status"], "running")
        self.assertTrue(svc.paper_feed_status()["paused"])
        svc.paper.fetcher = fake_fetcher
        self.assertEqual(svc.paper.run_once(now=datetime(2024, 3, 21, 23, tzinfo=UTC))["accounts_updated"], 0)
        svc.paper_continue_anyway()                                                    # the user's choice
        self.assertFalse(svc.paper_feed_status()["paused"])
        self.assertEqual(svc.paper.run_once(now=datetime(2024, 3, 21, 23, tzinfo=UTC))["accounts_updated"], 1)
        with self.assertRaises(ValueError):
            svc.paper_continue_anyway()                                                # nothing paused any more
        svc.paper.request_check()                                                      # same source again
        out = svc.paper.run_once(now=datetime(2024, 3, 21, 23, tzinfo=UTC))
        self.assertEqual((out["source_check"]["verdict"], out["paused"]), ("match", False))
        self.assertNotEqual(self.computed_at(), first)

    def test_no_research_dataset_never_pauses_and_a_new_dataset_is_checked(self):
        svc = self.svc
        out = svc.paper.run_once(now=datetime(2024, 3, 20, 23, tzinfo=UTC))
        self.assertEqual(out["source_check"]["verdict"], "no_dataset")
        self.assertEqual((out["paused"], out["accounts_updated"]), (False, 1))
        import_research_dataset(svc, self.tmp, date(2024, 1, 15), datetime(2024, 2, 10, 3, tzinfo=UTC), "RES_A")
        a = svc.paper.run_once(now=datetime(2024, 3, 20, 23, tzinfo=UTC))["source_check"]
        self.assertEqual((a["verdict"], a["dataset_name"]), ("match", "RES_A"))
        later = import_research_dataset(svc, self.tmp, date(2024, 1, 15), datetime(2024, 3, 2, 3, tzinfo=UTC),
                                        "RES_B")                                     # ends later: the one compared
        b = svc.paper.run_once(now=datetime(2024, 3, 20, 23, tzinfo=UTC))["source_check"]
        self.assertEqual((b["verdict"], b["dataset_id"]), ("match", later))
        self.assertNotEqual(a["checked_at"], b["checked_at"])

    def test_check_days_are_never_stored_in_the_feed(self):
        svc = self.svc
        did = import_research_dataset(svc, self.tmp, date(2024, 1, 15), datetime(2024, 2, 10, 3, tzinfo=UTC), "RES")
        empty = self.tmp / "fresh_root"
        rec = feed.source_check(svc.cfg, svc.load_dataset(did), fetcher=fake_fetcher)
        self.assertEqual(rec["verdict"], "match")
        self.assertFalse((feed.feed_dir(empty) / "days").exists())
        self.assertEqual(feed.status(svc.data_root)["n_days"], 0)                    # nothing downloaded into the feed

        def offline(side, a, b):
            raise ConnectionError("offline")
        self.assertEqual(feed.source_check(svc.cfg, svc.load_dataset(did), fetcher=offline)["verdict"], "error")


if __name__ == "__main__":
    unittest.main()
