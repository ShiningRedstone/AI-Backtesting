"""The Market simulator's predictor live on the chart (ADR-113): the 15-minute candle forecast and the level map, computed on
today's live NQ / ES prices (the Charts feed: Dukascopy USATECH / USA500 1-minute BID, the series the analysis used).

* Models: the FINAL models of the current analysis, trained on ALL discovery candles / decisions and frozen - exactly the
  ones the "Live (new days)" test uses (``newdays.train_candles``, ``levelmap.fit_frozen``), with the analysis' chosen
  model per forecast and its discovery out-of-sample ranges. Trained once per app start (in the background).
* Inputs: live knowledge only. The last ``HISTORY_DAYS`` calendar days of minutes are the history (ATR, previous day /
  week, swings, open FVGs); the minute still forming is left out; a 15-minute candle is predicted at its open, a level map
  is made at 9:30, 10:00 ... 15:30 New York - nothing before its moment. Scheduled news come from the stored calendar.
* Outcomes are shown only once known (a candle that has closed; a level touched, or its 2 hours / the session over).
* Every forecast carries its report-card verdict (beats its baseline beyond chance on discovery, or "no proven skill").
* Display only: never trains on these days, never stores them, never a run, a try or a holdout look.
"""
from __future__ import annotations

import threading
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HISTORY_DAYS = 120
NQ_SYMBOLS = ("NQ", "MNQ")
REFRESH_SECONDS = 60
NS = 1_000_000_000
MIN_NS = 60 * NS
MODELS = ("logistic", "boosting", "similar")


class PredictorError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if np.isfinite(v) else None


def minutes_frame(df: pd.DataFrame | None, drop_from_ns: int | None = None):
    """Feed DataFrame -> market ``Minute`` (whole minutes, sorted, unique; the minute still forming dropped)."""
    from edgelab.market import data as D
    if df is None or not len(df):
        return None
    ts = pd.DatetimeIndex(df.index).as_unit("ns").asi8
    keep = (ts % MIN_NS == 0)
    if drop_from_ns is not None:
        keep &= ts < drop_from_ns
    ts = ts[keep]
    if not len(ts):
        return None
    o = np.argsort(ts, kind="stable")
    ts = ts[o]
    u = np.r_[True, np.diff(ts) > 0]
    col = lambda k: df[k].to_numpy(float)[keep][o][u]                     # noqa: E731
    return D.Minute(ts[u], col("open"), col("high"), col("low"), col("close"))


def train(svc, summary: dict, progress=None) -> dict:
    """Everything frozen for live use (the same steps as the new-days test)."""
    from edgelab.market import analysis as A
    from edgelab.market import levelmap as L
    from edgelab.market import newdays as ND
    step = progress or (lambda s: None)
    key = summary["key"]
    fz = ND.train_candles(svc, progress=step)
    fc = summary.get("forecast") or {}
    up_c, sz_c = (fc.get("up") or {}).get("chosen") or "logistic", (fc.get("size") or {}).get("chosen") or "logistic"
    crd = fz["crd"]
    out = {"key": key, "names": fz["names"], "ok_news": fz["ok_news"], "up_chosen": up_c, "size_chosen": sz_c,
           "models": fz["models"], "size_models": fz["size_models"],
           "p_base": float((fz["yb"][fz["upm"]].sum() + 1) / (fz["upm"].sum() + 2)),
           "sim_up": (fz["Xd"][fz["upm"]], fz["yb"][fz["upm"]], crd["slot"][fz["upm"]]),
           "sim_size": (fz["Xd"], crd["size"], crd["slot"]),
           "size_q": ND.size_residuals(svc.data_root, key, sz_c)}
    lm_sum = summary.get("levelmap") or {}
    if lm_sum.get("targets"):
        step("Level map: training the frozen models on all discovery decisions")
        out["lm_chosen"] = {t: (lm_sum["targets"].get(t) or {}).get("chosen") or "logistic"
                            for t in L.LEVEL_TARGETS + L.DECISION_TARGETS}
        out["lm"] = L.fit_frozen(L.build(fz["cxd"]))
        out["lm_bands"] = L.bands_from_analysis(A.home(svc.data_root) / f"analysis_{key}" / "levelmap.npz", out["lm_chosen"])
    return out


