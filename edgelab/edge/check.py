"""Edge check (ADR-104, ADR-105): the frozen hypotheses of ``hypotheses.py`` measured on NQ: H1-H4 in 9:30-11:00 New
York, H5-H6 in 15:30-16:00. Data: the protocol's DISCOVERY period, or (ADR-105) another 1-minute dataset of the same
instrument, of which only the days BEFORE the discovery period are used (a fresh, independent sample; never the
discovery or holdout days). Nothing here is a run, a trial or a holdout look; the protocol is never touched.

Per hypothesis and day: direction d (+1 / -1) and the move from the entry minute's open to the exit bar's close (10:59
for H1-H4, 15:59 for H5-H6).
* gross   = d x (BID close at exit - BID open at entry), points: what the signal knew, before spread and costs;
* net     = the tradeable direction filled like a backtest: buy on ASK, sell on BID (the spread is in the prices), minus
            commission, fees and slippage of ONE MNQ contract from the configured cost scenario (the same numbers every
            backtest uses), in points;
* the test uses the move divided by the day's TYPICAL RANGE (average 9:30-11:00 high-low of the 20 previous days, known
  before the day starts), so quiet and wild years weigh alike.
Statistics: day-level shuffle test (10,000 shuffles of the directions over the same days: keeps drift and the number of
longs / shorts), Bonferroni over the whole set (p x 6), bootstrap intervals, per-year signs, the same signals on ES (if
imported; BID only, so gross only), and the smallest effect the test could have found.

A verdict is never 'profitable': at best a CANDIDATE for one holdout / forward test.
"""
from __future__ import annotations

import json
import math
from contextlib import nullcontext
from pathlib import Path

import numpy as np
import pandas as pd

from edgelab.edge import hypotheses as HY
from edgelab.edge import stats as ST

NY = "America/New_York"
WINDOW_FROM, WINDOW_MIN = 9 * 60 + 30, 90
RTH_FROM, RTH_TO = 9 * 60 + 30, 16 * 60          # 9:30-15:59 bars
MIN_WINDOW_BARS = 85                              # of 90: a day with more missing minutes is skipped
MIN_RTH_BARS = 350                                # of 390: yesterday must be a (nearly) full regular session
TYPICAL_DAYS = 20
MIN_YEAR_N = 20
MIN_SOURCE_DAYS = 60                              # another dataset must hold at least this many days before discovery
ALPHA = 0.05
SEED = 20261006


class EdgeError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def home(svc) -> Path:
    p = Path(svc.data_root) / "edge"
    p.mkdir(parents=True, exist_ok=True)
    return p


