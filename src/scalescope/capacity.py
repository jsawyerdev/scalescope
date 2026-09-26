"""Turns a demand forecast into a required-replica recommendation.

Per-pod capacity is never assumed. For request-rate demand it is configured
by the operator or estimated from the workload's own history; for CPU
demand it is each pod's CPU request. Without one, ScaleScope keeps the
current replica count and says so, rather than sizing a real workload with
an invented number.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Literal

import polars as pl

from scalescope.demand import DemandSignal
from scalescope.models.base import Forecast

DEFAULT_MIN_REPLICAS = 1
DEFAULT_MAX_REPLICAS = 30
DEFAULT_TARGET_UTILIZATION = 0.70
# Kubernetes HPA's default scale-down stabilization window.
DEFAULT_SCALE_DOWN_STABILIZATION_SECONDS = 300.0
MAX_SCALE_UP_PER_STEP = 4
MAX_SCALE_DOWN_PER_STEP = 2
MIN_CONFIDENCE_TO_SCALE = 0.10
STARTUP_LEAD_STEPS = 15  # pod startup + readiness lag, in ticks

# Samples outside this CPU band say little about per-pod throughput: near
# zero the ratio is dominated by idle overhead, near the top usage is
# clipped at the CPU request.
_ESTIMATE_MIN_CPU_PCT = 5.0
_ESTIMATE_MAX_CPU_PCT = 95.0
_MIN_ESTIMATE_SAMPLES = 10

CapacitySource = Literal["configured", "estimated", "cpu_request", "unavailable"]


@dataclass(frozen=True)
class ScalingPolicy:
    """Operator bounds on what a recommendation may be."""

    min_replicas: int = DEFAULT_MIN_REPLICAS
    max_replicas: int = DEFAULT_MAX_REPLICAS
    target_utilization: float = DEFAULT_TARGET_UTILIZATION


@dataclass(frozen=True)
class PodCapacity:
    """Demand one pod serves at 100% of its CPU request, and where it came from.

    In the demand signal's unit: requests/s, or CPU millicores.
    """

    per_pod: float | None
    source: CapacitySource


@dataclass(frozen=True)
class CapacityRecommendation:
    """Recommended replica count derived from a P90 demand forecast."""

    current_replicas: int
    recommended_replicas: int
    peak_forecast_p90: float
    projected_utilization: float | None
    confidence: float
    capacity: PodCapacity


def estimate_capacity_per_pod(observations: pl.DataFrame) -> float | None:
    """Median requests/s per pod at 100% CPU request, or None if unmeasurable.

    Assumes CPU scales linearly with throughput: a pod serving `r` req/s
    at `u`% of its CPU request serves `r / (u / 100)` req/s at 100%. Needs
    `_MIN_ESTIMATE_SAMPLES` observations with traffic, replicas, and CPU
    usage inside the informative band.
    """
    if observations.is_empty():
        return None
    usable = observations.filter(
        (pl.col("replicas") > 0)
        & (pl.col("request_rate") > 0)
        & (pl.col("cpu_usage_pct") >= _ESTIMATE_MIN_CPU_PCT)
        & (pl.col("cpu_usage_pct") < _ESTIMATE_MAX_CPU_PCT)
    )
    if usable.height < _MIN_ESTIMATE_SAMPLES:
        return None
    per_pod = (pl.col("request_rate") / pl.col("replicas")) / (
        pl.col("cpu_usage_pct") / 100
    )
    estimate = usable.select(per_pod.median()).item()
    return float(estimate) if estimate is not None and estimate > 0 else None


def resolve_capacity(
    observations: pl.DataFrame, signal: DemandSignal, configured_rps: float | None
) -> PodCapacity:
    """Per-pod capacity in the unit of `signal`.

    CPU demand: the latest per-pod CPU request. Request-rate demand: the
    operator's configured value if set, else an estimate from history.
    """
    if signal == "cpu_millicores":
        requests = observations["cpu_request_millicores"].drop_nulls()
        request = float(requests[-1]) if len(requests) else 0.0
        if request > 0:
            return PodCapacity(request, "cpu_request")
        return PodCapacity(None, "unavailable")
    if configured_rps is not None:
        return PodCapacity(configured_rps, "configured")
    estimate = estimate_capacity_per_pod(observations)
    if estimate is None:
        return PodCapacity(None, "unavailable")
    return PodCapacity(estimate, "estimated")


def recommend_replicas(
    current_replicas: int,
    forecast: Forecast,
    capacity: PodCapacity,
    policy: ScalingPolicy,
    peak_step: int | None = None,
    scaling_will_help: bool = True,
) -> CapacityRecommendation:
    """Recommend replicas to serve the P90 forecast at the target utilization.

    Asymmetric on purpose. Scale up for the peak within `peak_step` (pod
    startup + readiness lag; defaults to the full forecast): pods started
    now are ready just in time, and later peaks can wait. Scale down only
    if the peak over the whole horizon fits in fewer pods, so no pod is
    removed that the forecast says will be needed again. The current count
    is kept when capacity is unknown, the forecast is too uncertain, or
    `scaling_will_help` is False.
    """
    lead_window = (
        forecast.p90[: peak_step + 1] if peak_step is not None else forecast.p90
    )
    lead_peak = float(lead_window.max()) if len(lead_window) else 0.0
    peak_demand = float(forecast.p90.max()) if len(forecast.p90) else 0.0

    band_width = (
        float((forecast.p90 - forecast.p10).mean()) if len(forecast.p90) else 0.0
    )
    confidence = max(0.0, min(1.0, 1 - (band_width / max(peak_demand, 1.0))))

    recommended = current_replicas
    if (
        capacity.per_pod is not None
        and scaling_will_help
        and confidence >= MIN_CONFIDENCE_TO_SCALE
    ):
        safe_capacity_per_pod = capacity.per_pod * policy.target_utilization

        def pods_for(demand: float) -> int:
            required = math.ceil(demand / safe_capacity_per_pod)
            return max(policy.min_replicas, min(policy.max_replicas, required))

        scale_up_to = pods_for(lead_peak)
        hold_at = pods_for(peak_demand)
        if scale_up_to > current_replicas:
            recommended = min(scale_up_to, current_replicas + MAX_SCALE_UP_PER_STEP)
        elif hold_at < current_replicas:
            recommended = max(hold_at, current_replicas - MAX_SCALE_DOWN_PER_STEP)

    projected_utilization = (
        peak_demand / (recommended * capacity.per_pod)
        if capacity.per_pod is not None and recommended
        else None
    )

    return CapacityRecommendation(
        current_replicas=current_replicas,
        recommended_replicas=recommended,
        peak_forecast_p90=peak_demand,
        projected_utilization=projected_utilization,
        confidence=round(confidence, 3),
        capacity=capacity,
    )


class ScaleDownStabilizer:
    """Kubernetes HPA-style scale-down stabilization for one workload.

    Scale-ups apply at once. A scale-down goes only as low as the highest
    recommendation seen within the last `window_seconds`, so a brief dip in
    the forecast cannot remove pods that the next tick asks for again.
    """

    def __init__(self, window_seconds: float) -> None:
        self._window_seconds = window_seconds
        self._recommendations: deque[tuple[float, int]] = deque()

    def stabilize(self, now: float, current: int, recommended: int) -> int:
        self._recommendations.append((now, recommended))
        while self._recommendations[0][0] < now - self._window_seconds:
            self._recommendations.popleft()
        if recommended >= current:
            return recommended
        return min(current, max(r for _, r in self._recommendations))