def skill_of(svc) -> dict:
    """The report card's discovery verdict per forecast id (``works`` = beats its baseline beyond chance)."""
    from edgelab.market import report as RP
    try:
        rep = RP.build(svc)
    except Exception:                                                     # noqa: BLE001 - no verdict is shown as unknown
        return {}
    out = {}
    for t in rep.get("targets", []):
        d = t.get("discovery") or {}
        out[t["id"]] = {"works": bool(t.get("works")), "skill": _f(d.get("skill")), "name": t.get("name")}
    return out


def scheduled_end_ns(m, day) -> int | None:
    """When the session of ``day`` normally ends: the usual last minute of the earlier days in ``m`` (+ 1 minute)."""
    days = np.unique(m.day)
    days = days[days < day]
    if not len(days):
        return None
    last = np.searchsorted(m.day, days, side="right") - 1
    vals, counts = np.unique(m.sess_min[last], return_counts=True)
    usual = int(vals[np.argmax(counts)])
    first = int(m.ts[np.searchsorted(m.day, day)])
    start = first - int(m.sess_min[np.searchsorted(m.day, day)]) * MIN_NS        # the session open (18:00 New York)
    return start + (usual + 1) * MIN_NS


def predict_live(fz: dict, nq, es, news: list[dict], workdir: Path, now_ns: int) -> dict:
    """The candle forecasts and the level maps of the current (last) trading date of ``nq``."""
    from edgelab.market import forecast as F
    from edgelab.market import levelmap as L
    cx = F.Context(nq, es, news)
    m = cx.m
    today = m.day[-1]
    last_ns = int(m.ts[-1])
    day0 = int(m.ts[np.searchsorted(m.day, today)])
    b, typ, atr = cx.bars[15], cx.typ[15], cx.atr[15]
    ks = np.flatnonzero((b.day == today) & (b.ts <= now_ns))
    prev_atr = np.r_[np.nan, atr[:-1]] if len(atr) else atr
    ks = ks[np.isfinite(typ[ks]) & (typ[ks] > 0) & np.isfinite(prev_atr[ks])]
    candles = []
    if len(ks):
        X, names, _ = F.features(cx, b.ts[ks])
        if list(names) != list(fz["names"]):
            raise PredictorError("INPUTS_CHANGED", "The live inputs differ from the trained ones; re-run the analysis.")
        slot = b.slot[ks]
        p = {k: mo.predict(X) for k, mo in fz["models"].items()}
        p["similar"] = F._similar(*fz["sim_up"], X, slot, "binary")
        s = {k: mo.predict(X) for k, mo in fz["size_models"].items()}
        s["similar"] = F._similar(*fz["sim_size"], X, slot, "real")
        pu, ps = p[fz["up_chosen"]], s[fz["size_chosen"]]
        q = fz["size_q"]
        for n, k in enumerate(ks):
            t = int(b.ts[k])
            done = t + 15 * MIN_NS <= last_ns + MIN_NS
            ty = float(typ[k])
            row = {"t": t // NS, "p_up": _f(pu[n]), "p_up_base": fz["p_base"], "size_pts": _f(ty * np.exp(ps[n])),
                   "range50": None if q is None else [_f(ty * np.exp(ps[n] + q[1])), _f(ty * np.exp(ps[n] + q[2]))],
                   "range80": None if q is None else [_f(ty * np.exp(ps[n] + q[0])), _f(ty * np.exp(ps[n] + q[3]))],
                   "usual_pts": _f(ty), "complete": bool(done),
                   "models": {k2: _f(v[n]) for k2, v in p.items()}}
            if done:
                row.update(actual_up=int(np.sign(b.c[k] - b.o[k])), actual_pts=_f(b.h[k] - b.l[k]))
            row.update(o=_f(b.o[k]), h=_f(b.h[k]), l=_f(b.l[k]), c=_f(b.c[k]))
            candles.append(row)
    levelmaps = []
    if fz.get("lm") is not None:
        bl = L.build(cx, start_ns=day0)
        if bl.get("n_dec"):
            end_ns = scheduled_end_ns(m, today)
            if end_ns is not None and last_ns + MIN_NS < end_ns:              # today is not over: the time left in the session
                cur = bl["dec"]["day"] == today.astype("datetime64[D]")       # is the SCHEDULED one, not "until the last minute seen"
                bl["dec"]["minutes_left"] = np.where(cur, (end_ns - MIN_NS - bl["dec"]["t"]) / MIN_NS, bl["dec"]["minutes_left"])
            preds = L.predict_frozen(fz["lm"], bl)
            ch = fz["lm_chosen"]
            q2 = {t: preds[t][ch[t]][:, None] + fz["lm_bands"][t][None, :] for t in ("land2h", "land")}
            workdir.mkdir(parents=True, exist_ok=True)
            path = workdir / f"levelmap_live_{threading.get_ident()}.npz"
            L.save(path, bl, preds, q2)
            with np.load(path) as z:
                lm = {k2: z[k2] for k2 in z.files}
            path.unlink(missing_ok=True)
            day_end = int(m.ts[-1]) + MIN_NS
            for dec in L.day_levels(lm, day0, day_end + 86_400 * NS, ch):
                levelmaps.append(_live_outcomes(dec, last_ns))
    return {"date": str(today.astype("datetime64[D]")), "last_minute": last_ns // NS, "candles": candles,
            "levelmaps": levelmaps, "minutes": int(len(m)), "first_minute": int(m.ts[0]) // NS,
            "es_minutes": 0 if es is None else int(len(es))}


