"""Trade anatomy (ADR-104): what the trades of one My strategy report really did. Read-only, from the stored trade records.

* gross R per trade (before costs) with a bootstrap interval: does the setup know the direction at all?
* costs per trade in R, net R per trade;
* how far trades went in their favour / against them while open (MFE / MAE, bar resolution): did losers first show a
  profit (an exit problem) or never move (a signal problem)?
* the same split by direction, model and exit.
"""
from __future__ import annotations

import numpy as np

from edgelab.edge import stats as ST

SEED = 104
MFE_LEVELS = (0.5, 1.0, 1.5, 2.0)


def _f(x):
    try:
        v = float(x)
    except (TypeError, ValueError):
        return np.nan
    return v if np.isfinite(v) else np.nan


def _block(rows: list[dict]) -> dict:
    g = np.array([_f(r.get("gross_r")) for r in rows])
    n_ = np.array([_f(r.get("net_r")) for r in rows])
    ok = np.isfinite(g) & np.isfinite(n_)
    g, n_ = g[ok], n_[ok]
    out = {"n": int(len(g))}
    if len(g) == 0:
        return out
    out.update(gross_r=ST.summary(g), net_r=ST.summary(n_), cost_r=float(np.mean(g - n_)),
               gross_win=float(np.mean(g > 0)), net_win=float(np.mean(n_ > 0)))
    if len(g) >= 2:
        out["gross_ci95"] = ST.bootstrap_ci(g, 0.95, SEED)
        out["net_ci95"] = ST.bootstrap_ci(n_, 0.95, SEED + 1)
    return out


def analyse(trades: list[dict], explanations: dict | None = None) -> dict:
    """``trades``: stored trade records (gross_r, net_r, mfe_r, mae_r, direction, exit_reason, model)."""
    rows = [t for t in trades if t.get("gross_r") is not None and t.get("net_r") is not None]
    out: dict = {"all": _block(rows)}
    if not rows:
        out["verdict"] = {"code": "NO_TRADES", "text": "This report has no trades."}
        return out
    mfe = np.array([_f(t.get("mfe_r")) for t in rows])
    mae = np.array([_f(t.get("mae_r")) for t in rows])
    net = np.array([_f(t.get("net_r")) for t in rows])
    losers, winners = net < 0, net > 0
    ex = {}
    for lv in MFE_LEVELS:
        ex[str(lv)] = {"all": float(np.nanmean(mfe >= lv)),
                       "losers": float(np.nanmean(mfe[losers] >= lv)) if losers.any() else None}
    out["excursion"] = {
        "reached": ex,
        "never_moved": float(np.nanmean(mfe < 0.25)),                   # never 0.25 R in favour
        "losers_never_moved": float(np.nanmean(mfe[losers] < 0.25)) if losers.any() else None,
        "winners_heat_half_r": float(np.nanmean(mae[winners] >= 0.5)) if winners.any() else None,
        "mfe_hist": ST.histogram(mfe[np.isfinite(mfe)].clip(0, 5), 25),
        "mae_hist": ST.histogram(mae[np.isfinite(mae)].clip(0, 3), 25),
        "median_mfe_losers": float(np.nanmedian(mfe[losers])) if losers.any() else None,
        "median_mfe_winners": float(np.nanmedian(mfe[winners])) if winners.any() else None,
    }
    by = {}
    for key, fn in (("direction", lambda t: "Long" if (t.get("direction") or 0) > 0 else "Short"),
                    ("model", lambda t: {"ny_4step": "NY four-step", "judas": "Judas swing"}.get(t.get("model"),
                                                                                                 t.get("model") or "-")),
                    ("exit", lambda t: t.get("exit_reason") or "-")):
        groups: dict = {}
        for t in rows:
            groups.setdefault(fn(t), []).append(t)
        by[key] = [{"group": k, **_block(v)} for k, v in sorted(groups.items(), key=lambda kv: -len(kv[1]))]
    out["by"] = by
    out["verdict"] = verdict(out)
    return out


def verdict(a: dict) -> dict:
    al = a["all"]
    ci = al.get("gross_ci95")
    ex = a["excursion"]
    if al["n"] < 30 or ci is None:
        return {"code": "TOO_FEW", "text": f"Only {al['n']} trades: too few to judge."}
    if ci[0] <= 0 <= ci[1]:
        lines = ["No evidence the setups know the direction: the average result BEFORE costs is within chance "
                 "(its 95 % range includes 0).",
                 "Better exits cannot create an edge that is not in the entries; costs then make the result negative."]
        code = "NO_DIRECTION"
    elif ci[1] < 0:
        lines = ["Before costs the trades lose on average more than chance explains: the setups point the wrong way, or "
                 "the exits systematically cut winners / let losers run."]
        code = "WRONG_WAY"
    elif al["net_ci95"] and al["net_ci95"][0] > 0:
        lines = ["Profitable before AND after costs on this period (95 % ranges above 0). Still a discovery result: "
                 "test it once on the holdout or forward."]
        code = "POSITIVE"
    else:
        lines = ["The setups know something before costs, but costs take it: fewer contracts per R (wider stops) or "
                 "fewer trades would lower the cost per trade."]
        code = "COSTS"
    lr = ex["reached"]["1.0"]["losers"]
    if lr is not None and lr >= 0.3:
        lines.append(f"{lr:.0%} of the losing trades were 1 R in profit at some point: the exits give back a lot.")
    if ex["losers_never_moved"] is not None and ex["losers_never_moved"] >= 0.5:
        lines.append(f"{ex['losers_never_moved']:.0%} of the losing trades never got 0.25 R in profit: they were wrong from "
                     "the start.")
    return {"code": code, "text": " ".join(lines), "lines": lines}


def for_report(svc, report_id: str) -> dict:
    from edgelab.mystrategy import runner as R
    folder = R._bt_folder(svc, report_id)
    sm = R._read_json(folder / "summary.json") or {}
    docs = R.read_gz(folder / "trades.json.gz", [])
    trades = [{**{k: t.get(k) for k in ("trade_no", "direction", "exit_reason", "gross_r", "net_r", "mfe_r", "mae_r")},
               "model": (t.get("explanation") or {}).get("model")} for t in docs]
    out = analyse(trades)
    out["report"] = {k: sm.get(k) for k in ("id", "label", "kind", "created_at", "window", "trade_count")}
    return out
