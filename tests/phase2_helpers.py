"""Phase 2 test helpers: synthetic provider files written through the REAL import path.
All data here is synthetic (random walk) and labelled as such."""
from __future__ import annotations

import numpy as np
import pandas as pd

from edgelab.core.config import load_config
from edgelab.data.synthetic import generate_bars
from edgelab.features.sessions import load_sessions
from tests.helpers import CME

CFG = load_config(environ={})
SESSIONS = load_sessions(CFG)


def synthetic_canonical(start="2024-03-04", end="2024-03-16", tf=1, seed=11, **kw) -> pd.DataFrame:
    df, _ = generate_bars(CME, start, end, tf_minutes=tf, seed=seed, **kw)
    return df


def write_mt5_like(df: pd.DataFrame, path, spread_points: np.ndarray | None = None,
                   server_offset_h: int = 7) -> None:
    """MT5-style export: tab-separated, <DATE> <TIME> in broker server time = New York + 7h,
    tick volume, integer spread in 0.01-price 'points'."""
    ny = pd.DatetimeIndex(df["ts"]).tz_convert("America/New_York").tz_localize(None)
    server = ny + pd.Timedelta(hours=server_offset_h)
    sp = spread_points if spread_points is not None else np.full(len(df), 150)
    out = pd.DataFrame({"<DATE>": server.strftime("%Y.%m.%d"), "<TIME>": server.strftime("%H:%M:%S"),
                        "<OPEN>": df["open"].round(2), "<HIGH>": df["high"].round(2),
                        "<LOW>": df["low"].round(2), "<CLOSE>": df["close"].round(2),
                        "<TICKVOL>": df["volume"].astype(int), "<VOL>": 0, "<SPREAD>": sp})
    out.to_csv(path, sep="\t", index=False)


def write_generic_utc(df: pd.DataFrame, path, with_volume: bool = True, close_stamped: bool = False,
                      tf: int = 1) -> None:
    ts = pd.DatetimeIndex(df["ts"])
    if close_stamped:
        ts = ts + pd.Timedelta(minutes=tf)
    out = pd.DataFrame({"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": df["open"].round(2),
                        "high": df["high"].round(2), "low": df["low"].round(2),
                        "close": df["close"].round(2)})
    if with_volume:
        out["volume"] = df["volume"]
    out.to_csv(path, index=False)


# ---- Test-local runnable BID-only CFD research proxy (SYNTHETIC; never part of the shipped configs/) ----
# A CFD instrument flagged research_proxy whose symbol-level costs stay UNCONFIGURED while one named feed
# carries complete ASSUMED per-unit costs (no scenario). Tests use it where they need a runnable proxy
# dataset with a provider-specific cost profile; the numbers are test inputs, not broker figures.
TEST_PROXY = "TEST_PROXY_CFD"
TEST_FEED = "TESTFEED"
TEST_PROXY_PROFILE = f"{TEST_PROXY}@{TEST_FEED}"
TEST_PROXY_INSTRUMENT = {"exchange": "OTC", "asset_class": "cfd", "tick_size": 0.001, "tick_value": 0.001,
                         "point_value": 1, "min_size": 0.01, "size_step": 0.01, "underlying": "NDX",
                         "calendar": "CME_EQUITY", "research_proxy": True,
                         "price_source": "SYNTHETIC test feed, BID",
                         "description": "test-local synthetic CFD research proxy (not a tradable contract)"}
TEST_PROXY_COSTS = {"status": "unconfigured", "notes": f"test-local; only providers.{TEST_FEED} carries assumed costs",
                    "slippage_unit": "points", "commission_per_side": None, "slippage_ticks_market": None,
                    "slippage_ticks_stop": None, "spread_points": None,
                    "providers": {TEST_FEED: {
                        "status": "assumed", "notes": "test inputs only, not broker-verified",
                        "commission_per_side": 0.50, "fees_per_side": 0, "slippage_unit": "points",
                        "slippage_ticks_market": 0.25, "slippage_ticks_stop": 0.25, "slippage_ticks_limit": 0,
                        "spread_source": "fixed", "spread_points": 0.50, "financing_mode": "none"}}}


def with_test_proxy(cfg: dict) -> dict:
    """A deep copy of a loaded config with the test-local proxy instrument and its costs added."""
    import copy
    out = copy.deepcopy(dict(cfg))
    out["instruments"] = {**out["instruments"], TEST_PROXY: copy.deepcopy(TEST_PROXY_INSTRUMENT)}
    out["costs"]["symbols"] = {**out["costs"]["symbols"], TEST_PROXY: copy.deepcopy(TEST_PROXY_COSTS)}
    return out


def add_test_proxy_feed(configs_dir) -> None:
    """Add the test-local proxy instrument and cost profile to a COPIED workspace configs/ folder."""
    import copy
    from pathlib import Path

    import yaml
    configs_dir = Path(configs_dir)
    for name, section, key, value in (("instruments.yaml", ("instruments",), TEST_PROXY, TEST_PROXY_INSTRUMENT),
                                      ("costs.yaml", ("costs", "symbols"), TEST_PROXY, TEST_PROXY_COSTS)):
        path = configs_dir / name
        doc = yaml.safe_load(path.read_text())
        node = doc
        for part in section:
            node = node[part]
        node[key] = copy.deepcopy(value)
        path.write_text(yaml.safe_dump(doc, sort_keys=False))