def _live_outcomes(dec: dict, last_ns: int) -> dict:
    """Keep an outcome only once it is known at ``last_ns`` (a touch already seen, or the horizon over)."""
    t = dec["t"]
    two_h_over = t + 2 * 3600 * NS <= last_ns + MIN_NS
    out = {k: v for k, v in dec.items() if k not in ("up_first",)}
    lv = []
    for x in dec["levels"]:
        y = {k: v for k, v in x.items() if k not in ("reached2h", "reached", "reacted", "touch_ns")}
        touched = x.get("touch_ns") is not None and x["touch_ns"] <= last_ns
        y["touched_at"] = x["touch_ns"] // NS if touched else None
        y["within2h"] = True if (touched and x["touch_ns"] <= t + 2 * 3600 * NS) else (False if two_h_over else None)
        lv.append(y)
    out["levels"] = lv
    land = {}
    for k, v in dec["land"].items():
        horizon_over = two_h_over if k == "land2h" else False
        land[k] = {**v, "actual": v["actual"] if horizon_over else None}
    out["land"] = land
    out["t"] = t // NS
    return out


class LivePredictor:
    """One per app: trains in the background once, then recomputes the live view at most every REFRESH_SECONDS while
    a chart asks for it."""

    def __init__(self, svc, minutes_source=None, now=None):
        self.svc = svc
        self.minutes_source = minutes_source              # (code, start, end) -> DataFrame (default: the Charts feed)
        self.now = now or (lambda: datetime.now(timezone.utc))
        self.lock = threading.Lock()
        self.frozen: dict | None = None
        self.state, self.step, self.error = "idle", None, None
        self.result: dict | None = None
        self.computed_at = 0.0
        self.thread: threading.Thread | None = None
        self.skill: dict = {}

    def _minutes(self, code: str, start: datetime, end: datetime):
        if self.minutes_source is not None:
            return self.minutes_source(code, start, end)
        return self.svc._charts_feed().minutes(code, start, end)

    def compute(self) -> dict:
        """Synchronous: (re)train when the analysis changed, then predict the current day."""
        from edgelab.market import analysis as A
        from edgelab.market import news as N
        summary = A.latest(self.svc.data_root)
        if not summary:
            raise PredictorError("NO_ANALYSIS", "Run the analysis in Market simulator → Start here first; the predictor uses "
                                                "its models.")
        if self.frozen is None or self.frozen["key"] != summary["key"]:
            if self.result is None:
                self.state = "training"
            self.frozen = train(self.svc, summary, progress=self._set_step)
            self.skill = skill_of(self.svc)
        if self.result is None:
            self.state = "computing"
        self._set_step("Downloading the live NQ and ES minutes")
        now = self.now()
        start = now - timedelta(days=HISTORY_DAYS)
        drop = (int(now.timestamp()) // 60) * MIN_NS                       # the minute still forming is not used
        nq = minutes_frame(self._minutes("E_NQ-100", start, now + timedelta(minutes=1)), drop)
        if nq is None or len(nq) < 2000:
            raise PredictorError("NO_PRICES", "Too few live NQ minutes (the price download failed or the market has been closed).")
        es = minutes_frame(self._minutes("E_SandP-500", start, now + timedelta(minutes=1)), drop)
        news = N.events(self.svc.data_root, int(nq.ts[0]), int(nq.ts[-1]) + 86_400 * NS) if self.frozen["ok_news"] else []
        self._set_step("Predicting today with the frozen models")
        res = predict_live(self.frozen, nq, es, news, Path(self.svc.data_root) / "charts" / "predictor", int(now.timestamp() * NS))
        caveats = []
        if self.frozen["ok_news"]:
            last_ev = max((e["ts"] for e in news), default=0)
            if last_ev < int(nq.ts[-1]):
                caveats.append("The news calendar ends before today: news inputs are missing (update the news in Market "
                               "simulator → Start here).")
        else:
            caveats.append("No news calendar: the models were trained without news, so none is used live either.")
        if es is None:
            caveats.append("No live ES minutes: the NQ vs ES inputs are missing.")
        res.update(analysis_key=summary["key"], computed_at=now.isoformat(), caveats=caveats, skill=self.skill,
                   chosen={"up": self.frozen["up_chosen"], "size": self.frozen["size_chosen"], "levelmap": self.frozen.get("lm_chosen")},
                   has_levelmap=self.frozen.get("lm") is not None, trained_on="discovery (all of it), frozen")
        return res

    def _set_step(self, s: str) -> None:
        self.step = s

    def _run(self) -> None:
        try:
            res = self.compute()
            with self.lock:
                self.result, self.error, self.state = res, None, "ready"
        except PredictorError as e:
            with self.lock:
                self.error, self.state = {"code": e.code, "message": e.message}, "error"
        except Exception as e:                                             # noqa: BLE001 - shown on the chart
            with self.lock:
                self.error, self.state = {"code": "FAILED", "message": f"{type(e).__name__}: {e}"}, "error"
        finally:
            self.computed_at = _time.time()
            self.step = None
            self.thread = None

    def get(self, symbol: str) -> dict:
        if symbol not in NQ_SYMBOLS:
            return {"available": False, "reason": "The predictor was trained on NQ (with ES as context): it is drawn on NQ and "
                                                  "MNQ charts only."}
        with self.lock:
            stale = _time.time() - self.computed_at > REFRESH_SECONDS
            if self.thread is None and (stale or self.state == "idle"):
                if self.result is None:
                    self.state = "training" if self.frozen is None else "computing"
                self.thread = threading.Thread(target=self._run, daemon=True, name="charts-predictor")
                self.thread.start()
            return {"available": True, "state": self.state, "step": self.step, "error": self.error, "result": self.result}
