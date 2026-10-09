"""Simulated Lucid accounts on the live chart (ADR-112). NOTHING here is ever sent to a broker.

An account starts at a balance the user sets (default 50,000 USD) and follows the app's registered LucidFlex 50K rule
profile (``configs/prop/profiles``; with another starting balance the two starting-balance rules are marked CUSTOM, every
other rule - target, trailing max loss, lock, consistency, contract limits, scaling, payouts - is unchanged and counted
from that start). Orders are Tradovate's types: market, limit, stop (stop-market), stop-limit, market-if-touched,
trailing stop, trailing stop-limit; time in force Day / GTC / IOC / FOK; brackets (entry + take-profit and / or stop-loss,
the exits OCO, sized and priced from the entry fill) and OCO pairs; modify, cancel, cancel all, flatten, reverse.

Fills (``on_tick``) use real Dukascopy bid / ask ticks of the index CFDs the chart draws (ticks.py): buys at the ASK,
sells at the BID; an order only ever reacts to ticks stamped after it became active. Market: the next tick. Limit: at
once at the current price when marketable, else only when the price trades THROUGH the limit (filled at the limit).
Stop / trailing stop: the first tick through the stop, at that tick's price (the gap is paid). Stop-limit /
trailing-stop-limit: become a limit when the stop is touched. Market-if-touched: the first tick at or through the
price, at that tick's price. Full quantity, no partial fills (a stated simplification). Costs per contract and side from
the app's cost settings (configs/costs.yaml). Positions net per symbol (Tradovate); P&L = points x point value.

Rules: the authoritative state is the UNCHANGED prop lifecycle (``prop.lifecycle.simulate_lifecycle``) run on the
account's completed trades: a "trade" is one flat-to-flat episode of the whole account (all symbols; size = the most
micros held, NQ / ES = 10 micros; worst equity inside = the lowest marked-to-market equity seen on any tick). Only
trading days that have ENDED (18:00 New York) are given to it, so the end-of-day trailing floor, the pass check, the
funded start, the scaling tier and the payouts happen exactly at the day's end. Live, on every tick, the equity marked at
the executable price (longs at the bid, shorts at the ask) is compared with the current floor: touching it is a breach,
as at Lucid / Tradovate the positions are liquidated at the tick's prices, orders cancelled and the attempt failed. An
order that would take the account above its contract limit is rejected. "Reset" starts a new attempt (history kept).
"""
from __future__ import annotations

import json
import threading
import time as _time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

import pandas as pd

from edgelab.charts.feed import SYMBOLS
from edgelab.charts.ticks import Tick, TickSource

PROFILE_ID = "LUCID_LUCIDFLEX_50K"
MICROS = {"MNQ": 1, "MES": 1, "NQ": 10, "ES": 10}
ORDER_TYPES = ("market", "limit", "stop", "stop_limit", "mit", "trailing_stop", "trailing_stop_limit")
TIFS = ("day", "gtc", "ioc", "fok")
WORKING = ("working", "pending")
QUOTE_MAX_AGE_MS = 120_000           # a market order needs a tick from the last 2 minutes
BACKFILL_MAX = timedelta(days=3)     # how far back orders are checked after the app was closed
NY = "America/New_York"
LABEL = ("SIMULATED: no order is sent anywhere. Fills on Dukascopy index-CFD bid / ask ticks (they follow NQ / ES but are "
         "not CME prices); rules = the app's LucidFlex 50K profile (some rules are EdgeLab default assumptions).")


class SimError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _iso(ms: int | None) -> str | None:
    return None if ms is None else datetime.fromtimestamp(ms / 1000, timezone.utc).isoformat()


