"""DuckDB-backed observation, forecast, and recommendation store."""

from __future__ import annotations

import logging
from pathlib import Path

import duckdb
import polars as pl

logger = logging.getLogger(__name__)

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

CREATE TABLE IF NOT EXISTS recommendations (
    ts TIMESTAMP NOT NULL,
    workload VARCHAR NOT NULL,
    current_replicas INTEGER NOT NULL,
    recommended_replicas INTEGER NOT NULL,
    reason VARCHAR NOT NULL,
    confidence DOUBLE NOT NULL
);
"""


class Store:
    """Owns the DuckDB connection and schema for one ScaleScope instance."""

    def __init__(self, db_path: str) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = duckdb.connect(db_path)
        self._conn.execute(_SCHEMA)

    def insert_observation(self, row: dict) -> None:
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

    def insert_recommendation(self, row: dict) -> None:
        self._conn.execute(
            """
            INSERT INTO recommendations
            (ts, workload, current_replicas, recommended_replicas, reason, confidence)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            [
                row["ts"],
                row["workload"],
                row["current_replicas"],
                row["recommended_replicas"],
                row["reason"],
                row["confidence"],
            ],
        )

    def recent_observations(self, workload: str, limit: int) -> pl.DataFrame:
        return (
            self._conn.execute(
                """
            SELECT * FROM observations
            WHERE workload = ?
            ORDER BY ts DESC
            LIMIT ?
            """,
                [workload, limit],
            )
            .pl()
            .sort("ts")
        )

    def workloads(self) -> list[str]:
        rows = self._conn.execute(
            "SELECT DISTINCT workload FROM observations"
        ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        self._conn.close()
