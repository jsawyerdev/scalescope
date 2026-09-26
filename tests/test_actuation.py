"""The OBSERVE-mode write path's planning, against a real Store."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import pytest

from scalescope import main
from scalescope.capacity import ScaleDownStabilizer
from scalescope.k8s_actuator import KubernetesActuator
from scalescope.k8s_collector import KubernetesWorkloadTarget
from scalescope.storage import Store

_TARGET = KubernetesWorkloadTarget(namespace="payments", deployment="api")


class _RecordingActuator:
    def __init__(self) -> None:
        self.writes: list[tuple[str, int]] = []

    def scale(self, deployment: str, replicas: int) -> None:
        self.writes.append((deployment, replicas))


def _seed(store: Store, rows: int, **overrides: Any) -> None:
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(rows):
        store.insert_observation(
            {
                "ts": start + timedelta(seconds=2 * i),
                "workload": _TARGET.workload_id,
                "replicas": 2,
                "desired_replicas": 2,
                "request_rate": 0.0,
                "cpu_usage_pct": 90.0,
                "cpu_usage_millicores": 1800.0,
                "cpu_request_millicores": 1000.0,
                "cpu_throttled_pct": 0.0,
                "memory_usage_mb": 100.0,
                "latency_p95_ms": 0.0,
                "error_rate": 0.0,
                "pending_pods": 0,
                "restarts": 0,
                **overrides,
            }
        )


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Store:
    monkeypatch.setitem(main.app_state, "source", main._init_source_state())
    return Store(str(tmp_path / "actuate.duckdb"))


def test_actuation_sizes_cpu_demand_against_the_cpu_request(store: Store) -> None:
    # 1800m used across the Deployment with 1000m requested per pod: at the
    # 70% target that needs ceil(1800 / 700) = 3 pods.
    _seed(store, 60)
    actuator = _RecordingActuator()

    main._actuate(
        store, cast(KubernetesActuator, actuator), _TARGET, ScaleDownStabilizer(0)
    )

    assert actuator.writes == [("api", 3)]


def test_actuation_plans_from_desired_not_lagging_status_replicas(
    store: Store,
) -> None:
    # 8400m of demand needs 12 pods. The previous tick already wrote 6
    # (spec), but status still reports 2 while pods start. Planning from
    # status would rewrite 6 and stall; planning from spec takes the next
    # step to 10.
    _seed(store, 60, replicas=2, desired_replicas=6, cpu_usage_millicores=8400.0)
    actuator = _RecordingActuator()

    main._actuate(
        store, cast(KubernetesActuator, actuator), _TARGET, ScaleDownStabilizer(0)
    )

    assert actuator.writes == [("api", 10)]
