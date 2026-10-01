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

DIRECTIONAL BID/ASK (ADR-55, cost ``spread_source: quotes``): the backtester passes the entry-side
arrays (ASK for a long's buy, BID for a short's sell) to ``find_entry`` and the exit-side arrays
(BID for a long's sell, ASK for a short's buy) to ``simulate_exit`` as ``A``, plus the entry side
as ``ent``/``ib_ent``. Every rule above then applies unchanged on the side that executes. Entry-bar
certainty becomes two-sided: an exit touch at X after a level fill at L is certain only if
  (a) the exit-side touch implies the entry side also reached X (BID <= ASK at every instant: a
      long's BID high >= target implies ASK >= target; a short's ASK low <= target implies
      BID <= target; a STOP touch never implies it), and
  (b) (L - entry_side_open) * (X - L) > 0 (the entry side had to pass L to reach X).
Otherwise the touch is a conflict for the configured policy. This is never more optimistic than the
single-series rule; it differs only for a limit-entry fill bar's stop touch (now a conflict).

TRAILING / BREAKEVEN STOPS (ADR-61, ``simulate_exit_trailing``; used only when a strategy declares them):
  * Decisions are taken at a bar's CLOSE from bars <= k and take effect from bar k+1. The stop that
    applies on bar k was therefore fixed before bar k opened: bar k's own extreme can never raise the
    stop that bar k is tested against (no lookahead, no same-bar self-rescue).
  * Each bar is resolved exactly like a fixed stop with ``resolve_bar`` using the CURRENT stop: a gap of the
    open through the stop exits at the open (worse), a touch exits at the stop, a stop/target touch on one
    bar is a conflict for the configured policy (incl. intrabar replay with that bar's fixed stop).
  * The stop is a ratchet: it moves only in the favorable direction, never loosens, and a candidate that is
    not strictly on the protective side of the bar's close is ignored (it would be an immediate exit).
  * The favorable extreme is measured on the EXIT side of the quote model (long: BID highs, short: ASK lows),
    from the fill price; a stop/limit LEVEL fill starts the extreme at the fill price (the entry bar's own
    extremes may pre-date the fill), a market / gap-open fill includes the whole entry bar.
  * Take-profit stays fixed. Forced session close, time exit, max hold, end of data and signal exits keep
    their existing precedence and prices; trailing only adds earlier stop exits, so the same-trading-date
    and holding-time guarantees cannot be weakened. Reasons: TRAIL_STOP / TRAIL_STOP_GAP when the stop had moved.
  * NO-PROGRESS exit (ADR-62, same kernel): at the close of bar N since entry (entry bar = 1), if the favorable
    excursion (same exit-side extreme as above, including bar N) is below the required points / R / ATR, exit at that
    bar's close (reason NO_PROGRESS). Stop/target touches of bar N and the forced/time/max-hold closes come first.
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


def _certain(L: float, o_entry: float, stop: float, target: float, two_sided: bool) -> tuple[bool, bool]:
    """Entry-bar certainty of a stop / target touch after a level fill at L (module docstring)."""
    return ((not two_sided) and (L - o_entry) * (stop - L) > 0,
            (L - o_entry) * (target - L) > 0)


def _resolve_intrabar(ib: IntrabarData, k: int, direction: int, stop: float, target: float,
                      pending_entry: tuple | None, pol: FillPolicy, ib_ent: IntrabarData | None = None):
    """Replay LTF bars of HTF bar k. Returns (outcome, price, reason) or None if still ambiguous.
    ``ib`` is the exit side; ``ib_ent`` the entry side in directional BID/ASK mode (else ``ib``)."""
    E = ib if ib_ent is None else ib_ent
    s, e = int(ib.start[k]), int(ib.end[k])
    m = s
    if pending_entry is not None:
        entry_type, L = pending_entry
        while m < e:
            ok, _, kind = _trigger(direction, entry_type, E.o[m], E.h[m], E.l[m], L,
                                   pol.limit_penetration)
            if ok:
                break
            m += 1
        if m == e:
            return None
        st, tg = _touches(direction, ib.h[m], ib.l[m], stop, target)
        certain = kind == "open"
        st_c, tg_c = _certain(L, E.o[m], stop, target, ib_ent is not None)
        d = _decide(st, certain or st_c, tg, certain or tg_c)
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
                entry_level: float, ent: MarketArrays | None = None, ib_ent: IntrabarData | None = None):
    """Stop/target outcome on bar k. entry is given only when k is the fill bar.
    ``A``/``ib`` are the exit side; ``ent``/``ib_ent`` the entry side in directional BID/ASK mode.
    Returns (outcome, price, reason, conflict_resolution_label)."""
    is_entry_bar = entry is not None
    if not is_entry_bar:
        g = _gap(direction, A.o[k], stop, target, pol)
        if g:
            return g + ("",)
    st, tg = _touches(direction, A.h[k], A.l[k], stop, target)
    if is_entry_bar and entry.kind == "level":
        st_c, tg_c = _certain(entry.price, (A if ent is None else ent).o[k], stop, target, ent is not None)
        d = _decide(st, st_c, tg, tg_c)
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
            r = _resolve_intrabar(ib, k, direction, stop, target, pending, pol,
                                  ib_ent if ent is not None else None)
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
                  signal_exit_bar: int | None = None, ent: MarketArrays | None = None,
                  ib_ent: IntrabarData | None = None) -> dict:
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
                                            entry_type, entry_level, ent, ib_ent)
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


