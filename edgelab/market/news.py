"""Economic calendar for the market simulator (ADR-106): downloaded from the JBlanked News API (source "forex-factory"
by default: Forex Factory's calendar incl. its red / orange / yellow impact folders). Read-only research input; never a
dataset, never a trade signal by itself.

* The API key is the user's own and lives in the per-user app settings file (outside the workspace, never in the
  repository, never sent back to the page).
* One request (``/news/api/<source>/full-list/``) returns every event with its history. The raw response is kept as
  downloaded (SHA-256 recorded); the parsed table is rebuilt from it.
* The TIME ZONE of the timestamps is not assumed: it is proven from events with fixed New York release times (CPI,
  payrolls, jobless claims at 8:30, FOMC statements at 14:00) across daylight-saving changes. If no candidate zone puts
  >= 90 % of them on their known time, the calendar is refused (NEWS_TIMEZONE_UNPROVEN).
* LIVE knowledge: an event's time, impact and forecast are known in advance; its ACTUAL value only from its release
  minute on. The surprise is (actual - forecast) divided by the spread of that event's EARLIER surprises.
"""
from __future__ import annotations

import hashlib
import json
import re
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

import numpy as np
import pandas as pd

NY = "America/New_York"
API = "https://www.jblanked.com/news/api/{source}/full-list/"
SOURCES = ("forex-factory", "mql5", "fxstreet")
DEFAULT_SOURCE = "forex-factory"
SETTINGS_KEY = "jblanked_api_key"
IMPACT_RANK = {"high": 3, "medium": 2, "low": 1}
IMPACT_WORD = {3: "High (red)", 2: "Medium (orange)", 1: "Low (yellow)", 0: "None"}
ANCHORS = (                                    # (pattern on the event name, New York release minute)
    (r"non-?farm (employment change|payrolls)", 8 * 60 + 30),
    (r"^(core )?cpi (m/m|y/y)", 8 * 60 + 30),
    (r"(initial )?(unemployment|jobless) claims", 8 * 60 + 30),
    (r"^unemployment rate", 8 * 60 + 30),
    (r"^(core )?ppi m/m", 8 * 60 + 30),
    (r"^(core )?retail sales m/m", 8 * 60 + 30),
    (r"fomc statement|federal funds rate", 14 * 60),
)
MIN_ANCHORS = 20
MIN_MATCH = 0.90
SURPRISE_HISTORY = 6


class NewsError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def home(data_root) -> Path:
    p = Path(data_root) / "market" / "news"
    p.mkdir(parents=True, exist_ok=True)
    return p


# =============================================================================================== API key
def key_status() -> dict:
    from edgelab import runtime
    k = runtime.load_settings().get(SETTINGS_KEY) or ""
    return {"set": bool(k), "hint": (k[:3] + "…" + k[-3:]) if len(k) >= 8 else None}


def set_key(key: str | None) -> dict:
    from edgelab import runtime
    k = (key or "").strip()
    if k and (len(k) < 30 or not re.fullmatch(r"[A-Za-z0-9._\-]+", k)):
        raise NewsError("NEWS_KEY_FORMAT", "That does not look like a JBlanked API key (30+ letters and digits).")
    runtime.save_settings({SETTINGS_KEY: k or None})
    return key_status()


def _key() -> str:
    from edgelab import runtime
    k = runtime.load_settings().get(SETTINGS_KEY)
    if not k:
        raise NewsError("NEWS_KEY_MISSING", "Enter your JBlanked API key first (free account at jblanked.com).")
    return k


# =============================================================================================== download
def http_fetch(url: str, key: str, timeout: float = 60.0) -> bytes:
    req = urllib.request.Request(url, headers={"Authorization": f"Api-Key {key}", "Content-Type": "application/json",
                                               "User-Agent": "MunyunLab"})
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - fixed https host
        return r.read()


def download(data_root, source: str = DEFAULT_SOURCE, fetch: Callable[[str, str], bytes] | None = None) -> dict:
    """Fetch the full event list once, keep the raw bytes (content-addressed) and rebuild the parsed calendar."""
    if source not in SOURCES:
        raise NewsError("NEWS_SOURCE", f"Unknown news source '{source}'.")
    fetch = fetch or http_fetch
    try:
        raw = fetch(API.format(source=source), _key())
    except NewsError:
        raise
    except Exception as e:                                         # network, HTTP 401/429 ... reported as is
        raise NewsError("NEWS_DOWNLOAD_FAILED", f"The download failed: {type(e).__name__}: {e}") from e
    sha = hashlib.sha256(raw).hexdigest()
    rawdir = home(data_root) / "raw"
    rawdir.mkdir(exist_ok=True)
    p = rawdir / f"{source}_{sha[:16]}.json"
    if not p.exists():
        p.write_bytes(raw)
    meta = {"source": source, "raw_file": p.name, "sha256": sha, "bytes": len(raw),
            "downloaded_at": datetime.now(timezone.utc).isoformat()}
    cal = build(raw, meta)
    _write(home(data_root) / "calendar.json", cal)
    return status(data_root)


