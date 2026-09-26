"""Turns a demand forecast into a required-replica recommendation.

Per-pod capacity is never assumed. For request-rate demand it is, in order:
configured by the operator; read from the workload's own latency curve (a
fitted queueing model, see `performance.py`); or estimated from its CPU
history. For CPU demand it is each pod's CPU request. Without one,
ScaleScope keeps the current replica count and says so, rather than sizing
a real workload with an invented number.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass
from typing import Literal

import polars as pl

from scalescope.demand import DemandSignal
from scalescope.models.base import Forecast
from scalescope.performance import (
    LatencyModel,
    fit_latency_model,
    latency_target_ms,
)

DEFAULT_MIN_REPLICAS = 1
DEFAULT_MAX_REPLICAS = 30
DEFAULT_TARGET_UTILIZATION = 0.70
# Kubernetes HPA's default scale-down stabilization window.
HPA_SCALE_DOWN_STABILIZATION_SECONDS = 300.0
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

CapacitySource = Literal[
    "configured", "latency_model", "estimated", "cpu_request", "unavailable"
]
# Why a recommendation keeps the current count even if demand says otherwise.
HoldReason = Literal["diagnosis", "capacity_unknown", "low_confidence"]


@dataclass(frozen=True)
class ScalingPolicy:
    """Operator bounds on what a recommendation may be."""

    min_replicas: int = DEFAULT_MIN_REPLICAS
    max_replicas: int = DEFAULT_MAX_REPLICAS
    target_utilization: float = DEFAULT_TARGET_UTILIZATION


@dataclass(frozen=True)
class PodCapacity:
    """Demand one pod serves at full capacity, and where that figure came from.

    In the demand signal's unit: requests/s, or CPU millicores. Full capacity
    is 100% of the CPU request, or saturation throughput for a latency model.
    A latency model also sets how full a pod may run (`target_utilization`,
    the load that keeps p95 at `latency_target_ms` as a fraction of
    saturation); otherwise the policy's target applies.
    """

    per_pod: float | None
    source: CapacitySource
    target_utilization: float | None = None
    latency_model: LatencyModel | None = None
    latency_target_ms: float | None = None

    def utilization(self, policy: ScalingPolicy) -> float:
        """How full each pod may run: the latency model's target, else the policy's."""
        return self.target_utilization or policy.target_utilization


@dataclass(frozen=True)
class CapacityRecommendation:
    """Recommended replica count derived from a P90 demand forecast."""

    current_replicas: int
    recommended_replicas: int
    peak_forecast_p90: float
    projected_utilization: float | None
    confidence: float
    capacity: PodCapacity
    hold_reason: HoldReason | None
    # Pods the p90 forecast needs at each horizon step; None without capacity.
    pods_needed: list[int] | None


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


def latency_capacity(
    observations: pl.DataFrame, latency_slo_ms: float | None
) -> PodCapacity | None:
    """Capacity from the workload's fitted latency curve, if it is identifiable."""
    running = observations.filter(pl.col("replicas") > 0)
    if running.is_empty():
        return None
    load_per_pod = (running["request_rate"] / running["replicas"]).to_numpy()
    latency = running["latency_p95_ms"].to_numpy()
    model = fit_latency_model(load_per_pod, latency)
    if model is None:
        return None
    target = latency_target_ms(model, latency_slo_ms)
    load = model.load_for_latency(target)
    # Never plan beyond the highest load per pod seen meeting the target:
    # the curve is fitted, but past the data it is extrapolated.
    seen_meeting_target = load_per_pod[latency <= target]
    if load is None or seen_meeting_target.size == 0:
        return None
    load = min(load, float(seen_meeting_target.max()))
    if load <= 0:
        return None
    return PodCapacity(
        per_pod=model.saturation_rps,
        source="latency_model",
        target_utilization=load / model.saturation_rps,
        latency_model=model,
        latency_target_ms=target,
    )


def resolve_capacity(
    observations: pl.DataFrame,
    signal: DemandSignal,
    configured_rps: float | None,
    latency_slo_ms: float | None = None,
) -> PodCapacity:
    """Per-pod capacity in the unit of `signal`.

    CPU demand: the latest per-pod CPU request. Request-rate demand: the
    operator's configured value if set, else the latency model, else an
    estimate from CPU history.
    """
    if signal == "cpu_millicores":
        requests = observations["cpu_request_millicores"].drop_nulls()
        request = float(requests[-1]) if len(requests) else 0.0
        if request > 0:
            return PodCapacity(request, "cpu_request")
        return PodCapacity(None, "unavailable")
    if configured_rps is not None:
        return PodCapacity(configured_rps, "configured")
    from_latency = latency_capacity(observations, latency_slo_ms)
    if from_latency is not None:
        return from_latency
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

    hold_reason: HoldReason | None = None
    if not scaling_will_help:
        hold_reason = "diagnosis"
    elif capacity.per_pod is None:
        hold_reason = "capacity_unknown"
    elif confidence < MIN_CONFIDENCE_TO_SCALE:
        hold_reason = "low_confidence"

    recommended = current_replicas
    pods_needed: list[int] | None = None
    if capacity.per_pod is not None:
        safe_capacity_per_pod = capacity.per_pod * capacity.utilization(policy)

        def pods_for(demand: float) -> int:
            required = math.ceil(demand / safe_capacity_per_pod)
            return max(policy.min_replicas, min(policy.max_replicas, required))

        pods_needed = [pods_for(float(demand)) for demand in forecast.p90]
        if hold_reason is None:
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
        hold_reason=hold_reason,
        pods_needed=pods_needed,
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
