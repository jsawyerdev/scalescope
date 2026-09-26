from __future__ import annotations

import os
import subprocess
import sys
import textwrap
from pathlib import Path

from scalescope import storage


def test_timestamps_round_trip_as_utc_on_non_utc_host(tmp_path: Path) -> None:
    # A fresh interpreter, because the process timezone must be in effect
    # before DuckDB binds its first timestamp.
    script = textwrap.dedent(f"""
        from datetime import UTC, datetime
        from scalescope.storage import Store

        store = Store({str(tmp_path / "tz.duckdb")!r})
        store.insert_observation(
            {{
                "ts": datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
                "workload": "w",
                "replicas": 1,
                "request_rate": 1.0,
                "cpu_usage_pct": 1.0,
                "cpu_throttled_pct": 0.0,
                "memory_usage_mb": 1.0,
                "latency_p95_ms": 1.0,
                "error_rate": 0.0,
                "pending_pods": 0,
                "restarts": 0,
                "desired_replicas": 1,
                "cpu_usage_millicores": 0.0,
                "cpu_request_millicores": 0.0,
            }}
        )
        print(store.recent_observations("w", 1)["ts"][0].isoformat())
        """)
    result = subprocess.run(
        [sys.executable, "-c", script],
        env={**os.environ, "TZ": "America/New_York"},
        capture_output=True,
        text=True,
        check=True,
    )

    assert result.stdout.strip() == "2026-01-01T12:00:00"


def test_insert_statement_columns_match_observation_columns() -> None:
    insert_columns = (
        storage._INSERT.split("(", 1)[1].split(")", 1)[0].replace("\n", " ").split(",")
    )
    assert tuple(column.strip() for column in insert_columns) == (
        storage._OBSERVATION_COLUMNS
    )
