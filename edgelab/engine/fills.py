"""Order fill & exit kernel. Pure functions over NumPy arrays (numba-ready).

FILL RULES (all disclosed in every result's ``assumptions``):

Entry (signal decided at close of bar i):
  market  fills at open[i+1].
  stop    long: if open >= L fill at open (gap), elif high >= L fill at L.
  limit   long: if open <= L fill at open (price improvement), elif
          low <= L - penetration fill at L. (Shorts mirrored.)
  Pending stop/limit orders live ``entry_expiry_bars`` bars, and are cancelled
  on a bar where entries are blocked (session flatten) or a new session starts.

Exit, on each bar after the fill bar:
  1. Gap: open beyond stop -> exit at OPEN (worse than stop; stops are not
     guaranteed). Open beyond target -> exit at target (default, conservative)
     or at open (``target_gap_fill: open``).
  2. Touch: bar reaches stop/target -> exit at that level.
     Both touched on one bar = CONFLICT, resolved by policy:
       conservative -> stop first; optimistic -> target first;
       intrabar -> replay lower-timeframe bars; if those are missing,
       inconsistent with the bar, or themselves ambiguous -> fallback policy.
  3. Close-based: session flatten, time exit, max hold, end of data -> exit at close.

Entry bar of a stop/limit fill at level L: the bar's extremes may have occurred
BEFORE the fill. A level X is *certainly* reached after the fill only if price
had to pass L to get to X from the open: (L - open) * (X - L) > 0. Uncertain
touches are treated as conflicts. (Market fills at the open make every touch on
that bar certain.)
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

NONE, STOP, TARGET, AMBIG = 0, 1, 2, -1


@dataclass(frozen=True)
class MarketArrays:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    force_close: np.ndarray    # bool: flatten at this bar's close
    entry_allowed: np.ndarray  # bool: a fill may happen on this bar
    td: np.ndarray             # int64 trading-day ordinal

    @property
    def n(self) -> int:
        return len(self.o)


@dataclass(frozen=True)
class IntrabarData:
    o: np.ndarray
    h: np.ndarray
    l: np.ndarray
    c: np.ndarray
    start: np.ndarray      # per HTF bar: first LTF index
    end: np.ndarray        # per HTF bar: LTF end index (exclusive)
    reliable: np.ndarray   # per HTF bar: LTF reproduces HTF OHLC exactly


@dataclass(frozen=True)
class FillPolicy:
    same_bar: str = "conservative"        # conservative | optimistic | intrabar
    fallback: str = "conservative"        # used when intrabar cannot decide
    target_gap_fill: str = "limit_price"  # limit_price | open
    limit_penetration: float = 0.0        # points beyond limit required to fill
    allow_next_session_entry: bool = False


@dataclass
class Entry:
    filled: bool
    reason: str
    bar: int = -1
    price: float = math.nan
    kind: str = ""          # "open" (market/gap fill) or "level"
    order_type: str = ""    # market | stop | limit  (drives slippage)
    last_bar: int = -1      # last bar the order was working (for position bookkeeping)


def _trigger(direction: int, entry_type: str, o: float, h: float, l: float, L: float,
             pen: float) -> tuple[bool, float, str]:
    if entry_type == "stop":
        if direction > 0:
            if o >= L:
                return True, o, "open"
            if h >= L:
                return True, L, "level"
        else:
            if o <= L:
                return True, o, "open"
            if l <= L:
                return True, L, "level"
    else:  # limit
        if direction > 0:
            if o <= L:
                return True, o, "open"
            if l <= L - pen:
                return True, L, "level"
        else:
            if o >= L:
                return True, o, "open"
            if h >= L + pen:
                return True, L, "level"
    return False, math.nan, ""


def find_entry(A: MarketArrays, i: int, direction: int, entry_type: str, level: float,
               expiry: int, pol: FillPolicy) -> Entry:
    j = i + 1
    if j >= A.n:
        return Entry(False, "NO_NEXT_BAR", last_bar=i)
    if not pol.allow_next_session_entry and A.td[j] != A.td[i]:
        return Entry(False, "NEXT_BAR_NEW_SESSION", last_bar=i)
    if entry_type == "market":
        if not A.entry_allowed[j]:
            return Entry(False, "ENTRY_BLOCKED", last_bar=i)
        return Entry(True, "", j, float(A.o[j]), "open", "market", j)
    last = min(i + expiry, A.n - 1)
    for k in range(j, last + 1):
        if not A.entry_allowed[k] or (not pol.allow_next_session_entry and A.td[k] != A.td[i]):
            return Entry(False, "ENTRY_CANCELLED_SESSION", last_bar=k - 1)
        ok, px, kind = _trigger(direction, entry_type, A.o[k], A.h[k], A.l[k], level,
                                pol.limit_penetration)
        if ok:
            return Entry(True, "", k, float(px), kind, entry_type, k)
    return Entry(False, "ENTRY_NOT_FILLED", last_bar=last)


def _touches(direction: int, h: float, l: float, stop: float, target: float) -> tuple[bool, bool]:
    if direction > 0:
        return l <= stop, (h >= target) if target == target else False
    return h >= stop, (l <= target) if target == target else False


def _decide(st: bool, st_cert: bool, tg: bool, tg_cert: bool) -> int:
    if not st and not tg:
        return NONE
    if st and st_cert and not tg:
        return STOP
    if tg and tg_cert and not st:
        return TARGET
    return AMBIG


def _apply(policy: str, st: bool, tg: bool) -> int:
    if policy == "conservative":
        return STOP if st else NONE
    return TARGET if tg else NONE


def _gap(direction: int, o: float, stop: float, target: float, pol: FillPolicy):
    if (direction > 0 and o <= stop) or (direction < 0 and o >= stop):
        return STOP, o, "STOP_GAP"
    if target == target and ((direction > 0 and o >= target) or (direction < 0 and o <= target)):
        return TARGET, (o if pol.target_gap_fill == "open" else target), "TARGET_GAP"
    return None


def _resolve_intrabar(ib: IntrabarData, k: int, direction: int, stop: float, target: float,
                      pending_entry: tuple | None, pol: FillPolicy):
    """Replay LTF bars of HTF bar k. Returns (outcome, price, reason) or None if still ambiguous."""
    s, e = int(ib.start[k]), int(ib.end[k])
    m = s
    if pending_entry is not None:
        entry_type, L = pending_entry
        while m < e:
            ok, _, kind = _trigger(direction, entry_type, ib.o[m], ib.h[m], ib.l[m], L,
                                   pol.limit_penetration)
            if ok:
                break
            m += 1
        if m == e:
            return None
        st, tg = _touches(direction, ib.h[m], ib.l[m], stop, target)
        certain = kind == "open"
        d = _decide(st, certain or (L - ib.o[m]) * (stop - L) > 0,
                    tg, certain or (L - ib.o[m]) * (target - L) > 0)
        if d == AMBIG:
            return None
        if d == STOP:
            return STOP, stop, "STOP"
        if d == TARGET:
            return TARGET, target, "TARGET"
        m += 1
    for q in range(m, e):
        if q > s:
            g = _gap(direction, ib.o[q], stop, target, pol)
            if g:
                return g
        st, tg = _touches(direction, ib.h[q], ib.l[q], stop, target)
        if st and tg:
            return None
        if st:
            return STOP, stop, "STOP"
        if tg:
            return TARGET, target, "TARGET"
    # After a pending-entry fill, touches that happened before the fill legitimately
    # leave no exit. Otherwise LTF bars must show the HTF touch; if not, don't trust them.
    return (NONE, math.nan, "") if pending_entry is not None else None


def resolve_bar(A: MarketArrays, ib: IntrabarData | None, k: int, direction: int, stop: float,
                target: float, pol: FillPolicy, entry: Entry | None, entry_type: str,
                entry_level: float):
    """Stop/target outcome on bar k. entry is given only when k is the fill bar.
    Returns (outcome, price, reason, conflict_resolution_label)."""
    is_entry_bar = entry is not None
    if not is_entry_bar:
        g = _gap(direction, A.o[k], stop, target, pol)
        if g:
            return g + ("",)
    st, tg = _touches(direction, A.h[k], A.l[k], stop, target)
    if is_entry_bar and entry.kind == "level":
        L, o = entry.price, A.o[k]
        d = _decide(st, (L - o) * (stop - L) > 0, tg, (L - o) * (target - L) > 0)
    else:
        d = _decide(st, True, tg, True)
    if d == STOP:
        return STOP, stop, "STOP", ""
    if d == TARGET:
        return TARGET, target, "TARGET", ""
    if d == NONE:
        return NONE, math.nan, "", ""
    # ---- conflict ----
    if pol.same_bar == "intrabar":
        if ib is not None and ib.reliable[k]:
            pending = (entry_type, entry_level) if (is_entry_bar and entry.kind == "level") else None
            r = _resolve_intrabar(ib, k, direction, stop, target, pending, pol)
            if r is not None:
                return r + ("INTRABAR",)
            label = f"INTRABAR_AMBIGUOUS->{pol.fallback.upper()}"
        else:
            label = f"INTRABAR_UNAVAILABLE->{pol.fallback.upper()}"
        out = _apply(pol.fallback, st, tg)
    else:
        label = pol.same_bar.upper()
        out = _apply(pol.same_bar, st, tg)
    if out == STOP:
        return STOP, stop, "STOP", label
    if out == TARGET:
        return TARGET, target, "TARGET", label
    return NONE, math.nan, "", label


def first_event_bar(A: MarketArrays, start: int, limit: int, direction: int, stop: float,
                    target: float) -> int:
    """First bar in [start, limit] with a stop/target touch or a forced close.
    Vectorized in geometrically growing chunks (fast for short and long holds).
    Returns ``limit`` if nothing happens earlier (limit = time/hold/data end)."""
    k0, size = start, 64
    has_tgt = target == target
    while k0 <= limit:
        k1 = min(limit, k0 + size - 1)
        sl = slice(k0, k1 + 1)
        if direction > 0:
            m = A.l[sl] <= stop
            if has_tgt:
                m |= A.h[sl] >= target
        else:
            m = A.h[sl] >= stop
            if has_tgt:
                m |= A.l[sl] <= target
        m |= A.force_close[sl]
        if m.any():
            return k0 + int(np.argmax(m))
        k0, size = k1 + 1, size * 4
    return limit


def simulate_exit(A: MarketArrays, ib: IntrabarData | None, pol: FillPolicy, entry: Entry,
                  direction: int, stop: float, target: float, time_bars: int | None,
                  max_hold: int | None, entry_type: str, entry_level: float,
                  signal_exit_bar: int | None = None) -> dict:
    """``signal_exit_bar`` (Phase 3, optional): bar m at whose OPEN a signal exit executes
    (the exit condition was true at the close of m-1). Earlier stop/target/session/time exits
    take precedence; if the open of m gaps through the stop or target, the resting order
    fills there (STOP_GAP / TARGET_GAP, existing gap policy); otherwise exit reason SIGNAL."""
    f = entry.bar
    last = A.n - 1
    time_idx = f + time_bars - 1 if time_bars else last
    hold_idx = f + max_hold - 1 if max_hold else last
    limit = min(last, time_idx, hold_idx)
    sx = signal_exit_bar if (signal_exit_bar is not None and f < signal_exit_bar <= limit) else None
    if sx is not None:
        limit = sx - 1

    def close_reason(k: int) -> str | None:
        if A.force_close[k]:
            return "SESSION_CLOSE"
        if k >= time_idx and time_bars:
            return "TIME"
        if k >= hold_idx and max_hold:
            return "MAX_HOLD"
        if k == last:
            return "END_OF_DATA"
        return None

    conflict = ""
    out, px, reason, conflict = resolve_bar(A, ib, f, direction, stop, target, pol, entry,
                                            entry_type, entry_level)
    k = f
    def signal_exit() -> tuple[int, float, str]:
        g = _gap(direction, float(A.o[sx]), stop, target, pol)
        if g is not None:
            return sx, float(g[1]), g[2]
        return sx, float(A.o[sx]), "SIGNAL"

    if out == NONE:
        cr = close_reason(f)
        if cr:
            reason, px = cr, float(A.c[f])
        elif sx is not None and f + 1 > limit:           # exit signal on the entry bar itself
            k, px, reason = signal_exit()
        else:
            k = first_event_bar(A, f + 1, limit, direction, stop, target)
            out, px, reason, conflict2 = resolve_bar(A, ib, k, direction, stop, target, pol,
                                                     None, entry_type, entry_level)
            conflict = conflict2 or conflict
            if out == NONE:
                cr = close_reason(k)
                if cr is None and sx is not None and k == limit:
                    k, px, reason = signal_exit()
                elif cr is None:  # defensive: first_event_bar guarantees an event at k
                    raise AssertionError(f"no exit event at bar {k}")
                else:
                    reason, px = cr, float(A.c[k])
    # Bar-resolution excursions. Close-based exits held the whole exit bar; touch
    # exits only held it until the level, so the exit bar is capped at the exit price.
    k_full = k + 1 if reason in ("SESSION_CLOSE", "TIME", "MAX_HOLD", "END_OF_DATA") else k
    if k_full > f:
        hi, lo = A.h[f:k_full].max(), A.l[f:k_full].min()
    else:
        hi, lo = -math.inf, math.inf
    if direction > 0:
        mfe = max(hi - entry.price, px - entry.price, 0.0)
        mae = max(entry.price - lo, entry.price - px, 0.0)
    else:
        mfe = max(entry.price - lo, entry.price - px, 0.0)
        mae = max(hi - entry.price, px - entry.price, 0.0)
    return {"exit_bar": int(k), "exit_price_theo": float(px), "exit_reason": reason,
            "conflict_resolution": conflict, "mfe_points": float(mfe), "mae_points": float(mae)}
