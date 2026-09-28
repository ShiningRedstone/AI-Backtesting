"""Persistent storage for datasets, runs, trades and metrics.

Target architecture: DuckDB + Parquet (columnar, fast analytical scans).
Fallback: SQLite (stdlib), used automatically when duckdb is not installed.
Both implement the same interface and are exercised by the same test suite
(tests/test_store.py skips DuckDB when the package is absent).

Timestamps are stored as int64 nanoseconds UTC; content hashes are re-checked on
load so a silently altered dataset is detected.
"""
from __future__ import annotations

import json
import sqlite3
from abc import ABC, abstractmethod
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from edgelab.core.logging import get_logger
from edgelab.data.schema import BarArrays, DatasetManifest

log = get_logger("store")

SCHEMA = [
    """CREATE TABLE IF NOT EXISTS datasets (dataset_id TEXT PRIMARY KEY, content_hash TEXT,
        manifest_json TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS bars (dataset_id TEXT, ts_ns BIGINT, open DOUBLE, high DOUBLE,
        low DOUBLE, close DOUBLE, volume DOUBLE)""",
    """CREATE TABLE IF NOT EXISTS runs (run_id TEXT PRIMARY KEY, created_at TEXT, status TEXT,
        strategy_id TEXT, dataset_id TEXT, config_hash TEXT, trades_hash TEXT, record_json TEXT)""",
    """CREATE TABLE IF NOT EXISTS metrics (run_id TEXT, scope TEXT, metric TEXT, value DOUBLE,
        n BIGINT)""",
]
TS_COLS = ("signal_ts", "entry_ts", "exit_ts")


def _trades_to_table(run_id: str, trades: pd.DataFrame) -> pd.DataFrame:
    df = trades.copy()
    for c in TS_COLS:
        if c in df.columns:
            df[c] = pd.DatetimeIndex(df[c]).as_unit("ns").asi8  # int64 ns UTC
    df.insert(0, "run_id", run_id)
    return df


def _trades_from_table(df: pd.DataFrame) -> pd.DataFrame:
    df = df.drop(columns=["run_id"])
    for c in TS_COLS:
        if c in df.columns:
            df[c] = pd.to_datetime(df[c].astype("int64"), unit="ns", utc=True)
    return df.sort_values("trade_no").reset_index(drop=True) if "trade_no" in df else df


