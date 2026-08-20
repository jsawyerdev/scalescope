"""DuckDB-backed observation, forecast, and recommendation store."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

import duckdb
import polars as pl

logger = logging.getLogger(__name__)

_OBSERVATION_COLUMNS = [
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
]

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


class Store:
    """Owns the DuckDB connection and schema for one ScaleScope instance.

    DuckDB connections are not safe for concurrent use from multiple threads;
    FastAPI runs sync route handlers in a thread pool while the simulation
    loop writes from the event-loop thread, so every access to `_conn` must
    go through `_lock`.
    """

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = duckdb.connect(db_path)
        self._conn.execute(_SCHEMA)

    def insert_observation(self, row: dict) -> None:
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO observations
                (ts, workload, replicas, request_rate, cpu_usage_pct, cpu_throttled_pct,
                 memory_usage_mb, latency_p95_ms, error_rate, pending_pods, restarts)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    row["ts"],
                    row["workload"],
                    row["replicas"],
                    row["request_rate"],
                    row["cpu_usage_pct"],
                    row["cpu_throttled_pct"],
                    row["memory_usage_mb"],
                    row["latency_p95_ms"],
                    row["error_rate"],
                    row["pending_pods"],
                    row["restarts"],
                ],
            )

    def recent_observations(self, workload: str, limit: int) -> pl.DataFrame:
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
            # result, so an empty frame must be rebuilt with the known
            # columns before any caller can safely .sort("ts") it.
            return pl.DataFrame(schema={c: pl.Null for c in _OBSERVATION_COLUMNS})
        return result.sort("ts")

    def workloads(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT workload FROM observations"
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
