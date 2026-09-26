import numpy as np
import polars as pl
import pytest

from scalescope.capacity import (
    MAX_SCALE_UP_PER_STEP,
    PodCapacity,
    ScaleDownStabilizer,
    ScalingPolicy,
    estimate_capacity_per_pod,
    recommend_replicas,
    resolve_capacity,
)
from scalescope.models.base import Forecast

_CAPACITY = PodCapacity(220.0, "configured")
_POLICY = ScalingPolicy()


def _forecast(p50: list[float]) -> Forecast:
    arr = np.array(p50, dtype=float)
    return Forecast("test", arr * 0.8, arr, arr * 1.2)


def _history(rows: int, rps_per_pod_at_full_cpu: float) -> pl.DataFrame:
    replicas = [4] * rows
    request_rate = [400.0 + 10 * i for i in range(rows)]
    cpu = [
        rate / replicas[i] / rps_per_pod_at_full_cpu * 100
        for i, rate in enumerate(request_rate)
    ]
    return pl.DataFrame(
        {"replicas": replicas, "request_rate": request_rate, "cpu_usage_pct": cpu}
    )


def test_recommends_more_replicas_when_demand_rises() -> None:
    rec = recommend_replicas(8, _forecast([1800.0] * 10), _CAPACITY, _POLICY, 9)
    assert rec.recommended_replicas > 8


def test_recommendation_step_is_rate_limited() -> None:
    rec = recommend_replicas(3, _forecast([5000.0] * 10), _CAPACITY, _POLICY, 9)
    assert rec.recommended_replicas - rec.current_replicas <= MAX_SCALE_UP_PER_STEP


def test_stable_demand_keeps_replicas_flat() -> None:
    # Sizing uses p90 (1.2x p50 here); pick p50 so p90 is exactly 8 pods'
    # worth of capacity at the target utilization.
    p90_target = 8 * 220.0 * _POLICY.target_utilization
    rec = recommend_replicas(
        8, _forecast([p90_target / 1.2] * 10), _CAPACITY, _POLICY, 9
    )
    assert rec.recommended_replicas == 8


def test_replica_bounds_come_from_policy() -> None:
    policy = ScalingPolicy(min_replicas=2, max_replicas=5)
    low = recommend_replicas(3, _forecast([1.0] * 10), _CAPACITY, policy, 9)
    high = recommend_replicas(4, _forecast([9000.0] * 10), _CAPACITY, policy, 9)
    assert low.recommended_replicas == 2
    assert high.recommended_replicas == 5


def test_low_confidence_forecast_does_not_change_replicas() -> None:
    forecast = Forecast(
        "unstable",
        np.array([0.0] * 10),
        np.array([5000.0] * 10),
        np.array([5000.0] * 10),
    )
    rec = recommend_replicas(3, forecast, _CAPACITY, _POLICY, 9)

    assert rec.confidence == 0.0
    assert rec.recommended_replicas == 3
    assert rec.projected_utilization == pytest.approx(
        rec.peak_forecast_p90 / (3 * 220.0)
    )


def test_diagnosis_gate_keeps_current_replicas() -> None:
    rec = recommend_replicas(
        3, _forecast([5000.0] * 10), _CAPACITY, _POLICY, 9, scaling_will_help=False
    )
    assert rec.recommended_replicas == 3


def test_unknown_capacity_keeps_current_replicas_without_projection() -> None:
    rec = recommend_replicas(
        3, _forecast([5000.0] * 10), PodCapacity(None, "unavailable"), _POLICY, 9
    )
    assert rec.recommended_replicas == 3
    assert rec.projected_utilization is None


def test_capacity_estimate_recovers_per_pod_throughput() -> None:
    assert estimate_capacity_per_pod(_history(40, 150.0)) == pytest.approx(150.0)


def test_capacity_estimate_needs_enough_informative_samples() -> None:
    assert estimate_capacity_per_pod(_history(5, 150.0)) is None
    no_cpu = _history(40, 150.0).with_columns(pl.lit(0.0).alias("cpu_usage_pct"))
    assert estimate_capacity_per_pod(no_cpu) is None
    saturated = _history(40, 150.0).with_columns(pl.lit(100.0).alias("cpu_usage_pct"))
    assert estimate_capacity_per_pod(saturated) is None


