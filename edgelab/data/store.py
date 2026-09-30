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
from edgelab.data.schema import ASK_COLUMNS, BarArrays, DatasetManifest

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
    """CREATE TABLE IF NOT EXISTS dataset_reports (dataset_id TEXT PRIMARY KEY, report_json TEXT)""",
]
# Phase 4 search storage (SQLite only; see ResultStore._require_search_storage).
SEARCH_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS search_batches (search_id TEXT PRIMARY KEY, search_hash TEXT,
        config_hash TEXT, code_version TEXT, created_at TEXT, finished_at TEXT, status TEXT,
        spec_json TEXT, n_planned INTEGER, n_eligible INTEGER, n_ineligible INTEGER,
        n_evaluated INTEGER, n_skipped_resume INTEGER, n_failed INTEGER, n_cancelled INTEGER,
        n_trials INTEGER, shortlist_json TEXT, warnings_json TEXT, protocol_id TEXT)""",
    """CREATE TABLE IF NOT EXISTS search_cells (search_id TEXT, cell_id TEXT, plan_index INTEGER,
        strategy_id TEXT, dataset_id TEXT, dataset_content_hash TEXT, status TEXT, run_id TEXT,
        trades_hash TEXT, reasons_json TEXT, error TEXT, headline_json TEXT, current INTEGER,
        PRIMARY KEY (search_id, cell_id))""",
]
SEARCH_BATCH_COLS = ("search_id", "search_hash", "config_hash", "code_version", "created_at", "finished_at",
                     "status", "spec_json", "n_planned", "n_eligible", "n_ineligible", "n_evaluated",
                     "n_skipped_resume", "n_failed", "n_cancelled", "n_trials", "shortlist_json", "warnings_json",
                     "protocol_id")
SEARCH_CELL_COLS = ("search_id", "cell_id", "plan_index", "strategy_id", "dataset_id", "dataset_content_hash",
                    "status", "run_id", "trades_hash", "reasons_json", "error", "headline_json", "current")
# ADR-56 research protocol storage (SQLite only, like search storage): insert-only protocol records,
# the program-level trial ledger, proposal attempts and the holdout-access ledger.
PROTOCOL_SCHEMA = [
    """CREATE TABLE IF NOT EXISTS research_protocols (protocol_id TEXT PRIMARY KEY, material_hash TEXT,
        record_json TEXT, status TEXT, created_at TEXT, status_changed_at TEXT, scope_key TEXT)""",
    """CREATE TABLE IF NOT EXISTS protocol_trials (event_id INTEGER PRIMARY KEY AUTOINCREMENT, protocol_id TEXT,
        trial_id TEXT, trial_key TEXT, counted INTEGER, status TEXT, entry_point TEXT, strategy_id TEXT,
        logic_hash TEXT, definition_hash TEXT, family TEXT, dataset_id TEXT, source_dataset_id TEXT,
        evaluated_content_hash TEXT, window_start TEXT, window_end TEXT, config_hash TEXT, cost_scenario TEXT,
        proposal_id TEXT, search_id TEXT, run_id TEXT, error TEXT, created_at TEXT)""",
    """CREATE TABLE IF NOT EXISTS protocol_proposals (protocol_id TEXT, proposal_id TEXT, source TEXT,
        generation_id TEXT, gate_status TEXT, logic_hash TEXT, strategy_id TEXT, created_at TEXT,
        PRIMARY KEY (protocol_id, proposal_id))""",
    """CREATE TABLE IF NOT EXISTS holdout_access (access_id TEXT PRIMARY KEY, protocol_id TEXT, strategy_id TEXT,
        logic_hash TEXT, definition_hash TEXT, frozen_hash TEXT, search_id TEXT, status TEXT, reason_code TEXT,
        reason TEXT, run_id TEXT, result_json TEXT, created_at TEXT, completed_at TEXT)""",
]
TRIAL_COLS = ("protocol_id", "trial_id", "trial_key", "counted", "status", "entry_point", "strategy_id", "logic_hash",
              "definition_hash", "family", "dataset_id", "source_dataset_id", "evaluated_content_hash", "window_start",
              "window_end", "config_hash", "cost_scenario", "proposal_id", "search_id", "run_id", "error", "created_at")
HOLDOUT_COLS = ("access_id", "protocol_id", "strategy_id", "logic_hash", "definition_hash", "frozen_hash", "search_id",
                "status", "reason_code", "reason", "run_id", "result_json", "created_at", "completed_at")
TS_COLS = ("signal_ts", "entry_ts", "exit_ts")


class SearchStorageUnsupported(NotImplementedError):
    """Phase 4 search storage is SQLite-backed only."""


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
    def save_dataset(self, manifest: DatasetManifest, bars: BarArrays, report: Any = None) -> bool:
        """Store an immutable validated dataset. Returns False if it already existed (same content)."""
        if self._query("SELECT 1 FROM datasets WHERE dataset_id = ?", (manifest.dataset_id,)):
            existing = self._query("SELECT content_hash FROM datasets WHERE dataset_id = ?",
                                   (manifest.dataset_id,))[0][0]
            if existing != manifest.content_hash:
                raise ValueError(f"dataset_id {manifest.dataset_id} exists with different content")
            return False
        df = pd.DataFrame({"dataset_id": manifest.dataset_id, "ts_ns": bars.ts_ns,
                           "open": bars.open, "high": bars.high, "low": bars.low,
                           "close": bars.close, "volume": bars.volume})
        if bars.spread is not None:
            df["spread"] = bars.spread
        for k in ASK_COLUMNS if bars.has_ask_ohlc else ():
            df[k] = getattr(bars, k)
        self._write_bars(manifest.dataset_id, df)
        self._exec("INSERT INTO datasets VALUES (?, ?, ?, ?)",
                   (manifest.dataset_id, manifest.content_hash, json.dumps(manifest.to_dict()),
                    datetime.now(timezone.utc).isoformat()))
        if report is not None:
            self._exec("INSERT INTO dataset_reports VALUES (?, ?)",
                       (manifest.dataset_id, json.dumps(report.to_dict(), default=str)))
        return True

    @staticmethod
    def _ask_arrays(manifest: DatasetManifest, df: pd.DataFrame) -> dict:
        """Observed ASK OHLC (ADR-55), only for datasets whose manifest declares it."""
        if not manifest.has_ask_ohlc:
            return {}
        missing = [k for k in ASK_COLUMNS if k not in df.columns]
        if missing:
            raise ValueError(f"dataset {manifest.dataset_id}: manifest declares ASK OHLC but {missing} not stored")
        return {k: pd.to_numeric(df[k], errors="coerce").to_numpy(float) for k in ASK_COLUMNS}

    def load_dataset(self, dataset_id: str, tf_minutes: int) -> tuple[DatasetManifest, BarArrays]:
        rows = self._query("SELECT manifest_json FROM datasets WHERE dataset_id = ?", (dataset_id,))
        if not rows:
            raise KeyError(dataset_id)
        manifest = DatasetManifest.from_dict(json.loads(rows[0][0]))
        df = self._read_bars(dataset_id)
        spread = None
        if manifest.has_spread and "spread" in df.columns:
            spread = pd.to_numeric(df["spread"], errors="coerce").to_numpy(float)
        vol = pd.to_numeric(df["volume"], errors="coerce").to_numpy(float)
        bars = BarArrays(df["ts_ns"].to_numpy(np.int64), df["open"].to_numpy(float),
                         df["high"].to_numpy(float), df["low"].to_numpy(float),
                         df["close"].to_numpy(float), vol, tf_minutes, spread,
                         **self._ask_arrays(manifest, df))
        if bars.content_hash() != manifest.content_hash:
            raise ValueError(f"dataset {dataset_id}: stored bars do not match manifest hash")
        return manifest, bars

    def list_datasets(self) -> list[dict]:
        """Manifests of all stored datasets (for the Data Center)."""
        rows = self._query("SELECT manifest_json FROM datasets ORDER BY dataset_id")
        return [DatasetManifest.from_dict(json.loads(r[0])).to_dict() for r in rows]

    def get_manifest(self, dataset_id: str) -> DatasetManifest:
        rows = self._query("SELECT manifest_json FROM datasets WHERE dataset_id = ?", (dataset_id,))
        if not rows:
            raise KeyError(dataset_id)
        return DatasetManifest.from_dict(json.loads(rows[0][0]))

    def get_report(self, dataset_id: str) -> dict | None:
        rows = self._query("SELECT report_json FROM dataset_reports WHERE dataset_id = ?", (dataset_id,))
        return json.loads(rows[0][0]) if rows else None

    def find_by_content_hash(self, content_hash: str) -> list[str]:
        return [r[0] for r in self._query("SELECT dataset_id FROM datasets WHERE content_hash = ?",
                                          (content_hash,))]

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

    # --- Phase 4 search batches / cells (SQLite only) -----------------------------
    def _require_search_storage(self) -> None:
        if self.backend != "sqlite":
            raise SearchStorageUnsupported(
                f"search storage is SQLite-backed only (this store is {self.backend}); set "
                "storage.backend: sqlite to run batch searches")

    def upsert_search_batch(self, row: dict) -> None:
        """Insert or replace one search batch row (columns SEARCH_BATCH_COLS; *_json values as text)."""
        self._require_search_storage()
        cols = ", ".join(SEARCH_BATCH_COLS)
        marks = ", ".join("?" for _ in SEARCH_BATCH_COLS)
        self._exec(f"INSERT OR REPLACE INTO search_batches ({cols}) VALUES ({marks})",
                   tuple(row.get(c) for c in SEARCH_BATCH_COLS))

    def update_search_batch(self, search_id: str, **fields) -> None:
        self._require_search_storage()
        bad = set(fields) - set(SEARCH_BATCH_COLS) - {"search_id"}
        if bad or "search_id" in fields:
            raise ValueError(f"unknown/immutable search batch fields: {sorted(bad | ({'search_id'} & set(fields)))}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE search_batches SET {sets} WHERE search_id = ?", (*fields.values(), search_id))

    def upsert_search_cell(self, row: dict) -> None:
        self._require_search_storage()
        cols = ", ".join(SEARCH_CELL_COLS)
        marks = ", ".join("?" for _ in SEARCH_CELL_COLS)
        self._exec(f"INSERT OR REPLACE INTO search_cells ({cols}) VALUES ({marks})",
                   tuple(row.get(c) for c in SEARCH_CELL_COLS))

    def get_search_batch(self, search_id: str) -> dict | None:
        self._require_search_storage()
        rows = self._query(f"SELECT {', '.join(SEARCH_BATCH_COLS)} FROM search_batches WHERE search_id = ?",
                           (search_id,))
        return dict(zip(SEARCH_BATCH_COLS, rows[0])) if rows else None

    def list_search_cells(self, search_id: str, current: bool | None = None) -> list[dict]:
        """Cells of a search in plan order; `current=True` only those in the latest plan,
        `False` only historical ones (kept for research history, never deleted)."""
        self._require_search_storage()
        where = "" if current is None else f" AND current = {1 if current else 0}"
        rows = self._query(f"SELECT {', '.join(SEARCH_CELL_COLS)} FROM search_cells WHERE search_id = ?{where} "
                           "ORDER BY plan_index, cell_id", (search_id,))
        return [dict(zip(SEARCH_CELL_COLS, r)) for r in rows]

    def mark_current_search_cells(self, search_id: str, cell_ids: list[str]) -> None:
        """Flag exactly `cell_ids` as the search's current plan; other rows become historical."""
        self._require_search_storage()
        self._exec("UPDATE search_cells SET current = 0 WHERE search_id = ?", (search_id,))
        self._executemany("UPDATE search_cells SET current = 1 WHERE search_id = ? AND cell_id = ?",
                          [(search_id, c) for c in cell_ids])

    def list_search_batches(self) -> list[dict]:
        self._require_search_storage()
        rows = self._query(f"SELECT {', '.join(SEARCH_BATCH_COLS)} FROM search_batches ORDER BY created_at, search_id")
        return [dict(zip(SEARCH_BATCH_COLS, r)) for r in rows]

    # --- ADR-56 research protocol (SQLite only) -------------------------------------------
    def save_protocol(self, record: dict, scope_key: str) -> bool:
        """Insert-only. The same id with the same material is a no-op (False); the same id with
        different material cannot happen honestly and is refused (PROTOCOL_IMMUTABLE)."""
        from edgelab.research.protocol import ProtocolRefusal, verify_record
        self._require_search_storage()
        verify_record(record)                    # id and material_hash must both be the hash of the material
        rows = self._query("SELECT material_hash FROM research_protocols WHERE protocol_id = ?", (record["protocol_id"],))
        if rows:
            if rows[0][0] != record["material_hash"]:
                raise ProtocolRefusal("PROTOCOL_IMMUTABLE", "a protocol's material fields can never be changed; "
                                      "create a new protocol", protocol_id=record["protocol_id"])
            return False
        self._exec("INSERT INTO research_protocols VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (record["protocol_id"], record["material_hash"], json.dumps(record, sort_keys=True),
                    record["status"], record["created_at"], None, scope_key))
        return True

    def get_protocol(self, protocol_id: str) -> dict:
        self._require_search_storage()
        rows = self._query("SELECT record_json, status, status_changed_at FROM research_protocols WHERE protocol_id = ?",
                           (protocol_id,))
        if not rows:
            raise KeyError(protocol_id)
        rec = json.loads(rows[0][0])
        rec["status"], rec["status_changed_at"] = rows[0][1], rows[0][2]     # lifecycle only; material is immutable
        return rec

    def list_protocols(self, scope_key: str | None = None, status: str | None = None) -> list[dict]:
        self._require_search_storage()
        rows = self._query("SELECT protocol_id FROM research_protocols WHERE (? IS NULL OR scope_key = ?) "
                           "AND (? IS NULL OR status = ?) ORDER BY created_at, protocol_id",
                           (scope_key, scope_key, status, status))
        return [self.get_protocol(r[0]) for r in rows]

    def retire_protocol(self, protocol_id: str) -> None:
        self._require_search_storage()
        self._exec("UPDATE research_protocols SET status = 'RETIRED', status_changed_at = ? "
                   "WHERE protocol_id = ? AND status = 'ACTIVE'", (datetime.now(timezone.utc).isoformat(), protocol_id))

    def add_trial_event(self, row: dict) -> bool:
        """Append one evaluation event; ``counted`` is decided here: the first completed event of a
        (protocol, trial_key) is the trial, every later one is a recorded duplicate."""
        self._require_search_storage()
        counted = row["status"] == "completed" and not self._query(
            "SELECT 1 FROM protocol_trials WHERE protocol_id = ? AND trial_key = ? AND counted = 1",
            (row["protocol_id"], row["trial_key"]))
        vals = {**row, "counted": 1 if counted else 0}
        self._exec(f"INSERT INTO protocol_trials ({', '.join(TRIAL_COLS)}) VALUES ({', '.join('?' for _ in TRIAL_COLS)})",
                   tuple(vals.get(c) for c in TRIAL_COLS))
        return bool(counted)

    def trial_counted(self, protocol_id: str, trial_key: str) -> bool:
        return bool(self._query("SELECT 1 FROM protocol_trials WHERE protocol_id = ? AND trial_key = ? AND counted = 1",
                                (protocol_id, trial_key)))

    def count_trials(self, protocol_id: str) -> int:
        return int(self._query("SELECT COUNT(*) FROM protocol_trials WHERE protocol_id = ? AND counted = 1",
                               (protocol_id,))[0][0])

    def list_trial_events(self, protocol_id: str) -> list[dict]:
        self._require_search_storage()
        rows = self._query(f"SELECT {', '.join(TRIAL_COLS)} FROM protocol_trials WHERE protocol_id = ? ORDER BY event_id",
                           (protocol_id,))
        return [dict(zip(TRIAL_COLS, r)) for r in rows]

    def add_proposal_attempt(self, row: dict) -> bool:
        self._require_search_storage()
        if self._query("SELECT 1 FROM protocol_proposals WHERE protocol_id = ? AND proposal_id = ?",
                       (row["protocol_id"], row["proposal_id"])):
            return False                                     # the same generation re-stored: one attempt
        self._exec("INSERT INTO protocol_proposals VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                   tuple(row.get(c) for c in ("protocol_id", "proposal_id", "source", "generation_id", "gate_status",
                                              "logic_hash", "strategy_id", "created_at")))
        return True

    def list_proposal_attempts(self, protocol_id: str) -> list[dict]:
        cols = ("protocol_id", "proposal_id", "source", "generation_id", "gate_status", "logic_hash", "strategy_id",
                "created_at")
        rows = self._query(f"SELECT {', '.join(cols)} FROM protocol_proposals WHERE protocol_id = ? "
                           "ORDER BY created_at, proposal_id", (protocol_id,))
        return [dict(zip(cols, r)) for r in rows]

    def add_holdout_access(self, row: dict) -> None:
        self._require_search_storage()
        self._exec(f"INSERT INTO holdout_access ({', '.join(HOLDOUT_COLS)}) VALUES ({', '.join('?' for _ in HOLDOUT_COLS)})",
                   tuple(row.get(c) for c in HOLDOUT_COLS))

    def update_holdout_access(self, access_id: str, **fields) -> None:
        bad = set(fields) - {"status", "run_id", "result_json", "completed_at", "reason", "reason_code"}
        if bad:
            raise ValueError(f"immutable holdout-access fields: {sorted(bad)}")
        sets = ", ".join(f"{k} = ?" for k in fields)
        self._exec(f"UPDATE holdout_access SET {sets} WHERE access_id = ?", (*fields.values(), access_id))

    def list_holdout_access(self, protocol_id: str) -> list[dict]:
        self._require_search_storage()
        rows = self._query(f"SELECT {', '.join(HOLDOUT_COLS)} FROM holdout_access WHERE protocol_id = ? "
                           "ORDER BY created_at, access_id", (protocol_id,))
        return [dict(zip(HOLDOUT_COLS, r)) for r in rows]

    def has_run(self, run_id: str) -> bool:
        return bool(self._query("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)))

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
        # check_same_thread=False: the web app (Phase 3.5) serves requests on worker threads and
        # serializes every store access behind one lock, so the connection is never shared
        # concurrently. Single-threaded callers (CLI, tests) are unaffected.
        self.con = sqlite3.connect(self.path, check_same_thread=False)
        for s in SCHEMA + SEARCH_SCHEMA + PROTOCOL_SCHEMA:
            self.con.execute(s.replace("DOUBLE", "REAL").replace("BIGINT", "INTEGER"))
        self.con.execute("CREATE INDEX IF NOT EXISTS ix_bars ON bars(dataset_id, ts_ns)")
        self._add_missing_columns("bars", {"spread": "REAL"})   # Phase 2 migration
        self._add_missing_columns("bars", {k: "REAL" for k in ASK_COLUMNS})   # ADR-55: NULL for older rows
        self._add_missing_columns("search_batches", {"protocol_id": "TEXT"})   # ADR-56: NULL = no protocol
        self.con.commit()

    def _columns(self, name):
        return [r[1] for r in self.con.execute(f"PRAGMA table_info({name})").fetchall()]

    def _add_missing_columns(self, name, wanted: dict):
        have = set(self._columns(name))
        for col, typ in wanted.items():
            if col not in have:
                self.con.execute(f'ALTER TABLE {name} ADD COLUMN "{col}" {typ}')

    def _exec(self, sql, params=()):
        self.con.execute(sql, params)
        self.con.commit()

    def _executemany(self, sql, rows):
        self.con.executemany(sql, rows)
        self.con.commit()

    def _query(self, sql, params=()):
        return self.con.execute(sql, params).fetchall()

    def _append_table(self, name, df):
        if self._has_table(name):   # schema evolution: new result columns are added, never dropped
            self._add_missing_columns(name, {c: "REAL" if df[c].dtype.kind in "fiub" else "TEXT"
                                             for c in df.columns})
        df.to_sql(name, self.con, if_exists="append", index=False, chunksize=50_000)
        self.con.commit()

    def _has_table(self, name):
        return bool(self._query("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)))

    def _read_sql(self, sql, params=()):
        return pd.read_sql_query(sql, self.con, params=params)

    def _write_bars(self, dataset_id, df):
        self._append_table("bars", df)

    def _read_bars(self, dataset_id):
        return pd.read_sql_query("SELECT ts_ns, open, high, low, close, volume, spread, "
                                 + ", ".join(ASK_COLUMNS) + " FROM bars "
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
        have = {r[0] for r in self.con.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_name = ?", [name]).fetchall()}
        for c in df.columns:
            if c not in have:
                typ = "DOUBLE" if df[c].dtype.kind in "fiub" else "VARCHAR"
                self.con.execute(f'ALTER TABLE {name} ADD COLUMN "{c}" {typ}')
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
        return self.con.execute(f"SELECT * FROM read_parquet('{self._pq(dataset_id)}') "
                                "ORDER BY ts_ns").df()

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
