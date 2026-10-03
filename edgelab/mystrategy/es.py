"""ES reference prices for My strategy's SMT divergence (ADR-95).

The ES series is a READ-ONLY comparison series: it is never traded, never a research dataset and never part of the
research configuration (adding an instrument to ``configs/`` would change the active protocol's settings fingerprint,
ADR-89). It is validated on import, content-hashed and stored under ``<data>/my_strategy/es/``:

* ``ES_<hash16>.npz``: bar-open timestamps (UTC ns) and OHLC (float64), exactly as in the file.
* ``es.json``: the active series (content hash, source file + SHA-256, range, identity as stated by the user).

Identity (stated by the user on import, never guessed): Dukascopy USA500.IDX/USD (feed E_SandP-500, the S&P 500 index
CFD), 1-minute BID candles, one row per minute with an explicit +00:00 offset, timestamp = bar OPEN. The volume column is
a provider-defined measure and is ignored.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import numpy as np

from edgelab.core.fsutil import atomic_write_text

HEADER = ["timestamp", "open", "high", "low", "close", "volume"]
IDENTITY = {"provider": "DUKASCOPY", "instrument": "USA500.IDX/USD", "feed": "E_SandP-500",
            "description": "S&P 500 index CFD (the ES stand-in, like USATECH.IDX/USD is for NQ)",
            "price_basis": "bid", "timeframe": "1m", "timestamps": "bar open, UTC (explicit +00:00 offset per row)",
            "volume": "ignored (provider-defined measure)", "role": "SMT comparison only; never traded"}
MAX_BYTES = 400 * 1024 * 1024


class EsError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


@dataclass(frozen=True)
class EsSeries:
    ts: np.ndarray            # bar open, UTC ns (strictly increasing)
    high: np.ndarray
    low: np.ndarray
    content_hash: str
    manifest: dict

    def align(self, ts_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """(high, low) per given bar timestamp; NaN where ES has no bar at that exact minute."""
        ts_ns = np.asarray(ts_ns, dtype=np.int64)
        h = np.full(len(ts_ns), np.nan)
        lo = np.full(len(ts_ns), np.nan)
        if len(self.ts) and len(ts_ns):
            j = np.searchsorted(self.ts, ts_ns)
            jj = np.minimum(j, len(self.ts) - 1)
            ok = self.ts[jj] == ts_ns
            h[ok], lo[ok] = self.high[jj[ok]], self.low[jj[ok]]
        return h, lo

    def covers(self, start_ns: int, end_ns: int) -> bool:
        return bool(len(self.ts)) and int(self.ts[0]) <= start_ns and int(self.ts[-1]) >= end_ns


def folder(data_root) -> Path:
    return Path(data_root) / "my_strategy" / "es"


def content_hash(ts: np.ndarray, o, h, lo, c) -> str:
    d = hashlib.sha256(b"my_strategy_es_v1")
    for a in (ts.astype(np.int64), o, h, lo, c):
        d.update(np.ascontiguousarray(a).tobytes())
    return d.hexdigest()


def _file_sha256(path: Path) -> str:
    d = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            d.update(chunk)
    return d.hexdigest()


def read_csv(path: Path) -> dict:
    """Parse and validate the file. Refuses (never repairs) anything that does not match the stated format."""
    import pandas as pd
    if not path.is_file():
        raise EsError("ES_FILE_MISSING", f"No file at {path}.")
    if path.suffix.lower() != ".csv":
        raise EsError("ES_FILE_TYPE", "The ES file must be a .csv file.")
    if path.stat().st_size > MAX_BYTES:
        raise EsError("ES_FILE_TOO_BIG", "The ES file is larger than 400 MB.")
    with open(path, "r", encoding="utf-8") as f:
        head = f.readline().strip().split(",")
    if head != HEADER:
        raise EsError("ES_BAD_HEADER", f"Expected the columns {','.join(HEADER)} (Dukascopy download), found {','.join(head)}.")
    df = pd.read_csv(path, dtype={"timestamp": str}, usecols=HEADER[:5])
    if df.empty:
        raise EsError("ES_EMPTY", "The ES file has no rows.")
    raw = df["timestamp"].astype(str)
    if not raw.str.endswith("+00:00").all():
        bad = raw[~raw.str.endswith("+00:00")].iloc[0]
        raise EsError("ES_TIMEZONE", f"Every timestamp must carry the explicit UTC offset +00:00 (found '{bad}').")
    try:
        ts = pd.to_datetime(raw, format="ISO8601", utc=True)
    except (ValueError, TypeError) as e:
        raise EsError("ES_TIMESTAMP", f"Unreadable timestamp: {e}") from e
    ns = ts.dt.tz_convert("UTC").dt.tz_localize(None).to_numpy().astype("datetime64[ns]").astype(np.int64)
    if (ns % 60_000_000_000).any():
        raise EsError("ES_NOT_1M", "Timestamps must be whole minutes (1-minute bars).")
    if len(ns) > 1 and not (np.diff(ns) > 0).all():
        raise EsError("ES_ORDER", "Timestamps must be strictly increasing without duplicates.")
    px = {k: df[k].to_numpy(dtype=np.float64) for k in ("open", "high", "low", "close")}
    for k, a in px.items():
        if not np.isfinite(a).all() or (a <= 0).any():
            raise EsError("ES_PRICES", f"Column {k} has missing, non-finite or non-positive prices.")
    o, h, lo, c = px["open"], px["high"], px["low"], px["close"]
    if (h < np.maximum(o, c)).any() or (lo > np.minimum(o, c)).any():
        raise EsError("ES_OHLC", "Some bars have a high below the open/close or a low above it.")
    return {"ts": ns, "open": o, "high": h, "low": lo, "close": c}


def import_csv(data_root, path, *, identity_confirmed: bool) -> dict:
    """Validate, hash and store the ES file; it becomes the active ES series. Same content = same id (idempotent)."""
    if identity_confirmed is not True:
        raise EsError("ES_IDENTITY_UNCONFIRMED", "Confirm what the file is (Dukascopy USA500.IDX/USD, 1-minute BID, UTC "
                                                 "bar-open timestamps) before importing it.")
    p = Path(str(path)).expanduser()
    bars = read_csv(p)
    ch = content_hash(bars["ts"], bars["open"], bars["high"], bars["low"], bars["close"])
    es_id = "ES_" + ch[:16].upper()
    out = folder(data_root)
    out.mkdir(parents=True, exist_ok=True)
    npz = out / f"{es_id}.npz"
    if not npz.exists():
        tmp = out / f"{es_id}.tmp.npz"
        np.savez_compressed(tmp, **bars)
        tmp.replace(npz)
    ts = bars["ts"]
    man = {"es_id": es_id, "content_hash": ch, "file": npz.name, "source_file": str(p), "source_sha256": _file_sha256(p),
           "bars": int(len(ts)), "first_bar_open_utc": _iso(ts[0]), "last_bar_open_utc": _iso(ts[-1]),
           "identity": IDENTITY, "identity_status": "user_specified",
           "imported_at": datetime.now(timezone.utc).isoformat()}
    atomic_write_text(out / "es.json", json.dumps(man, indent=1))
    _CACHE.clear()
    return man


def _iso(ns) -> str:
    return datetime.fromtimestamp(int(ns) / 1e9, tz=timezone.utc).isoformat()


def manifest(data_root) -> dict | None:
    try:
        return json.loads((folder(data_root) / "es.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


_CACHE: dict = {}


def load(data_root) -> EsSeries | None:
    """The active ES series (re-hashed on first load in a process; a changed file is refused, never used)."""
    man = manifest(data_root)
    if man is None:
        return None
    key = (str(Path(data_root).resolve()), man["content_hash"])
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    with np.load(folder(data_root) / man["file"]) as z:
        bars = {k: z[k] for k in ("ts", "open", "high", "low", "close")}
    if content_hash(bars["ts"], bars["open"], bars["high"], bars["low"], bars["close"]) != man["content_hash"]:
        raise EsError("ES_HASH_MISMATCH", "The stored ES data no longer matches its content hash. Import the file again.")
    es = EsSeries(bars["ts"], bars["high"], bars["low"], man["content_hash"], man)
    _CACHE.clear()
    _CACHE[key] = es
    return es


def status(data_root) -> dict:
    man = manifest(data_root)
    if man is None:
        return {"imported": False, "identity": IDENTITY}
    return {"imported": True, **{k: man.get(k) for k in ("es_id", "content_hash", "source_file", "source_sha256", "bars",
                                                          "first_bar_open_utc", "last_bar_open_utc", "identity",
                                                          "identity_status", "imported_at")}}
