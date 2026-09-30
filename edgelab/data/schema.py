"""Canonical market-data schema.

TIMESTAMP CONVENTION (the single most important data decision):
  ``ts`` is the bar OPEN time, UTC, nanosecond resolution.
  A bar's OHLC is only knowable at ``ts + timeframe`` (its close).
  Providers that stamp bars at their close must be converted on import
  (see providers.CSVProvider ``timestamp_convention``). Getting this wrong
  shifts every signal one bar into the future - a silent lookahead.

pandas 3.x infers microsecond datetime resolution by default, so every
conversion to int64 goes through ``to_utc_ns`` to pin the unit.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_arrays

BAR_COLUMNS = ("ts", "open", "high", "low", "close", "volume")
PRICE_COLUMNS = ("open", "high", "low", "close")
# Optional second quote side (ADR-55): the primary OHLC stays the dataset's price_basis (BID for
# Dukascopy); these hold the ASK feed's own observed OHLC. All four or none - never inferred.
ASK_COLUMNS = ("ask_open", "ask_high", "ask_low", "ask_close")
ASSET_TYPES = ("FUTURE", "CFD", "SPOT", "ETF", "INDEX", "CRYPTO", "FX", "SYNTHETIC", "unspecified")
VOLUME_TYPES = ("exchange", "tick", "none", "synthetic", "unknown")
PRICE_BASES = ("bid", "ask", "mid", "last", "unknown")


class DataRequiredError(RuntimeError):
    """Raised when an analysis needs data that is not available. Never fabricate."""

    def __init__(self, provider: str, instrument: str, date_range: str = "?",
                 timeframe: str = "?", credentials: str = "none", note: str = ""):
        self.fields = dict(provider=provider, instrument=instrument, date_range=date_range,
                           timeframe=timeframe, credentials=credentials, note=note)
        msg = ("DATA REQUIRED:\n"
               f"  Provider:    {provider}\n  Instrument:  {instrument}\n"
               f"  Date range:  {date_range}\n  Timeframe:   {timeframe}\n"
               f"  Credentials: {credentials}")
        if note:
            msg += f"\n  Note:        {note}"
        super().__init__(msg)


def to_utc_ns(values) -> pd.DatetimeIndex:
    """Parse to a tz-aware UTC DatetimeIndex with nanosecond unit.

    tz-naive input is REJECTED: guessing a timezone is how DST bugs are born.
    """
    idx = pd.DatetimeIndex(pd.to_datetime(values))
    if idx.tz is None:
        raise ValueError("timestamps are timezone-naive; localize them explicitly first")
    return idx.tz_convert("UTC").as_unit("ns")


def timeframe_minutes(tf: str) -> int:
    tf = tf.strip().lower()
    if tf in ("d", "1d", "daily"):
        return 1440
    if tf.endswith("m"):
        return int(tf[:-1])
    if tf.endswith("h"):
        return int(tf[:-1]) * 60
    raise ValueError(f"unsupported timeframe {tf!r}")


@dataclass(frozen=True)
class BarArrays:
    """Read-only columnar view of validated bars. This is what the engine consumes."""
    ts_ns: np.ndarray
    open: np.ndarray
    high: np.ndarray
    low: np.ndarray
    close: np.ndarray
    volume: np.ndarray
    tf_minutes: int
    spread: np.ndarray | None = None   # optional per-bar spread in price points (Phase 2, CFDs)
    ask_open: np.ndarray | None = None   # optional observed ASK OHLC (ADR-55): all four or none
    ask_high: np.ndarray | None = None
    ask_low: np.ndarray | None = None
    ask_close: np.ndarray | None = None

    def __post_init__(self):
        present = [getattr(self, k) is not None for k in ASK_COLUMNS]
        if any(present) and not all(present):
            raise ValueError("ASK OHLC is all-or-none: got only "
                             f"{[k for k, p in zip(ASK_COLUMNS, present) if p]}")
        if all(present) and any(len(getattr(self, k)) != len(self.ts_ns) for k in ASK_COLUMNS):
            raise ValueError("ASK OHLC arrays must have one value per bar")
        for name in ("ts_ns", "open", "high", "low", "close", "volume", "spread") + ASK_COLUMNS:
            arr = getattr(self, name)
            if arr is None:
                continue
            if arr.flags.writeable:
                arr = arr.copy()
                arr.flags.writeable = False
                object.__setattr__(self, name, arr)

    def __len__(self) -> int:
        return len(self.ts_ns)

    @property
    def has_ask_ohlc(self) -> bool:
        return self.ask_open is not None

    def _ask(self, n: int | None = None) -> dict:
        if not self.has_ask_ohlc:
            return {}
        return {k: getattr(self, k)[:n] if n is not None else getattr(self, k) for k in ASK_COLUMNS}

    @classmethod
    def from_frame(cls, df: pd.DataFrame, tf_minutes: int) -> "BarArrays":
        ts = to_utc_ns(df["ts"])
        have = [k for k in ASK_COLUMNS if k in df.columns]
        if have and len(have) != len(ASK_COLUMNS):
            raise ValueError(f"ASK OHLC is all-or-none: frame has only {have}")
        ask = {k: df[k].to_numpy(np.float64, copy=True) for k in have}
        return cls(ts_ns=ts.asi8.copy(),
                   open=df["open"].to_numpy(np.float64, copy=True),
                   high=df["high"].to_numpy(np.float64, copy=True),
                   low=df["low"].to_numpy(np.float64, copy=True),
                   close=df["close"].to_numpy(np.float64, copy=True),
                   volume=df["volume"].to_numpy(np.float64, copy=True),
                   tf_minutes=tf_minutes,
                   spread=(df["spread"].to_numpy(np.float64, copy=True)
                           if "spread" in df.columns else None), **ask)

    def head(self, n: int) -> "BarArrays":
        """Truncated view - used by the causality (lookahead) checker."""
        return BarArrays(self.ts_ns[:n], self.open[:n], self.high[:n], self.low[:n],
                         self.close[:n], self.volume[:n], self.tf_minutes,
                         None if self.spread is None else self.spread[:n], **self._ask(n))

    @property
    def ts(self) -> pd.DatetimeIndex:
        return pd.DatetimeIndex(self.ts_ns.astype("datetime64[ns]")).tz_localize("UTC")

    @property
    def ts_close_ns(self) -> np.ndarray:
        return self.ts_ns + np.int64(self.tf_minutes) * 60_000_000_000

    @property
    def has_volume(self) -> bool:
        return bool(len(self.volume)) and bool(np.isfinite(self.volume).all())

    def content_hash(self) -> str:
        arrays = [self.ts_ns, self.open, self.high, self.low, self.close, self.volume]
        if self.spread is not None:          # absent spread leaves Phase 1 hashes unchanged
            arrays.append(self.spread)
        if self.has_ask_ohlc:                # absent ASK OHLC leaves every earlier hash unchanged
            arrays += [np.frombuffer(b"ask_ohlc", np.uint8)] + [getattr(self, k) for k in ASK_COLUMNS]
        return hash_arrays(*arrays)

    def to_frame(self) -> pd.DataFrame:
        df = pd.DataFrame({"ts": self.ts, "open": self.open, "high": self.high,
                           "low": self.low, "close": self.close, "volume": self.volume})
        if self.spread is not None:
            df["spread"] = self.spread
        for k, v in self._ask().items():
            df[k] = v
        return df


@dataclass
class DatasetManifest:
    """Provenance record stored with every dataset and referenced by every run."""
    dataset_id: str
    provider: str
    instrument: str
    timeframe: str
    timezone: str
    timestamp_convention: str          # always "bar_open_utc" after import
    start: str
    end: str
    n_bars: int
    content_hash: str
    imported_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    source_detail: dict[str, Any] = field(default_factory=dict)  # file path, vendor, seed...
    contract: str = "unspecified"       # e.g. "continuous", "NQH25"
    adjustment: str = "unspecified"     # none | back_adjusted | ratio_adjusted | n/a
    missing_bars: int | None = None
    duplicate_bars: int | None = None
    quality_status: str | None = None
    # ---- Phase 2 provenance (defaults keep Phase 1 manifests loadable) ----
    dataset_name: str = ""               # family name, e.g. NAS100_CFD_DUKASCOPY
    asset_type: str = "unspecified"      # FUTURE | CFD | SYNTHETIC | ...
    symbol: str = ""                     # provider's own symbol, e.g. USA100IDXUSD
    source_timezone: str = "unspecified"
    source_timestamp_convention: str = "unspecified"   # open | close (as delivered)
    source_file_sha256: str | None = None
    calendar: str = ""
    calendar_fingerprint: str = ""
    volume_type: str = "unknown"         # exchange | tick | none | synthetic | unknown
    price_basis: str = "unknown"         # bid | ask | mid | last | unknown
    has_bid_ask: bool = False
    has_spread: bool = False
    spread_source: str = "none"          # none | column | bid_ask_close
    has_ask_ohlc: bool = False           # observed ASK OHLC stored beside the primary OHLC (ADR-55)
    provider_notes: str = ""
    import_version: str = ""
    parent_dataset_id: str | None = None  # derived (resampled) datasets point to their source
    derivation: str | None = None

    def to_dict(self) -> dict:
        d = asdict(self)
        if not d.get("has_ask_ohlc"):        # keeps manifests (and manifest hashes) of datasets
            d.pop("has_ask_ohlc", None)      # without ASK OHLC byte-identical to before ADR-55
        return d

    def identity(self) -> dict:
        """Everything that defines the dataset, minus the wall-clock import time."""
        d = self.to_dict()
        d.pop("imported_at", None)
        return d

    def manifest_hash(self) -> str:
        """Deterministic: re-importing identical input with identical metadata gives the same hash."""
        from edgelab.core.identity import hash_obj
        return hash_obj(self.identity())

    @classmethod
    def from_dict(cls, d: dict) -> "DatasetManifest":
        """Tolerant loader: unknown keys are kept in source_detail, missing keys defaulted."""
        from dataclasses import fields
        known = {f.name for f in fields(cls)}
        kwargs = {k: v for k, v in d.items() if k in known}
        extra = {k: v for k, v in d.items() if k not in known}
        if extra:
            kwargs.setdefault("source_detail", {})
            kwargs["source_detail"] = {**kwargs["source_detail"], "_unknown_manifest_keys": extra}
        return cls(**kwargs)


def canonicalize(df: pd.DataFrame) -> pd.DataFrame:
    """Return a canonical-column copy with UTC-ns timestamps. Does not sort or dedupe
    (validation must see the data as delivered)."""
    missing = [c for c in BAR_COLUMNS if c not in df.columns]
    if missing:
        raise ValueError(f"bars missing required columns: {missing}")
    out = df.loc[:, list(BAR_COLUMNS) + [c for c in df.columns if c not in BAR_COLUMNS]].copy()
    out["ts"] = to_utc_ns(out["ts"])
    extra = tuple(c for c in ("spread",) + ASK_COLUMNS if c in out.columns)
    for c in PRICE_COLUMNS + ("volume",) + extra:
        out[c] = pd.to_numeric(out[c], errors="coerce").astype(np.float64)
    return out.reset_index(drop=True)
