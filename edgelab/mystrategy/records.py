"""Trade records and report numbers of My strategy (ADR-93): every trade with its checklist, the levels it used and
the candles of every timeframe it used. Display/report only: nothing here changes a backtest."""
from __future__ import annotations

import math
from typing import Any

import numpy as np
import pandas as pd

from edgelab.analytics.metrics import compute_metrics
from edgelab.mystrategy.frames import LABEL_TF, TF_LABEL, build_base, build_tf

NY = "America/New_York"
TS_KEYS = ("_ts", "t_from")
# candles stored per trade: (timeframe, candles before the trading day's first bar, candles after the exit)
CHART_TFS = {"1m": (0, 60), "2m": (0, 30), "3m": (0, 30), "4m": (0, 30), "5m": (0, 30), "15m": (96, 16),
             "30m": (48, 8), "1h": (120, 6), "4h": (120, 4), "1D": (90, 1)}
DEFAULT_CHART = ("1m", "5m", "15m", "1h", "4h", "1D")


def iso(ns: int | None) -> str | None:
    if ns is None:
        return None
    return pd.Timestamp(int(ns), tz="UTC").isoformat()


def jsonable(x: Any) -> Any:
    """JSON-safe copy; integer timestamps (keys ending in _ts / t_from / ts) become ISO UTC strings."""
    if isinstance(x, dict):
        out = {}
        for k, v in x.items():
            k = str(k)
            if (k.endswith(TS_KEYS) or k == "ts") and isinstance(v, (int, np.integer)) and not isinstance(v, bool):
                out[k] = iso(int(v))
            else:
                out[k] = jsonable(v)
        return out
    if isinstance(x, (list, tuple)):
        return [jsonable(v) for v in x]
    if isinstance(x, np.ndarray):
        return [jsonable(v) for v in x.tolist()]
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else round(float(x), 6)
    if isinstance(x, pd.Timestamp):
        return x.isoformat()
    return x


TRADE_COLS = ("trade_no", "signal_bar", "entry_bar", "exit_bar", "entry_ts", "exit_ts", "direction", "contracts",
              "entry_type", "entry_price_theo", "stop_price", "target_price", "exit_price_theo", "exit_reason",
              "risk_points", "risk_usd", "gross_usd", "cost_usd", "net_usd", "gross_r", "net_r", "mfe_r", "mae_r",
              "holding_minutes", "final_stop_price", "equity_before")


def trade_rows(trades: pd.DataFrame) -> list[dict]:
    out = []
    for r in trades.to_dict("records"):
        out.append({k: (r[k].isoformat() if isinstance(r.get(k), pd.Timestamp) else r.get(k))
                    for k in TRADE_COLS if k in r})
    return jsonable(out)


def charts_used(expl: dict) -> list[str]:
    tfs = set(DEFAULT_CHART)
    conf = (expl or {}).get("confirmation") or {}
    if conf.get("tf"):
        tfs.add(conf["tf"])
    for z in (expl or {}).get("key_levels") or []:
        tfs.add(z["tf"])
    return sorted(tfs, key=lambda t: LABEL_TF[t])


