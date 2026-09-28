"""Named session windows (New York 09:30-10:30, London, Asia, ...).

Storage stays UTC; membership is evaluated in the WINDOW's own timezone on the wall
clock, so "New York 09:00-10:00" is 09:00-10:00 New York time on every date - 14:00 UTC
in winter, 13:00 UTC in summer - and London/New York windows each follow their own DST
calendar (they switch on different dates).

Definitions
  * A bar belongs to a window if its OPEN time (local) is in [start, end).
    end <= start means the window wraps midnight (e.g. 19:00-02:00).
  * A session INSTANCE is identified by the local date on which it starts; ``weekdays``
    filters instances by that start weekday.
  * An instance's scheduled END (UTC) is when its values become final. If end falls in a
    DST gap it is shifted forward; if ambiguous (fall-back) the LATER instant is used
    (conservative: values are exposed later, never earlier).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

import numpy as np
import pandas as pd

from edgelab.core.identity import hash_obj
from edgelab.data.calendar import WEEKDAYS, _minutes

EPOCH = np.datetime64("1970-01-01", "D")


@dataclass(frozen=True)
class SessionWindow:
    name: str
    timezone: str
    start: str
    end: str
    weekdays: tuple[int, ...] = (0, 1, 2, 3, 4)

    @property
    def start_min(self) -> int:
        return _minutes(self.start)

    @property
    def end_min(self) -> int:
        return _minutes(self.end)

    @property
    def wraps(self) -> bool:
        return self.end_min <= self.start_min

    @property
    def length_minutes(self) -> int:
        return (self.end_min - self.start_min) % 1440 or 1440

    def definition(self) -> dict:
        return {"name": self.name, "timezone": self.timezone, "start": self.start,
                "end": self.end, "weekdays": list(self.weekdays)}

    def fingerprint(self) -> str:
        return hash_obj(self.definition())

    def membership(self, ts_ns: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """-> (in_session bool, instance int64 [days since epoch of local start date; -1 outside],
               minutes_into_session float [wall clock; NaN outside])."""
        idx = pd.DatetimeIndex(np.asarray(ts_ns).astype("datetime64[ns]")).tz_localize("UTC") \
            .tz_convert(self.timezone)
        mins = np.asarray(idx.hour * 60 + idx.minute, dtype=np.int64)
        local_day = idx.tz_localize(None).normalize().values.astype("datetime64[D]")
        s, e = self.start_min, self.end_min
        if not self.wraps:
            inside = (mins >= s) & (mins < e)
            start_day = local_day
        else:
            inside = (mins >= s) | (mins < e)
            start_day = np.where(mins >= s, local_day, local_day - np.timedelta64(1, "D"))
        wd = (start_day.astype(np.int64) - 4) % 7          # 1970-01-01 was a Thursday (=3); Mon=0
        inside &= np.isin(wd, self.weekdays)
        inst = np.where(inside, (start_day - EPOCH).astype(np.int64), -1)
        into = np.where(inside, ((mins - s) % 1440).astype(float), np.nan)
        return inside, inst, into

    def end_ns(self, instances: np.ndarray) -> np.ndarray:
        """Scheduled UTC end (ns) of each instance (days since epoch of its start date)."""
        days = EPOCH + np.asarray(instances, dtype=np.int64).astype("timedelta64[D]")
        end_local = days.astype("datetime64[ns]") + np.timedelta64(self.end_min, "m")
        if self.wraps:
            end_local = end_local + np.timedelta64(1, "D")
        idx = pd.DatetimeIndex(end_local).tz_localize(self.timezone, nonexistent="shift_forward",
                                                      ambiguous=np.zeros(len(end_local), bool))
        return idx.tz_convert("UTC").as_unit("ns").asi8


def load_sessions(cfg: Mapping) -> dict[str, SessionWindow]:
    out = {}
    for name, w in (cfg.get("sessions") or {}).items():
        days = w.get("weekdays", ["mon", "tue", "wed", "thu", "fri"])
        out[name] = SessionWindow(name, w["timezone"], w["start"], w["end"],
                                  tuple(WEEKDAYS[d.lower()[:3]] for d in days))
    return out
