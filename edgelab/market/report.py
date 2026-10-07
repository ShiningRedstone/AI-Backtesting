"""The Market simulator's report card (ADR-110): every forecast in one place, read from the saved predictions only.

Per forecast: what it predicts, its baseline, the official model (chosen on the discovery walk-forward), its score on
the discovery months the choice never saw, on the holdout (after a look) and on new days, a calibration table (said vs
happened) and a month-by-month skill series for each source. 'Works' = its discovery score beats the baseline with the
95 % day-bootstrap range above 0. Nothing is recomputed from the market data and nothing is trained here.
"""
from __future__ import annotations

import math

import numpy as np

from edgelab.market import data as D

GROUPS = {"candle": "Next 15-minute candle", "levels": "Levels", "direction": "Direction calls (rest of the candle)",
          "day": "The rest of the day"}
TARGETS = (  # id, group, words, kind, baseline words
    ("size", "candle", "Size of the next 15-min candle", "real", "the usual size for that time of day"),
    ("up", "candle", "Next 15-min candle up or down", "binary", "the usual up-rate"),
    ("levels", "levels", "Reference level reached before the session ends", "binary", "distance only"),
    ("reach2h", "levels", "Level traded within 2 hours (level map)", "binary", "random walk: distance, time, volatility"),
    ("reach", "levels", "Level traded before the session ends (level map)", "binary", "random walk: distance, time, volatility"),
    ("react", "levels", "Price reacts at the level (1 ATR away before 1 ATR through)", "binary", "the usual reaction rate"),
    ("first", "levels", "Nearest level above traded before the nearest below", "binary", "random walk (gambler's ruin)"),
    ("dir0", "direction", "Rest of the candle, called at the open", "binary", "the usual up-rate"),
    ("dir5", "direction", "Rest of the candle, called at minute 5", "binary", "the usual up-rate"),
    ("dir10", "direction", "Rest of the candle, called at minute 10", "binary", "the usual up-rate"),
    ("bias", "day", "Session closes above the current price", "binary", "the usual up-rate"),
    ("land2h", "day", "Where price is 2 hours later", "real", "no change"),
    ("land", "day", "Where price is at the session end", "real", "no change"),
)
LM = ("reach2h", "reach", "react", "first", "land2h", "land")


def _load(path) -> dict | None:
    try:
        with np.load(path) as z:
            return {k: z[k] for k in z.files}
    except (OSError, ValueError):
        return None


def _days(x) -> np.ndarray:
    return np.asarray(x).astype(np.int64).astype("datetime64[D]")


def arrays(tid: str, z: dict | None, model: str) -> dict | None:
    """(p, base, y, day) of one forecast from a saved predictions file (discovery, holdout or new days)."""
    if z is None:
        return None
    try:
        if tid == "up":
            m = z["up"] != 0
            return {"p": z[f"p_up_{model}"][m], "base": z["p_up_baseline"][m], "y": (z["up"][m] > 0).astype(float),
                    "day": _days(z["day"])[m]}
        if tid == "size":
            p, y = z[f"size_{model}"].astype(float), z["size"].astype(float)
            m = np.isfinite(y)
            return {"p": p[m], "base": np.zeros(int(m.sum())), "y": y[m], "day": _days(z["day"])[m]}
        if tid == "levels":
            if "level_t" not in z or not len(z["level_t"]):
                return None
            return {"p": z[f"level_{model}"], "base": z["level_baseline"], "y": z["level_reached"].astype(float),
                    "day": D.trading_day(z["level_t"]).astype("datetime64[D]")}
        if tid in LM:
            dday = _days(z["dec_day"])
            if tid in ("reach2h", "reach", "react"):
                y = z[f"row_{tid}"].astype(float)
                day = dday[z["row_dec"]]
            else:
                y = z[f"dec_{tid}"].astype(float)
                day = dday
            p = z[f"p_{tid}_{model}"].astype(float)
            base = z[f"p_{tid}_baseline"].astype(float) if tid not in ("land2h", "land") else np.zeros(len(y))
            m = np.isfinite(y) & np.isfinite(p)
            return {"p": p[m], "base": base[m], "y": y[m], "day": day[m]}
        if tid.startswith("dir"):
            s = int(tid[3:])
            m = (z["stage"] == s) & (z["y"] != 0) & np.isfinite(z[f"p_{model}"])
            return {"p": z[f"p_{model}"][m].astype(float), "base": z["p_baseline"][m].astype(float),
                    "y": (z["y"][m] > 0).astype(float), "day": _days(z["day"])[m]}
    except KeyError:
        return None
    return None


def _losses(a: dict, kind: str):
    if kind == "binary":
        return (np.clip(a["p"], 0, 1) - a["y"]) ** 2, (np.clip(a["base"], 0, 1) - a["y"]) ** 2
    return np.abs(a["p"] - a["y"]), np.abs(a["base"] - a["y"])


def monthly(a: dict | None, kind: str) -> list[dict]:
    if not a or not len(a["y"]):
        return []
    lm, lb = _losses(a, kind)
    mo = a["day"].astype("datetime64[M]")
    out = []
    for m in np.unique(mo):
        s = mo == m
        if s.sum() < 20 or lb[s].sum() <= 0:
            continue
        row = {"month": str(m), "n": int(s.sum()), "skill": float(1 - lm[s].sum() / lb[s].sum())}
        if kind == "binary":
            row["accuracy"] = float(np.mean((a["p"][s] > 0.5) == (a["y"][s] > 0.5)))
        out.append(row)
    return out


