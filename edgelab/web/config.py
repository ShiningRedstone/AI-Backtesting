"""Web settings (configs/web.yaml, optional). Deliberately NOT part of the research config:
changing a port or the builder's timeframe menu must not change any research config hash."""
from __future__ import annotations

import ipaddress
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from edgelab.data.schema import timeframe_minutes

DEFAULT_TIMEFRAMES = ["1m", "2m", "3m", "5m", "10m", "15m", "30m", "1h", "2h", "4h"]


class WebConfigError(ValueError):
    pass


@dataclass(frozen=True)
class WebConfig:
    host: str = "127.0.0.1"
    port: int = 8765
    builder_timeframes: list = field(default_factory=lambda: list(DEFAULT_TIMEFRAMES))
    import_dirs: list = field(default_factory=lambda: ["data/import"])
    max_request_mb: float = 5.0

    @property
    def is_loopback(self) -> bool:
        if self.host == "localhost":
            return True
        try:
            return ipaddress.ip_address(self.host).is_loopback
        except ValueError:
            return False


def load_web_config(root: str | Path) -> WebConfig:
    p = Path(root) / "configs" / "web.yaml"
    raw = (yaml.safe_load(p.read_text()) or {}).get("web", {}) if p.exists() else {}
    unknown = set(raw) - {"host", "port", "builder_timeframes", "import_dirs", "max_request_mb"}
    if unknown:
        raise WebConfigError(f"configs/web.yaml: unknown keys {sorted(unknown)}")
    cfg = WebConfig(**raw)
    for tf in cfg.builder_timeframes:
        try:
            timeframe_minutes(str(tf))
        except ValueError as exc:
            raise WebConfigError(f"configs/web.yaml builder_timeframes: {exc}") from None
    if not (0 < int(cfg.port) < 65536):
        raise WebConfigError("configs/web.yaml: port must be 1-65535")
    return cfg