def _write(path: Path, obj) -> None:
    from edgelab.core.fsutil import atomic_write_text
    atomic_write_text(path, json.dumps(obj))


# =============================================================================================== parsing
def number(x) -> float | None:
    """'3.1%', '250K', '-1.2M', '0.25B', 3.1 -> float (K/M/B/T scale kept: 250K -> 250000). None if absent."""
    if x is None:
        return None
    if isinstance(x, (int, float)):
        return float(x) if np.isfinite(x) else None
    s = str(x).strip().replace(",", "")
    if not s or s.lower() in ("n/a", "na", "none", "null", "-", "tentative"):
        return None
    m = re.fullmatch(r"([<>]?)\s*(-?\d+(?:\.\d+)?)\s*([%KMBT]?)", s, flags=re.I)
    if not m:
        return None
    v = float(m.group(2))
    return v * {"": 1, "%": 1, "K": 1e3, "M": 1e6, "B": 1e9, "T": 1e12}[m.group(3).upper()]


def parse_full_list(obj) -> list[dict]:
    """Rows {currency, event_id, name, category, impact, local (naive 'YYYY-MM-DD HH:MM:SS'), actual, forecast,
    previous} from a full-list response. Unknown fields are ignored; malformed rows are counted, never repaired."""
    rows: list[dict] = []
    if not isinstance(obj, dict):
        raise NewsError("NEWS_FORMAT", "The response is not a JSON object of currencies.")
    for cur, block in obj.items():
        events = (block or {}).get("Events") if isinstance(block, dict) else None
        if not isinstance(events, list):
            continue
        for ev in events:
            if not isinstance(ev, dict) or "Name" not in ev:
                continue
            base = {"currency": str(cur).upper(), "event_id": str(ev.get("Event_ID", "")), "name": str(ev["Name"]),
                    "category": ev.get("Category") or "", "impact": str(ev.get("Impact") or "None")}
            for hi in ev.get("History") or []:
                if not isinstance(hi, dict) or not hi.get("Date"):
                    continue
                rows.append({**base, "local": str(hi["Date"]), "actual": number(hi.get("Actual")),
                             "forecast": number(hi.get("Forecast")), "previous": number(hi.get("Previous"))})
    return rows


def _naive(local: list[str]) -> pd.DatetimeIndex:
    return pd.to_datetime(pd.Series(local).str.replace(".", "-", regex=False), format="%Y-%m-%d %H:%M:%S",
                          errors="coerce").pipe(pd.DatetimeIndex)


ZONES = tuple([f"UTC{h:+d}" for h in range(-12, 15)] + ["America/New_York", "NY+7 (trading-server time)",
                                                        "Europe/London", "Europe/Athens"])


def to_utc(naive: pd.DatetimeIndex, zone: str) -> pd.DatetimeIndex:
    if zone.startswith("UTC"):
        return (naive - pd.Timedelta(hours=int(zone[3:]))).tz_localize("UTC")
    if zone.startswith("NY+7"):
        return (naive - pd.Timedelta(hours=7)).tz_localize(NY, ambiguous="NaT", nonexistent="NaT").tz_convert("UTC")
    return naive.tz_localize(zone, ambiguous="NaT", nonexistent="NaT").tz_convert("UTC")