def simulate_exit_trailing(A: MarketArrays, ib: IntrabarData | None, pol: FillPolicy, entry: Entry,
                           direction: int, stop: float, target: float, time_bars: int | None,
                           max_hold: int | None, entry_type: str, entry_level: float, trail,
                           atr: np.ndarray | None, level: np.ndarray | None, signal_bar: int,
                           signal_exit_bar: int | None = None, ent: MarketArrays | None = None,
                           ib_ent: IntrabarData | None = None) -> dict:
    """Exit simulation with a trailing / breakeven stop (module docstring). Parameters as in
    ``simulate_exit`` plus ``trail`` (engine.signals.TrailSpec), the per-bar ``atr`` and ``level`` arrays of
    the traded direction (known at each bar's close) and ``signal_bar`` (ATR for R/ATR activation is the
    value at the decision bar). Returns the ``simulate_exit`` dict plus ``final_stop`` and ``trail_updates``."""
    d, f, last = direction, entry.bar, A.n - 1
    time_idx = f + time_bars - 1 if time_bars else last
    hold_idx = f + max_hold - 1 if max_hold else last
    limit = min(last, time_idx, hold_idx)
    sx = signal_exit_bar if (signal_exit_bar is not None and f < signal_exit_bar <= limit) else None
    if sx is not None:
        limit = sx - 1
    fill = entry.price
    risk0 = d * (fill - stop)
    atr_sig = float(atr[signal_bar]) if atr is not None else math.nan

    def points(kind: str, v: float) -> float:
        return v if kind == "points" else v * risk0 if kind == "r" else v * atr_sig

    act_pts = points(trail.activation_kind, trail.activation) if trail.activation_kind != "immediate" else 0.0
    be_pts = points(trail.be_kind, trail.be_trigger) if trail.be_kind else math.nan
    np_pts = points(trail.np_kind, trail.np_value) if trail.np_bars else math.nan
    extreme = fill
    if entry.kind == "open":
        extreme = max(fill, float(A.h[f])) if d > 0 else min(fill, float(A.l[f]))
    new_extreme = extreme != fill
    active = trail.mode in ("distance", "level") and trail.activation_kind == "immediate"
    be_on = False
    cur, updates, conflict = stop, 0, ""

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

    k, px, reason = f, math.nan, ""
    for k in range(f, limit + 1):
        out, px, reason, c2 = resolve_bar(A, ib, k, d, cur, target, pol, entry if k == f else None,
                                          entry_type, entry_level, ent, ib_ent)
        conflict = c2 or conflict
        if out != NONE:
            if cur != stop and reason in ("STOP", "STOP_GAP"):
                reason = "TRAIL_" + reason
            break
        cr = close_reason(k)
        if cr:
            reason, px = cr, float(A.c[k])
            break
        # ---- decisions at the CLOSE of bar k (apply from bar k+1) ----
        if k > f:
            hk, lk = float(A.h[k]), float(A.l[k])
            new_extreme = (hk > extreme) if d > 0 else (lk < extreme)
            if new_extreme:
                extreme = hk if d > 0 else lk
        profit = d * (extreme - fill)
        if trail.np_bars and k - f + 1 == trail.np_bars and not profit >= np_pts:
            reason, px = "NO_PROGRESS", float(A.c[k])
            break
        if not active and trail.mode not in ("breakeven", "none") and profit >= act_pts:
            active = True
        if trail.be_kind and not be_on and profit >= be_pts:
            be_on = True
        close_k = float(A.c[k])
        cands = []
        if be_on:
            cands.append((fill + d * trail.be_offset, 0.0))
        n_held = k - f + 1
        if active and trail.mode in ("distance", "level") and n_held % trail.every_bars == 0 \
                and (new_extreme or not trail.only_new_extreme):
            if trail.mode == "distance":
                dist = trail.distance if trail.distance_kind == "points" else trail.distance * float(atr[k])
                cands.append((extreme - d * dist, trail.min_step))
            else:
                cands.append((float(level[k]), trail.min_step))
        for cand, step in cands:
            if math.isfinite(cand) and d * (close_k - cand) > 0 and d * (cand - cur) > step:
                cur, updates = cand, updates + 1
        if sx is not None and k == limit:
            g = _gap(d, float(A.o[sx]), cur, target, pol)
            if g is not None:
                k, px, reason = sx, float(g[1]), g[2]
                if cur != stop and reason == "STOP_GAP":
                    reason = "TRAIL_STOP_GAP"
            else:
                k, px, reason = sx, float(A.o[sx]), "SIGNAL"
            break
    else:                                   # defensive: limit always carries a close-based or signal exit
        raise AssertionError(f"no exit event by bar {limit}")
    k_full = k + 1 if reason in ("SESSION_CLOSE", "TIME", "MAX_HOLD", "END_OF_DATA", "NO_PROGRESS") else k
    if k_full > f:
        hi, lo = A.h[f:k_full].max(), A.l[f:k_full].min()
    else:
        hi, lo = -math.inf, math.inf
    if d > 0:
        mfe = max(hi - fill, px - fill, 0.0)
        mae = max(fill - lo, fill - px, 0.0)
    else:
        mfe = max(fill - lo, fill - px, 0.0)
        mae = max(hi - fill, px - fill, 0.0)
    return {"exit_bar": int(k), "exit_price_theo": float(px), "exit_reason": reason,
            "conflict_resolution": conflict, "mfe_points": float(mfe), "mae_points": float(mae),
            "final_stop": float(cur), "trail_updates": int(updates)}
