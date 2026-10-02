"""Demo workspace: a SEPARATE project root filled with synthetic data and the fixture
strategies, so a new user can see the whole flow. Nothing here touches the real workspace,
so demo runs can never mix with real research results.

Synthetic bars go through the normal import pipeline (normalize -> validate -> manifest ->
hash -> store); the provider name starts with SYNTHETIC, which the whole system flags.
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pandas as pd

from edgelab.data.synthetic import generate_bars
from edgelab.data.calendar import load_calendars
from edgelab.runtime import copy_default_configs

MARKER = "DEMO_WORKSPACE"
NOTICE = "Synthetic demonstration - not evidence of trading performance."


def is_demo_root(root: str | Path) -> bool:
    return (Path(root) / MARKER).exists()


def _write_csv(df: pd.DataFrame, path: Path) -> None:
    ts = pd.DatetimeIndex(df["ts"])
    pd.DataFrame({"ts": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": df["open"].round(2),
                  "high": df["high"].round(2), "low": df["low"].round(2), "close": df["close"].round(2),
                  "volume": df["volume"]}).to_csv(path, index=False)


def create_demo_workspace(root: str | Path, repo: str | Path) -> dict:
    """Idempotent: creates the workspace once; later calls reuse it."""
    root, repo = Path(root), Path(repo)
    if root.exists() and any(root.iterdir()) and not is_demo_root(root):
        raise ValueError(f"{root} exists and is not a demo workspace; refusing to write demo data into it")
    if is_demo_root(root):
        return {"root": str(root), "created": False}
    root.mkdir(parents=True, exist_ok=True)
    copy_default_configs(repo / "configs", root / "configs")       # new workspace: no legacy HistData blocks
    (root / MARKER).write_text(NOTICE + "\n")
    from edgelab.services import Services
    svc = Services(root=root)
    imp = root / "data" / "import"
    imp.mkdir(parents=True, exist_ok=True)
    cal = load_calendars(svc.cfg)["CME_EQUITY"]
    made = []
    for inst, asset, provider, seed in (("NQ", "FUTURE", "SYNTHETIC_DEMO", 21),
                                        ("NAS100_CFD", "CFD", "SYNTHETIC_DEMO_CFD", 22)):
        df, _ = generate_bars(cal, "2024-01-02", "2024-03-29", tf_minutes=5, seed=seed)
        f = imp / f"SYNTHETIC_{inst}_5m.csv"
        _write_csv(df, f)
        r = svc.import_file(dict(file=str(f), instrument=inst, provider=provider, asset_type=asset,
                                 timeframe="5m", source_timezone="UTC", price_basis="unknown",
                                 notes=NOTICE))
        made.append(r["dataset_id"])
    saved = []
    for f in sorted((repo / "strategies" / "fixtures").glob("*.yaml")):
        if "variations" in f.name or "proposals" in f.name:
            continue
        saved.append(svc.save_strategy(str(f))["strategy_id"])
    shutil.copytree(repo / "strategies" / "fixtures", root / "strategies" / "fixtures")
    return {"root": str(root), "created": True, "datasets": made, "strategies": saved}
