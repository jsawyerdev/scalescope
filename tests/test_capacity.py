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
        {
            "replicas": replicas,
            "request_rate": request_rate,
            "cpu_usage_pct": cpu,
            "latency_p95_ms": [30.0] * rows,  # flat: the latency model declines
        }
    )


def _queueing_history(rows: int, mu: float, base_ms: float, c: float) -> pl.DataFrame:
    rng = np.random.default_rng(3)
    replicas = rng.integers(2, 8, rows)
    per_pod = rng.uniform(0.2 * mu, 0.93 * mu, rows)
    latency = (base_ms + c / (mu - per_pod)) * (1 + rng.normal(0, 0.05, rows))
    return pl.DataFrame(
        {
            "replicas": replicas,
            "request_rate": per_pod * replicas,
            "cpu_usage_pct": [0.0] * rows,
            "latency_p95_ms": latency,
        }
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


def test_latency_model_sizes_pods_for_the_latency_target() -> None:
    capacity = resolve_capacity(
        _queueing_history(200, mu=220.0, base_ms=20.0, c=3000.0), "request_rate", None
    )

    assert capacity.source == "latency_model"
    assert capacity.per_pod is not None and capacity.target_utilization is not None
    assert capacity.per_pod == pytest.approx(220.0, rel=0.05)
    # Default target: twice the no-load latency, 2 * (20 + 3000/220) = 67.3 ms,
    # reached at 220 - 3000 / (67.3 - 20) = 156.5 req/s per pod.
    assert capacity.per_pod * capacity.target_utilization == pytest.approx(
        156.5, rel=0.05
    )


def test_latency_slo_overrides_the_default_target() -> None:
    history = _queueing_history(200, mu=220.0, base_ms=20.0, c=3000.0)
    capacity = resolve_capacity(history, "request_rate", None, latency_slo_ms=120.0)

    assert capacity.latency_target_ms == 120.0
    assert capacity.per_pod is not None and capacity.target_utilization is not None
    # 220 - 3000 / (120 - 20) = 190 req/s per pod.
    assert capacity.per_pod * capacity.target_utilization == pytest.approx(
        190.0, rel=0.05
    )


def test_latency_capacity_does_not_extrapolate_past_observed_load() -> None:
    history = _queueing_history(200, mu=220.0, base_ms=20.0, c=3000.0)
    # Keep only loads up to 120 req/s per pod; the curve says 156.5 is safe,
    # but nothing above 120 was ever seen.
    observed = history.filter(pl.col("request_rate") / pl.col("replicas") <= 120.0)
    capacity = resolve_capacity(observed, "request_rate", None)

    assert capacity.source == "latency_model"
    assert capacity.per_pod is not None and capacity.target_utilization is not None
    assert capacity.per_pod * capacity.target_utilization <= 120.0


def test_configured_capacity_wins_over_the_latency_model() -> None:
    history = _queueing_history(200, mu=220.0, base_ms=20.0, c=3000.0)
    assert resolve_capacity(history, "request_rate", 100.0).source == "configured"


def test_recommendation_uses_the_latency_model_utilization() -> None:
    latency_sized = PodCapacity(220.0, "latency_model", target_utilization=0.5)
    # 1100 req/s at 110 req/s per pod (220 * 0.5) -> 10 pods.
    rec = recommend_replicas(8, _forecast([1100.0 / 1.2] * 10), latency_sized, _POLICY)
    assert rec.pods_needed == [10] * 10


def _long(values: list[float]) -> Forecast:
    return _forecast(values)


def test_long_forecast_scales_up_for_a_rise_within_the_startup_time() -> None:
    # Short term: flat 800 fits in 8 pods. The long-memory forecast sees
    # the usual rise to 1600 in the next minute: pods start now.
    rec = recommend_replicas(
        8,
        _forecast([800.0] * 10),
        _CAPACITY,
        _POLICY,
        9,
        long_forecast=_long([1600.0, 1600.0, 800.0]),
        lead_minutes=1,
    )
    assert rec.recommended_replicas > 8
    assert rec.anticipated


def test_long_forecast_beyond_the_startup_time_changes_nothing_yet() -> None:
    short = _forecast([800.0] * 10)
    rec = recommend_replicas(
        8,
        short,
        _CAPACITY,
        _POLICY,
        9,
        long_forecast=_long([800.0, 800.0, 800.0, 1600.0]),
        lead_minutes=1,
    )
    without = recommend_replicas(8, short, _CAPACITY, _POLICY, 9)
    assert rec.recommended_replicas == without.recommended_replicas
    assert not rec.anticipated


def test_long_forecast_keeps_pods_needed_again_within_twice_the_startup_time() -> None:
    # Short term alone would release pods; demand returns in minute 2.
    short = _forecast([200.0] * 10)
    without = recommend_replicas(8, short, _CAPACITY, _POLICY, 9)
    held = recommend_replicas(
        8,
        short,
        _CAPACITY,
        _POLICY,
        9,
        long_forecast=_long([200.0, 1000.0, 200.0]),
        lead_minutes=1,
    )
    released = recommend_replicas(
        8,
        short,
        _CAPACITY,
        _POLICY,
        9,
        long_forecast=_long([200.0, 200.0, 1000.0]),
        lead_minutes=1,
    )

    assert without.recommended_replicas < 8
    assert held.recommended_replicas == 8 and held.anticipated
    assert released.recommended_replicas == without.recommended_replicas
    assert not released.anticipated