# =============================================================================================== day table
def day_table(ts_ns: np.ndarray, o, h, l, c, ao=None, ac=None, *, tz: str = NY, first_day=None, last_day=None) -> dict:
    """The regular-session minutes (9:30-15:59) of every day as (days x 390) arrays (slots 0..89 = the 9:30-11:00
    window), yesterday's regular session, and the typical range. Only bars inside [first_day, last_day] (NY dates) are
    used. A day counts (``window_ok``) when its 9:30-11:00 window is (nearly) complete."""
    idx = pd.DatetimeIndex(np.asarray(ts_ns, dtype="int64"), tz="UTC").tz_convert(tz)
    mins = np.asarray(idx.hour * 60 + idx.minute)
    dates = np.asarray(idx.tz_localize(None).normalize().values.astype("datetime64[D]"))
    keep = np.ones(len(dates), bool)
    if first_day is not None:
        keep &= dates >= np.datetime64(first_day, "D")
    if last_day is not None:
        keep &= dates <= np.datetime64(last_day, "D")
    arrays = {"o": o, "h": h, "l": l, "c": c, "ao": ao, "ac": ac}
    # regular-session minutes (the morning window = the first 90 slots)
    w = keep & (mins >= RTH_FROM) & (mins < RTH_TO)
    days = np.unique(dates[w & (mins < WINDOW_FROM + WINDOW_MIN)])
    w &= np.isin(dates, days)                             # days without any window bar are not in the table
    di, slot = np.searchsorted(days, dates[w]), mins[w] - RTH_FROM
    t: dict = {"days": days}
    for k, a in arrays.items():
        if a is None:
            t[k] = None
            continue
        m = np.full((len(days), HY.DAY_SLOTS), np.nan)
        m[di, slot] = np.asarray(a, dtype=float)[w]
        t[k] = m
    count = np.sum(np.isfinite(t["c"][:, :WINDOW_MIN]), axis=1)
    need = [0, 1, 29, 30, HY.EXIT_SLOT]
    ok = (count >= MIN_WINDOW_BARS) & np.all(np.isfinite(t["o"][:, need]), axis=1) & \
        np.all(np.isfinite(t["c"][:, need]), axis=1)
    if t["ao"] is not None:
        ok &= np.all(np.isfinite(t["ao"][:, need]), axis=1) & np.all(np.isfinite(t["ac"][:, need]), axis=1)
    t["window_ok"] = ok
    # yesterday's regular session (9:30-15:59): high, low, close of the 15:59 bar
    r = keep & (mins >= RTH_FROM) & (mins < RTH_TO)
    rdays = np.unique(dates[r])
    rdi = np.searchsorted(rdays, dates[r])
    rcount = np.bincount(rdi, minlength=len(rdays))
    rhigh = np.full(len(rdays), -np.inf)
    rlow = np.full(len(rdays), np.inf)
    np.maximum.at(rhigh, rdi, np.asarray(h, float)[r])
    np.minimum.at(rlow, rdi, np.asarray(l, float)[r])
    rclose = np.full(len(rdays), np.nan)
    last = r & (mins == RTH_TO - 1)
    rclose[np.searchsorted(rdays, dates[last])] = np.asarray(c, float)[last]
    rok = (rcount >= MIN_RTH_BARS) & np.isfinite(rclose)
    t["prev_high"] = np.full(len(days), np.nan)
    t["prev_low"] = np.full(len(days), np.nan)
    t["prev_close"] = np.full(len(days), np.nan)
    good = rdays[rok]
    for i, d in enumerate(days):
        j = np.searchsorted(good, d) - 1                  # the last full session strictly before today
        if j >= 0 and (d - good[j]).astype(int) <= 4:
            k = np.searchsorted(rdays, good[j])
            t["prev_high"][i], t["prev_low"][i], t["prev_close"][i] = rhigh[k], rlow[k], rclose[k]
    # typical range: average window high-low of the 20 previous usable days (known before the day starts)
    rng_ = np.where(ok, np.nanmax(t["h"][:, :WINDOW_MIN], axis=1) - np.nanmin(t["l"][:, :WINDOW_MIN], axis=1), np.nan)
    typ = np.full(len(days), np.nan)
    hist: list[float] = []
    for i in range(len(days)):
        if len(hist) >= TYPICAL_DAYS:
            typ[i] = float(np.mean(hist[-TYPICAL_DAYS:]))
        if ok[i] and np.isfinite(rng_[i]):
            hist.append(float(rng_[i]))
    t["typical"] = typ
    t["usable"] = ok & np.isfinite(typ) & (typ > 0)
    return t


# =============================================================================================== one hypothesis
def _costs_points(costs, inst, entry: float, exit_: float, entry_ns: int, exit_ns: int, d: int) -> float:
    base = costs.round_trip_base("market", "market", 1, inst, spread_points=None, entry_price=entry, exit_price=exit_)
    fin = costs.financing_usd(d, entry, 1, inst, entry_ns, exit_ns)
    usd = (base["commission_usd"] + base["fees_usd"] + base["slippage_usd"] + base["spread_usd"] + fin) * costs.multiplier
    return usd / inst.point_value


