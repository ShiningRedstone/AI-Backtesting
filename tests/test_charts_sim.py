"""Simulated Lucid accounts (ADR-112) on SYNTHETIC bid / ask ticks (Dukascopy cannot be reached here).

Guarantees tested (known answers):
* fills: market at the next tick's ask (buy) / bid (sell); a resting limit only when the price trades THROUGH it, at the
  limit; a marketable limit at the current price; stop at the first tick through it (gap paid); stop-limit; MIT;
  trailing stop ratchets; IOC; no order is ever filled by a tick stamped before it became active;
* costs per contract and side from the app's cost settings; P&L = points x point value;
* brackets (exits priced from the entry fill, OCO), OCO pairs, flatten / reverse / cancel / modify;
* the rules: contract limit (NQ = 10 micros; evaluation 40, funded 20 at the start), live max-loss breach liquidates at
  the tick and fails the attempt, the end-of-day trailing floor moves only after 18:00 New York, the 50 % consistency
  rule holds the pass back, a pass becomes a funded account the next day, a custom starting balance moves every level;
* Day orders expire at 17:00 New York; reset starts a new attempt; the manager cursor and the API.
"""
import shutil
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd

from edgelab.charts import sim as S
from edgelab.charts.ticks import Tick, TickSource

REPO = Path(__file__).resolve().parents[1]
COSTS = {"MNQ": 0.75, "MES": 0.75, "NQ": 2.2, "ES": 2.2}