class ResultStore(ABC):
    backend = "abstract"

    # --- run ids ---------------------------------------------------------------
    def next_run_id(self, year: int | None = None) -> str:
        year = year or datetime.now(timezone.utc).year
        prefix = f"RUN_{year}_"
        rows = self._query("SELECT run_id FROM runs WHERE run_id LIKE ?", (prefix + "%",))
        seq = max((int(r[0].rsplit("_", 1)[1]) for r in rows), default=0) + 1
        return f"{prefix}{seq:05d}"

    # --- datasets --------------------------------------------------------------
    def save_dataset(self, manifest: DatasetManifest, bars: BarArrays) -> None:
        if self._query("SELECT 1 FROM datasets WHERE dataset_id = ?", (manifest.dataset_id,)):
            existing = self._query("SELECT content_hash FROM datasets WHERE dataset_id = ?",
                                   (manifest.dataset_id,))[0][0]
            if existing != manifest.content_hash:
                raise ValueError(f"dataset_id {manifest.dataset_id} exists with different content")
            return
        df = pd.DataFrame({"dataset_id": manifest.dataset_id, "ts_ns": bars.ts_ns,
                           "open": bars.open, "high": bars.high, "low": bars.low,
                           "close": bars.close, "volume": bars.volume})
        self._write_bars(manifest.dataset_id, df)
        self._exec("INSERT INTO datasets VALUES (?, ?, ?, ?)",
                   (manifest.dataset_id, manifest.content_hash, json.dumps(manifest.to_dict()),
                    datetime.now(timezone.utc).isoformat()))

    def load_dataset(self, dataset_id: str, tf_minutes: int) -> tuple[DatasetManifest, BarArrays]:
        rows = self._query("SELECT manifest_json FROM datasets WHERE dataset_id = ?", (dataset_id,))
        if not rows:
            raise KeyError(dataset_id)
        manifest = DatasetManifest(**json.loads(rows[0][0]))
        df = self._read_bars(dataset_id)
        bars = BarArrays(df["ts_ns"].to_numpy(np.int64), df["open"].to_numpy(float),
                         df["high"].to_numpy(float), df["low"].to_numpy(float),
                         df["close"].to_numpy(float), df["volume"].to_numpy(float), tf_minutes)
        if bars.content_hash() != manifest.content_hash:
            raise ValueError(f"dataset {dataset_id}: stored bars do not match manifest hash")
        return manifest, bars

    # --- runs --------------------------------------------------------------------
    def save_run(self, run_id: str, record: dict, trades: pd.DataFrame, metrics: dict) -> None:
        self._exec("INSERT INTO runs VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   (run_id, record["created_at"], record.get("status", "IN_SAMPLE"),
                    record["strategy"]["strategy_id"], record["dataset"]["dataset_id"],
                    record["config_hash"], record["trades_hash"],
                    json.dumps(record, default=str, sort_keys=True)))
        if not trades.empty:
            self._append_table("trades", _trades_to_table(run_id, trades))
        n = int(metrics.get("trade_count", 0))
        rows = [(run_id, "full", k, float(v), n) for k, v in metrics.items()
                if isinstance(v, (int, float, np.floating, np.integer)) and not isinstance(v, bool)]
        self._executemany("INSERT INTO metrics VALUES (?, ?, ?, ?, ?)", rows)
        log.event("run_saved", run_id=run_id, trades=len(trades), backend=self.backend)

    def load_run(self, run_id: str) -> tuple[dict, pd.DataFrame]:
        rows = self._query("SELECT record_json FROM runs WHERE run_id = ?", (run_id,))
        if not rows:
            raise KeyError(run_id)
        record = json.loads(rows[0][0])
        if not self._has_table("trades"):
            return record, pd.DataFrame()
        tr = self._read_sql("SELECT * FROM trades WHERE run_id = ?", (run_id,))
        return record, (_trades_from_table(tr) if len(tr) else pd.DataFrame())

    def list_runs(self) -> pd.DataFrame:
        rows = self._query("SELECT run_id, created_at, status, strategy_id, dataset_id, trades_hash "
                           "FROM runs ORDER BY run_id")
        return pd.DataFrame(rows, columns=["run_id", "created_at", "status", "strategy_id",
                                           "dataset_id", "trades_hash"])

    # --- backend primitives ------------------------------------------------------
    @abstractmethod
    def _exec(self, sql: str, params: tuple = ()) -> None: ...
    @abstractmethod
    def _executemany(self, sql: str, rows: list) -> None: ...
    @abstractmethod
    def _query(self, sql: str, params: tuple = ()) -> list: ...
    @abstractmethod
    def _append_table(self, name: str, df: pd.DataFrame) -> None: ...
    @abstractmethod
    def _has_table(self, name: str) -> bool: ...
    @abstractmethod
    def _read_sql(self, sql: str, params: tuple = ()) -> pd.DataFrame: ...
    @abstractmethod
    def _write_bars(self, dataset_id: str, df: pd.DataFrame) -> None: ...
    @abstractmethod
    def _read_bars(self, dataset_id: str) -> pd.DataFrame: ...
    @abstractmethod
    def close(self) -> None: ...


