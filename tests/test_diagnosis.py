import polars as pl
import pytest

from scalescope.diagnosis import Diagnosis, diagnose


def _obs(**overrides) -> dict:
    base = {
        "replicas": 8,
        "request_rate": 1000.0,
        "cpu_usage_pct": 50.0,
        "cpu_throttled_pct": 0.0,
        "memory_usage_mb": 200.0,
        "latency_p95_ms": 30.0,
        "error_rate": 0.0,
        "pending_pods": 0,
        "restarts": 0,
    }
    base.update(overrides)
    return base


def _frame(rows: list[dict]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_empty_observations_is_healthy():
    result = diagnose(pl.DataFrame())
    assert result.diagnosis == Diagnosis.HEALTHY
    assert result.scaling_will_help


def test_pending_pods_flags_node_capacity_bottleneck():
    rows = [_obs(pending_pods=3)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.NODE_CAPACITY_BOTTLENECK
    assert not result.scaling_will_help


def test_cpu_throttling_flags_cpu_limit_constraint():
    rows = [_obs(cpu_throttled_pct=12.0)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.CPU_LIMIT_CONSTRAINT
    assert not result.scaling_will_help


def test_flat_traffic_rising_memory_flags_possible_leak():
    rows = [_obs(request_rate=1000.0, memory_usage_mb=200.0 + i) for i in range(20)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.POSSIBLE_MEMORY_LEAK
    assert not result.scaling_will_help


def test_healthy_steady_state():
    rows = [_obs() for _ in range(20)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.HEALTHY
    assert result.scaling_will_help