def trades_of(hid: str, t: dict, cost=None) -> dict:
    """Per signal day: direction, gross points, normalised long-direction move, net points of the trade and of the
    opposite trade (when ASK prices and a cost function exist)."""
    d, e = HY.signals(hid, t)
    x = HY.BY_ID[hid].exit_slot
    rows = np.flatnonzero((d != 0) & t["usable"])
    rows = np.array([i for i in rows if np.isfinite(t["o"][i, e[i]]) and np.isfinite(t["c"][i, x]) and
                     (t["ao"] is None or (np.isfinite(t["ao"][i, e[i]]) and np.isfinite(t["ac"][i, x])))], dtype=int)
    out = {"day": t["days"][rows], "dir": d[rows].astype(float), "entry_slot": e[rows].astype(int),
           "typical": t["typical"][rows]}
    ent = t["o"][rows, e[rows]]
    ext = t["c"][rows, x]
    move = ext - ent                                      # long direction, points (BID)
    out["move_pts"] = move
    out["gross_pts"] = out["dir"] * move
    out["move_norm"] = move / out["typical"]
    out["gross_norm"] = out["gross_pts"] / out["typical"]
    if t["ao"] is not None and cost is not None:
        net, net_opp = np.empty(len(rows)), np.empty(len(rows))
        for j, i in enumerate(rows):
            for which, sgn in ((net, out["dir"][j]), (net_opp, -out["dir"][j])):
                if sgn > 0:                               # buy on ASK, sell on BID
                    a, b = t["ao"][i, e[i]], t["c"][i, x]
                else:                                     # sell on BID, buy back on ASK
                    a, b = t["o"][i, e[i]], t["ac"][i, x]
                which[j] = sgn * (b - a) - cost(a, b, i, int(e[i]), int(sgn))
        out["net_pts"], out["net_opp_pts"] = net, net_opp
    return out


def evaluate(hid: str, t: dict, cost=None, es_t: dict | None = None) -> dict:
    tr = trades_of(hid, t, cost)
    n = len(tr["dir"])
    seed = SEED + int(hid[1:])
    res: dict = {"id": hid, "n": n, "days_usable": int(t["usable"].sum())}
    if n < 2:
        res.update(verdict="TOO_FEW", reason="Fewer than 2 signal days.")
        return res
    g_pts, g_norm = ST.summary(tr["gross_pts"]), ST.summary(tr["gross_norm"])
    sh = ST.shuffle_test(tr["dir"], tr["move_norm"], seed)
    p_adj = min(1.0, sh["p"] * HY.FAMILY)
    level_b = 1 - ALPHA / HY.FAMILY
    sign = 1.0 if g_norm["mean"] >= 0 else -1.0
    res.update(
        longs=int((tr["dir"] > 0).sum()), shorts=int((tr["dir"] < 0).sum()),
        gross_pts=g_pts, gross_norm=g_norm,
        gross_pts_ci95=ST.bootstrap_ci(tr["gross_pts"], 0.95, seed),
        gross_norm_ci95=ST.bootstrap_ci(tr["gross_norm"], 0.95, seed),
        gross_norm_ci_bonf=ST.bootstrap_ci(tr["gross_norm"], level_b, seed),
        gross_pts_ci_bonf=ST.bootstrap_ci(tr["gross_pts"], level_b, seed),
        p=sh["p"], p_bonf=p_adj, null=sh["null_hist"], null_mean=sh["null_mean"], null_sd=sh["null_sd"],
        observed_norm=sh["observed"],
        win_share=float(np.mean(tr["gross_pts"] > 0)),
        win_share_all_up=float(np.mean(tr["move_pts"] > 0)),
        typical_pts=float(np.mean(tr["typical"])),
        detectable_norm=ST.detectable_mean(g_norm["se"], ALPHA / HY.FAMILY),
        direction=("as stated" if sign > 0 else "opposite"))
    if res["detectable_norm"] is not None:
        res["detectable_pts"] = res["detectable_norm"] * res["typical_pts"]
    # the tradeable direction (the sign of the gross mean), filled like a backtest
    if "net_pts" in tr:
        net = tr["net_pts"] if sign > 0 else tr["net_opp_pts"]
        res["net_pts"] = ST.summary(net)
        res["net_pts_ci95"] = ST.bootstrap_ci(net, 0.95, seed + 100)
        res["net_win_share"] = float(np.mean(net > 0))
        res["cost_pts"] = float(np.mean(sign * tr["gross_pts"] - net))     # spread + commission + fees + slippage
    # per year (by the NY date of the day)
    years = pd.DatetimeIndex(tr["day"]).year.to_numpy()
    by_year = []
    for y in sorted(set(years.tolist())):
        m = years == y
        by_year.append({"year": int(y), "n": int(m.sum()), "gross_pts": float(np.mean(tr["gross_pts"][m])),
                        "gross_norm": float(np.mean(tr["gross_norm"][m]))})
    res["by_year"] = by_year
    big = [y for y in by_year if y["n"] >= MIN_YEAR_N]
    res["years_same_sign"] = sum(1 for y in big if y["gross_norm"] * sign > 0)
    res["years_counted"] = len(big)
    # the same signals on ES (an independent market: a real effect usually shows in both)
    if es_t is not None:
        et = trades_of(hid, es_t, None)
        if len(et["dir"]) >= 30:
            esh = ST.shuffle_test(et["dir"], et["move_norm"], seed + 200)
            em = ST.summary(et["gross_norm"])
            res["es"] = {"n": len(et["dir"]), "gross_norm": em, "gross_pts": ST.summary(et["gross_pts"]), "p": esh["p"],
                         "same_sign": bool(em["mean"] * sign > 0)}
        else:
            res["es"] = {"n": len(et["dir"]), "too_few": True}
    res.update(_verdict(res))
    return res