class SQLiteStore(ResultStore):
    backend = "sqlite"

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = sqlite3.connect(self.path)
        for s in SCHEMA:
            self.con.execute(s.replace("DOUBLE", "REAL").replace("BIGINT", "INTEGER"))
        self.con.execute("CREATE INDEX IF NOT EXISTS ix_bars ON bars(dataset_id, ts_ns)")
        self.con.commit()

    def _exec(self, sql, params=()):
        self.con.execute(sql, params)
        self.con.commit()

    def _executemany(self, sql, rows):
        self.con.executemany(sql, rows)
        self.con.commit()

    def _query(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()

    def _append_table(self, name, df):
        df.to_sql(name, self.con, if_exists="append", index=False, chunksize=50_000)
        self.con.commit()

    def _has_table(self, name):
        return bool(self._query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))

    def _read_sql(self, sql, params=()):
        return pd.read_sql_query(sql, self.con, params=params)

    def _write_bars(self, dataset_id, df):
        self._append_table("bars", df)

    def _read_bars(self, dataset_id):
        return pd.read_sql_query("SELECT ts_ns, open, high, low, close, volume FROM bars "
                                 "WHERE dataset_id = ? ORDER BY ts_ns", self.con, params=(dataset_id,))

    def close(self):
        self.con.close()


class DuckDBStore(ResultStore):
    """DuckDB metadata + one Parquet file per dataset (written by DuckDB itself,
    so pyarrow is not required). Untested in the build sandbox (duckdb not
    installable offline); covered by tests/test_store.py once installed."""
    backend = "duckdb"

    def __init__(self, path: str | Path, parquet_dir: str | Path):
        import duckdb
        self.path = str(path)
        self.parquet_dir = Path(parquet_dir)
        self.parquet_dir.mkdir(parents=True, exist_ok=True)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.con = duckdb.connect(self.path)
        for s in SCHEMA:
            if "CREATE TABLE IF NOT EXISTS bars" in s:
                continue  # bars live in Parquet
            self.con.execute(s)

    def _exec(self, sql, params=()):
        self.con.execute(sql, list(params))

    def _executemany(self, sql, rows):
        if rows:
            self.con.executemany(sql, [list(r) for r in rows])

    def _query(self, sql, params=()):
        return self.con.execute(sql, list(params)).fetchall()

    def _append_table(self, name, df):
        self.con.register("_tmp_df", df)
        self.con.execute(f"CREATE TABLE IF NOT EXISTS {name} AS SELECT * FROM _tmp_df WHERE false")
        self.con.execute(f"INSERT INTO {name} BY NAME SELECT * FROM _tmp_df")
        self.con.unregister("_tmp_df")

    def _has_table(self, name):
        return bool(self._query("SELECT 1 FROM information_schema.tables WHERE table_name = ?", (name,)))

    def _read_sql(self, sql, params=()):
        return self.con.execute(sql, list(params)).df()

    def _pq(self, dataset_id: str) -> Path:
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in dataset_id)
        return self.parquet_dir / f"{safe}.parquet"

    def _write_bars(self, dataset_id, df):
        self.con.register("_bars_df", df.drop(columns=["dataset_id"]))
        self.con.execute(f"COPY (SELECT * FROM _bars_df ORDER BY ts_ns) TO '{self._pq(dataset_id)}' "
                         "(FORMAT PARQUET)")
        self.con.unregister("_bars_df")

    def _read_bars(self, dataset_id):
        return self.con.execute(f"SELECT ts_ns, open, high, low, close, volume FROM "
                                f"read_parquet('{self._pq(dataset_id)}') ORDER BY ts_ns").df()

    def close(self):
        self.con.close()


def open_store(cfg: dict, root: str | Path | None = None) -> ResultStore:
    scfg = cfg["storage"]
    root = Path(root) if root else Path(scfg.get("root", "."))
    backend = scfg["backend"]
    if backend in ("auto", "duckdb"):
        try:
            import duckdb  # noqa: F401
            return DuckDBStore(root / scfg["duckdb_path"], root / scfg["parquet_dir"])
        except ImportError:
            if backend == "duckdb":
                raise
            log.event("store_fallback", severity="WARNING",
                      error="duckdb not installed; using SQLite fallback")
    return SQLiteStore(root / scfg["sqlite_path"])
