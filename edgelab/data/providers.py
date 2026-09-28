"""Market data provider abstraction.

The backtester never talks to a provider directly. Providers return raw frames in
the canonical column set; ``validate_and_freeze`` turns them into the only object
the engine accepts.

Implemented here: CSVProvider, SyntheticProvider.
Stubbed honestly: ParquetProvider (needs pyarrow or duckdb), VendorAPIProvider
(needs a real vendor + credentials). Stubs raise DataRequiredError describing
exactly what is needed - they never return placeholder data.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from edgelab.data.calendar import SessionCalendar
from edgelab.data.schema import DataRequiredError, canonicalize
from edgelab.data.synthetic import generate_bars


class MarketDataProvider(ABC):
    name: str = "abstract"

    @abstractmethod
    def load_bars(self, instrument: str, timeframe: str, start: str | None = None,
                  end: str | None = None) -> pd.DataFrame: ...

    def load_ticks(self, instrument: str, start: str, end: str) -> pd.DataFrame:
        raise DataRequiredError(self.name, instrument, f"{start}..{end}", "tick",
                                note="this provider does not supply tick data")

    def load_events(self, start: str, end: str) -> pd.DataFrame:
        raise DataRequiredError(self.name, "economic calendar", f"{start}..{end}", "n/a",
                                note="no event source configured; event analysis will be skipped")

    def get_metadata(self) -> dict[str, Any]:
        return {"provider": self.name}


class CSVProvider(MarketDataProvider):
    """CSV bars. Handles vendor timezones and bar-close timestamp conventions.

    timestamp_convention:
      "open"  - vendor stamps the bar at its open (TradingView style): used as-is.
      "close" - vendor stamps the bar at its close: shifted back one bar so that
                ``ts`` is the open. Skipping this shift is a one-bar lookahead.
    """
    name = "csv"

    def __init__(self, path: str | Path, source_timezone: str = "UTC",
                 timestamp_convention: str = "open", column_map: Mapping[str, str] | None = None,
                 tf_minutes: int | None = None):
        if timestamp_convention not in ("open", "close"):
            raise ValueError("timestamp_convention must be 'open' or 'close'")
        if timestamp_convention == "close" and not tf_minutes:
            raise ValueError("tf_minutes is required to convert close-stamped bars")
        self.path = Path(path)
        self.source_timezone = source_timezone
        self.timestamp_convention = timestamp_convention
        self.column_map = dict(column_map or {})
        self.tf_minutes = tf_minutes

    def load_bars(self, instrument: str, timeframe: str, start: str | None = None,
                  end: str | None = None) -> pd.DataFrame:
        if not self.path.exists():
            raise DataRequiredError("csv", instrument, f"{start}..{end}", timeframe,
                                    note=f"file not found: {self.path}")
        df = pd.read_csv(self.path).rename(columns=self.column_map)
        ts = pd.to_datetime(df["ts"])
        if ts.dt.tz is None:
            ts = ts.dt.tz_localize(self.source_timezone, ambiguous="raise", nonexistent="raise")
        if self.timestamp_convention == "close":
            ts = ts - pd.Timedelta(minutes=self.tf_minutes)
        df["ts"] = ts
        out = canonicalize(df)
        if start:
            out = out[out["ts"] >= pd.Timestamp(start, tz="UTC")]
        if end:
            out = out[out["ts"] <= pd.Timestamp(end, tz="UTC")]
        return out.reset_index(drop=True)

    def get_metadata(self) -> dict[str, Any]:
        return {"provider": self.name, "path": str(self.path),
                "source_timezone": self.source_timezone,
                "timestamp_convention": self.timestamp_convention}


class SyntheticProvider(MarketDataProvider):
    name = "synthetic"

    def __init__(self, calendar: SessionCalendar, **gen_kwargs):
        self.calendar = calendar
        self.gen_kwargs = gen_kwargs
        self.truth: dict = {}

    def load_bars(self, instrument: str, timeframe: str, start: str | None = None,
                  end: str | None = None) -> pd.DataFrame:
        from edgelab.data.schema import timeframe_minutes
        bars, self.truth = generate_bars(self.calendar, start, end,
                                         tf_minutes=timeframe_minutes(timeframe), **self.gen_kwargs)
        return bars

    def get_metadata(self) -> dict[str, Any]:
        return {"provider": self.name, **self.truth}


class ParquetProvider(MarketDataProvider):
    name = "parquet"

    def __init__(self, path: str | Path):
        self.path = Path(path)

    def load_bars(self, instrument, timeframe, start=None, end=None):
        try:
            return canonicalize(pd.read_parquet(self.path))
        except ImportError as exc:
            raise DataRequiredError("parquet", instrument, f"{start}..{end}", timeframe,
                                    note=f"install pyarrow or duckdb to read Parquet ({exc})") from exc


class VendorAPIProvider(MarketDataProvider):
    """Placeholder for a licensed vendor (e.g. Databento, CQG, Rithmic, IQFeed).

    Not implemented: no vendor account/credentials exist in this build. Scraping
    or bypassing access controls is out of scope by design.
    """
    name = "vendor_api"

    def __init__(self, vendor: str, credential_env: str):
        self.vendor, self.credential_env = vendor, credential_env

    def load_bars(self, instrument, timeframe, start=None, end=None):
        raise DataRequiredError(self.vendor, instrument, f"{start}..{end}", timeframe,
                                credentials=f"env var {self.credential_env}",
                                note="vendor adapter not implemented yet")
