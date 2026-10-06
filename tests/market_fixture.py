"""SYNTHETIC stand-ins for the market simulator's downloads (ADR-106): a JBlanked 'full-list' response and a Dukascopy
minute fetcher. Used by tests and demos only; every value is generated, nothing is real market or news data.

The news timestamps are written in a trading-server clock (New York time + 7 hours, 'NY+7') so the time-zone proof has
to find it from the fixed-time releases (payrolls / CPI / claims at 8:30, FOMC at 14:00 New York)."""
from __future__ import annotations

import json
from datetime import date, datetime, timedelta

import numpy as np
import pandas as pd

NY = "America/New_York"


def _server_time(d: date, hh: int, mm: int, zone: str = "NY+7") -> str:
    t = pd.Timestamp(datetime(d.year, d.month, d.day, hh, mm)).tz_localize(NY)
    if zone == "NY+7":
        local = t.tz_localize(None) + pd.Timedelta(hours=7)
    elif zone == "UTC":
        local = t.tz_convert("UTC").tz_localize(None)
    else:
        raise ValueError(zone)
    return local.strftime("%Y.%m.%d %H:%M:%S")


def news_json(start: str, end: str, zone: str = "NY+7", seed: int = 3) -> bytes:
    rng = np.random.default_rng(seed)
    days = pd.bdate_range(start, end)
    ev = {"nfp": [], "cpi": [], "claims": [], "fomc": [], "ism": []}
    for d in days:
        d = d.date()
        if d.weekday() == 4 and d.day <= 7:
            ev["nfp"].append((d, 8, 30))
        if d.weekday() == 2 and 10 <= d.day <= 16:
            ev["cpi"].append((d, 8, 30))
        if d.weekday() == 3:
            ev["claims"].append((d, 8, 30))
        if d.weekday() == 2 and d.month in (1, 3, 5, 6, 7, 9, 11, 12) and 15 <= d.day <= 21:
            ev["fomc"].append((d, 14, 0))
        if d.day <= 3 and d.weekday() < 5:
            ev["ism"].append((d, 10, 0))

    def hist(items, scale):
        out = []
        prev = 0.0
        for d, hh, mm in items:
            fc = round(float(rng.normal(0, scale)), 2)
            act = round(fc + float(rng.normal(0, scale)), 2)
            out.append({"Date": _server_time(d, hh, mm, zone), "Actual": act, "Forecast": fc, "Previous": prev})
            prev = act
        return out
    usd = [
        {"Name": "Non-Farm Employment Change", "Event_ID": 1, "Category": "Employment", "Impact": "High", "History": hist(ev["nfp"], 50)},
        {"Name": "CPI m/m", "Event_ID": 2, "Category": "Inflation", "Impact": "High", "History": hist(ev["cpi"], 0.2)},
        {"Name": "Unemployment Claims", "Event_ID": 3, "Category": "Employment", "Impact": "Medium", "History": hist(ev["claims"], 10)},
        {"Name": "FOMC Statement", "Event_ID": 4, "Category": "Rates", "Impact": "High", "History": hist(ev["fomc"], 0.1)},
        {"Name": "ISM Manufacturing PMI", "Event_ID": 5, "Category": "Business", "Impact": "High", "History": hist(ev["ism"], 1.0)},
    ]
    obj = {"USD": {"Events": usd}, "EUR": {"Events": [{"Name": "German Ifo", "Event_ID": 99, "Impact": "Medium",
                                                     "History": [{"Date": "2023.01.25 11:00:00", "Actual": "1.0%"}]}]}}
    return json.dumps(obj).encode()


def fake_news_fetch(start="2022-06-01", end="2024-12-31", zone="NY+7"):
    body = news_json(start, end, zone)
    return lambda url, key: body


def fake_minutes(code: str, start: datetime, end: datetime) -> pd.DataFrame:
    """Deterministic random-walk minutes for [start, end) (UTC), different per instrument and day."""
    idx = pd.date_range(pd.Timestamp(start), pd.Timestamp(end), freq="1min", inclusive="left")
    if not len(idx):
        return pd.DataFrame(columns=["open", "high", "low", "close", "volume"])
    seed = int(pd.Timestamp(start).value // 86_400_000_000_000) + (7 if "SandP" in code else 0)
    rng = np.random.default_rng(seed)
    level = 18000.0 if "NQ" in code else 4500.0
    c = level + np.cumsum(rng.normal(0, 2.0 if "NQ" in code else 0.5, len(idx)))
    o = np.r_[c[0], c[:-1]]
    h = np.maximum(o, c) + rng.uniform(0, 1, len(idx))
    lo = np.minimum(o, c) - rng.uniform(0, 1, len(idx))
    return pd.DataFrame({"open": o, "high": h, "low": lo, "close": c, "volume": 1.0}, index=idx)