VERDICT_TEXT = {
    "TOO_FEW": "Too few signal days to say anything.",
    "NO_EVIDENCE": "No evidence the signal knows the direction: the result is within what shuffled directions give.",
    "NOT_TRADEABLE": "The signal carries information, but after spread and costs the trade is not clearly profitable.",
    "INCONSISTENT": "Significant and profitable after costs on average, but not in most years or not on ES: likely "
                    "a period effect or chance.",
    "CANDIDATE": "Passed every check on the discovery period. A candidate for ONE holdout or forward test - not proof.",
}


def _verdict(r: dict) -> dict:
    if r["n"] < 30:
        v = "TOO_FEW"
    elif r["p_bonf"] >= ALPHA:
        v = "NO_EVIDENCE"
    elif not r.get("net_pts_ci95") or r["net_pts_ci95"][0] <= 0:
        v = "NOT_TRADEABLE"
    else:
        es = r.get("es")
        es_bad = es is not None and not es.get("too_few") and (not es["same_sign"] or es["p"] >= ALPHA)
        years_bad = r["years_counted"] == 0 or r["years_same_sign"] < 0.75 * r["years_counted"]
        v = "INCONSISTENT" if (es_bad or years_bad) else "CANDIDATE"
    return {"verdict": v, "verdict_text": VERDICT_TEXT[v]}


# =============================================================================================== the run
DISCOVERY = "discovery"


def _parent_and_windows(svc):
    from edgelab.mystrategy import runner as R
    parent = R._parent(svc)
    if parent is None:
        raise EdgeError("NO_PROTOCOL", "The edge check needs the workspace's active research protocol (its data and "
                                       "discovery dates). None, or more than one, is active.")
    w = R.windows(parent["material"])
    return parent, R._ts(w["discovery"]["start"]), R._ts(w["discovery"]["end"])


def sources(svc) -> list[dict]:
    """Where the check can run: the protocol's discovery period, and every other 1-minute dataset of the same instrument
    with BID and ASK prices that holds at least MIN_SOURCE_DAYS days BEFORE the discovery period (only those days are
    used: an independent sample). Datasets that cannot be used are listed with the reason."""
    from edgelab.mystrategy import runner as R
    try:
        parent, d_start, d_end = _parent_and_windows(svc)
        main_id = R.dataset_1m(svc, parent)
    except (EdgeError, R.MyStrategyError):
        return []
    main = svc.store.get_manifest(main_id)
    out = [{"key": DISCOVERY, "dataset_id": main_id, "name": main.dataset_name or main_id,
            "label": "Discovery period of your research protocol", "first_day": str(d_start.tz_convert(NY).date()),
            "last_day": str(d_end.tz_convert(NY).date()), "usable": True}]
    cut = d_start.tz_convert(NY).normalize()
    for m in svc.store.list_datasets():
        if m.get("timeframe") != "1m" or m.get("parent_dataset_id"):
            continue
        row = {"key": m["dataset_id"], "dataset_id": m["dataset_id"], "name": m.get("dataset_name") or m["dataset_id"],
               "label": "Earlier days only (before the discovery period)", "dataset_start": m.get("start"),
               "dataset_end": m.get("end")}
        start = R._ts(m["start"]).tz_convert(NY) if m.get("start") else None
        reason = None
        if m.get("instrument") != main.instrument:
            reason = f"A different instrument ({m.get('instrument')}); the hypotheses are about {main.instrument}."
        elif not m.get("has_ask_ohlc"):
            reason = "No ASK prices: the net result needs BID and ASK."
        elif start is None or (cut - start.normalize()).days < MIN_SOURCE_DAYS * 7 // 5:
            reason = (f"Fewer than {MIN_SOURCE_DAYS} trading days before the discovery period starts "
                      f"({cut.date()}); only those days could be used.")
        row.update(usable=reason is None, reason=reason)
        if reason is None:
            row["first_day"] = str(start.date())
            row["last_day"] = str((cut - pd.Timedelta(days=1)).date())
        out.append(row)
    return out