class Charts:
    """Candles of every timeframe built from the run's bars (the same series the rules read)."""

    def __init__(self, bars, calendar, series: str):
        self.b = build_base(bars, calendar, series)
        self.tf = {}

    def _t(self, label: str):
        tf = LABEL_TF[label]
        if tf not in self.tf:
            self.tf[tf] = build_tf(self.b, tf)
        return self.tf[tf]

    def day_start(self, i: int) -> int:
        td = self.b.td
        j = int(np.searchsorted(td, td[i]))
        return j

    def window(self, label: str, i_from: int, i_to: int, until: int | None = None) -> list[list]:
        """Candles of ``label`` covering 1m bars [i_from, i_to] plus the configured margins. With ``until`` (holdout
        review) nothing after that 1m bar is shown: the candle containing it is rebuilt from bars <= until."""
        t = self._t(label)
        before, after = CHART_TFS[label]
        a = max(0, int(t.of_bar[i_from]) - before)
        hi_bar = i_to if until is None else min(i_to, until)
        z = min(len(t) - 1, int(t.of_bar[hi_bar]) + (after if until is None else 0))
        o, h, l, c = t.o[a:z + 1].copy(), t.h[a:z + 1].copy(), t.l[a:z + 1].copy(), t.c[a:z + 1].copy()
        if until is not None and z >= a:
            st = int(t.start[z])
            seg = slice(st, until + 1)
            o[-1], h[-1], l[-1], c[-1] = self.b.o[st], self.b.h[seg].max(), self.b.l[seg].min(), self.b.c[until]
        t0 = t.t0[a:z + 1] // 1_000_000_000
        return [[int(t0[k]), round(float(o[k]), 4), round(float(h[k]), 4), round(float(l[k]), 4), round(float(c[k]), 4)]
                for k in range(len(t0))]

    def for_trade(self, tfs: list[str], signal_bar: int, exit_bar: int | None, until: int | None = None) -> dict:
        ds = self.day_start(signal_bar)
        end = exit_bar if exit_bar is not None else signal_bar
        return {lbl: self.window(lbl, ds, end, until) for lbl in tfs}


def monthly(trades: pd.DataFrame) -> list[dict]:
    if trades.empty:
        return []
    t = trades.copy()
    t["month"] = pd.to_datetime(t["exit_ts"], utc=True).dt.tz_convert(NY).dt.strftime("%Y-%m")
    g = t.groupby("month")
    out = []
    for m, x in g:
        r = x["net_r"].to_numpy(float)
        out.append({"month": m, "trades": int(len(x)), "wins": int((r > 0).sum()), "net_r": round(float(r.sum()), 4),
                    "net_usd": round(float(x["net_usd"].sum()), 2)})
    return out


def weekly_counts(trades: pd.DataFrame, start, end) -> dict:
    """Trades per NY calendar week across the tested window (weeks without trades count as 0)."""
    s, e = pd.Timestamp(start).tz_convert(NY), pd.Timestamp(end).tz_convert(NY)
    weeks = pd.period_range(s.tz_localize(None), e.tz_localize(None), freq="W-SUN")
    counts = pd.Series(0, index=weeks.astype(str))
    if not trades.empty:
        w = pd.to_datetime(trades["entry_ts"], utc=True).dt.tz_convert(NY).dt.tz_localize(None).dt.to_period("W-SUN")
        vc = w.astype(str).value_counts()
        counts = counts.add(vc, fill_value=0)
    arr = counts.to_numpy(float)
    if not len(arr):
        return {}
    return {"weeks": int(len(arr)), "mean": round(float(arr.mean()), 3), "median": float(np.median(arr)),
            "weeks_with_0": int((arr == 0).sum()), "weeks_3_to_5": int(((arr >= 3) & (arr <= 5)).sum()),
            "weeks_above_5": int((arr > 5).sum()), "max": int(arr.max())}


def report_numbers(trades: pd.DataFrame, start, end, sample_thresholds=None) -> dict:
    met = compute_metrics(trades, sample_thresholds=sample_thresholds, span=(start, end))
    months = monthly(trades)
    met["months_total"] = len(months)
    met["months_losing"] = sum(1 for m in months if m["net_r"] < 0)
    met["months_winning"] = sum(1 for m in months if m["net_r"] > 0)
    if not trades.empty:
        planned = (trades["target_price"] - trades["entry_price_theo"]).abs() / trades["risk_points"]
        met["avg_planned_rr"] = float(planned.mean())
        rr = trades.loc[trades["net_r"] > 0, "net_r"]
        met["avg_win_r"] = float(rr.mean()) if len(rr) else None
        met["breakeven_exits"] = int(((trades["net_r"].abs() < 0.2) & (trades["exit_reason"].str.contains("TRAIL"))).sum())
    met["weekly"] = weekly_counts(trades, start, end)
    return jsonable({"metrics": met, "monthly": months})
