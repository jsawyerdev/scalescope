from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from scalescope import demo_history
from scalescope.storage import Store

WEDNESDAY = datetime(2026, 1, 7, tzinfo=UTC)


def test_demo_demand_is_busy_on_weekday_mornings_and_quiet_at_night() -> None:
    at = demo_history.demand_level
    morning, night = WEDNESDAY.replace(hour=10), WEDNESDAY.replace(hour=3)
    saturday_morning = morning + timedelta(days=3)

    assert at(morning) > 2 * at(night)
    assert at(saturday_morning) < at(morning)


def test_backfill_generates_weeks_of_history_once(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "demo.duckdb"))

    added = demo_history.backfill(store, "sample-app", WEDNESDAY)
    again = demo_history.backfill(store, "sample-app", WEDNESDAY)

    history = store.minute_history("sample-app", WEDNESDAY - timedelta(days=30))
    assert added == demo_history.DEMO_HISTORY_DAYS * 1440 == history.height
    assert again == 0
    assert history["minute"][-1] == (WEDNESDAY - timedelta(minutes=1)).replace(
        tzinfo=None
    )
    assert (history["replicas"] >= 3).all()
    assert (history["latency_p95_ms"] > 0).all()
