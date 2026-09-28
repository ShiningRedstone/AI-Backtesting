"""Exchange session calendars.

Model: every *trading date* D has one session that opens at ``session_open``
(local time, on the previous calendar day if session_open > session_close) and
closes at ``session_close`` on D. CME equity index futures, for example:
trading date Tuesday = Monday 18:00 ET -> Tuesday 17:00 ET. Weekends fall out
naturally because Saturday/Sunday are not trading weekdays.

All times are local to the calendar's IANA timezone; conversions go through
zoneinfo, so DST shifts are handled by construction.

LIMITATION (documented, not hidden): no exchange holiday schedule ships with
this build (``exchange_calendars`` is not installed). Holidays / early closes
must be supplied in configs/data.yaml. Unlisted holidays surface in validation
as MISSING_DAY gaps rather than passing silently.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from typing import Iterable, Mapping

import numpy as np
import pandas as pd

WEEKDAYS = {"mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6}


def _parse_time(s: str) -> time:
    if s in ("24:00", "24:00:00"):
        return time(0, 0)  # handled as end-of-day by callers via _minutes
    return time.fromisoformat(s)


def _minutes(s: str) -> int:
    hh, mm = s.split(":")[:2]
    return int(hh) * 60 + int(mm)


@dataclass(frozen=True)
class SessionCalendar:
    name: str
    timezone: str
    session_open: str           # "18:00"; if > session_close, it is on the previous day
    session_close: str          # "17:00"; "24:00" allowed for 24h sessions
    trading_weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)
    holidays: frozenset = field(default_factory=frozenset)          # trading dates closed
    early_closes: Mapping[date, str] = field(default_factory=dict)  # trading date -> "13:00"

    def fingerprint(self) -> str:
        """Stable hash of the full calendar definition (used in dataset + feature-cache identity)."""
        from edgelab.core.identity import hash_obj
        return hash_obj({"name": self.name, "tz": self.timezone, "open": self.session_open,
                         "close": self.session_close, "weekdays": list(self.trading_weekdays),
                         "holidays": sorted(str(d) for d in self.holidays),
                         "early_closes": {str(k): v for k, v in sorted(self.early_closes.items())}})

    # ---- derived -------------------------------------------------------------
    @property
    def open_min(self) -> int:
        return _minutes(self.session_open)

    @property
    def close_min(self) -> int:
        return _minutes(self.session_close)

    @property
    def overnight(self) -> bool:
        """True when the session starts on the previous calendar day."""
        return self.open_min > self.close_min or (self.open_min == self.close_min and self.open_min != 0)

    @property
    def _td_shift(self) -> pd.Timedelta:
        # Adding this shift to a local timestamp and taking the date gives the trading date.
        return pd.Timedelta(minutes=(1440 - self.open_min) % 1440)

    def session_bounds(self, trading_date: date) -> tuple[pd.Timestamp, pd.Timestamp]:
        """(open, close) of a trading date as tz-aware local timestamps."""
        tz = self.timezone
        d = pd.Timestamp(trading_date)
        open_day = d - pd.Timedelta(days=1) if self.overnight else d
        open_ts = (open_day + pd.Timedelta(minutes=self.open_min)).tz_localize(tz)
        close_str = self.early_closes.get(trading_date, self.session_close)
        close_ts = (d + pd.Timedelta(minutes=_minutes(close_str))).tz_localize(tz)
        return open_ts, close_ts

    def is_trading_date(self, d: date) -> bool:
        return d.weekday() in self.trading_weekdays and d not in self.holidays

    # ---- vectorized ---------------------------------------------------------
    def local(self, ts_utc: pd.DatetimeIndex) -> pd.DatetimeIndex:
        return ts_utc.tz_convert(self.timezone)

    def trading_dates(self, ts_utc: pd.DatetimeIndex) -> np.ndarray:
        """Trading date (datetime64[D]) for each bar-open timestamp."""
        loc = self.local(ts_utc).tz_localize(None)
        return (loc + self._td_shift).normalize().values.astype("datetime64[D]")

    def in_session(self, ts_utc: pd.DatetimeIndex) -> np.ndarray:
        """Whether each bar-open timestamp falls inside a scheduled session."""
        loc = self.local(ts_utc)
        td = self.trading_dates(ts_utc)
        td_idx = pd.DatetimeIndex(td)
        wd_ok = np.isin(td_idx.weekday, self.trading_weekdays)
        hol = np.isin(td, np.array(sorted(self.holidays), dtype="datetime64[D]")) \
            if self.holidays else np.zeros(len(td), bool)
        # minutes elapsed since the session open of the bar's own trading date
        mins_local = loc.hour * 60 + loc.minute
        since_open = (np.asarray(mins_local) - self.open_min) % 1440
        session_len = np.full(len(td), (self.close_min - self.open_min) % 1440 or 1440)
        if self.early_closes:
            for d, t in self.early_closes.items():
                session_len[td == np.datetime64(d, "D")] = (_minutes(t) - self.open_min) % 1440
        return wd_ok & ~hol & (since_open < session_len)

    def expected_bar_opens(self, start_utc: pd.Timestamp, end_utc: pd.Timestamp,
                           tf_minutes: int) -> pd.DatetimeIndex:
        """All bar-open timestamps (UTC) a complete feed would contain in [start, end].

        Bars are anchored to the session open, so 45m/240m bars align to the
        trading day rather than to midnight UTC.
        """
        start_utc = pd.Timestamp(start_utc).tz_convert("UTC")
        end_utc = pd.Timestamp(end_utc).tz_convert("UTC")
        first = (start_utc.tz_convert(self.timezone) - pd.Timedelta(days=2)).date()
        last = (end_utc.tz_convert(self.timezone) + pd.Timedelta(days=2)).date()
        parts = []
        d = first
        while d <= last:
            if self.is_trading_date(d):
                o, c = self.session_bounds(d)
                rng = pd.date_range(o, c, freq=f"{tf_minutes}min", inclusive="left")
                parts.append(rng.tz_convert("UTC"))
            d += timedelta(days=1)
        if not parts:
            return pd.DatetimeIndex([], tz="UTC").as_unit("ns")
        idx = parts[0].append(parts[1:]) if len(parts) > 1 else parts[0]
        idx = idx[(idx >= start_utc) & (idx <= end_utc)]
        return idx.as_unit("ns")


def calendar_from_config(name: str, cfg: Mapping) -> SessionCalendar:
    days = cfg.get("trading_weekdays", ["mon", "tue", "wed", "thu", "fri"])
    return SessionCalendar(
        name=name,
        timezone=cfg["timezone"],
        session_open=cfg["session_open"],
        session_close=cfg["session_close"],
        trading_weekdays=tuple(WEEKDAYS[d.lower()[:3]] for d in days),
        holidays=frozenset(pd.Timestamp(h).date() for h in cfg.get("holidays", []) or []),
        early_closes={pd.Timestamp(k).date(): v for k, v in (cfg.get("early_closes") or {}).items()},
    )


def load_calendars(cfg: Mapping) -> dict[str, SessionCalendar]:
    return {n: calendar_from_config(n, c) for n, c in cfg["calendars"].items()}