def day_start_ms(ms: int, reset: str = "18:00", tz: str = NY) -> int:
    """Start of the trading day containing ``ms``: the last ``reset`` (New York) at or before it."""
    t = pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(tz)
    hh, mm = (int(x) for x in reset.split(":"))
    d = t.tz_localize(None).normalize()
    b = (d + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz)
    if b > t:
        b = (d - pd.Timedelta(days=1) + pd.Timedelta(hours=hh, minutes=mm)).tz_localize(tz)
    return int(b.tz_convert("UTC").value // 1_000_000)


def day_expiry_ms(ms: int) -> int:
    """When a Day order expires: the next 17:00 New York (the CME close) after it was placed."""
    t = pd.Timestamp(ms, unit="ms", tz="UTC").tz_convert(NY)
    e = pd.Timestamp(t.tz_localize(None).normalize() + pd.Timedelta(hours=17)).tz_localize(NY)
    if e <= t:
        e = pd.Timestamp(t.tz_localize(None).normalize() + pd.Timedelta(days=1, hours=17)).tz_localize(NY)
    return int(e.tz_convert("UTC").value // 1_000_000)


def round_tick(x: float, tick: float = 0.25) -> float:
    return round(round(float(x) / tick) * tick, 4)


# ============================================================================ rules
def account_profile(base_doc: dict, start_balance: float) -> dict:
    """The registered LucidFlex profile, with the two starting balances set to the user's (CUSTOM) when they differ."""
    from edgelab.prop.profiles import customize
    s = float(start_balance)
    if abs(float(base_doc["rules"]["evaluation.starting_balance"]["value"]) - s) < 1e-9 and \
            abs(float(base_doc["rules"]["funded.starting_balance"]["value"]) - s) < 1e-9:
        return base_doc
    return customize(base_doc, {"evaluation.starting_balance": s, "funded.starting_balance": s},
                     "starting balance chosen by the user for a simulated account", version=int(base_doc["version"]))


def episodes_frame(eps: list[dict]) -> pd.DataFrame:
    cols = ["entry_ts", "exit_ts", "contracts", "net_usd", "trade_no", "mae_points", "risk_points", "risk_usd"]
    if not eps:
        return pd.DataFrame({c: pd.Series(dtype="float64") for c in cols}).assign(
            entry_ts=pd.Series(dtype="datetime64[ns, UTC]"), exit_ts=pd.Series(dtype="datetime64[ns, UTC]"))
    return pd.DataFrame({
        "entry_ts": pd.to_datetime([e["entry_ms"] for e in eps], unit="ms", utc=True),
        "exit_ts": pd.to_datetime([e["exit_ms"] for e in eps], unit="ms", utc=True),
        "contracts": [float(e["micros"]) for e in eps], "net_usd": [float(e["net"]) for e in eps],
        "trade_no": list(range(1, len(eps) + 1)),
        "mae_points": [float(e["mae"]) for e in eps], "risk_points": 1.0, "risk_usd": 1.0})   # MAE given in USD


def base_state(profile: dict, eps: list[dict], today_ms: int) -> dict:
    """The lifecycle on the trades of the trading days that have ended (before ``today_ms``), read for live use."""
    from edgelab.prop.lifecycle import simulate_lifecycle
    from edgelab.prop.profiles import resolve
    R = resolve(profile)
    done = [e for e in eps if e["exit_ms"] <= today_ms]
    life = simulate_lifecycle(episodes_frame(done), profile)
    ev, fu = life["evaluation"], life["funded"]
    out = {"n_done": len(done), "today_ms": today_ms, "life": life, "R": R}
    if ev["status"] == "PASS":
        out.update(stage="funded", balance=fu["balance"], floor=fu["floor"], peak=fu["highest_eod_balance"],
                   start=fu["starting_balance"], locked=fu.get("floor_locked", False),
                   micros=float(fu["next_session_permitted_micros"]))
        if fu["status"] in ("FAIL", "INCOMPATIBLE"):
            out.update(state="failed", why=(fu["breach"] or {}).get("detail") or fu["status"])
        elif fu["outcome"] in ("LIVE_TRANSITION_ELIGIBLE", "PAYOUT_COUNT_LIMIT_REACHED"):
            out.update(state="complete", why={"LIVE_TRANSITION_ELIGIBLE": "Live transition eligible (after the last payout)",
                                              "PAYOUT_COUNT_LIMIT_REACHED": "Payout limit reached"}[fu["outcome"]])
        else:
            out.update(state="active", why=None)
    else:
        d = R["evaluation"]
        trig = d["drawdown"]["lock_trigger_offset"]
        out.update(stage="evaluation", balance=ev["balance"], floor=ev["floor"], peak=ev["highest_eod_balance"],
                   start=float(d["starting_balance"]), micros=float(d["max_micros"]),
                   locked=trig is not None and ev["highest_eod_balance"] >= float(d["starting_balance"]) + float(trig) - 1e-9)
        if ev["status"] == "FAIL" and ev["failure_reason"] == "NOT_PASSED_BY_END_OF_DATA":
            out.update(state="active", why=None)
        else:
            out.update(state="failed", why=(ev["breach"] or {}).get("detail") or ev["failure_reason"])
    return out


# ============================================================================ the order engine
class Engine:
    """Applies ticks, fills and the live rule checks to one attempt (a JSON-able dict). Transient caches start with '_'."""

    def __init__(self, profile: dict, costs: dict[str, float]):
        self.profile, self.costs = profile, costs           # costs: symbol -> USD per contract per side

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def micros(att: dict) -> float:
        return sum(abs(p["qty"]) * MICROS[s] for s, p in att["positions"].items())

    def base(self, att: dict, ms: int) -> dict:
        today = day_start_ms(ms)
        b = att.get("_base")
        if b is None or b["today_ms"] != today or b["n_done"] != sum(1 for e in att["episodes"] if e["exit_ms"] <= today):
            b = att["_base"] = base_state(self.profile, att["episodes"], today)
            if b["state"] != "active" and att["state"] == "active":
                self._end(att, ms, b["state"], b["why"])
        return b

    def unrealized(self, att: dict, last: dict[str, Tick]) -> float:
        u = 0.0
        for s, p in att["positions"].items():
            q = last.get(SYMBOLS[s]["code"])
            if p["qty"] and q is not None:
                px = q.bid if p["qty"] > 0 else q.ask
                u += (px - p["avg"]) * p["qty"] * SYMBOLS[s]["point_value"]
        return u

    def live(self, att: dict, ms: int, last: dict[str, Tick]) -> dict:
        """Balance / equity / floor right now (base + today's closed trades + the open trade)."""
        b = self.base(att, ms)
        today_net = sum(e["net"] for e in att["episodes"] if e["exit_ms"] > b["today_ms"])
        open_net = att["open"]["net"] if att["open"] else 0.0
        bal = b["balance"] + today_net + open_net
        u = self.unrealized(att, last)
        return {"stage": b["stage"], "balance": bal, "equity": bal + u, "unrealized": u, "floor": b["floor"],
                "start": b["start"], "micros_allowed": b["micros"], "today_pnl": today_net + open_net + u, "base": b}

    def _end(self, att: dict, ms: int, state: str, why: str | None) -> None:
        att["state"], att["ended_ms"], att["end_reason"] = state, ms, why
        for o in att["orders"]:
            if o["status"] in WORKING:
                o.update(status="cancelled", reason="account " + ("failed" if state == "failed" else "finished"), done_ms=ms)

    # ---------------------------------------------------------------- fills
    def _fill(self, att: dict, o: dict, price: float, ms: int, last: dict[str, Tick], why: str = "") -> bool:
        s, qty = o["symbol"], int(o["qty"])
        signed = qty if o["side"] == "buy" else -qty
        pos = att["positions"].setdefault(s, {"qty": 0, "avg": 0.0})
        old = pos["qty"]
        new = old + signed
        if abs(new) > abs(old) or (old and new and (old > 0) != (new > 0)):      # adds risk: check the contract limit
            lv = self.live(att, ms, last)
            after = self.micros(att) - abs(old) * MICROS[s] + abs(new) * MICROS[s]
            if after > lv["micros_allowed"] + 1e-9:
                o.update(status="rejected", done_ms=ms, reason=f"contract limit: {after:g} micros would exceed the "
                                                               f"{lv['micros_allowed']:g} allowed ({lv['stage']})")
                return False
        pv = SYMBOLS[s]["point_value"]
        realized = 0.0
        if old and (old > 0) != (signed > 0):                                    # reduces / closes / flips
            closed = min(abs(old), abs(signed))
            realized = (price - pos["avg"]) * closed * (1 if old > 0 else -1) * pv
        if new == 0:
            pos.update(qty=0, avg=0.0)
        elif old == 0 or (old > 0) != (new > 0):
            pos.update(qty=new, avg=price)
        elif abs(new) > abs(old):
            pos.update(qty=new, avg=(pos["avg"] * abs(old) + price * abs(signed)) / abs(new))
        else:
            pos["qty"] = new
        fee = self.costs.get(s, 0.0) * qty
        was_flat = att["open"] is None
        if was_flat:
            att["open"] = {"entry_ms": ms, "net": 0.0, "worst": 0.0, "micros": 0.0, "fills": 0}
        ep = att["open"]
        ep["net"] += realized - fee
        ep["fills"] += 1
        ep["micros"] = max(ep["micros"], self.micros(att))
        att["fills"].append({"id": f"F{len(att['fills']) + 1}", "order": o["id"], "symbol": s, "side": o["side"], "qty": qty,
                             "price": round(price, 4), "ms": ms, "realized": round(realized, 2), "fee": round(fee, 2),
                             "position": pos["qty"], "why": why})
        o.update(status="filled", fill_price=round(price, 4), done_ms=ms)
        self._mark(att, last)
        if all(p["qty"] == 0 for p in att["positions"].values()):
            att["episodes"].append({"entry_ms": ep["entry_ms"], "exit_ms": ms, "net": round(ep["net"], 6),
                                    "mae": round(max(0.0, -ep["worst"]), 6), "micros": ep["micros"], "fills": ep["fills"]})
            att["open"] = None
        # links: OCO siblings, bracket children, exits of a flat symbol
        if o.get("oco"):
            for x in att["orders"]:
                if x is not o and x.get("oco") == o["oco"] and x["status"] in WORKING:
                    x.update(status="cancelled", reason="OCO: the other order filled", done_ms=ms)
        for x in att["orders"]:
            if x.get("parent") == o["id"] and x["status"] == "pending":
                x.update(status="working", qty=qty, active_ms=ms)
                tick = SYMBOLS[s]["tick"]
                d = 1 if o["side"] == "buy" else -1
                if x["role"] == "tp" and x.get("ticks") is not None:
                    x["price"] = round_tick(price + d * x["ticks"] * tick, tick)
                if x["role"] == "sl" and x.get("ticks") is not None:
                    if x["type"] == "trailing_stop":
                        x["trail"] = x["ticks"] * tick
                    else:
                        x["stop"] = round_tick(price - d * x["ticks"] * tick, tick)
                q = last.get(SYMBOLS[s]["code"])
                if x["type"] == "limit" and q is not None:                      # rests unless marketable right now
                    x["rested"] = not ((q.ask <= x["price"]) if x["side"] == "buy" else (q.bid >= x["price"]))
        if pos["qty"] == 0:
            for x in att["orders"]:
                if x["symbol"] == s and x.get("role") in ("tp", "sl") and x["status"] == "working" and x is not o:
                    x.update(status="cancelled", reason="position closed", done_ms=ms)
        return True

    def _mark(self, att: dict, last: dict[str, Tick]) -> None:
        if att["open"] is not None:
            rel = att["open"]["net"] + self.unrealized(att, last)
            att["open"]["worst"] = min(att["open"]["worst"], rel)

    # ---------------------------------------------------------------- ticks
    def on_tick(self, att: dict, code: str, t: Tick, last: dict[str, Tick]) -> None:
        if att["state"] != "active":
            return
        last[code] = t
        for o in att["orders"]:
            if o["status"] == "working" and o["tif"] == "day" and t.ts >= o["expires_ms"]:
                o.update(status="expired", reason="Day order: the session closed", done_ms=t.ts)
        for o in list(att["orders"]):
            if o["status"] != "working" or SYMBOLS[o["symbol"]]["code"] != code or o["active_ms"] >= t.ts:
                continue
            self._order_tick(att, o, t, last)
            if att["state"] != "active":
                return
        self._mark(att, last)
        lv = self.live(att, t.ts, last)
        if att["state"] == "active" and lv["equity"] <= lv["floor"] + 1e-9:          # open or closed: touching the floor fails
            self._liquidate(att, t.ts, last, f"equity {lv['equity']:,.2f} reached the max-loss floor {lv['floor']:,.2f} "
                                             f"({lv['stage']}); positions closed at the tick's bid / ask")

    def _order_tick(self, att: dict, o: dict, t: Tick, last: dict[str, Tick]) -> None:
        buy = o["side"] == "buy"
        px = t.ask if buy else t.bid
        typ = o["type"]
        if typ == "market":
            self._fill(att, o, px, t.ts, last)
            return
        if typ in ("trailing_stop", "trailing_stop_limit") and not o.get("triggered"):
            ref = o.get("ref")
            ref = px if ref is None else (min(ref, px) if buy else max(ref, px))
            o["ref"] = ref
            o["stop"] = round(ref + o["trail"] if buy else ref - o["trail"], 4)
        if typ in ("stop", "stop_limit", "trailing_stop", "trailing_stop_limit") and not o.get("triggered"):
            hit = px >= o["stop"] if buy else px <= o["stop"]
            if not hit:
                return
            if typ in ("stop", "trailing_stop"):
                self._fill(att, o, px, t.ts, last, "stop triggered")
                return
            o["triggered"], o["rested"] = True, False
            if typ == "trailing_stop_limit":
                o["price"] = round(o["stop"] + o["offset"] if buy else o["stop"] - o["offset"], 4)
        if typ == "mit":
            if (px <= o["price"]) if buy else (px >= o["price"]):
                self._fill(att, o, px, t.ts, last, "touched")
            return
        # limit behaviour (limit, triggered stop-limit / trailing-stop-limit). "rested" = it was not marketable when it
        # became active (set at placement against the current quote): then only a trade THROUGH the limit fills it.
        L, first = o["price"], not o.get("rested")
        if first and ((px <= L) if buy else (px >= L)):
            self._fill(att, o, px, t.ts, last, "marketable limit")
        elif (px < L) if buy else (px > L):
            self._fill(att, o, L, t.ts, last, "price traded through the limit")
        elif o["tif"] in ("ioc", "fok"):
            o.update(status="cancelled", reason=f"{o['tif'].upper()}: not fillable at once", done_ms=t.ts)
        else:
            o["rested"] = True

    def _liquidate(self, att: dict, ms: int, last: dict[str, Tick], why: str) -> None:
        for s, p in list(att["positions"].items()):
            if p["qty"]:
                q = last[SYMBOLS[s]["code"]]
                o = {"id": f"L{len(att['orders']) + 1}", "symbol": s, "side": "sell" if p["qty"] > 0 else "buy",
                     "qty": abs(p["qty"]), "type": "market", "tif": "day", "status": "working", "placed_ms": ms,
                     "active_ms": ms, "role": "liquidation", "expires_ms": ms + 1}
                att["orders"].append(o)
                self._fill(att, o, q.bid if p["qty"] > 0 else q.ask, ms, last, "liquidated: max loss")
        att["breach"] = {"ms": ms, "detail": why}
        self._end(att, ms, "failed", "Max loss limit: " + why)

    def housekeep(self, att: dict, ms: int) -> None:
        """Wall-clock duties while no tick arrives: Day orders expire at the close; the trading day can end."""
        if att["state"] != "active":
            return
        for o in att["orders"]:
            if o["status"] in WORKING and o["tif"] == "day" and ms >= o["expires_ms"]:
                o.update(status="expired", reason="Day order: the session closed", done_ms=ms)
        self.base(att, ms)


# ============================================================================ accounts + orders (validation)
def new_attempt(n: int, start_balance: float, ms: int) -> dict:
    return {"n": n, "start_balance": float(start_balance), "started_ms": ms, "state": "active", "ended_ms": None,
            "end_reason": None, "orders": [], "fills": [], "episodes": [], "open": None, "positions": {}, "seen": {},
            "breach": None, "notes": [], "seq": 0}


def _num(x, name: str, positive: bool = True) -> float:
    try:
        v = float(x)
    except (TypeError, ValueError):
        raise SimError("BAD_ORDER", f"{name} must be a number.") from None
    if v != v or v in (float("inf"), float("-inf")) or (positive and v <= 0):
        raise SimError("BAD_ORDER", f"{name} must be a positive number.")
    return v


def make_order(att: dict, spec: dict, ms: int, role: str = "entry") -> dict:
    """A validated order record from a request (prices rounded to the 0.25 tick as Tradovate requires)."""
    s = spec.get("symbol")
    if s not in SYMBOLS:
        raise SimError("BAD_ORDER", "Symbol must be MNQ, NQ, ES or MES.")
    side = spec.get("side")
    if side not in ("buy", "sell"):
        raise SimError("BAD_ORDER", "Side must be buy or sell.")
    typ = spec.get("type", "market")
    if typ not in ORDER_TYPES:
        raise SimError("BAD_ORDER", f"Unknown order type {typ!r}.")
    tif = spec.get("tif", "day")
    if tif not in TIFS:
        raise SimError("BAD_ORDER", f"Unknown time in force {tif!r}.")
    if tif in ("ioc", "fok") and typ not in ("market", "limit"):
        raise SimError("BAD_ORDER", "IOC / FOK apply to market and limit orders only.")
    qty = spec.get("qty", 1)
    if not isinstance(qty, int) or isinstance(qty, bool) or not 1 <= qty <= 400:
        raise SimError("BAD_ORDER", "Quantity must be a whole number of contracts (1 ... 400).")
    tick = SYMBOLS[s]["tick"]
    att["seq"] += 1
    o = {"id": f"O{att['seq']}", "symbol": s, "side": side, "qty": qty, "type": typ, "tif": tif, "status": "working",
         "placed_ms": ms, "active_ms": ms, "expires_ms": day_expiry_ms(ms), "role": role, "price": None, "stop": None,
         "trail": None, "offset": None, "parent": None, "oco": None}
    if typ in ("limit", "stop_limit", "mit"):
        o["price"] = round_tick(_num(spec.get("price"), "Price"), tick)
    if typ in ("stop", "stop_limit"):
        o["stop"] = round_tick(_num(spec.get("stop"), "Stop price"), tick)
    if typ in ("trailing_stop", "trailing_stop_limit"):
        o["trail"] = round_tick(_num(spec.get("trail"), "Trail distance (points)"), tick)
    if typ == "trailing_stop_limit":
        o["offset"] = round_tick(_num(spec.get("offset", 0), "Limit offset", positive=False), tick)
        if o["offset"] < 0:
            raise SimError("BAD_ORDER", "Limit offset cannot be negative.")
    return o


class SimManager:
    """Accounts on disk (<data>/charts/sim/<id>.json), one engine thread checking ticks while anything is working."""

    def __init__(self, data_root, root, costs: dict[str, float], ticks: TickSource | None = None,
                 now: Callable[[], datetime] | None = None, autostart: bool = True):
        self.dir = Path(data_root) / "charts" / "sim"
        self.root, self.costs = Path(root), costs
        self.ticks = ticks or TickSource()
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.RLock()
        self.accounts: dict[str, dict] = {}
        self.engines: dict[str, Engine] = {}
        self.last: dict[str, Tick] = {}
        self.watch: dict[str, float] = {}                 # codes a chart asked quotes for -> last ask (wall time)
        self.problem: dict[str, str | None] = {}
        self.autostart = autostart
        self.thread: threading.Thread | None = None
        self._load()

    # ---------------------------------------------------------------- storage
    def _ms(self) -> int:
        return int(self.now().timestamp() * 1000)

    def _load(self) -> None:
        if not self.dir.is_dir():
            return
        for p in sorted(self.dir.glob("SIM*.json")):
            try:
                a = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            self.accounts[a["id"]] = a

    def _save(self, a: dict) -> None:
        from edgelab.core.fsutil import atomic_write_text
        self.dir.mkdir(parents=True, exist_ok=True)
        clean = {**a, "attempts": [{k: v for k, v in t.items() if not k.startswith("_")} for t in a["attempts"]]}
        atomic_write_text(self.dir / f"{a['id']}.json", json.dumps(clean, separators=(",", ":")))

    def _profile_doc(self) -> dict:
        from edgelab.paper.engine import profile_version
        from edgelab.prop.profiles import profiles_dir, read_registry
        vers = [int(e["version"]) for e in read_registry(profiles_dir(self.root))["profiles"] if e["profile_id"] == PROFILE_ID]
        if not vers:
            raise SimError("NO_PROFILE", "The LucidFlex 50K rule profile is not registered in this workspace.")
        return profile_version(self.root, PROFILE_ID, max(vers))

    def engine(self, a: dict) -> Engine:
        att = a["attempts"][-1]
        key = f"{a['id']}#{att['n']}"
        e = self.engines.get(key)
        if e is None:
            from edgelab.paper.engine import profile_version
            doc = profile_version(self.root, a["profile"]["id"], a["profile"]["version"])
            e = self.engines[key] = Engine(account_profile(doc, att["start_balance"]), self.costs)
        return e

    def get(self, aid: str) -> dict:
        a = self.accounts.get(aid)
        if a is None:
            raise SimError("NO_ACCOUNT", f"No simulated account {aid!r}.")
        return a

    # ---------------------------------------------------------------- accounts
    def create(self, name: str, start_balance: float) -> dict:
        name = (name or "").strip()[:60] or "Lucid 50K sim"
        bal = _num(start_balance, "Starting balance")
        if not 1_000 <= bal <= 10_000_000:
            raise SimError("BAD_ACCOUNT", "Starting balance must be between 1,000 and 10,000,000 USD.")
        doc = self._profile_doc()
        with self.lock:
            ms = self._ms()
            a = {"id": "SIM" + uuid.uuid4().hex[:10].upper(), "name": name, "created_ms": ms,
                 "profile": {"id": doc["profile_id"], "version": int(doc["version"])}, "attempts": [new_attempt(1, bal, ms)]}
            self.accounts[a["id"]] = a
            self._save(a)
            return a

    def reset(self, aid: str, start_balance: float | None = None) -> dict:
        with self.lock:
            a = self.get(aid)
            att = a["attempts"][-1]
            ms = self._ms()
            if att["state"] == "active":
                self.engine(a)._end(att, ms, "reset", "Reset by the user")
            bal = att["start_balance"] if start_balance is None else _num(start_balance, "Starting balance")
            a["attempts"].append(new_attempt(att["n"] + 1, bal, ms))
            self._save(a)
            return a

    def rename(self, aid: str, name: str) -> dict:
        with self.lock:
            a = self.get(aid)
            a["name"] = (name or "").strip()[:60] or a["name"]
            self._save(a)
            return a

    def delete(self, aid: str) -> None:
        with self.lock:
            self.get(aid)
            self.accounts.pop(aid)
            (self.dir / f"{aid}.json").unlink(missing_ok=True)

    # ---------------------------------------------------------------- orders
    def _active(self, a: dict) -> dict:
        att = a["attempts"][-1]
        if att["state"] != "active":
            raise SimError("ACCOUNT_CLOSED", f"This attempt is {att['state']} ({att['end_reason']}). Reset the account to trade again.")
        return att

    def _fresh_quote(self, symbol: str, ms: int) -> Tick:
        q = self.last.get(SYMBOLS[symbol]["code"])
        if q is None or ms - q.ts > QUOTE_MAX_AGE_MS:
            raise SimError("NO_PRICES", "No live prices for the last 2 minutes (market closed or the feed is down): "
                                        "market orders are refused. Limit / stop orders can still be placed.")
        return q

    def place(self, aid: str, spec: dict) -> dict:
        """One order, a bracket (``tp_ticks`` / ``sl_ticks`` / ``sl_trailing``) or an OCO pair (``oco: [a, b]``)."""
        with self.lock:
            a = self.get(aid)
            att = self._active(a)
            ms = self._ms()
            if spec.get("oco"):
                pair = spec["oco"]
                if not isinstance(pair, list) or len(pair) != 2:
                    raise SimError("BAD_ORDER", "An OCO needs exactly two orders.")
                orders = [make_order(att, x, ms) for x in pair]
                if orders[0]["symbol"] != orders[1]["symbol"]:
                    raise SimError("BAD_ORDER", "Both OCO orders must be on the same symbol.")
                for o in orders:
                    if o["type"] == "market":
                        raise SimError("BAD_ORDER", "An OCO pair cannot contain a market order.")
                    o["oco"] = f"G{orders[0]['id']}"
                    self._rest(o)
                att["orders"].extend(orders)
                self._save(a)
                self._kick()
                return {"orders": [o["id"] for o in orders]}
            o = make_order(att, spec, ms)
            if o["type"] == "market":
                self._fresh_quote(o["symbol"], ms)
            self._rest(o)
            self._check_limit(a, att, o, ms)
            att["orders"].append(o)
            ids = [o["id"]]
            tp, sl = spec.get("tp_ticks"), spec.get("sl_ticks")
            if tp is not None or sl is not None:
                grp = f"B{o['id']}"
                exit_side = "sell" if o["side"] == "buy" else "buy"
                for role, val in (("tp", tp), ("sl", sl)):
                    if val is None:
                        continue
                    n = _num(val, "Bracket distance (ticks)")
                    if n != int(n) or n > 100_000:
                        raise SimError("BAD_ORDER", "Bracket distances are whole numbers of ticks.")
                    att["seq"] += 1
                    typ = "limit" if role == "tp" else ("trailing_stop" if spec.get("sl_trailing") else "stop")
                    att["orders"].append({"id": f"O{att['seq']}", "symbol": o["symbol"], "side": exit_side, "qty": o["qty"],
                                          "type": typ, "tif": "gtc" if o["tif"] == "gtc" else "day", "status": "pending",
                                          "placed_ms": ms, "active_ms": ms, "expires_ms": o["expires_ms"], "role": role,
                                          "price": None, "stop": None, "trail": None, "offset": None, "parent": o["id"],
                                          "oco": grp, "ticks": int(n)})
                    ids.append(f"O{att['seq']}")
            self._save(a)
            self._kick()
            return {"orders": ids}

    def _rest(self, o: dict) -> None:
        """A limit that is not marketable against the current quote rests: only a trade through it fills it."""
        q = self.last.get(SYMBOLS[o["symbol"]]["code"])
        if o["type"] == "limit" and q is not None:
            o["rested"] = not ((q.ask <= o["price"]) if o["side"] == "buy" else (q.bid >= o["price"]))

    def _check_limit(self, a: dict, att: dict, o: dict, ms: int) -> None:
        eng = self.engine(a)
        lv = eng.live(att, ms, self.last)
        pos = att["positions"].get(o["symbol"], {"qty": 0})["qty"]
        new = pos + (o["qty"] if o["side"] == "buy" else -o["qty"])
        if abs(new) <= abs(pos) and (not pos or not new or (pos > 0) == (new > 0)):
            return
        after = eng.micros(att) - abs(pos) * MICROS[o["symbol"]] + abs(new) * MICROS[o["symbol"]]
        if after > lv["micros_allowed"] + 1e-9:
            att["seq"] -= 1
            raise SimError("CONTRACT_LIMIT", f"Rejected: {after:g} micros would exceed the {lv['micros_allowed']:g} micros "
                                             f"allowed now ({lv['stage']}; NQ / ES count as 10 micros).")

    def modify(self, aid: str, oid: str, change: dict) -> dict:
        with self.lock:
            a = self.get(aid)
            att = self._active(a)
            o = next((x for x in att["orders"] if x["id"] == oid), None)
            if o is None or o["status"] not in WORKING:
                raise SimError("NOT_WORKING", "That order is no longer working.")
            tick = SYMBOLS[o["symbol"]]["tick"]
            for k in ("price", "stop", "trail", "offset"):
                if k in change and change[k] is not None:
                    if o.get(k) is None and not (k == "price" and o.get("role") == "tp") and not (k == "stop" and o.get("role") == "sl"):
                        raise SimError("BAD_ORDER", f"This order has no {k}.")
                    o[k] = round_tick(_num(change[k], k, positive=k != "offset"), tick)
                    if k in ("price", "stop", "trail"):
                        o.pop("ticks", None)                                     # a moved bracket exit keeps its own price
            if "qty" in change:
                q = change["qty"]
                if not isinstance(q, int) or isinstance(q, bool) or not 1 <= q <= 400:
                    raise SimError("BAD_ORDER", "Quantity must be a whole number of contracts (1 ... 400).")
                o["qty"] = q
            if o["type"] in ("trailing_stop", "trailing_stop_limit") and "trail" in change:
                o["ref"] = None
            o["active_ms"] = max(o["active_ms"], self._ms())                      # a modified order reacts to newer ticks only
            if o["type"] == "limit":
                self._rest(o)
            self._save(a)
            return o

    def cancel(self, aid: str, oid: str | None = None) -> int:
        with self.lock:
            a = self.get(aid)
            att = a["attempts"][-1]
            ms, n = self._ms(), 0
            for o in att["orders"]:
                if o["status"] in WORKING and (oid is None or o["id"] == oid or o.get("parent") == oid):
                    o.update(status="cancelled", reason="cancelled by the user", done_ms=ms)
                    n += 1
            if oid is not None and n == 0:
                raise SimError("NOT_WORKING", "That order is no longer working.")
            self._save(a)
            return n

    def _flatten(self, a: dict, symbol: str | None, ms: int, why: str) -> list[str]:
        att = a["attempts"][-1]
        ids = []
        for o in att["orders"]:
            if o["status"] in WORKING and (symbol is None or o["symbol"] == symbol):
                o.update(status="cancelled", reason=why, done_ms=ms)
        for s, p in att["positions"].items():
            if p["qty"] and (symbol is None or s == symbol):
                o = make_order(att, {"symbol": s, "side": "sell" if p["qty"] > 0 else "buy", "qty": abs(p["qty"]),
                                     "type": "market"}, ms, role="flatten")
                att["orders"].append(o)
                ids.append(o["id"])
        return ids

    def flatten(self, aid: str, symbol: str | None = None) -> dict:
        """Exit at market and cancel the working orders (Tradovate's Exit / Flatten)."""
        with self.lock:
            a = self.get(aid)
            self._active(a)
            ms = self._ms()
            if any(p["qty"] for s, p in a["attempts"][-1]["positions"].items() if symbol is None or s == symbol):
                for s, p in a["attempts"][-1]["positions"].items():
                    if p["qty"] and (symbol is None or s == symbol):
                        self._fresh_quote(s, ms)
            ids = self._flatten(a, symbol, ms, "flatten")
            self._save(a)
            self._kick()
            return {"orders": ids}

    def reverse(self, aid: str, symbol: str) -> dict:
        with self.lock:
            a = self.get(aid)
            att = self._active(a)
            p = att["positions"].get(symbol, {"qty": 0})["qty"]
            if not p:
                raise SimError("NO_POSITION", f"No {symbol} position to reverse.")
            return self.place(aid, {"symbol": symbol, "side": "sell" if p > 0 else "buy", "qty": 2 * abs(p), "type": "market"})

    # ---------------------------------------------------------------- the engine thread
    def _kick(self) -> None:
        if not self.autostart:
            return
        if self.thread is None or not self.thread.is_alive():
            self.thread = threading.Thread(target=self._run, daemon=True, name="charts-sim")
            self.thread.start()

    def touch(self, symbol: str) -> None:
        self.watch[SYMBOLS[symbol]["code"]] = _time.time()
        self._kick()

    def _interest(self) -> dict[str, list[dict]]:
        """code -> active attempts with working orders or positions on it."""
        out: dict[str, list[dict]] = {}
        for a in self.accounts.values():
            att = a["attempts"][-1]
            if att["state"] != "active":
                continue
            codes = {SYMBOLS[o["symbol"]]["code"] for o in att["orders"] if o["status"] == "working"}
            codes |= {SYMBOLS[s]["code"] for s, p in att["positions"].items() if p["qty"]}
            for c in codes:
                out.setdefault(c, []).append(a)
        return out

    def step(self) -> None:
        """One cycle: fetch new ticks per code, feed them in time order to every interested attempt, housekeeping, save."""
        with self.lock:
            interest = self._interest()
            now_ms = self._ms()
            codes = set(interest) | {c for c, t in self.watch.items() if c and _time.time() - t < 90}
        for code in sorted(codes):
            atts = interest.get(code, [])
            with self.lock:
                starts = []
                for a in atts:
                    att = a["attempts"][-1]
                    seen = att["seen"].get(code)
                    if seen is None:
                        seen = min([o["active_ms"] for o in att["orders"] if o["status"] == "working"
                                    and SYMBOLS[o["symbol"]]["code"] == code] or [now_ms])
                    starts.append(seen)
                last = self.last.get(code)
                since = min(starts + [last.ts if last else now_ms - 10_000])
                floor = now_ms - int(BACKFILL_MAX.total_seconds() * 1000)
                if since < floor:
                    for a in atts:
                        att = a["attempts"][-1]
                        att["notes"].append(f"Prices from {_iso(since)} to {_iso(floor)} were not checked (the app was "
                                            f"closed for more than {BACKFILL_MAX.days} days): working orders were not monitored then.")
                        att["seen"][code] = floor
                    since = floor
            try:
                ticks = self.ticks.since(code, since)
                self.problem[code] = None
            except Exception as e:                                         # noqa: BLE001 - shown, retried next cycle
                self.problem[code] = f"{type(e).__name__}: {e}"
                continue
            with self.lock:
                for t in ticks:
                    for a in atts:
                        att = a["attempts"][-1]
                        if t.ts > att["seen"].get(code, since):
                            self.engine(a).on_tick(att, code, t, self.last)
                            att["seen"][code] = t.ts
                    if not self.last.get(code) or t.ts >= self.last[code].ts:
                        self.last[code] = t
        busy = {id(x) for xs in interest.values() for x in xs}
        with self.lock:
            ms = self._ms()
            for a in self.accounts.values():
                att = a["attempts"][-1]
                before = (att["state"], len(att["fills"]), sum(1 for o in att["orders"] if o["status"] in WORKING))
                self.engine(a).housekeep(att, ms)
                after = (att["state"], len(att["fills"]), sum(1 for o in att["orders"] if o["status"] in WORKING))
                if before != after or id(a) in busy:
                    self._save(a)

    def _run(self) -> None:
        idle_since = None
        while True:
            busy = bool(self._interest()) or any(_time.time() - t < 90 for t in self.watch.values())
            if not busy:
                idle_since = idle_since or _time.time()
                if _time.time() - idle_since > 30:
                    self.thread = None
                    return
            else:
                idle_since = None
            try:
                self.step()
            except Exception:                                              # noqa: BLE001 - the loop never dies silently
                import logging
                logging.getLogger("edgelab.charts.sim").exception("sim step failed")
            _time.sleep(2.0)

    # ---------------------------------------------------------------- views
    def quote(self, symbol: str) -> dict:
        self.touch(symbol)
        q = self.last.get(SYMBOLS[symbol]["code"])
        code = SYMBOLS[symbol]["code"]
        return {"symbol": symbol, "bid": q.bid if q else None, "ask": q.ask if q else None, "ms": q.ts if q else None,
                "error": self.problem.get(code)}

    def view(self, aid: str, full: bool = True) -> dict:
        with self.lock:
            a = self.get(aid)
            att = a["attempts"][-1]
            eng = self.engine(a)
            ms = self._ms()
            lv = eng.live(att, ms, self.last)
            b = lv["base"]
            life, R = b["life"], b["R"]
            st = R[b["stage"]]
            ev = life["evaluation"]
            days = [d for d in (ev["days"] if b["stage"] == "evaluation" else life["funded"].get("days", [])) if d["n_trades"]]
            today_pnl = lv["today_pnl"]
            day_pnls = [d["day_pnl"] for d in days] + ([today_pnl] if att["open"] or any(e["exit_ms"] > b["today_ms"] for e in att["episodes"]) else [])
            profit = lv["balance"] - lv["start"]
            rules = {"stage": b["stage"], "start": lv["start"], "balance": round(lv["balance"], 2), "equity": round(lv["equity"], 2),
                     "unrealized": round(lv["unrealized"], 2), "floor": round(lv["floor"], 2),
                     "room": round(lv["equity"] - lv["floor"], 2), "max_loss": float(st["drawdown"]["max_loss"]),
                     "floor_locked": b["locked"], "highest_eod": round(b["peak"], 2),
                     "lock_at": None if st["drawdown"]["lock_trigger_offset"] is None else lv["start"] + float(st["drawdown"]["lock_trigger_offset"]),
                     "micros_allowed": lv["micros_allowed"], "micros_now": eng.micros(att), "today_pnl": round(today_pnl, 2),
                     "trading_days": len(day_pnls), "profit": round(profit, 2)}
            if b["stage"] == "evaluation":
                tgt = float(R["evaluation"]["profit_target"])
                best = max(day_pnls, default=0.0)
                pct = float(R["evaluation"]["consistency"]["percent"])
                rules.update(target=tgt, target_left=round(max(0.0, tgt - profit), 2), best_day=round(best, 2),
                             consistency_percent=pct, consistency_now=round(best / profit * 100, 1) if profit > 0 else None,
                             consistency_ok=profit <= 0 or best <= profit * pct / 100 + 1e-9)
            else:
                po = R["payout"]
                pays = life["payouts"]
                last_pay = pays[-1]["date"] if pays else None
                fdays = [d for d in life["funded"].get("days", []) if d["n_trades"] and (last_pay is None or d["date"] > last_pay)]
                win = sum(1 for d in fdays if d["day_pnl"] >= float(po["winning_day_threshold"]) - 1e-9)
                rules.update(payouts=[{k: x[k] for k in ("n", "date", "gross", "trader_share", "balance_after")} for x in pays],
                             winning_days=win, winning_days_required=int(po["winning_days_required"]),
                             winning_day_threshold=float(po["winning_day_threshold"]), payout_minimum=float(po["minimum"]),
                             payouts_left=None if po["count_limit"] is None else int(po["count_limit"]) - len(pays),
                             trader_paid=life["totals"]["trader_payout"], pass_date=(ev["pass"] or {}).get("date"))
            out = {"id": a["id"], "name": a["name"], "created_ms": a["created_ms"], "profile": a["profile"],
                   "attempt": att["n"], "attempts": len(a["attempts"]), "state": att["state"], "end_reason": att["end_reason"],
                   "start_balance": att["start_balance"], "rules": rules, "breach": att["breach"], "label": LABEL,
                   "rule_basis": life["rule_basis"]["label"], "assumed_rules": life["rule_basis"]["assumed_rules"],
                   "positions": [{"symbol": s, "qty": p["qty"], "avg": round(p["avg"], 4),
                                  "pnl": round(self._pos_pnl(s, p), 2)} for s, p in att["positions"].items() if p["qty"]],
                   "working": [self._ov(o) for o in att["orders"] if o["status"] in WORKING]}
            if full:
                out.update(orders=[self._ov(o) for o in att["orders"]][-300:], fills=att["fills"][-300:],
                           trades=[{**e, "n": i + 1} for i, e in enumerate(att["episodes"])][-300:],
                           days=[{k: d.get(k) for k in ("date", "stage", "day_pnl", "balance_end", "floor", "n_trades")}
                                 for d in ev["days"] + life["funded"].get("days", [])],
                           notes=att["notes"][-20:], history=[{"n": t["n"], "start_balance": t["start_balance"], "state": t["state"],
                                                                "end_reason": t["end_reason"], "started_ms": t["started_ms"],
                                                                "ended_ms": t["ended_ms"], "trades": len(t["episodes"]),
                                                                "net": round(sum(e["net"] for e in t["episodes"]), 2)}
                                                               for t in a["attempts"]],
                           headline=life.get("headline"))
            return out

    def _pos_pnl(self, s: str, p: dict) -> float:
        q = self.last.get(SYMBOLS[s]["code"])
        if q is None:
            return 0.0
        return ((q.bid if p["qty"] > 0 else q.ask) - p["avg"]) * p["qty"] * SYMBOLS[s]["point_value"]

    @staticmethod
    def _ov(o: dict) -> dict:
        return {k: o.get(k) for k in ("id", "symbol", "side", "qty", "type", "tif", "status", "price", "stop", "trail", "offset",
                                      "role", "parent", "oco", "placed_ms", "done_ms", "fill_price", "reason", "triggered", "ticks")}

    def list(self) -> list[dict]:
        out = []
        for aid in sorted(self.accounts, key=lambda k: -self.accounts[k]["created_ms"]):
            try:
                out.append(self.view(aid, full=False))
            except Exception as e:                                         # noqa: BLE001 - one broken file never hides the rest
                out.append({"id": aid, "name": self.accounts[aid].get("name"), "error": str(e)})
        return out