def prove_timezone(rows: list[dict]) -> dict:
    """Which zone puts the fixed-time USD events on their known New York minute? Returns the evidence; ``zone`` is None
    when nothing is proven."""
    anchors = []
    for i, r in enumerate(rows):
        if r["currency"] != "USD":
            continue
        nm = r["name"].strip().lower()
        for pat, minute in ANCHORS:
            if re.search(pat, nm):
                anchors.append((i, minute))
                break
    out = {"anchors": len(anchors), "zone": None, "candidates": []}
    if len(anchors) < MIN_ANCHORS:
        out["reason"] = f"Only {len(anchors)} fixed-time events (CPI, payrolls, claims, FOMC ...) found; {MIN_ANCHORS} needed."
        return out
    naive = _naive([rows[i]["local"] for i, _ in anchors])
    want = np.array([m for _, m in anchors])
    seen: dict = {}
    for z in ZONES:
        ny = to_utc(naive, z).tz_convert(NY)
        got = np.asarray(ny.hour * 60 + ny.minute, dtype=float)
        got[np.asarray(ny.isna())] = np.nan
        share = float(np.nanmean(got == want)) if np.isfinite(got).any() else 0.0
        sig = tuple(np.nan_to_num(got, nan=-1).astype(int).tolist())
        if sig in seen:                                            # an equivalent zone (same New York times)
            continue
        seen[sig] = z
        out["candidates"].append({"zone": z, "match": share})
    out["candidates"].sort(key=lambda c: -c["match"])
    best = out["candidates"][0]
    second = out["candidates"][1]["match"] if len(out["candidates"]) > 1 else 0.0
    if best["match"] >= MIN_MATCH and (best["match"] - second) * len(anchors) >= 3:     # >= 3 more events on time
        out["zone"], out["match"] = best["zone"], best["match"]
    else:
        out["reason"] = (f"No time zone puts {MIN_MATCH:.0%} of the {len(anchors)} fixed-time events on their known New "
                         f"York time (best: {best['zone']} with {best['match']:.0%}).")
    out["candidates"] = out["candidates"][:5]
    return out


def build(raw: bytes, meta: dict) -> dict:
    try:
        obj = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as e:
        raise NewsError("NEWS_FORMAT", f"The response is not JSON: {e}") from e
    rows = parse_full_list(obj)
    if not rows:
        raise NewsError("NEWS_EMPTY", "The response holds no events with history.")
    tz = prove_timezone(rows)
    cal = {"meta": meta, "timezone": tz, "rows_total": len(rows), "events": []}
    if tz["zone"] is None:
        cal["refused"] = {"code": "NEWS_TIMEZONE_UNPROVEN", "message": tz.get("reason")}
        return cal
    usd = [r for r in rows if r["currency"] == "USD"]
    naive = _naive([r["local"] for r in usd])
    utc = to_utc(naive, tz["zone"])
    ev = []
    for r, t in zip(usd, utc):
        if pd.isna(t):
            continue
        ev.append({**{k: r[k] for k in ("event_id", "name", "category", "actual", "forecast", "previous")},
                   "impact": IMPACT_RANK.get(r["impact"].strip().lower(), 0), "ts": int(t.value)})
    ev.sort(key=lambda e: (e["ts"], e["event_id"]))
    dedup, seen = [], set()
    for e in ev:
        k = (e["ts"], e["event_id"], e["name"])
        if k not in seen:
            seen.add(k)
            dedup.append(e)
    _surprises(dedup)
    cal["events"] = dedup
    cal["dropped_unmapped_times"] = len(usd) - len(ev)
    return cal


def _surprises(ev: list[dict]) -> None:
    """surprise = actual - forecast; z = surprise / std of the SAME event's earlier surprises (>= 6 of them)."""
    hist: dict = {}
    for e in ev:
        s = None if e["actual"] is None or e["forecast"] is None else e["actual"] - e["forecast"]
        e["surprise"] = s
        past = hist.setdefault(e["event_id"] or e["name"], [])
        e["surprise_z"] = None
        if s is not None and len(past) >= SURPRISE_HISTORY:
            sd = float(np.std(past, ddof=1))
            if sd > 0:
                e["surprise_z"] = float(s / sd)
        if s is not None:
            past.append(s)


# =============================================================================================== reading
def calendar(data_root) -> dict | None:
    try:
        return json.loads((home(data_root) / "calendar.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def events(data_root, start_ns: int | None = None, end_ns: int | None = None, min_impact: int = 0) -> list[dict]:
    """USD events in [start, end] (UTC ns) of at least ``min_impact``; [] if no proven calendar."""
    cal = calendar(data_root)
    if not cal or cal.get("refused") or not cal.get("events"):
        return []
    return [e for e in cal["events"] if (start_ns is None or e["ts"] >= start_ns) and
            (end_ns is None or e["ts"] <= end_ns) and e["impact"] >= min_impact]


def status(data_root) -> dict:
    cal = calendar(data_root)
    out = {"key": key_status(), "sources": list(SOURCES), "default_source": DEFAULT_SOURCE, "downloaded": cal is not None}
    if cal:
        ev = cal.get("events") or []
        out.update(meta=cal["meta"], timezone={k: cal["timezone"].get(k) for k in ("zone", "match", "anchors", "reason",
                                                                                   "candidates")},
                   refused=cal.get("refused"), events=len(ev),
                   high=sum(1 for e in ev if e["impact"] == 3),
                   first=pd.Timestamp(ev[0]["ts"], tz="UTC").isoformat() if ev else None,
                   last=pd.Timestamp(ev[-1]["ts"], tz="UTC").isoformat() if ev else None)
    return out
