"""The mechanical rules of the Fair price strategy (ADR-114).

Per trading date, every enabled session (Asia, London, New York open, New York afternoon) is an entry window of
``session.window_minutes`` from its opening minute. Inside it:

* **Fair price** = the session's opening price (the afternoon: the 9:30 opening price by default). On a scheduled
  high-impact 8:30 USD release (Market simulator news calendar) the New York session starts at the release and its fair
  price is the price just before it. Optional (``fair.adapt``): a narrow pre-open range or, after the losing-streak stop,
  the last minutes' narrow range moves the fair price to its middle.
* **Continuation** (once per session): the opening candle's colour, entered on one of the first ``cont.max_minutes``
  candles that closes beyond the structure before the open, if it agrees with the higher-timeframe bias (the reversal of
  the move over ``cont.bias_hours``). Static stop / target, doubled with a big opening candle.
* **Reversions** towards the fair price on a 1-minute candle close: a displacement (body bigger than the previous body,
  close beyond the previous candle's wick, the previous candle of the other colour) or a break of structure (a close
  beyond the most recent unbroken swing). Evaluation rules: static target / stop. Funded rules: take profit = the largest
  step that fits the room to fair price, sized so a win pays ``funded.win_usd``.
* A trade needs at least ``entry.min_distance_share`` of its take profit as room to the fair price. A session stops
  after ``day.max_losses_in_row`` losses in a row.

CAUSALITY: a decision at bar i reads bars <= i only (swings are used once confirmed by later candles that are <= i-1;
news events are scheduled in advance and their surprise is known from the release minute, decisions come after it).
The strategy keeps its own copy of each trade (same fill, stop-first, time-exit rules as the engine) only to know when
it is flat and to count losses; the engine's result is authoritative.
"""
from __future__ import annotations

import math
from collections import Counter

import numpy as np
import pandas as pd

from edgelab.engine.signals import SignalSet
from edgelab.fairprice.params import SESSIONS, minutes
from edgelab.mystrategy.frames import build_base

RULES_VERSION = 1
NS_MIN = 60_000_000_000
PHASES = ("eval", "funded")


def _iso(ns: int) -> str:
    return pd.Timestamp(int(ns), tz="UTC").isoformat()


class News:
    """High-impact USD releases of the news calendar: ``events`` = sorted UTC ns of the release minutes with the
    largest |surprise z| known at each (None when no surprise is known); ``first``/``last`` = the calendar's span."""

    def __init__(self, events: list[dict], first: int | None, last: int | None, content_hash: str):
        by: dict[int, dict] = {}
        for e in events:
            x = by.setdefault(int(e["ts"]), {"names": [], "z": None})
            x["names"].append(str(e.get("name") or ""))
            z = e.get("surprise_z")
            if z is not None and (x["z"] is None or abs(z) > x["z"]):
                x["z"] = abs(float(z))
        self.by_ts = by
        self.first, self.last = first, last
        self.content_hash = content_hash

    def covers(self, ns: int) -> bool:
        return self.first is not None and self.last is not None and self.first <= ns <= self.last