def inputs(svc, source: str | None = None, lock=None):
    """(source row, NQ 1m dataset cut to the source's days, start, end, ES arrays or None). The discovery source is
    the protocol's discovery period; another dataset is cut to the bars BEFORE the discovery period starts (never the
    discovery or holdout days)."""
    from edgelab.mystrategy import es as ES
    source = source or DISCOVERY
    with (lock if lock is not None else nullcontext()):
        rows = {r["key"]: r for r in sources(svc)}
        parent, d_start, d_end = _parent_and_windows(svc)
    row = rows.get(source)
    if row is None:
        raise EdgeError("UNKNOWN_SOURCE", f"Unknown data source '{source}'.")
    if not row["usable"]:
        raise EdgeError("SOURCE_NOT_USABLE", row["reason"])
    if source == DISCOVERY:
        start, end = d_start, d_end
        ds = svc._cell_dataset(row["dataset_id"], (start, end), lock)
    else:
        full = svc._cell_dataset(row["dataset_id"], None, lock)
        start = pd.Timestamp(int(full.bars.ts_ns[0]), tz="UTC")
        end = d_start - pd.Timedelta(nanoseconds=1)       # strictly before the first discovery bar
        ds = svc._cell_dataset(row["dataset_id"], (start, end), lock)
    if not ds.bars.has_ask_ohlc:
        raise EdgeError("ASK_OHLC_REQUIRED", "The dataset has no ASK prices; the net result needs BID and ASK.")
    if ds.calendar.timezone != NY:
        raise EdgeError("CALENDAR_TZ", f"The dataset's calendar is in {ds.calendar.timezone}, not New York time.")
    if int(ds.bars.ts_ns[-1]) >= d_start.value and source != DISCOVERY:  # defensive: never a discovery bar
        raise EdgeError("SOURCE_OVERLAP", "The cut dataset reaches into the discovery period.")
    es = None
    try:
        s = ES.load(svc.data_root)                         # hash-checked
    except ES.EsError:
        s = None
    if s is not None:
        with np.load(ES.folder(svc.data_root) / s.manifest["file"]) as z:
            es = {k: z[k] for k in ("ts", "open", "high", "low", "close")}
        es["content_hash"] = s.content_hash
    return row, ds, start, end, es


def cache_key(ds, start, end, cfg_hash: str, es_hash: str | None) -> str:
    from edgelab.core.identity import hash_obj
    return hash_obj({"set": HY.fingerprint(), "data": ds.manifest.content_hash, "start": str(start), "end": str(end),
                     "config": cfg_hash, "es": es_hash, "seed": SEED}, 16)


def _set_latest(svc, source: str, name: str) -> None:
    from edgelab.mystrategy import runner as R
    ptr = _pointers(svc)
    ptr[source] = name
    R._write_json(home(svc) / "latest.json", {"by_source": ptr})


def _pointers(svc) -> dict:
    from edgelab.mystrategy import runner as R
    raw = R._read_json(home(svc) / "latest.json") or {}
    if "by_source" in raw:
        return dict(raw["by_source"])
    return {DISCOVERY: raw["file"]} if raw.get("file") else {}      # ADR-104 pointer = the discovery result


