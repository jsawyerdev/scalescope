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


def _observation(ts: object, request_rate: float) -> dict[str, object]:
    return {
        "ts": ts,
        "workload": "w",
        "replicas": 2,
        "request_rate": request_rate,
        "cpu_usage_pct": 50.0,
        "cpu_throttled_pct": 0.0,
        "memory_usage_mb": 1.0,
        "latency_p95_ms": 40.0,
        "error_rate": 0.0,
        "pending_pods": 0,
        "restarts": 0,
        "desired_replicas": 2,
        "cpu_usage_millicores": 500.0,
        "cpu_request_millicores": 1000.0,
    }


def test_minute_rollups_cover_complete_minutes_once(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    store = storage.Store(str(tmp_path / "minutes.duckdb"))
    start = datetime(2026, 1, 5, 10, 0, tzinfo=UTC)
    for i in range(150):  # 5 minutes at 2s ticks
        store.insert_observation(_observation(start + timedelta(seconds=2 * i), i))
    now = start + timedelta(minutes=4, seconds=30)

    assert store.roll_up_minutes(now) == 4  # minute 4 is still in progress
    assert store.roll_up_minutes(now) == 0
    assert store.roll_up_minutes(start + timedelta(minutes=6)) == 1

    history = store.minute_history("w", start)
    assert history["samples"].to_list() == [30] * 5
    assert history["request_rate"][0] == sum(range(30)) / 30

    assert store.prune_minutes(start + timedelta(minutes=2)) == 2
    assert store.minute_history("w", start).height == 3


def test_forecast_accuracy_scores_against_later_minutes(tmp_path: Path) -> None:
    from datetime import UTC, datetime, timedelta

    import polars as pl

    store = storage.Store(str(tmp_path / "accuracy.duckdb"))
    minute = datetime(2026, 1, 5, 10, 0, tzinfo=UTC).replace(tzinfo=None)
    store.insert_minutes(
        pl.DataFrame(
            {
                "workload": ["w", "w"],
                "minute": [minute, minute + timedelta(minutes=1)],
                "samples": [30, 30],
                "request_rate": [100.0, 200.0],
                "cpu_usage_millicores": [0.0, 0.0],
                "replicas": [2.0, 2.0],
                "latency_p95_ms": [40.0, 40.0],
            }
        )
    )
    store.log_forecast(
        "w",
        made_at=minute,
        signal="request_rate",
        minutes=[minute, minute + timedelta(minutes=1)],
        p50=[110.0, 180.0],
        p90=[150.0, 190.0],
        baseline=100.0,
    )

    (row,) = store.forecast_accuracy("w", minute).to_dicts()

    assert row["minutes"] == 2
    assert row["model_error"] == (10 + 20) / 300
    assert row["baseline_error"] == 100 / 300
    assert row["p90_coverage"] == 0.5

    assert store.prune_forecast_log(minute + timedelta(minutes=1)) == 1
    assert store.forecast_accuracy("w", minute)["minutes"].to_list() == [1]