class Rules:
    def __init__(self, bars, calendar, s: dict, phase: str, news: News | None = None, skip: set | None = None,
                 trade_from_td: int | None = None, point_value: float | None = None):
        if phase not in PHASES:
            raise ValueError(f"phase must be one of {PHASES}")
        if s["models.flip"] and not (point_value and point_value > 0):
            raise ValueError("flipped trades keep the original contracts, which needs the contract's point value")
        self.pv = point_value
        self.s, self.phase, self.cal, self.news = s, phase, calendar, news
        self.skip = set(skip or ())
        self.trade_from_td = trade_from_td
        b = self.b = build_base(bars, calendar, s["models.price_series"])
        self.n = b.n
        bid = (bars.open, bars.high, bars.low, bars.close)
        ask = b.ask if b.ask is not None else bid
        # entry side (long buys on ASK, short sells on BID), exit side (long sells on BID, short buys on ASK)
        self.ent = {1: ask, -1: bid}
        self.ex = {1: bid, -1: ask}
        self.stats: Counter = Counter()
        self.explain: dict[int, dict] = {}
        self.days: list[dict] = []
        k = int(s["entry.swing_strength"])
        self.k = k
        self.sw_hi = self._swings(b.h, k, high=True)
        self.sw_lo = self._swings(b.l, k, high=False)

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _swings(x: np.ndarray, k: int, high: bool) -> np.ndarray:
        """last[m] = the latest swing index j <= m (a wick beyond the k candles on both sides), -1 if none. A swing at
        j is only CONFIRMED at bar j + k, so callers look it up at m = decision - 1 - k."""
        n = len(x)
        flag = np.zeros(n, dtype=bool)
        if n > 2 * k:
            core = x[k:n - k]
            ok = np.ones(n - 2 * k, dtype=bool)
            for t in range(1, k + 1):
                left, right = x[k - t:n - k - t], x[k + t:n - k + t]
                ok &= (core > left) & (core > right) if high else (core < left) & (core < right)
            flag[k:n - k] = ok
        idx = np.where(flag, np.arange(n), -1)
        return np.maximum.accumulate(idx) if n else idx

    def _last_swing(self, d: int, i: int, lookback: int) -> int:
        """Most recent swing high (d > 0) / low (d < 0) confirmed by bar i - 1, inside the look-back; -1 if none."""
        m = i - 1 - self.k
        if m < 0:
            return -1
        j = int((self.sw_hi if d > 0 else self.sw_lo)[m])
        if j < 0 or self.b.ts[i] - self.b.ts[j] > lookback * NS_MIN:
            return -1
        return j

    def _bar_at(self, lo: int, hi: int, mod: int) -> int:
        """Index in [lo, hi] of the bar whose New York minute is ``mod``; -1 if that minute has no bar."""
        seg = self.b.mod[lo:hi + 1]
        hit = np.flatnonzero(seg == mod)
        return lo + int(hit[0]) if len(hit) else -1

    def _price_before(self, ns: int) -> float | None:
        """Close of the last bar that opened at or before ``ns``."""
        j = int(np.searchsorted(self.b.ts, ns, side="right")) - 1
        return float(self.b.c[j]) if j >= 0 else None

    def _consolidation(self, end_bar: int) -> dict | None:
        """The range of the ``fair.cons_minutes`` minutes ending with bar ``end_bar`` (inclusive), if narrow enough."""
        s, b = self.s, self.b
        t0 = b.ts[end_bar] - (int(s["fair.cons_minutes"]) - 1) * NS_MIN
        a = int(np.searchsorted(b.ts, t0, side="left"))
        if a > end_bar:
            return None
        hi, lo = float(b.h[a:end_bar + 1].max()), float(b.l[a:end_bar + 1].min())
        if hi - lo > float(s["fair.cons_max_points"]):
            return None
        return {"price": (hi + lo) / 2, "high": hi, "low": lo, "from": _iso(b.ts[a]), "to": _iso(b.ts[end_bar])}

    # ------------------------------------------------------------------ the strategy's own copy of a trade
    def _sim(self, d: int, i: int, stop: float, target: float, exit_bar: int, force: np.ndarray) -> tuple[int, str]:
        """(exit bar, 'win'/'loss'/'none'): market fill at the next open, stop first, target, time exit at the open
        after a flagged bar. Mirrors the engine's rules; used only to know when the strategy is flat again."""
        b, n = self.b, self.n
        f = i + 1
        if f >= n or b.td[f] != b.td[i]:
            return i, "none"
        eo = self.ent[d][0]
        xh, xl, xo = self.ex[d][1], self.ex[d][2], self.ex[d][0]
        fill = float(eo[f])
        if not d * (fill - stop) > 0 or not d * (target - fill) > 0:
            return i, "none"
        for k in range(f, n):
            if k > f and (k - 1 == exit_bar or force[k - 1]):
                return k, "win" if d * (float(xo[k]) - fill) > 0 else "loss"
            if (d > 0 and xl[k] <= stop) or (d < 0 and xh[k] >= stop):
                return k, "loss"
            if (d > 0 and xh[k] >= target) or (d < 0 and xl[k] <= target):
                return k, "win"
            if b.td[k] != b.td[f]:
                return k, "none"
        return n - 1, "none"

    # ------------------------------------------------------------------ main
    def run(self) -> tuple[SignalSet, dict, Counter]:
        s, b, n = self.s, self.b, self.n
        sig = SignalSet.empty(n)
        risk = np.full(n, np.nan)
        if n == 0:
            sig.risk_usd = risk
            return sig, self.explain, self.stats
        force_so = (minutes(s["session.force_exit"]) - self.cal.open_min) % 1440
        force = (b.since_open + 1 >= force_so) & (b.since_open < b.session_len)
        exits = force.copy()
        bounds = np.flatnonzero(np.r_[True, b.td[1:] != b.td[:-1]])
        ends = np.r_[bounds[1:] - 1, n - 1]
        self._busy = -1
        self._last_exit = -10**9
        for ds, de in zip(bounds, ends):
            ds, de = int(ds), int(de)
            td = int(b.td[ds])
            if self.trade_from_td is not None and td < self.trade_from_td:
                continue
            day = {"date": str(np.datetime64(td, "D")), "sessions": []}
            wd = int((td + 3) % 7)
            if (wd == 0 and s["session.skip_monday"]) or (wd == 4 and s["session.skip_friday"]):
                self.stats["days_skipped_weekday"] += 1
                continue
            self.stats["days"] += 1
            for ses in self._sessions(ds, de):
                info = self._session(ses, sig, risk, exits, force)
                day["sessions"].append(info)
            day["trades"] = sum(x.get("trades", 0) for x in day["sessions"])
            self.days.append(day)
        sig.exit_long, sig.exit_short = exits.copy(), exits.copy()
        sig.exit_cooldown_bars = int(s["day.cooldown"])
        sig.risk_usd = risk
        return sig, self.explain, self.stats

    def _sessions(self, ds: int, de: int) -> list[dict]:
        """The day's enabled sessions in time order, each with its window, fair price and exit time."""
        s, b = self.s, self.b
        out = []
        am_bar = self._bar_at(ds, de, minutes(s["session.ny_am_open"]))
        W = int(s["session.window_minutes"])
        for key, name in SESSIONS:
            if not s[f"session.{key}"]:
                continue
            ob = self._bar_at(ds, de, minutes(s[f"session.{key}_open"]))
            self.stats["sessions_considered"] += 1
            if ob < 0:
                self.stats["sessions_no_open_bar"] += 1
                out.append({"key": key, "name": name, "skipped": "no bar at the opening minute"})
                continue
            t_open = int(b.ts[ob])
            ses = {"key": key, "name": name, "open_bar": ob, "open_ts": t_open, "first": ob,
                   "end_ns": t_open + W * NS_MIN, "cont": True}
            fair = {"price": float(b.o[ob]), "source": "session_open", "ts": _iso(t_open)}
            if key == "ny_pm" and s["fair.pm_target"] == "ny_am_open":
                if am_bar >= 0 and am_bar < ob:
                    fair = {"price": float(b.o[am_bar]), "source": "ny_am_open", "ts": _iso(b.ts[am_bar])}
                else:
                    self.stats["pm_without_am_open"] += 1
            if s["fair.adapt"] and ob > 0:
                c = self._consolidation(ob - 1)
                if c is not None:
                    fair = {"price": c["price"], "source": "pre_open_consolidation", "ts": c["to"], "range": c}
                    self.stats["fair_pre_open_consolidation"] += 1
            if key == "ny_am" and self.news is not None and s["news.enabled"]:
                nb = self._bar_at(ds, de, minutes(s["news.time"]))
                if nb >= 0 and nb < ob:
                    t_news = int(b.ts[nb])
                    if not self.news.covers(t_news):
                        self.stats["days_news_unknown"] += 1
                    elif t_news in self.news.by_ts:
                        ev = self.news.by_ts[t_news]
                        lim = float(s["news.max_surprise_z"])
                        self.stats["news_days"] += 1
                        if lim > 0 and ev["z"] is not None and ev["z"] > lim:
                            self.stats["news_days_surprise_too_big"] += 1
                            ses["news_skipped"] = {"names": ev["names"], "z": ev["z"]}
                        else:
                            fair = {"price": float(b.o[nb]), "source": "pre_news", "ts": _iso(t_news),
                                    "news": {"names": ev["names"], "z": ev["z"]}}
                            ses["first"] = nb
                            # the news window ends at news.until on this trading date (no DST change inside a
                            # trading day: the clocks change on Sunday 2:00, before the 18:00 session start)
                            day_start = int(b.ts[ds]) - int(b.since_open[ds]) * NS_MIN
                            end_so = (minutes(s["news.until"]) - self.cal.open_min) % 1440
                            ses["end_ns"] = max(ses["end_ns"], day_start + end_so * NS_MIN)
            ses["fair"] = fair
            ses["exit_ns"] = ses["end_ns"] + int(s["session.exit_after_minutes"]) * NS_MIN
            out.append(ses)
        return sorted([x for x in out if "first" in x], key=lambda x: int(b.ts[x["first"]])) + \
            [x for x in out if "first" not in x]

    def _session(self, ses: dict, sig: SignalSet, risk: np.ndarray, exits: np.ndarray, force: np.ndarray) -> dict:
        s, b, n = self.s, self.b, self.n
        info = {k: ses[k] for k in ("key", "name") if k in ses}
        if "open_bar" not in ses:
            info["skipped"] = ses.get("skipped")
            return info
        first, ob = ses["first"], ses["open_bar"]
        last = int(np.searchsorted(b.ts, ses["end_ns"], side="left")) - 1   # last bar opening inside the window
        xb = int(np.searchsorted(b.ts, ses["exit_ns"] - NS_MIN, side="left"))   # first bar ending at/after exit time
        exit_bar = xb if xb < n and b.td[xb] == b.td[ob] else -1
        if exit_bar >= 0:
            exits[exit_bar] = True
        fair = dict(ses["fair"])
        info.update(open=_iso(ses["open_ts"]), fair=fair, trades=0, signals=0)
        if ses.get("news_skipped"):
            info["news_skipped"] = ses["news_skipped"]
        phase = self.phase
        cont_on = bool(s[f"cont.{phase}"])
        disp_on, bos_on = bool(s[f"{phase}.displacement"]), bool(s[f"{phase}.bos"])
        allowed = s["models.direction"]
        streak = trades = 0
        adapted = False
        cont_done = not cont_on
        cont_dir, cont_level, cont_info = self._cont_setup(ob) if cont_on else (0, None, None)
        if cont_on and cont_dir == 0:
            cont_done = True
        max_tr = int(s["day.max_trades_per_session"])
        i = first
        while i <= last:
            if 0 <= exit_bar <= i:
                break
            if i <= self._busy or i - self._last_exit < int(s["day.cooldown"]) or force[i]:
                i += 1
                continue
            if max_tr and trades >= max_tr:
                break
            plan = None
            if not cont_done and i >= ob:
                if i >= ob + int(s["cont.max_minutes"]) or b.ts[i] - b.ts[ob] >= int(s["cont.max_minutes"]) * NS_MIN:
                    cont_done = True
                elif cont_dir * (b.c[i] - cont_level) > 0:
                    cont_done = True
                    plan = self._plan_cont(i, cont_dir, cont_info)
            if plan is None:
                plan = self._plan_reversion(i, fair, disp_on, bos_on)
            if plan is None:
                i += 1
                continue
            d = plan["direction"]
            if (allowed == "long_only" and d < 0) or (allowed == "short_only" and d > 0):
                self.stats["skipped_direction_not_allowed"] += 1
                i += 1
                continue
            if i in self.skip:                       # the trader declined this setup (holdout review): stay flat
                self.stats["declined"] += 1
                i += 1
                continue
            stop, target = plan["stop"], plan["target"]
            same = s["models.flip"] and s["models.flip_levels"] == "same_distances"
            if same:                                 # the whole trade flipped: track the flipped trade itself
                fl = self._flip_same(i, d, plan)
                xbar, outcome = self._sim(-d, i, fl["stop"], fl["target"], exit_bar, force)
            else:
                xbar, outcome = self._sim(d, i, stop, target, exit_bar, force)
            if outcome == "none":                    # counted only; whether it fills is the engine's business
                self.stats["signals_not_filled_in_own_tracking"] += 1
            if same:
                sig.direction[i] = -d
                sig.stop_price[i], sig.target_price[i] = fl["stop"], fl["target"]
                if fl["risk_usd"] is not None:
                    risk[i] = fl["risk_usd"]
                plan = {**plan, "explain": {**plan["explain"], **fl["explain"]}}
                self.stats["signals_flipped"] += 1
            elif s["models.flip"]:
                fl = self._flip(i, d, plan)
                sig.direction[i] = -d
                sig.stop_price[i], sig.target_price[i] = fl["stop"], fl["target"]
                risk[i] = fl["risk_usd"]
                plan = {**plan, "explain": {**plan["explain"], **fl["explain"]}}
                self.stats["signals_flipped"] += 1
            else:
                sig.direction[i] = d
                sig.stop_price[i], sig.target_price[i] = stop, target
                if plan["risk_usd"] is not None:
                    risk[i] = plan["risk_usd"]
            info["signals"] += 1
            trades += 1
            self.stats[f"signals_{plan['setup']}"] += 1
            self.explain[i] = {**plan["explain"], "phase": phase, "session": ses["name"], "session_key": ses["key"],
                               "session_open": _iso(ses["open_ts"]), "fair": dict(fair),
                               "streak_before": streak, "trade_in_session": trades,
                               "signal_ts": _iso(b.ts[i]), "model": plan["setup"],
                               "direction": -d if s["models.flip"] else d}
            xbar = max(i, xbar)
            self._busy, self._last_exit = xbar, xbar
            streak = streak + 1 if outcome == "loss" else 0
            if streak >= int(s["day.max_losses_in_row"]):
                c = self._consolidation(xbar) if (s["fair.adapt"] and not adapted and xbar <= last) else None
                if c is None:
                    info["stopped_by_losses"] = True
                    self.stats["sessions_stopped_losses"] += 1
                    break
                fair = {"price": c["price"], "source": "consolidation_after_losses", "ts": c["to"], "range": c}
                info["fair_moved"] = fair
                adapted, streak = True, 0
                self.stats["fair_moved_after_losses"] += 1
            i = max(i + 1, xbar)
        info["trades"] = trades
        return info

    # ------------------------------------------------------------------ continuation
    def _cont_setup(self, ob: int) -> tuple[int, float | None, dict | None]:
        """Direction (opening candle colour, if the bias agrees) and the structure level the continuation must close
        beyond. Everything here is known by the opening candle's close."""
        s, b = self.s, self.b
        d = int(np.sign(b.c[ob] - b.o[ob]))
        if d == 0:
            self.stats["cont_doji_open"] += 1
            return 0, None, None
        info = {"opening_candle": {"o": float(b.o[ob]), "h": float(b.h[ob]), "l": float(b.l[ob]), "c": float(b.c[ob]),
                                   "ts": _iso(b.ts[ob])}}
        H = int(s["cont.bias_hours"])
        if H > 0:
            ref = self._price_before(int(b.ts[ob]) - H * 3600 * 1_000_000_000)
            if ref is None:
                self.stats["cont_no_bias_history"] += 1
                return 0, None, None
            bias = 1 if b.o[ob] < ref else -1 if b.o[ob] > ref else 0
            info["bias"] = {"hours": H, "price_then": ref, "price_at_open": float(b.o[ob]), "direction": bias}
            if bias != d:
                self.stats["cont_against_bias"] += 1
                return 0, None, None
        mode = s["cont.structure"]
        if mode == "swing":
            j = self._last_swing(d, ob, int(s["entry.bos_lookback"]))
            if j < 0:
                self.stats["cont_no_structure"] += 1
                return 0, None, None
            level = float(b.h[j] if d > 0 else b.l[j])
            info["structure"] = {"kind": "swing", "price": level, "ts": _iso(b.ts[j])}
        else:
            m = 3 if mode == "prev3" else 1
            a = ob - m
            if a < 0 or b.td[a] != b.td[ob]:
                self.stats["cont_no_structure"] += 1
                return 0, None, None
            level = float(b.h[a:ob].max() if d > 0 else b.l[a:ob].min())
            info["structure"] = {"kind": mode, "price": level, "ts": _iso(b.ts[a])}
        return d, level, info

    def _plan_cont(self, i: int, d: int, info: dict) -> dict | None:
        s, b = self.s, self.b
        oc = info["opening_candle"]
        big = float(s["cont.big_candle_points"])
        mult = 2.0 if big > 0 and oc["h"] - oc["l"] > big else 1.0
        S, T = float(s["cont.stop_points"]) * mult, float(s["cont.target_points"]) * mult
        pe = float(self.ent[d][3][i])
        return self._order(i, d, "continuation", pe, S, T, {
            **info, "big_opening_candle": mult > 1, "checklist": {
                "opening candle colour": True, "agrees with the bias": True if "bias" in info else None,
                "closed beyond the structure": True}})

    # ------------------------------------------------------------------ reversions
    def _plan_reversion(self, i: int, fair: dict, disp_on: bool, bos_on: bool) -> dict | None:
        s, b = self.s, self.b
        F = fair["price"]
        d = int(np.sign(F - b.c[i]))
        if d == 0:
            return None
        pe = float(self.ent[d][3][i])
        dist = d * (F - pe)
        if dist < float(s["entry.min_gap_points"]):
            return None
        kind, extra = None, {}
        if bos_on:
            j = self._last_swing(d, i, int(s["entry.bos_lookback"]))
            if j >= 0:
                lvl = float(b.h[j] if d > 0 else b.l[j])
                between = b.c[j + 1:i]
                unbroken = not len(between) or (between.max() <= lvl if d > 0 else between.min() >= lvl)
                if unbroken and d * (b.c[i] - lvl) > 0:
                    kind, extra = "bos", {"structure": {"kind": "swing", "price": lvl, "ts": _iso(b.ts[j])}}
        if kind is None and disp_on and i > 0 and b.td[i - 1] == b.td[i]:
            p = i - 1
            body, pbody = b.c[i] - b.o[i], b.c[p] - b.o[p]
            beyond = b.c[i] > b.h[p] if d > 0 else b.c[i] < b.l[p]
            opposite = (pbody * d < 0) if s["entry.disp_opposite"] else True
            if d * body > 0 and abs(body) > abs(pbody) and beyond and opposite:
                kind, extra = "displacement", {"previous_candle": {"o": float(b.o[p]), "h": float(b.h[p]),
                                                                   "l": float(b.l[p]), "c": float(b.c[p]),
                                                                   "ts": _iso(b.ts[p])}}
        if kind is None:
            return None
        share = float(s["entry.min_distance_share"])
        if self.phase == "eval":
            T, S = float(s["eval.target_points"]), float(s["eval.stop_points"])
            if dist < share * T:
                self.stats["skipped_too_close_to_fair"] += 1
                return None
        else:
            step = float(s["funded.target_step"])
            T = math.floor(dist / share / step + 1e-9) * step
            T = min(T, float(s["funded.max_target"]))
            S = float(s["funded.stop_points"])
            if T < float(s["funded.min_target"]) - 1e-9:
                self.stats["skipped_too_close_to_fair"] += 1
                return None
        return self._order(i, d, kind, pe, S, T, {**extra, "distance_points": round(dist, 4), "share_needed": share,
                                                   "checklist": {"towards the fair price": True,
                                                                 f"{kind.replace('bos', 'break of structure')}": True,
                                                                 "enough room to the fair price": True}})

    def _flip_same(self, i: int, d: int, plan: dict) -> dict:
        """The whole trade flipped (the user's second style): direction -d from the flipped side's planned entry, the SAME
        stop and target distances (same reward : risk), sized by the normal rules (the same budget on the same stop
        distance, so the same micros)."""
        e = plan["explain"]
        S, T = float(e["stop_points"]), float(e["target_points"])
        pe_f = float(self.ent[-d][3][i])
        stop_f, target_f = pe_f + d * S, pe_f - d * T
        return {"stop": stop_f, "target": target_f, "risk_usd": plan["risk_usd"], "explain": {
            "flip": {"style": "same_distances", "setup_direction": d, "setup_stop": plan["stop"],
                     "setup_target": plan["target"], "setup_stop_points": S, "setup_target_points": T,
                     "setup_r_planned": round(T / S, 4) if S else None},
            "planned_entry": pe_f, "stop": stop_f, "target_price": target_f}}

    def _flip(self, i: int, d: int, plan: dict) -> dict:
        """The opposite trade of a setup (the user's rule): direction -d, stop at the setup's target, target at the setup's
        stop, and the SAME contracts the setup would get. The engine sizes from a per-signal budget, so the budget is
        (n + 0.5) x the flipped planned risk x point value: rounding down gives exactly n (0 stays 0). n = the setup's
        contracts under the same rule as the engine (rounded down, capped)."""
        s, pv = self.s, float(self.pv)
        stop, target = plan["stop"], plan["target"]
        S = abs(plan["explain"]["planned_entry"] - stop)
        budget = plan["risk_usd"] if plan["risk_usd"] is not None else float(s["eval.risk_usd"])
        n = min(int(math.floor(budget / (S * pv) + 1e-9)), int(s["risk.max_contracts"])) if S > 0 else 0
        pe_f = float(self.ent[-d][3][i])                     # the flipped side's planned entry (BID for a short)
        pr = abs(pe_f - target)
        risk_usd = (n + 0.5) * pr * pv if n >= 1 and pr > 0 else 0.0
        T = plan["explain"]["target_points"]
        return {"stop": target, "target": stop, "risk_usd": risk_usd, "explain": {
            "flip": {"setup_direction": d, "setup_stop": stop, "setup_target": target, "setup_stop_points": S,
                     "setup_target_points": T, "setup_r_planned": round(T / S, 4) if S else None, "contracts_kept": n},
            "planned_entry": pe_f, "stop": target, "target_price": stop, "stop_points": T, "target_points": S,
            "risk_budget_usd": round(n * pr * pv, 2), "target": {"points": S, "r_planned": round(S / T, 4) if T else None}}}

    def _order(self, i: int, d: int, setup: str, pe: float, S: float, T: float, explain: dict) -> dict:
        s = self.s
        stop, target = pe - d * S, pe + d * T
        if self.phase == "eval":
            risk_usd, budget = None, float(s["eval.risk_usd"])
        else:
            risk_usd = float(s["funded.win_usd"]) * S / T
            budget = risk_usd
        return {"direction": d, "setup": setup, "stop": stop, "target": target, "risk_usd": risk_usd,
                "explain": {**explain, "setup": setup, "planned_entry": pe, "stop_points": S, "target_points": T,
                            "stop": stop, "target_price": target, "risk_budget_usd": round(budget, 2),
                            "target": {"points": T, "r_planned": round(T / S, 4)}}}
