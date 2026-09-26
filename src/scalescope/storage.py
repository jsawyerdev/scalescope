"""DuckDB-backed observation store."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import duckdb
import polars as pl

_OBSERVATION_COLUMNS = (
    "ts",
    "workload",
    "replicas",
    "request_rate",
    "cpu_usage_pct",
    "cpu_throttled_pct",
    "memory_usage_mb",
    "latency_p95_ms",
    "error_rate",
    "pending_pods",
    "restarts",
    "desired_replicas",
    "cpu_usage_millicores",
    "cpu_request_millicores",
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS observations (
    ts TIMESTAMP NOT NULL,
    workload VARCHAR NOT NULL,
    replicas INTEGER NOT NULL,
    request_rate DOUBLE NOT NULL,
    cpu_usage_pct DOUBLE NOT NULL,
    cpu_throttled_pct DOUBLE NOT NULL,
    memory_usage_mb DOUBLE NOT NULL,
    latency_p95_ms DOUBLE NOT NULL,
    error_rate DOUBLE NOT NULL,
    pending_pods INTEGER NOT NULL,
    restarts INTEGER NOT NULL
);
"""

# Columns added after the first release: existing databases gain them on
# startup, with values that keep old rows meaningful.
_MIGRATIONS = """
ALTER TABLE observations ADD COLUMN IF NOT EXISTS desired_replicas INTEGER;
UPDATE observations SET desired_replicas = replicas WHERE desired_replicas IS NULL;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS
    cpu_usage_millicores DOUBLE DEFAULT 0;
ALTER TABLE observations ADD COLUMN IF NOT EXISTS
    cpu_request_millicores DOUBLE DEFAULT 0;
"""

# Column order must match _OBSERVATION_COLUMNS, which supplies the values.
_INSERT = """
INSERT INTO observations
(ts, workload, replicas, request_rate, cpu_usage_pct, cpu_throttled_pct,
 memory_usage_mb, latency_p95_ms, error_rate, pending_pods, restarts,
 desired_replicas, cpu_usage_millicores, cpu_request_millicores)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _naive_utc(ts: datetime) -> datetime:
    """`ts` as a naive UTC datetime, the convention of the TIMESTAMP column.

    DuckDB converts a timezone-aware datetime to the process's local wall
    clock when binding it to a plain TIMESTAMP, which would shift every
    stored observation on any host not running in UTC.
    """
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


class Store:
    """Owns the DuckDB connection and schema for one ScaleScope instance.

    DuckDB connections are not safe for concurrent use from multiple threads;
    FastAPI runs sync route handlers in a thread pool and the data-source
    loops write through `asyncio.to_thread`, so every access to `_conn` must
    go through `_lock`.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = duckdb.connect(db_path)
        self._conn.execute(_SCHEMA)
        self._conn.execute(_MIGRATIONS)

    def insert_observation(self, row: dict[str, Any]) -> None:
        """Insert one observation; `row["ts"]` is stored as UTC."""
        values = [
            _naive_utc(row[column]) if column == "ts" else row[column]
            for column in _OBSERVATION_COLUMNS
        ]
        with self._lock:
            self._conn.execute(_INSERT, values)

    def recent_observations(self, workload: str, limit: int) -> pl.DataFrame:
        """The newest `limit` observations for `workload`, sorted ascending by ts."""
        with self._lock:
            result = self._conn.execute(
                """
                SELECT * FROM observations
                WHERE workload = ?
                ORDER BY ts DESC
                LIMIT ?
                """,
                [workload, limit],
            ).pl()
        if result.is_empty():
            # DuckDB's arrow conversion drops column schema on a zero-row
            # result; rebuild it so callers can still index known columns.
            return pl.DataFrame(schema={c: pl.Null for c in _OBSERVATION_COLUMNS})
        return result.sort("ts")

    def prune(self, older_than: datetime) -> int:
        """Delete observations older than `older_than`; returns rows deleted."""
        with self._lock:
            deleted = self._conn.execute(
                "DELETE FROM observations WHERE ts < ?", [_naive_utc(older_than)]
            ).fetchone()
        return int(deleted[0]) if deleted else 0

    def workloads(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT workload FROM observations"
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
