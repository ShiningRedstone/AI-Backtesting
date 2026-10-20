"""The rules of the Premarket breakout strategy (ADR-115).

Per trading date:

1. **Premarket range:** the highest high and lowest low of the 1-minute bars from ``pm.start`` up to ``pm.end`` (New York).
   Days with fewer than ``pm.min_minutes`` bars there, or a range outside the size limits, are skipped.
2. **Breakout:** the first candle of ``entry.tf`` (5 minutes by default; buckets anchored to the 18:00 session open, so
   on the New York clock) starting in [``entry.from``, ``entry.until``) whose CLOSE is beyond the high (long) or the low
   (short), by ``entry.buffer_points``. The signal is at the bar where that candle is known complete (its last minute,
   or the first bar of the next bucket when its last minute is missing); the engine enters at the next minute's open.
3. **Stop** at the breakout candle's wick (low for a long, high for a short) plus ``stop.buffer_points``; **target** at
   ``target.rr`` x the stop distance from the planned entry (the entry side's close: ASK for buys, BID for sells).
4. ``day.max_trades`` per day (1 = the first breakout only); open trades closed at ``target.force_exit``.

CAUSALITY: the premarket range uses bars before ``pm.end`` <= the first possible signal; a candle is used only from the
bar at which it is known complete (``frames.build_tf`` completion index). The strategy keeps its own copy of each trade
(engine fill / stop-first / time-exit rules) only to know when it is flat; the signal is always emitted, the engine is
authoritative.
"""
from __future__ import annotations

from collections import Counter

import numpy as np
import pandas as pd

from edgelab.engine.signals import SignalSet
from edgelab.mystrategy.frames import LABEL_TF, build_base, build_tf
from edgelab.pmbreakout.params import minutes

RULES_VERSION = 1


def _iso(ns: int) -> str:
    return pd.Timestamp(int(ns), tz="UTC").isoformat()


