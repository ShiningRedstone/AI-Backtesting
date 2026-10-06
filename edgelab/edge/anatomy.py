"""Trade anatomy (ADR-104): what the trades of one My strategy report really did. Read-only, from the stored trade records.

* gross R per trade (before costs) with a bootstrap interval: does the setup know the direction at all?
* costs per trade in R, net R per trade;
* how far trades went in their favour / against them while open (MFE / MAE, bar resolution): did losers first show a
  profit (an exit problem) or never move (a signal problem)?
* the same split by direction, model and exit;
* (ADR-105) the chance after correcting for the number of tries: a discovery report is the best of every My strategy /
  autotuner try on the same period, so its p-value is multiplied by the number of counted tries (Bonferroni).
"""
from __future__ import annotations

import math

import numpy as np

from edgelab.edge import stats as ST

SEED = 104
ALPHA = 0.05
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


def selection(block: dict, tries: dict | None) -> dict | None:
    """The chance of the gross (and net) result after correcting for ``tries['n']`` tries on the same period.
    Two-sided normal p of the mean (n >= 30), times the number of tries (Bonferroni; strict when the tries are similar
    to each other, so the true correction lies between 1 and n)."""
    if tries is None:
        return None
    if not tries.get("applies", True):
        return {"applies": False, "reason": tries.get("reason")}
    n_tries = max(1, int(tries.get("n") or 0))
    out = {"applies": True, "tries": n_tries, "by": tries.get("by", []),
           "t_needed": ST.z_two_sided(ALPHA / n_tries), "t_needed_one": ST.z_two_sided(ALPHA)}
    for k in ("gross_r", "net_r"):
        t = (block.get(k) or {}).get("t")
        if t is None:
            out[k] = None
            continue
        p = math.erfc(abs(t) / math.sqrt(2))
        out[k] = {"t": t, "p": p, "p_corrected": min(1.0, p * n_tries)}
    return out


def analyse(trades: list[dict], explanations: dict | None = None, tries: dict | None = None) -> dict:
    """``trades``: stored trade records (gross_r, net_r, mfe_r, mae_r, direction, exit_reason, model). ``tries``:
    {n, by} of the protocols the report was chosen among, or {applies: False, reason} (a holdout look)."""
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
    out["selection"] = selection(out["all"], tries)
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
    sel = a.get("selection")
    if code in ("POSITIVE", "COSTS") and sel and sel.get("applies") and sel["tries"] > 1 and sel.get("gross_r") \
            and sel["gross_r"]["p_corrected"] >= ALPHA:
        g = sel["gross_r"]
        lines = [f"Before correcting for your {sel['tries']:,} tries the setups seemed to know the direction "
                 f"(t = {g['t']:.1f}). But this report is the best of {sel['tries']:,} tries on the same period, and "
                 f"the best of that many random tries reaches this by chance: after the correction the chance is "
                 f"{g['p_corrected']:.0%} (it would need t of at least {sel['t_needed']:.1f}).",
                 "Treat it as no evidence: a result chosen as the best of many usually falls apart on new data."]
        code = "SELECTION"
    lr = ex["reached"]["1.0"]["losers"]
    if lr is not None and lr >= 0.3:
        lines.append(f"{lr:.0%} of the losing trades were 1 R in profit at some point: the exits give back a lot.")
    if ex["losers_never_moved"] is not None and ex["losers_never_moved"] >= 0.5:
        lines.append(f"{ex['losers_never_moved']:.0%} of the losing trades never got 0.25 R in profit: they were wrong from "
                     "the start.")
    return {"code": code, "text": " ".join(lines), "lines": lines}


TRY_ROLES = (("my_strategy", "My strategy backtests"), ("my_autotune", "Old autotuner (10,000 combinations)"),
             ("my_autotune_flip", "Old autotuner, flipped entries"), ("my_optimizer", "Strategy autotuner"))


def tries_of(svc, kind: str | None) -> dict:
    """Every counted try of My strategy and its autotuners under the active research protocol (active AND retired
    protocols: a try stays a try). A holdout report is one look at unseen data: no correction applies to it."""
    from edgelab.mystrategy import runner as R
    if kind and kind != "discovery_backtest":
        return {"applies": False, "reason": "A holdout result is one look at data the tries never saw: no correction "
                                            "for the number of tries applies (the tries chose the strategy before)."}
    parent = R._parent(svc)
    if parent is None:
        return {"applies": False, "reason": "No active research protocol: the tries cannot be counted."}
    names = dict(TRY_ROLES)
    by: dict = {}
    for p in svc.store.list_protocols():
        role = p["material"].get("role")
        if role in names and (p["material"].get("parent") or {}).get("protocol_id") == parent["protocol_id"]:
            by[role] = by.get(role, 0) + svc.store.count_trials(p["protocol_id"])
    rows = [{"role": r, "name": names[r], "tries": by[r]} for r, _ in TRY_ROLES if by.get(r)]
    return {"applies": True, "n": sum(x["tries"] for x in rows), "by": rows}


def for_report(svc, report_id: str) -> dict:
    from edgelab.mystrategy import runner as R
    folder = R._bt_folder(svc, report_id)
    sm = R._read_json(folder / "summary.json") or {}
    docs = R.read_gz(folder / "trades.json.gz", [])
    trades = [{**{k: t.get(k) for k in ("trade_no", "direction", "exit_reason", "gross_r", "net_r", "mfe_r", "mae_r")},
               "model": (t.get("explanation") or {}).get("model")} for t in docs]
    out = analyse(trades, tries=tries_of(svc, sm.get("kind")))
    out["report"] = {k: sm.get(k) for k in ("id", "label", "kind", "created_at", "window", "trade_count")}
    return out