def calibration(a: dict | None) -> list[dict]:
    """Said vs happened in 10 %-wide bins of the predicted probability (bins with 30+ predictions)."""
    if not a or not len(a["y"]):
        return []
    out = []
    p = np.clip(a["p"], 0, 1)
    for lo in np.arange(0, 1, 0.1):
        b = (p >= lo) & (p < lo + 0.1 if lo < 0.9 else p <= 1)
        if b.sum() >= 30:
            out.append({"from": float(round(lo, 1)), "n": int(b.sum()), "said": float(p[b].mean()), "happened": float(a["y"][b].mean())})
    return out


def overall(a: dict | None, kind: str) -> dict | None:
    """Skill with a 95 % range from resampling whole days (same method as forecast.skill_ci)."""
    from edgelab.market import forecast as F
    if not a or len(a["y"]) < 50:
        return None
    lm, lb = _losses(a, kind)
    if lb.sum() <= 0:
        return None
    ci = F.skill_ci(lm, lb, a["day"])
    out = {"n": int(len(a["y"])), "skill": float(1 - lm.sum() / lb.sum()), "skill_ci": ci, "real": bool(ci and ci[0] > 0)}
    if kind == "binary":
        out["accuracy"] = float(np.mean((a["p"] > 0.5) == (a["y"] > 0.5)))
        out["baseline_accuracy"] = float(np.mean((a["base"] > 0.5) == (a["y"] > 0.5)))
    return out


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if math.isfinite(v) else None


def build(svc) -> dict:
    from edgelab.market import analysis as A
    from edgelab.market import direction as DR
    from edgelab.market import holdout as H
    from edgelab.market import newdays as ND
    la = A.latest(svc.data_root)
    if not la:
        raise KeyError("no analysis yet")
    key = la["key"]
    adir = A.home(svc.data_root) / f"analysis_{key}"
    fc, lm = la.get("forecast") or {}, (la.get("levelmap") or {}).get("targets") or {}
    dsum = DR.latest(svc.data_root, key)
    h1, h2 = H.status(svc), DR.holdout_status(svc)
    nfold = ND.folder(svc.data_root, "")
    files = {
        "discovery": {"candle": _load(adir / "forecast.npz"), "lm": _load(adir / "levelmap.npz"), "dir": _load(adir / "direction.npz")},
        "holdout": {"candle": _load(H.home(svc.data_root) / "predictions.npz") if h1.get("used") else None,
                    "lm": _load(H.home(svc.data_root) / "levelmap.npz") if h1.get("used") else None,
                    "dir": _load(DR.holdout_home(svc.data_root) / "predictions.npz") if h2.get("used") else None},
        "new": {"candle": _load(nfold / f"predictions_{key}.npz"), "lm": _load(nfold / f"levelmap_{key}.npz"),
                "dir": _load(nfold / f"direction_{key}.npz")}}
    out = []
    for tid, grp, words, kind, base in TARGETS:
        if tid in ("up", "size", "levels", "bias"):
            ev, fk = fc.get(tid) or {}, "candle"
        elif tid in LM:
            ev, fk = lm.get(tid) or {}, "lm"
        else:
            ev, fk = ((dsum or {}).get("stages") or {}).get(tid[3:]) or {}, "dir"
        model = ev.get("chosen")
        if not model:
            continue
        late = ((ev.get("scores") or {}).get(model) or {}).get("late")
        row = {"id": tid, "group": grp, "group_name": GROUPS[grp], "name": words, "kind": kind, "baseline": base, "model": model,
               "discovery": late, "chosen_on": ev.get("chosen_on"), "reported_on": ev.get("reported_on"),
               "works": bool(late and late.get("real")), "monthly": {}, "sources": {}}
        if tid == "bias":                       # its outcomes are not saved per row: discovery and holdout summary only
            hs = ((h1.get("result") or {}).get("targets") or {}).get("bias")
            row["sources"]["holdout"] = (hs or {}).get("official")
            row["monthly"]["holdout"] = [{"month": x["month"], "n": x["n"], "skill": x["skill"], "accuracy": x.get("accuracy")}
                                         for x in (hs or {}).get("by_month", [])]
            out.append(row)
            continue
        for src in ("discovery", "holdout", "new"):
            a = arrays(tid, files[src][fk], model)
            if a is None:
                continue
            row["monthly"][src] = monthly(a, kind)
            if src == "discovery":
                row["calibration"] = calibration(a) if kind == "binary" else None
            else:
                row["sources"][src] = overall(a, kind)
                if kind == "binary":
                    row.setdefault("calibration_by", {})[src] = calibration(a)
        if kind == "real" and tid == "size":
            row["bands"] = (fc.get("size") or {}).get("bands")
        elif kind == "real":
            row["bands"] = (lm.get(tid) or {}).get("bands")
        if tid.startswith("dir"):
            c = ev.get("calls") or {}
            row["calls"] = {"rule": c.get("rule"), "discovery": c.get("late")}
            hr = ((h2.get("result") or {}).get("stages") or {}).get(tid[3:])
            if hr:
                row["calls"]["holdout"] = hr.get("calls")
            nd = ((ND.scores(svc.data_root, key) or {}).get("direction") or {}).get("stages", {}).get(tid[3:])
            if nd:
                row["calls"]["new"] = nd.get("calls")
        out.append(row)
    nds = ND.scores(svc.data_root, key) or {}
    return {"key": key, "targets": out, "groups": GROUPS,
            "holdout": {"first_used": bool(h1.get("used")), "second_used": bool(h2.get("used")),
                        "window": h1.get("holdout") or h2.get("holdout")},
            "new_days": {"days": len(nds.get("days") or []), "history": nds.get("history"), "computed_at": nds.get("computed_at")},
            "direction_ready": dsum is not None}
