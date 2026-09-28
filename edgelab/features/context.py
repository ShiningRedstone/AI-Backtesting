"""What a feature implementation receives. Everything here is known at or before each bar."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

import numpy as np

from edgelab.data.calendar import SessionCalendar
from edgelab.data.schema import BarArrays
from edgelab.features.sessions import SessionWindow


@dataclass(frozen=True)
class FeatureInput:
    bars: BarArrays                  # the bars of the timeframe being computed (base or higher)
    ts_close_ns: np.ndarray          # when each bar's values become known (HTF: capped at session close)
    calendar: SessionCalendar
    sessions: Mapping[str, SessionWindow]
    volume_type: str
    tick_size: float
    dep: Callable                    # dep(FeatureSpec) -> dict[str, ndarray] on these same bars

    def session(self, name: str) -> SessionWindow:
        if name not in self.sessions:
            from edgelab.features.spec import FeatureError
            raise FeatureError(f"unknown session {name!r}; configured: {sorted(self.sessions)}")
        return self.sessions[name]
