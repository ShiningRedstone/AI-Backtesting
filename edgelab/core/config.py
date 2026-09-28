"""Configuration loading.

Design:
  * All tunable values live in YAML files under ``configs/``.
  * Environment variables override YAML using ``EDGELAB__SECTION__KEY=value``
    (double underscore = nesting). Values are parsed as YAML scalars, so
    ``EDGELAB__BACKTEST__COST_MULTIPLIER=2`` becomes the int 2.
  * Credentials are NEVER read from YAML. Use ``get_secret(name)`` which only
    reads the environment.
  * Every loaded config has a stable SHA-256 hash (sorted-key JSON), which is
    stored with each research run for reproducibility.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path
from typing import Any, Mapping

import yaml

ENV_PREFIX = "EDGELAB__"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG_DIR = PROJECT_ROOT / "configs"

# Files merged (in this order) into one config tree. Top-level keys must not collide.
CONFIG_FILES = ("data.yaml", "instruments.yaml", "costs.yaml", "backtest.yaml",
                "storage.yaml", "logging.yaml")


class ConfigError(ValueError):
    """Raised when configuration is missing or invalid."""


def _deep_merge(base: dict, override: Mapping) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, Mapping) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def _apply_env_overrides(cfg: dict, environ: Mapping[str, str]) -> dict:
    out = copy.deepcopy(cfg)
    for key, raw in environ.items():
        if not key.startswith(ENV_PREFIX):
            continue
        path = [p.lower() for p in key[len(ENV_PREFIX):].split("__") if p]
        if not path:
            continue
        node = out
        for p in path[:-1]:
            if p not in node or not isinstance(node[p], dict):
                node[p] = {}
            node = node[p]
        node[path[-1]] = yaml.safe_load(raw)
    return out


def config_hash(cfg: Mapping[str, Any]) -> str:
    """Stable hash of a config tree (key order independent)."""
    blob = json.dumps(cfg, sort_keys=True, default=str, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def load_config(config_dir: str | Path | None = None,
                environ: Mapping[str, str] | None = None,
                overrides: Mapping[str, Any] | None = None) -> dict:
    """Load and validate the merged configuration tree."""
    config_dir = Path(config_dir) if config_dir else DEFAULT_CONFIG_DIR
    if not config_dir.is_dir():
        raise ConfigError(f"config directory not found: {config_dir}")
    cfg: dict = {}
    for name in CONFIG_FILES:
        path = config_dir / name
        if not path.exists():
            raise ConfigError(f"missing config file: {path}")
        with open(path) as fh:
            part = yaml.safe_load(fh) or {}
        clash = set(part) & set(cfg)
        if clash:
            raise ConfigError(f"{name} redefines top-level keys {sorted(clash)}")
        cfg.update(part)
    cfg = _apply_env_overrides(cfg, os.environ if environ is None else environ)
    if overrides:
        cfg = _deep_merge(cfg, overrides)
    validate_config(cfg)
    return cfg


_ALLOWED = {
    ("backtest", "same_bar_policy"): {"conservative", "optimistic", "intrabar"},
    ("backtest", "intrabar_fallback"): {"conservative", "optimistic"},
    ("backtest", "gap_fill", "target"): {"limit_price", "open"},
    ("storage", "backend"): {"auto", "duckdb", "sqlite"},
}


def _get(cfg: Mapping, path: tuple[str, ...]) -> Any:
    node: Any = cfg
    for p in path:
        if not isinstance(node, Mapping) or p not in node:
            raise ConfigError(f"missing config key: {'.'.join(path)}")
        node = node[p]
    return node


def validate_config(cfg: Mapping) -> None:
    for path, allowed in _ALLOWED.items():
        val = _get(cfg, path)
        if val not in allowed:
            raise ConfigError(f"{'.'.join(path)}={val!r}; allowed: {sorted(allowed)}")
    mult = _get(cfg, ("backtest", "cost_multiplier"))
    if not isinstance(mult, (int, float)) or mult < 0:
        raise ConfigError("backtest.cost_multiplier must be a non-negative number")
    if _get(cfg, ("backtest", "causality_cuts")) < 1:
        raise ConfigError("backtest.causality_cuts must be >= 1")
    for sym, meta in _get(cfg, ("instruments",)).items():
        for k in ("tick_size", "tick_value", "calendar"):
            if k not in meta:
                raise ConfigError(f"instruments.{sym} missing '{k}'")
    if "default" not in _get(cfg, ("costs",)):
        raise ConfigError("costs.default block is required")


def get_secret(name: str, required: bool = True) -> str | None:
    """Credentials come only from the environment, never from config files."""
    val = os.environ.get(name)
    if required and not val:
        raise ConfigError(f"credential {name} not set in environment")
    return val
