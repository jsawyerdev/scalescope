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

# One row per workload per complete minute, kept far longer than raw
# observations: the long-memory model learns daily and weekly patterns
# from it. Means over the minute's observations.
_MINUTE_SCHEMA = """
CREATE TABLE IF NOT EXISTS demand_minutes (
    workload VARCHAR NOT NULL,
    minute TIMESTAMP NOT NULL,
    samples INTEGER NOT NULL,
    request_rate DOUBLE NOT NULL,
    cpu_usage_millicores DOUBLE NOT NULL,
    replicas DOUBLE NOT NULL,
    latency_p95_ms DOUBLE NOT NULL,
    PRIMARY KEY (workload, minute)
);
"""
MINUTE_COLUMNS = (
    "workload",
    "minute",
    "samples",
    "request_rate",
    "cpu_usage_millicores",
    "replicas",
    "latency_p95_ms",
)

# Each long-memory forecast, kept so it can be scored against what then
# happened: live, out-of-sample accuracy rather than a backtest.
_FORECAST_LOG_SCHEMA = """
CREATE TABLE IF NOT EXISTS forecast_log (
    workload VARCHAR NOT NULL,
    made_at TIMESTAMP NOT NULL,
    minute TIMESTAMP NOT NULL,
    signal VARCHAR NOT NULL,
    p50 DOUBLE NOT NULL,
    p90 DOUBLE NOT NULL,
    baseline DOUBLE NOT NULL,
    PRIMARY KEY (workload, made_at, minute)
);
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
        self._conn.execute(_MINUTE_SCHEMA)
        self._conn.execute(_FORECAST_LOG_SCHEMA)

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

    def prune_minutes(self, older_than: datetime) -> int:
        """Delete minute rollups older than `older_than`; returns rows deleted."""
        with self._lock:
            deleted = self._conn.execute(
                "DELETE FROM demand_minutes WHERE minute < ?", [_naive_utc(older_than)]
            ).fetchone()
        return int(deleted[0]) if deleted else 0

    def prune_forecast_log(self, older_than: datetime) -> int:
        """Delete logged forecasts for minutes before `older_than`."""
        with self._lock:
            deleted = self._conn.execute(
                "DELETE FROM forecast_log WHERE minute < ?", [_naive_utc(older_than)]
            ).fetchone()
        return int(deleted[0]) if deleted else 0

    def roll_up_minutes(self, before: datetime, since: datetime | None = None) -> int:
        """Aggregate every complete minute before `before` into `demand_minutes`.

        Idempotent: a minute already rolled up is left alone, so this can run
        repeatedly over overlapping ranges. `since` bounds the scan; None
        rolls up all retained observations (at startup, after downtime).
        Returns the number of minutes added.
        """
        with self._lock:
            added = self._conn.execute(
                """
                INSERT INTO demand_minutes
                SELECT
                    workload,
                    date_trunc('minute', ts) AS minute,
                    count(*),
                    avg(request_rate),
                    avg(cpu_usage_millicores),
                    avg(replicas),
                    avg(latency_p95_ms)
                FROM observations
                WHERE ts < date_trunc('minute', ?::TIMESTAMP)
                  AND ts >= coalesce(?::TIMESTAMP, '-infinity'::TIMESTAMP)
                GROUP BY workload, minute
                ON CONFLICT DO NOTHING
                """,
                [_naive_utc(before), _naive_utc(since) if since else None],
            ).fetchone()
        return int(added[0]) if added else 0

    def insert_minutes(self, minutes: pl.DataFrame) -> int:
        """Add minute rows (columns `MINUTE_COLUMNS`); existing minutes are kept."""
        rows = minutes.select(MINUTE_COLUMNS)
        with self._lock:
            self._conn.register("new_minutes", rows)
            try:
                added = self._conn.execute(
                    "INSERT INTO demand_minutes SELECT * FROM new_minutes "
                    "ON CONFLICT DO NOTHING"
                ).fetchone()
            finally:
                self._conn.unregister("new_minutes")
        return int(added[0]) if added else 0

    def minute_history(self, workload: str, since: datetime) -> pl.DataFrame:
        """Minute rollups for `workload` from `since`, sorted ascending by minute."""
        with self._lock:
            result = self._conn.execute(
                """
                SELECT * FROM demand_minutes
                WHERE workload = ? AND minute >= ?
                ORDER BY minute
                """,
                [workload, _naive_utc(since)],
            ).pl()
        if result.is_empty():
            return pl.DataFrame(schema={c: pl.Null for c in MINUTE_COLUMNS})
        return result

    def log_forecast(
        self,
        workload: str,
        made_at: datetime,
        signal: str,
        minutes: list[datetime],
        p50: list[float],
        p90: list[float],
        baseline: float,
    ) -> None:
        """Record one long-memory forecast for later scoring."""
        rows = [
            (workload, _naive_utc(made_at), _naive_utc(m), signal, lo, hi, baseline)
            for m, lo, hi in zip(minutes, p50, p90, strict=True)
        ]
        with self._lock:
            self._conn.executemany(
                "INSERT INTO forecast_log VALUES (?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT DO NOTHING",
                rows,
            )

    def forecast_accuracy(self, workload: str, since: datetime) -> pl.DataFrame:
        """Logged forecasts since `since` joined with what then happened.

        Columns: day (UTC date of the forecast minute), minutes scored,
        model and baseline mean absolute error as a fraction of actual
        demand, and the share of minutes at or below p90.
        """
        with self._lock:
            return self._conn.execute(
                """
                WITH scored AS (
                    SELECT
                        f.minute, f.p50, f.p90, f.baseline,
                        CASE f.signal
                            WHEN 'request_rate' THEN m.request_rate
                            ELSE m.cpu_usage_millicores
                        END AS actual
                    FROM forecast_log f
                    JOIN demand_minutes m USING (workload, minute)
                    WHERE f.workload = ? AND f.minute >= ?
                )
                SELECT
                    CAST(minute AS DATE) AS day,
                    count(*) AS minutes,
                    sum(abs(actual - p50)) / nullif(sum(actual), 0) AS model_error,
                    sum(abs(actual - baseline)) / nullif(sum(actual), 0)
                        AS baseline_error,
                    avg(CASE WHEN actual <= p90 THEN 1.0 ELSE 0.0 END) AS p90_coverage
                FROM scored
                GROUP BY day
                ORDER BY day
                """,
                [workload, _naive_utc(since)],
            ).pl()

    def workloads(self) -> list[str]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT DISTINCT workload FROM observations"
            ).fetchall()
        return [r[0] for r in rows]

    def close(self) -> None:
        with self._lock:
            self._conn.close()