def test_configured_capacity_overrides_estimate() -> None:
    history = _history(40, 150.0)
    assert resolve_capacity(history, "request_rate", 300.0) == PodCapacity(
        300.0, "configured"
    )
    estimated = resolve_capacity(history, "request_rate", None)
    assert estimated.source == "estimated"
    assert estimated.per_pod == pytest.approx(150.0)
    assert resolve_capacity(history.head(3), "request_rate", None) == PodCapacity(
        None, "unavailable"
    )


def test_cpu_demand_is_sized_against_the_cpu_request() -> None:
    history = pl.DataFrame({"cpu_request_millicores": [0.0, 250.0]})
    assert resolve_capacity(history, "cpu_millicores", 300.0) == PodCapacity(
        250.0, "cpu_request"
    )
    no_request = pl.DataFrame({"cpu_request_millicores": [0.0]})
    assert resolve_capacity(no_request, "cpu_millicores", None).per_pod is None


def test_scale_up_waits_for_demand_inside_the_startup_lead() -> None:
    # Peak only after the lead window: pods started now would idle early.
    p50 = [100.0] * 20 + [3000.0] * 10
    rec = recommend_replicas(2, _forecast(p50), _CAPACITY, _POLICY, peak_step=15)
    assert rec.recommended_replicas == 2


def test_scale_down_waits_until_the_whole_horizon_is_low() -> None:
    # Low now, high later in the horizon: removing pods would only force a
    # scale-up again before the peak.
    p50 = [100.0] * 20 + [1500.0] * 10
    rec = recommend_replicas(10, _forecast(p50), _CAPACITY, _POLICY, peak_step=15)
    assert rec.recommended_replicas == 10
    low = recommend_replicas(10, _forecast([100.0] * 30), _CAPACITY, _POLICY, 15)
    assert low.recommended_replicas < 10


def test_stabilizer_holds_scale_down_at_the_window_high() -> None:
    stabilizer = ScaleDownStabilizer(window_seconds=60)
    assert stabilizer.stabilize(0, current=5, recommended=8) == 8
    assert stabilizer.stabilize(10, current=8, recommended=4) == 8
    assert stabilizer.stabilize(80, current=8, recommended=4) == 4


def test_stabilizer_with_no_window_passes_recommendations_through() -> None:
    stabilizer = ScaleDownStabilizer(window_seconds=0)
    assert stabilizer.stabilize(0, current=5, recommended=8) == 8
    assert stabilizer.stabilize(1, current=8, recommended=4) == 4


def test_hold_reasons_explain_an_unchanged_count() -> None:
    rising = _forecast([5000.0] * 10)
    uncertain = Forecast("x", np.zeros(10), np.full(10, 5000.0), np.full(10, 5000.0))
    unknown = PodCapacity(None, "unavailable")

    assert recommend_replicas(3, rising, _CAPACITY, _POLICY, 9).hold_reason is None
    assert (
        recommend_replicas(
            3, rising, _CAPACITY, _POLICY, 9, scaling_will_help=False
        ).hold_reason
        == "diagnosis"
    )
    assert (
        recommend_replicas(3, rising, unknown, _POLICY, 9).hold_reason
        == "capacity_unknown"
    )
    assert (
        recommend_replicas(3, uncertain, _CAPACITY, _POLICY, 9).hold_reason
        == "low_confidence"
    )


def test_pods_needed_follows_the_p90_forecast() -> None:
    per_pod = 220.0 * _POLICY.target_utilization
    # p90 is 1.2x p50 here: 1.5 and 4.5 pods' worth of busy-case demand.
    forecast = _forecast([per_pod * 1.5 / 1.2, per_pod * 4.5 / 1.2])
    rec = recommend_replicas(3, forecast, _CAPACITY, _POLICY)
    assert rec.pods_needed == [2, 5]
    unknown = recommend_replicas(3, forecast, PodCapacity(None, "unavailable"), _POLICY)
    assert unknown.pods_needed is None