def run(svc, *, source: str | None = None, lock=None, progress=None) -> dict:
    from edgelab.engine.costs import cost_model_from_config
    from edgelab.instruments import contract_for, execution_view
    from edgelab.mystrategy import runner as R
    source = source or DISCOVERY
    if progress:
        progress("Loading and checking the price data" + (" (discovery period only)" if source == DISCOVERY
                                                           else " (days before the discovery period only)"))
    row, ds, start, end, es = inputs(svc, source, lock)
    key = cache_key(ds, start, end, svc._config_hash(), None if es is None else es["content_hash"])
    path = home(svc) / f"check_{key}.json"
    hit = R._read_json(path)
    if hit is not None:
        _set_latest(svc, source, path.name)
        return hit
    costs = cost_model_from_config(svc.cfg, ds.instrument.symbol, provider=ds.manifest.provider)
    if costs.status == "unconfigured":
        raise EdgeError("COSTS_UNCONFIGURED", "The cost scenario is unconfigured; enter the broker numbers first.")
    inst = execution_view(ds.instrument, contract_for(svc.cfg, {"contract": "MNQ"}))
    b = ds.bars
    first_day = pd.Timestamp(start).tz_convert(NY).date()
    last_day = pd.Timestamp(end).tz_convert(NY).date()
    if progress:
        progress("Building the 9:30-16:00 table of every day")
    t = day_table(b.ts_ns, b.open, b.high, b.low, b.close, b.ask_open, b.ask_close, first_day=first_day, last_day=last_day)
    day_ns = (pd.DatetimeIndex(t["days"]).tz_localize(NY) + pd.Timedelta(minutes=RTH_FROM)).asi8
    minute = 60_000_000_000

    def cost_for(exit_slot: int):
        def cost(a, x, i, slot, sgn):
            return _costs_points(costs, inst, a, x, int(day_ns[i] + slot * minute), int(day_ns[i] + (exit_slot + 1) * minute),
                                 sgn)
        return cost
    es_t = None
    if es is not None:
        es_t = day_table(es["ts"], es["open"], es["high"], es["low"], es["close"], first_day=first_day, last_day=last_day)
    out = {"set": HY.manifest(), "computed_at": R._now(), "app_version": R._code_version(), "key": key,
           "source": {"key": source, "label": row["label"], "independent": source != DISCOVERY},
           "window": {"start": start.isoformat(), "end": end.isoformat(), "first_day": str(first_day),
                      "last_day": str(last_day)},
           "dataset": {"dataset_id": ds.manifest.dataset_id, "content_hash": ds.manifest.content_hash,
                       "instrument": ds.instrument.symbol, "provider": ds.manifest.provider,
                       "name": row.get("name")},
           "days": {"in_window": int(len(t["days"])), "usable": int(t["usable"].sum()),
                    "skipped_missing_minutes": int((~t["window_ok"]).sum()),
                    "skipped_first_20_days": int((t["window_ok"] & ~np.isfinite(t["typical"])).sum())},
           "costs": {"scenario": costs.to_dict().get("scenario"), "contract": "MNQ",
                     "note": "buy on ASK, sell on BID (spread in the prices) + commission, fees and slippage of one MNQ "
                             "contract from the configured cost scenario"},
           "es": None if es_t is None else {"content_hash": es["content_hash"], "usable_days": int(es_t["usable"].sum())},
           "family": HY.FAMILY, "alpha": ALPHA, "results": []}
    for k, hyp in enumerate(HY.H):
        if progress:
            progress(f"Testing {hyp.id} ({k + 1} of {len(HY.H)}): {hyp.name}")
        out["results"].append(evaluate(hyp.id, t, cost_for(hyp.exit_slot), es_t))
    out = _jsonable(out)
    R._write_json(path, out)
    _set_latest(svc, source, path.name)
    return out


def latest(svc, source: str | None = None) -> dict | None:
    from edgelab.mystrategy import runner as R
    name = _pointers(svc).get(source or DISCOVERY)
    return R._read_json(home(svc) / name) if name else None


def latest_all(svc) -> dict:
    from edgelab.mystrategy import runner as R
    out = {}
    for k, name in _pointers(svc).items():
        r = R._read_json(home(svc) / name)
        if r is not None:
            out[k] = r
    return out


def _jsonable(x):
    if isinstance(x, dict):
        return {k: _jsonable(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_jsonable(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        v = float(x)
        return v if math.isfinite(v) else None
    if isinstance(x, np.ndarray):
        return [_jsonable(v) for v in x.tolist()]
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def dumps(x) -> str:
    return json.dumps(_jsonable(x))
