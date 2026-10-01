"""Day-trading invariants (ADR-60): the checks every factory strategy must pass.

The policy below is GLOBAL and frozen. It is not a variation dimension: no generated variant,
template or AI proposal can relax it. ``validate_day_trading`` inspects a plain DSL definition
(it does not trust how the definition was produced) and returns machine-readable violations;
``check_engine_config`` checks the backtest configuration a factory strategy is run under.

Three layers keep a position inside one America/New_York trading date:

1. Strategy flat window. The definition declares a strategy-local ENTRY window and a HOLD window
   (same timezone and weekdays; entry inside hold). Every traded side's ``exit.signal`` contains
   ``session(HOLD).in_session == 0``: the first bar opening outside HOLD raises the exit flag and the
   engine exits at the next bar's open = ``HOLD.end + timeframe`` (the strategy's flat instant).
   Entries are only possible inside ENTRY, and a stop/limit order cannot outlive HOLD.
   The flat instant is checked in New York time under every London/Tokyo/New York DST combination:
   it must come after the window start within the SAME trading date (which opens at 18:00 NY) and no
   later than ``latest_flat_ny`` (16:00 NY, before the 16:15 Dukascopy / 17:00 CME close).
2. Hard holding cap. ``exit.max_hold_bars`` is REQUIRED and ``max_hold_bars x timeframe`` must stay
   below 23 hours (a backstop; the real invariant is the same trading date).
3. Engine. ``check_engine_config`` refuses a backtest config that could hold overnight
   (``session.hold_overnight``), does not flatten daily, or flattens after the policy cutoff. The
   engine then also force-closes at its flatten time and on the last bar of every trading date.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any, Mapping
from zoneinfo import ZoneInfo

from edgelab.data.schema import timeframe_minutes

DAY_TRADING_POLICY = {
    "policy_id": "same_ny_trading_date/1",
    "timezone": "America/New_York",
    "trading_date_open_ny": "18:00",
    "latest_flat_ny": "16:00",
    "max_hold_minutes_exclusive": 23 * 60,
    "requires": ["strategy-local ENTRY and HOLD windows (same timezone and weekdays, entry inside hold)",
                 "exit.signal of every traded side contains session(HOLD).in_session == 0",
                 "exit.max_hold_bars x timeframe < 23 h",
                 "stop/limit entry orders expire inside HOLD",
                 "backtest config: session.flatten_daily true, flatten_time <= 16:00, hold_overnight false"],
    "variable": False,
}
# Dates covering every offset combination: winter; US DST only (mid-March); summer; UK winter / US DST (late Oct).
DST_PROBE_DATES = (date(2024, 1, 10), date(2024, 3, 13), date(2024, 7, 10), date(2024, 10, 30))
_NY = ZoneInfo("America/New_York")


def _m(hhmm: str) -> int:
    h, m = str(hhmm).split(":")
    return int(h) * 60 + int(m)


def flat_condition(hold_session: str) -> dict:
    """The exit condition that makes a position flat at HOLD.end + timeframe."""
    return {"left": {"feature": "session", "params": {"session": hold_session}, "output": "in_session"},
            "op": "==", "right": 0}


def _is_flat_cond(c: Any) -> str | None:
    if not isinstance(c, Mapping) or c.get("op") != "==" or c.get("right") not in (0, 0.0):
        return None
    left = c.get("left")
    if not isinstance(left, Mapping) or left.get("feature") != "session" or left.get("output") != "in_session":
        return None
    if left.get("lag", 0) != 0 or left.get("timeframe") is not None:
        return None
    return (left.get("params") or {}).get("session")


def _flat_sessions(cond: Any) -> set:
    """Hold sessions whose flat condition ALONE triggers this exit (the condition itself or a top-level
    ``any`` member). A flat condition nested under ``all``/``not`` does not force anything."""
    if cond is None:
        return set()
    s = _is_flat_cond(cond)
    if s:
        return {s}
    if isinstance(cond, Mapping) and isinstance(cond.get("any"), list):
        return {x for x in (_is_flat_cond(c) for c in cond["any"]) if x}
    return set()


def ny_span(window: Mapping, tf_min: int, on: date) -> tuple[datetime, datetime]:
    """(start, flat instant) in New York time of a HOLD window instance starting on local date ``on``."""
    tz = ZoneInfo(window["timezone"])
    s, e = _m(window["start"]), _m(window["end"])
    dur = (e - s) % 1440 + tf_min
    start = datetime(on.year, on.month, on.day, s // 60, s % 60, tzinfo=tz)
    flat = start + timedelta(minutes=dur)      # wall-clock-agnostic: an absolute duration
    return start.astimezone(_NY), flat.astimezone(_NY)


def _since_open(t: datetime) -> int:
    return (t.hour * 60 + t.minute - _m(DAY_TRADING_POLICY["trading_date_open_ny"])) % 1440


def _inside(inner: Mapping, outer: Mapping) -> bool:
    s = _m(outer["start"])
    rel = lambda x: (_m(x) - s) % 1440
    span = rel(outer["end"]) or 1440
    return rel(inner["start"]) < span and 0 < (rel(inner["end"]) or 1440) <= span and \
        rel(inner["start"]) < (rel(inner["end"]) or 1440)


def validate_day_trading(defn: Mapping) -> list[dict]:
    """-> [] when the definition satisfies the policy, else [{code, detail}]. Never repairs anything."""
    out: list[dict] = []
    bad = lambda code, detail: out.append({"code": code, "detail": detail})
    try:
        tf = timeframe_minutes(str(defn.get("timeframe")))
    except ValueError:
        return [{"code": "DT_TIMEFRAME", "detail": f"invalid timeframe {defn.get('timeframe')!r}"}]
    entry, ex = defn.get("entry") or {}, defn.get("exit") or {}
    local = defn.get("sessions") or {}
    ename = entry.get("session")
    if not ename or ename not in local:
        bad("DT_NO_ENTRY_WINDOW", "entry.session must name a strategy-local entry window")
    sides = {"long": ("long",), "short": ("short",), "both": ("long", "short")}.get(entry.get("direction"), ())
    sig = ex.get("signal") or {}
    holds = set()
    for side in sides:
        found = _flat_sessions(sig.get(side))
        if not found:
            bad("DT_NO_FORCED_FLAT", f"exit.signal.{side} has no forced-flat condition "
                                     f"session(HOLD).in_session == 0 at its top level")
        holds |= found
    if len(holds) > 1:
        bad("DT_AMBIGUOUS_HOLD", f"sides use different hold windows {sorted(holds)}")
    hname = next(iter(holds)) if len(holds) == 1 else None
    if hname is not None and hname not in local:
        bad("DT_HOLD_NOT_LOCAL", f"hold window {hname!r} must be a strategy-local session")
        hname = None
    mh = ex.get("max_hold_bars")
    if not isinstance(mh, int) or isinstance(mh, bool) or mh < 1:
        bad("DT_NO_MAX_HOLD", "exit.max_hold_bars is required (hard holding-time guard)")
    elif mh * tf >= DAY_TRADING_POLICY["max_hold_minutes_exclusive"]:
        bad("DT_MAX_HOLD_TOO_LONG", f"max_hold_bars {mh} x {tf}m >= 23 h")
    ts = ex.get("time_stop_bars")
    if isinstance(ts, int) and ts * tf >= DAY_TRADING_POLICY["max_hold_minutes_exclusive"]:
        bad("DT_MAX_HOLD_TOO_LONG", f"time_stop_bars {ts} x {tf}m >= 23 h")
    if out or hname is None or not ename:
        return out
    E, H = local[ename], local[hname]
    wd = lambda w: [str(d).lower()[:3] for d in w.get("weekdays", ["mon", "tue", "wed", "thu", "fri"])]
    if E.get("timezone") != H.get("timezone") or wd(E) != wd(H):
        bad("DT_WINDOW_MISMATCH", "entry and hold windows must share timezone and weekdays")
    elif not _inside(E, H):
        bad("DT_ENTRY_OUTSIDE_HOLD", f"entry window {E['start']}-{E['end']} is not inside hold window "
                                     f"{H['start']}-{H['end']}")
    latest = _m(DAY_TRADING_POLICY["latest_flat_ny"])
    latest_since_open = (latest - _m(DAY_TRADING_POLICY["trading_date_open_ny"])) % 1440
    for d in DST_PROBE_DATES:
        s, f = ny_span(H, tf, d)
        dur = int((f - s).total_seconds() // 60)
        if dur >= DAY_TRADING_POLICY["max_hold_minutes_exclusive"]:
            bad("DT_WINDOW_TOO_LONG", f"hold window spans >= 23 h on {d}")
            break
        if _since_open(s) + dur > latest_since_open:      # crosses 18:00 NY or ends after the cutoff
            bad("DT_CROSSES_TRADING_DATE", f"hold window {H['start']}-{H['end']} {H['timezone']} (flat "
                                           f"{f.strftime('%H:%M')} NY on {d}) is not inside one NY trading "
                                           f"date ending by {DAY_TRADING_POLICY['latest_flat_ny']} NY")
            break
    order = entry.get("order") or {}
    if order.get("type", "market") != "market":
        exp = order.get("expiry_bars", 1)
        rel = lambda x: (_m(x) - _m(H["start"])) % 1440
        if isinstance(exp, int) and rel(E["end"]) + exp * tf > (rel(H["end"]) or 1440):
            bad("DT_ORDER_OUTLIVES_WINDOW", f"a {order.get('type')} order placed at the end of the entry window "
                                            f"would still work after the hold window ({exp} bar expiry)")
    return out


def check_engine_config(bt_cfg: Mapping) -> list[dict]:
    """Backtest config refusals for factory strategies ([] = compatible)."""
    out = []
    sess = (bt_cfg or {}).get("session", {}) or {}
    if sess.get("hold_overnight", False):
        out.append({"code": "DT_ENGINE_HOLD_OVERNIGHT", "detail": "backtest.session.hold_overnight must be false"})
    if not sess.get("flatten_daily", True) or not sess.get("flatten_time"):
        out.append({"code": "DT_ENGINE_NO_DAILY_FLATTEN",
                    "detail": "backtest.session.flatten_daily must be true with a flatten_time"})
    elif _m(sess["flatten_time"]) > _m(DAY_TRADING_POLICY["latest_flat_ny"]):
        out.append({"code": "DT_ENGINE_FLATTEN_LATE",
                    "detail": f"flatten_time {sess['flatten_time']} is after {DAY_TRADING_POLICY['latest_flat_ny']}"})
    return out