def ms(s: str) -> int:
    """New York wall time -> epoch ms."""
    return int(pd.Timestamp(s, tz="America/New_York").tz_convert("UTC").value // 1_000_000)


class Feed:
    """A scripted tick stream per code; ``since`` returns what a live poll would return."""

    def __init__(self):
        self.ticks: dict[str, list[Tick]] = {}

    def add(self, code, t, bid, ask):
        self.ticks.setdefault(code, []).append(Tick(ms(t), bid, ask))

    def fetch(self, code, start, end):
        rows = [t for t in self.ticks.get(code, []) if start.timestamp() * 1000 <= t.ts <= end.timestamp() * 1000]
        return pd.DataFrame({"bidPrice": [t.bid for t in rows], "askPrice": [t.ask for t in rows]},
                            index=pd.to_datetime([t.ts for t in rows], unit="ms", utc=True))


NQ = "E_NQ-100"


class SimCase(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        shutil.copytree(REPO / "configs", self.tmp / "configs")
        self.feed = Feed()
        self.clock = [ms("2026-10-05 09:30")]
        now = lambda: datetime.fromtimestamp(self.clock[0] / 1000, timezone.utc)   # noqa: E731
        self.m = S.SimManager(self.tmp / "data", self.tmp, COSTS, ticks=TickSource(self.feed.fetch, now), now=now,
                              autostart=False)
        self.a = self.m.create("test", 50000)["id"]
        for sym in ("MNQ", "MES"):
            self.m.touch(sym)                                             # a chart asks for quotes

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def at(self, t):
        self.clock[0] = ms(t)

    def tick(self, t, bid, ask, code=NQ, step=True):
        self.feed.add(code, t, bid, ask)
        self.at(t)
        if step:
            self.m.step()

    def att(self):
        return self.m.accounts[self.a]["attempts"][-1]

    def order(self, oid):
        return next(o for o in self.att()["orders"] if o["id"] == oid)

    def place(self, **spec):
        return self.m.place(self.a, {"symbol": "MNQ", "qty": 1, **spec})["orders"]


class TestFills(SimCase):
    def test_market_fills_at_next_ask_then_bid_with_costs(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)            # a quote exists
        oid, = self.place(side="buy", type="market")
        self.assertEqual(self.order(oid)["status"], "working")          # the tick before the order never fills it
        self.tick("2026-10-05 09:30:05", 20001.00, 20001.75)
        o = self.order(oid)
        self.assertEqual((o["status"], o["fill_price"]), ("filled", 20001.75))
        sid, = self.place(side="sell", type="market")
        self.tick("2026-10-05 09:31:00", 20011.00, 20011.50)
        self.assertEqual(self.order(sid)["fill_price"], 20011.00)
        ep = self.att()["episodes"][0]
        self.assertAlmostEqual(ep["net"], (20011.00 - 20001.75) * 2 - 2 * 0.75)   # MNQ $2 / point, 0.75 per side
        v = self.m.view(self.a)
        self.assertAlmostEqual(v["rules"]["balance"], 50000 + ep["net"])

    def test_market_refused_without_fresh_prices(self):
        with self.assertRaises(S.SimError) as e:
            self.place(side="buy", type="market")
        self.assertEqual(e.exception.code, "NO_PRICES")

    def test_limit_trades_through_marketable_and_ioc(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        oid, = self.place(side="buy", type="limit", price=19990.00)
        self.tick("2026-10-05 09:30:05", 19989.50, 19990.00)            # ask touches the limit: not through
        self.assertEqual(self.order(oid)["status"], "working")
        self.tick("2026-10-05 09:30:10", 19985.00, 19985.50)            # through: filled AT the limit
        self.assertEqual(self.order(oid)["fill_price"], 19990.00)
        mid, = self.place(side="sell", type="limit", price=19980.00)    # marketable: the current bid
        self.tick("2026-10-05 09:30:15", 19986.00, 19986.50)
        self.assertEqual(self.order(mid)["fill_price"], 19986.00)
        iid, = self.place(side="buy", type="limit", price=19900.00, tif="ioc")
        self.tick("2026-10-05 09:30:20", 19986.00, 19986.50)
        self.assertEqual(self.order(iid)["status"], "cancelled")

    def test_stop_stop_limit_mit_trailing(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        st, = self.place(side="buy", type="stop", stop=20010.00)
        self.tick("2026-10-05 09:30:05", 20005.00, 20005.50)
        self.assertEqual(self.order(st)["status"], "working")
        self.tick("2026-10-05 09:30:10", 20014.00, 20014.50)            # gaps through: fills at the tick's ask
        self.assertEqual(self.order(st)["fill_price"], 20014.50)
        sl, = self.place(side="sell", type="stop_limit", stop=20000.00, price=19999.00)
        self.tick("2026-10-05 09:30:15", 19999.75, 20000.25)            # triggered, bid 19999.75 >= limit: filled
        self.assertEqual(self.order(sl)["fill_price"], 19999.75)
        mit, = self.place(side="buy", type="mit", price=19990.00)
        self.tick("2026-10-05 09:30:20", 19992.00, 19992.50)
        self.assertEqual(self.order(mit)["status"], "working")
        self.tick("2026-10-05 09:30:25", 19989.00, 19989.50)
        self.assertEqual(self.order(mit)["fill_price"], 19989.50)
        tr, = self.place(side="sell", type="trailing_stop", trail=5.0)
        for t, b in (("09:30:30", 19990.00), ("09:30:35", 20000.00), ("09:30:40", 19996.00)):
            self.tick(f"2026-10-05 {t}", b, b + 0.5)
        self.assertEqual((self.order(tr)["status"], self.order(tr)["stop"]), ("working", 19995.00))   # ratcheted from 20000
        self.tick("2026-10-05 09:30:45", 19994.00, 19994.50)
        self.assertEqual(self.order(tr)["fill_price"], 19994.00)
        self.assertEqual(self.att()["positions"]["MNQ"]["qty"], 0)


class TestLinksAndActions(SimCase):
    def test_bracket_oco_and_flatten(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        ids = self.place(side="buy", type="market", qty=2, tp_ticks=40, sl_ticks=20)
        entry, tp, sl = ids
        self.assertEqual(self.order(tp)["status"], "pending")
        self.tick("2026-10-05 09:30:05", 20000.00, 20000.50)
        self.assertEqual((self.order(tp)["price"], self.order(sl)["stop"], self.order(tp)["qty"]), (20010.50, 19995.50, 2))
        self.tick("2026-10-05 09:31:00", 20011.00, 20011.50)            # through the take-profit
        self.assertEqual(self.order(tp)["fill_price"], 20010.50)
        self.assertEqual(self.order(sl)["status"], "cancelled")
        a, b = self.m.place(self.a, {"oco": [{"symbol": "MNQ", "side": "buy", "type": "limit", "price": 19990, "qty": 1},
                                             {"symbol": "MNQ", "side": "buy", "type": "stop", "stop": 20030, "qty": 1}]})["orders"]
        self.tick("2026-10-05 09:32:00", 20031.00, 20031.50)
        self.assertEqual((self.order(b)["status"], self.order(a)["status"]), ("filled", "cancelled"))
        self.place(side="sell", type="limit", price=20100)
        self.m.flatten(self.a)
        self.tick("2026-10-05 09:32:05", 20020.00, 20020.50)
        self.assertEqual(self.att()["positions"]["MNQ"]["qty"], 0)
        self.assertFalse([o for o in self.att()["orders"] if o["status"] == "working"])

    def test_reverse_modify_cancel(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        self.place(side="buy", type="market", qty=2)
        self.tick("2026-10-05 09:30:05", 20000.00, 20000.50)
        self.m.reverse(self.a, "MNQ")
        self.tick("2026-10-05 09:30:10", 20002.00, 20002.50)
        self.assertEqual(self.att()["positions"]["MNQ"]["qty"], -2)
        lid, = self.place(side="buy", type="limit", price=19950)
        self.m.modify(self.a, lid, {"price": 19990.13})
        self.assertEqual(self.order(lid)["price"], 19990.25)             # Tradovate tick rounding
        self.assertEqual(self.m.cancel(self.a, lid), 1)
        with self.assertRaises(S.SimError):
            self.m.cancel(self.a, lid)


class TestRules(SimCase):
    def test_contract_limits(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        self.m.place(self.a, {"symbol": "NQ", "side": "buy", "qty": 4, "type": "market"})       # 40 micros: allowed
        with self.assertRaises(S.SimError) as e:
            self.m.place(self.a, {"symbol": "MNQ", "side": "buy", "qty": 41, "type": "market"})
        self.assertEqual(e.exception.code, "CONTRACT_LIMIT")
        self.tick("2026-10-05 09:30:05", 20000.00, 20000.50)
        with self.assertRaises(S.SimError):
            self.place(side="buy", type="market")                         # 41 micros with the NQ held
        self.m.place(self.a, {"symbol": "NQ", "side": "sell", "qty": 4, "type": "market"})        # reducing is always allowed

    def test_live_breach_liquidates_and_fails(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        self.m.place(self.a, {"symbol": "NQ", "side": "buy", "qty": 2, "type": "market"})       # $40 / point
        self.tick("2026-10-05 09:30:05", 20000.00, 20000.50)
        self.place(side="sell", type="limit", price=20500)
        self.tick("2026-10-05 09:40:00", 19960.00, 19960.50)            # -1,610 + costs: not yet
        self.assertEqual(self.att()["state"], "active")
        self.tick("2026-10-05 09:45:00", 19949.00, 19949.50)            # equity <= 48,000: breach
        att = self.att()
        self.assertEqual(att["state"], "failed")
        self.assertEqual(att["positions"]["NQ"]["qty"], 0)
        self.assertEqual(att["fills"][-1]["price"], 19949.00)             # liquidated at the tick's bid
        self.assertFalse([o for o in att["orders"] if o["status"] == "working"])
        with self.assertRaises(S.SimError) as e:
            self.place(side="buy", type="limit", price=19000)
        self.assertEqual(e.exception.code, "ACCOUNT_CLOSED")
        self.m.reset(self.a)
        self.assertEqual((self.att()["n"], self.att()["state"]), (2, "active"))
        self.assertEqual(len(self.m.view(self.a)["history"]), 2)

    def _day_trade(self, day, pnl_points, qty=1, symbol="NQ"):
        """Buy at 10:00 and sell at 10:05 New York on ``day`` for ``pnl_points`` (ask = bid + 0.25)."""
        self.tick(f"{day} 09:59:00", 20000.00, 20000.25)
        self.m.place(self.a, {"symbol": symbol, "side": "buy", "qty": qty, "type": "market"})
        self.tick(f"{day} 10:00:00", 20000.00, 20000.25)
        self.m.place(self.a, {"symbol": symbol, "side": "sell", "qty": qty, "type": "market"})
        self.tick(f"{day} 10:05:00", 20000.25 + pnl_points, 20000.50 + pnl_points)

    def test_eod_floor_consistency_pass_and_funded(self):
        self._day_trade("2026-10-05", 50)                                 # +1,000 - 4.40
        v = self.m.view(self.a)["rules"]
        self.assertEqual(v["floor"], 48000.0)                             # the floor moves only at the day's end
        self.at("2026-10-06 09:00")
        self.m.step()
        self.assertAlmostEqual(self.m.view(self.a)["rules"]["floor"], 48995.6, places=4)
        self._day_trade("2026-10-06", 110)                                # +2,200: target reached, best day > 50 %
        self.at("2026-10-07 09:00")
        self.m.step()
        v = self.m.view(self.a)["rules"]
        self.assertEqual(v["stage"], "evaluation")                        # consistency holds the pass back
        self.assertFalse(v["consistency_ok"])
        self._day_trade("2026-10-07", 70)                                 # +1,395.6: best day 2,195.6 <= 50 % of 4,586.8
        self.at("2026-10-08 09:00")
        self.m.step()
        v = self.m.view(self.a)
        self.assertEqual(v["rules"]["stage"], "funded")
        self.assertEqual(v["rules"]["balance"], 50000.0)                  # a separate funded account
        self.assertEqual(v["rules"]["micros_allowed"], 20.0)              # funded scaling starts at 20 micros
        with self.assertRaises(S.SimError):
            self.m.place(self.a, {"symbol": "NQ", "side": "buy", "qty": 3, "type": "limit", "price": 19000})

    def test_custom_start_balance_and_day_expiry(self):
        b = self.m.create("big", 100000)["id"]
        v = self.m.view(b)["rules"]
        self.assertEqual((v["floor"], v["target"], v["start"]), (98000.0, 3000.0, 100000.0))
        self.assertIn("starting", " ".join(self.m.view(b)["assumed_rules"]) + "starting")
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        oid, = self.place(side="buy", type="limit", price=19000)
        gid, = self.place(side="buy", type="limit", price=19000, tif="gtc")
        self.at("2026-10-05 17:00:01")
        self.m.step()
        self.assertEqual((self.order(oid)["status"], self.order(gid)["status"]), ("expired", "working"))


class TestCursorAndApi(SimCase):
    def test_backfill_from_placement_and_persistence(self):
        self.tick("2026-10-05 09:30:00", 20000.00, 20000.50)
        lid, = self.place(side="buy", type="limit", price=19990)
        for t, b in (("09:31", 19995.0), ("09:32", 19985.0)):            # the app was "closed": ticks arrive later
            self.tick(f"2026-10-05 {t}", b, b + 0.5, step=False)
        self.at("2026-10-05 09:40")
        self.m.step()
        self.assertEqual(self.order(lid)["fill_price"], 19990.0)
        m2 = S.SimManager(self.tmp / "data", self.tmp, COSTS, autostart=False)
        self.assertEqual(m2.accounts[self.a]["attempts"][-1]["orders"][0]["status"], "filled")

    def test_api(self):
        from edgelab.web.app import create_app
        c = create_app(self.tmp).test_client()
        r = c.post("/api/sim/accounts", json={"name": "A", "start_balance": 50000})
        self.assertEqual(r.status_code, 201)
        aid = r.get_json()["id"]
        self.assertEqual(c.get("/api/sim/accounts").status_code, 200)
        r = c.post(f"/api/sim/accounts/{aid}/order", json={"symbol": "MNQ", "side": "buy", "qty": 1, "type": "limit", "price": 100})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(len(r.get_json()["account"]["working"]), 1)
        self.assertEqual(c.post(f"/api/sim/accounts/{aid}/order", json={"symbol": "MNQ", "side": "buy", "type": "market"}).status_code, 409)
        self.assertEqual(c.post(f"/api/sim/accounts/{aid}/order", json={"symbol": "XX", "side": "buy"}).status_code, 422)
        self.assertEqual(c.post(f"/api/sim/accounts/{aid}/explode", json={}).status_code, 400)
        self.assertEqual(c.get("/api/sim/accounts/SIM0000000000").status_code, 404)
        self.assertEqual(c.post(f"/api/sim/accounts/{aid}/cancel", json={}).get_json()["cancelled"], 1)


if __name__ == "__main__":
    unittest.main()