class Rules:
    def __init__(self, bars, calendar, s: dict, skip: set | None = None, trade_from_td: int | None = None):
        self.s, self.cal = s, calendar
        self.skip = set(skip or ())
        self.trade_from_td = trade_from_td
        b = self.b = build_base(bars, calendar, s["models.price_series"])
        self.n = b.n
        bid = (bars.open, bars.high, bars.low, bars.close)
        ask = b.ask if b.ask is not None else bid
        self.ent = {1: ask, -1: bid}           # long buys on ASK, short sells on BID
        self.ex = {1: bid, -1: ask}
        self.tf = build_tf(b, LABEL_TF[s["entry.tf"]])
        self.stats: Counter = Counter()
        self.explain: dict[int, dict] = {}
        self.days: list[dict] = []
        so = lambda hhmm: (minutes(hhmm) - calendar.open_min) % 1440      # noqa: E731
        self.so_pm0, self.so_pm1 = so(s["pm.start"]), so(s["pm.end"])
        self.so_from, self.so_until, self.so_force = so(s["entry.from"]), so(s["entry.until"]), so(s["target.force_exit"])

    # ------------------------------------------------------------------ the strategy's own copy of a trade
    def _sim(self, d: int, i: int, stop: float, target: float, force: np.ndarray) -> int:
        """Exit bar of the trade signalled at bar i (market fill at the next open, stop first, target, time exit at the
        open after a flagged bar). Only used to know when the strategy is flat again."""
        b, n = self.b, self.n
        f = i + 1
        if f >= n or b.td[f] != b.td[i]:
            return i
        xh, xl = self.ex[d][1], self.ex[d][2]
        for k in range(f, n):
            if k > f and force[k - 1]:
                return k
            if (d > 0 and xl[k] <= stop) or (d < 0 and xh[k] >= stop):
                return k
            if (d > 0 and xh[k] >= target) or (d < 0 and xl[k] <= target):
                return k
            if b.td[k] != b.td[f]:
                return k
        return n - 1

    # ------------------------------------------------------------------ main
    def run(self) -> tuple[SignalSet, dict, Counter]:
        s, b, n = self.s, self.b, self.n
        sig = SignalSet.empty(n)
        if n == 0:
            return sig, self.explain, self.stats
        so = b.since_open
        force = (so + 1 >= self.so_force) & (so < b.session_len)
        t = self.tf
        bounds = np.flatnonzero(np.r_[True, b.td[1:] != b.td[:-1]])
        ends = np.r_[bounds[1:] - 1, n - 1]
        allowed = s["models.direction"]
        busy = -1
        for ds, de in zip(bounds, ends):
            ds, de = int(ds), int(de)
            td = int(b.td[ds])
            if self.trade_from_td is not None and td < self.trade_from_td:
                continue
            wd = int((td + 3) % 7)
            if (wd == 0 and s["day.skip_monday"]) or (wd == 4 and s["day.skip_friday"]):
                self.stats["days_skipped_weekday"] += 1
                continue
            self.stats["days"] += 1
            seg = so[ds:de + 1]
            pm = np.flatnonzero((seg >= self.so_pm0) & (seg < self.so_pm1)) + ds
            day = {"date": str(np.datetime64(td, "D")), "trades": 0}
            if len(pm) < int(s["pm.min_minutes"]):
                self.stats["days_premarket_too_few_bars"] += 1
                day["skipped"] = "premarket data missing"
                self.days.append(day)
                continue
            hi, lo = float(b.h[pm].max()), float(b.l[pm].min())
            rng = hi - lo
            day.update(premarket_high=hi, premarket_low=lo, range=round(rng, 4))
            if (s["pm.min_range_points"] and rng < s["pm.min_range_points"]) or \
                    (s["pm.max_range_points"] and rng > s["pm.max_range_points"]):
                self.stats["days_range_outside_limits"] += 1
                day["skipped"] = "premarket range outside the limits"
                self.days.append(day)
                continue
            self.stats["days_with_range"] += 1
            pm_info = {"high": hi, "low": lo, "range": rng, "from": _iso(b.ts[pm[0]]), "to": _iso(b.ts[pm[-1]]),
                       "bars": int(len(pm)), "high_ts": _iso(b.ts[pm[int(np.argmax(b.h[pm]))]]),
                       "low_ts": _iso(b.ts[pm[int(np.argmin(b.l[pm]))]])}
            # candles of this day starting inside the entry window, in time order
            k0 = int(t.of_bar[ds])
            k1 = int(t.of_bar[de])
            used = {1: False, -1: False}
            trades = 0
            buf = float(s["entry.buffer_points"])
            for k in range(k0, k1 + 1):
                st = int(t.start[k])
                cs = int(so[st]) - (int(so[st]) % t.tf if t.tf < 1440 else 0)
                if cs < self.so_from:
                    continue
                if cs >= self.so_until:
                    break
                i = int(t.comp[k])
                if i >= n or b.td[min(i, n - 1)] != td:          # not known complete inside this trading day
                    continue
                if trades >= int(s["day.max_trades"]):
                    break
                c = float(t.c[k])
                d = 1 if c > hi + buf else -1 if c < lo - buf else 0
                if d == 0:
                    continue
                if s["entry.first_break_only"] and used[d]:
                    continue
                used[d] = True
                self.stats["breakouts"] += 1
                if (allowed == "long_only" and d < 0) or (allowed == "short_only" and d > 0):
                    self.stats["skipped_direction_not_allowed"] += 1
                    continue
                if i <= busy or force[i]:
                    self.stats["skipped_in_position_or_closing"] += 1
                    continue
                if i in self.skip:                                # the trader declined it (holdout review)
                    self.stats["declined"] += 1
                    continue
                pe = float(self.ent[d][3][i])
                wick = float(t.l[k]) if d > 0 else float(t.h[k])
                stop = wick - d * float(s["stop.buffer_points"])
                risk = d * (pe - stop)
                if not risk > 0:
                    self.stats["skipped_stop_beyond_entry"] += 1
                    continue
                if (s["stop.min_points"] and risk < s["stop.min_points"]) or \
                        (s["stop.max_points"] and risk > s["stop.max_points"]):
                    self.stats["skipped_stop_size"] += 1
                    continue
                rr = float(s["target.rr"])
                target = pe + d * rr * risk
                sig.direction[i] = d
                sig.stop_price[i], sig.target_price[i] = stop, target
                trades += 1
                day["trades"] += 1
                self.stats["signals_long" if d > 0 else "signals_short"] += 1
                self.explain[i] = {
                    "model": "breakout", "direction": d, "signal_ts": _iso(b.ts[i]),
                    "premarket": pm_info, "level": hi if d > 0 else lo,
                    "candle": {"tf": s["entry.tf"], "t0": _iso(t.t0[k]), "o": float(t.o[k]), "h": float(t.h[k]),
                               "l": float(t.l[k]), "c": c},
                    "confirmation": {"tf": s["entry.tf"]},             # the candle's timeframe (trade list, charts)
                    "planned_entry": pe, "stop": {"price": stop, "points": round(risk, 4), "wick": wick},
                    "target": {"price": target, "r_planned": rr, "points": round(rr * risk, 4)},
                    "trade_of_day": trades,
                    "checklist": {"premarket range marked": True, f"{s['entry.tf']} close beyond the "
                                  + ("high" if d > 0 else "low"): True, "stop at the candle's wick": True}}
                busy = max(i, self._sim(d, i, stop, target, force))
            self.days.append(day)
        sig.exit_long, sig.exit_short = force.copy(), force.copy()
        sig.max_trades_per_day = int(s["day.max_trades"])
        return sig, self.explain, self.stats
