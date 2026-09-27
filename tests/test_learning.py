from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import numpy as np
import polars as pl
import pytest
from _traffic import MINUTES_PER_DAY, MONDAY, weekly_traffic

from scalescope.learning import Learner
from scalescope.storage import Store

HORIZON = 30


def _store_with_history(
    tmp_path: Path, days: int, *, gap: slice | None = None, latency: bool = False
) -> tuple[Store, datetime]:
    store = Store(str(tmp_path / "learning.duckdb"))
    demand = weekly_traffic(days)
    minutes = [MONDAY + timedelta(minutes=i) for i in range(len(demand))]
    replicas = np.full(len(demand), 5.0)
    per_pod = demand / replicas
    # Queueing latency, saturating at 400 requests/s per pod.
    latency_ms = 20 + 3000 / np.maximum(400 - per_pod, 1) if latency else per_pod * 0
    frame = pl.DataFrame(
        {
            "workload": ["w"] * len(demand),
            "minute": minutes,
            "samples": [30] * len(demand),
            "request_rate": demand,
            "cpu_usage_millicores": demand,
            "replicas": replicas,
            "latency_p95_ms": latency_ms,
        }
    )
    if gap is not None:
        frame = frame.with_row_index().filter(
            ~pl.col("index").is_between(gap.start, gap.stop - 1)
        )
        frame = frame.drop("index")
    store.insert_minutes(frame)
    now = (MONDAY + timedelta(minutes=len(demand))).replace(tzinfo=UTC)
    return store, now


def test_no_model_until_a_day_of_history(tmp_path: Path) -> None:
    store, now = _store_with_history(tmp_path, 1)
    store.prune_minutes(datetime(2026, 1, 5, 1, 0, tzinfo=UTC))
    learner = Learner(store, HORIZON)

    assert learner.retrain("w", "request_rate", now) is None
    assert learner.forecast("w", "request_rate", now) is None
    status = learner.status("w", now)
    assert not status.knows_daily_pattern
    assert status.history_minutes == MINUTES_PER_DAY - 60


def test_forecast_starts_at_the_current_minute(tmp_path: Path) -> None:
    store, now = _store_with_history(tmp_path, 3)
    learner = Learner(store, HORIZON)
    assert learner.retrain("w", "request_rate", now) is not None

    # Two minutes later, before the next rollup: forecast through the gap.
    later = now + timedelta(minutes=2, seconds=20)
    forecast = learner.forecast("w", "request_rate", later)

    assert forecast is not None
    assert len(forecast.p50) == HORIZON
    assert learner.forecast("w", "cpu_millicores", later) is None


def test_stale_history_gives_no_forecast(tmp_path: Path) -> None:
    store, now = _store_with_history(tmp_path, 3)
    learner = Learner(store, HORIZON)
    learner.retrain("w", "request_rate", now)

    assert learner.forecast("w", "request_rate", now + timedelta(hours=1)) is None


def test_gaps_in_history_are_tolerated(tmp_path: Path) -> None:
    store, now = _store_with_history(tmp_path, 3, gap=slice(1500, 1800))
    learner = Learner(store, HORIZON)

    series = learner.minute_series(learner.minute_history("w", now), "request_rate")

    assert series is not None
    assert len(series.values) == 3 * MINUTES_PER_DAY
    assert np.isnan(series.values[1500:1800]).all()
    assert learner.retrain("w", "request_rate", now) is not None


def test_retraining_logs_a_forecast_that_is_scored_later(tmp_path: Path) -> None:
    store, now = _store_with_history(tmp_path, 3)
    learner = Learner(store, HORIZON)
    learner.retrain("w", "request_rate", now)

    # The forecast minutes then happen: roll up actual demand for them.
    actual = weekly_traffic(4)[3 * MINUTES_PER_DAY : 3 * MINUTES_PER_DAY + HORIZON]
    store.insert_minutes(
        pl.DataFrame(
            {
                "workload": ["w"] * HORIZON,
                "minute": [
                    now.replace(tzinfo=None) + timedelta(minutes=i)
                    for i in range(HORIZON)
                ],
                "samples": [30] * HORIZON,
                "request_rate": actual,
                "cpu_usage_millicores": actual,
                "replicas": [5.0] * HORIZON,
                "latency_p95_ms": [0.0] * HORIZON,
            }
        )
    )
    status = learner.status("w", now + timedelta(minutes=HORIZON))

    assert status.knows_daily_pattern
    assert not status.knows_weekly_pattern
    assert status.scored_minutes == HORIZON
    assert status.model_error is not None and status.model_error < 0.2
    assert status.baseline_error is not None
    assert status.p90_coverage is not None


def test_latency_curve_is_learned_from_minute_history(tmp_path: Path) -> None:
    store, now = _store_with_history(tmp_path, 3, latency=True)
    learner = Learner(store, HORIZON)
    learner.retrain("w", "request_rate", now)

    capacity = learner.learned_capacity("w", "request_rate")

    assert capacity is not None and capacity.source == "latency_model"
    assert capacity.per_pod == pytest.approx(400, rel=0.05)
    assert learner.learned_capacity("w", "cpu_millicores") is None
