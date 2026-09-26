import polars as pl

from scalescope.diagnosis import DIAGNOSIS_WINDOW_STEPS, Diagnosis, diagnose


def _obs(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
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


def _frame(rows: list[dict[str, object]]) -> pl.DataFrame:
    return pl.DataFrame(rows)


def test_empty_observations_is_healthy() -> None:
    result = diagnose(pl.DataFrame())
    assert result.diagnosis == Diagnosis.HEALTHY
    assert result.scaling_will_help


def test_pending_pods_flags_node_capacity_bottleneck() -> None:
    rows = [_obs(pending_pods=3)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.NODE_CAPACITY_BOTTLENECK
    assert not result.scaling_will_help


def test_cpu_throttling_flags_cpu_limit_constraint() -> None:
    rows = [_obs(cpu_throttled_pct=12.0)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.CPU_LIMIT_CONSTRAINT
    assert not result.scaling_will_help


def test_flat_traffic_rising_memory_flags_possible_leak() -> None:
    rows = [_obs(request_rate=1000.0, memory_usage_mb=200.0 + i) for i in range(20)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.POSSIBLE_MEMORY_LEAK
    assert not result.scaling_will_help


def test_healthy_steady_state() -> None:
    rows = [_obs() for _ in range(20)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.HEALTHY
    assert result.scaling_will_help


def test_memory_slope_is_measured_per_step() -> None:
    # 0.305 MB/tick over 30 rows: 29 steps. Dividing by the row count instead
    # would report 0.295 MB/tick and miss the 0.3 threshold.
    rows = [_obs(memory_usage_mb=200.0 + 0.305 * i) for i in range(30)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.POSSIBLE_MEMORY_LEAK


def test_only_newest_window_is_diagnosed() -> None:
    leaking = [_obs(memory_usage_mb=200.0 + i) for i in range(DIAGNOSIS_WINDOW_STEPS)]
    steady = [_obs(memory_usage_mb=500.0) for _ in range(DIAGNOSIS_WINDOW_STEPS)]
    result = diagnose(_frame(leaking + steady))
    assert result.diagnosis == Diagnosis.HEALTHY


def test_max_replicas_under_cpu_pressure_flags_hpa_ceiling() -> None:
    rows = [_obs(replicas=30, cpu_usage_pct=95.0) for _ in range(20)]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.HPA_CEILING
    assert not result.scaling_will_help


def test_rising_traffic_and_latency_with_low_cpu_flags_non_cpu_bottleneck() -> None:
    rows = [
        _obs(request_rate=1000.0 + 20.0 * i, latency_p95_ms=30.0 + 1.0 * i)
        for i in range(20)
    ]
    result = diagnose(_frame(rows))
    assert result.diagnosis == Diagnosis.LIKELY_NON_CPU_BOTTLENECK
    assert result.scaling_will_help
