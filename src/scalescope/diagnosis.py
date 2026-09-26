"""Deterministic rule engine: classifies why a workload is under pressure.

ML supplies forecasts/anomalies; this module never calls a model. It exists
so "scaling will not fix this" is a rule-based conclusion, not a guess.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

import polars as pl

from scalescope.capacity import DEFAULT_MAX_REPLICAS


class Diagnosis(Enum):
    HEALTHY = "healthy"
    CPU_LIMIT_CONSTRAINT = "cpu_limit_constraint"
    POSSIBLE_MEMORY_LEAK = "possible_memory_leak"
    NODE_CAPACITY_BOTTLENECK = "node_capacity_bottleneck"
    LIKELY_NON_CPU_BOTTLENECK = "likely_non_cpu_bottleneck"
    HPA_CEILING = "hpa_ceiling"


@dataclass(frozen=True)
class DiagnosisResult:
    diagnosis: Diagnosis
    scaling_will_help: bool
    explanation: str


# Thresholds are deliberately explicit and reviewable, not learned.
DIAGNOSIS_WINDOW_STEPS = 30
MIN_TREND_WINDOW_STEPS = 10
CPU_THROTTLE_THRESHOLD_PCT = 5.0
CPU_HIGH_THRESHOLD_PCT = 80.0
MEMORY_SLOPE_MB_PER_STEP_THRESHOLD = 0.3
TRAFFIC_FLAT_THRESHOLD_PCT = 5.0
TRAFFIC_RISE_THRESHOLD_PCT = 15.0
LATENCY_RISE_THRESHOLD_PCT = 15.0
PENDING_PODS_THRESHOLD = 1


def diagnose(
    observations: pl.DataFrame, max_replicas: int = DEFAULT_MAX_REPLICAS
) -> DiagnosisResult:
    """Classify the current health state of a workload from recent observations.

    `observations` must be sorted ascending by ts and contain at least the
    columns produced by `scalescope.simulator.WorkloadSimulator.step`. Only
    the newest `DIAGNOSIS_WINDOW_STEPS` rows are considered.
    """
    if observations.is_empty():
        return DiagnosisResult(Diagnosis.HEALTHY, True, "no data yet")

    latest = observations.tail(1).row(0, named=True)
    window = observations.tail(DIAGNOSIS_WINDOW_STEPS)

    if latest["pending_pods"] >= PENDING_PODS_THRESHOLD:
        return DiagnosisResult(
            Diagnosis.NODE_CAPACITY_BOTTLENECK,
            False,
            f"{latest['pending_pods']} pods pending scheduling; cluster is capacity-constrained, "
            "not the workload's replica count.",
        )

    if latest["cpu_throttled_pct"] >= CPU_THROTTLE_THRESHOLD_PCT:
        return DiagnosisResult(
            Diagnosis.CPU_LIMIT_CONSTRAINT,
            False,
            f"CPU throttling at {latest['cpu_throttled_pct']:.1f}% with usage "
            f"{latest['cpu_usage_pct']:.1f}%; containers are hitting their CPU limit. "
            "Adding replicas has low expected value until limits are reviewed.",
        )

    if len(window) >= MIN_TREND_WINDOW_STEPS:
        memory_slope = (
            window["memory_usage_mb"][-1] - window["memory_usage_mb"][0]
        ) / (len(window) - 1)
        traffic_change_pct = (
            abs(window["request_rate"][-1] - window["request_rate"][0])
            / max(window["request_rate"][0], 1.0)
            * 100
        )
        if (
            memory_slope >= MEMORY_SLOPE_MB_PER_STEP_THRESHOLD
            and traffic_change_pct < TRAFFIC_FLAT_THRESHOLD_PCT
        ):
            return DiagnosisResult(
                Diagnosis.POSSIBLE_MEMORY_LEAK,
                False,
                f"Memory growing {memory_slope:.2f} MB/tick while traffic is flat "
                f"({traffic_change_pct:.1f}% change). Scaling would mask, not fix, "
                "a probable memory leak.",
            )

    if (
        latest["replicas"] >= max_replicas
        and latest["cpu_usage_pct"] >= CPU_HIGH_THRESHOLD_PCT
    ):
        return DiagnosisResult(
            Diagnosis.HPA_CEILING,
            False,
            f"At max replicas ({max_replicas}) with CPU usage {latest['cpu_usage_pct']:.1f}%; "
            "demand still increasing beyond configured ceiling.",
        )

    if len(window) >= MIN_TREND_WINDOW_STEPS:
        traffic_change_pct = (
            (window["request_rate"][-1] - window["request_rate"][0])
            / max(window["request_rate"][0], 1.0)
            * 100
        )
        latency_change_pct = (
            (window["latency_p95_ms"][-1] - window["latency_p95_ms"][0])
            / max(window["latency_p95_ms"][0], 1.0)
            * 100
        )
        if (
            traffic_change_pct > TRAFFIC_RISE_THRESHOLD_PCT
            and latest["cpu_usage_pct"] < CPU_HIGH_THRESHOLD_PCT
            and latency_change_pct > LATENCY_RISE_THRESHOLD_PCT
        ):
            return DiagnosisResult(
                Diagnosis.LIKELY_NON_CPU_BOTTLENECK,
                True,
                f"Traffic up {traffic_change_pct:.1f}%, latency up {latency_change_pct:.1f}%, "
                f"but CPU only {latest['cpu_usage_pct']:.1f}%. Likely bottleneck is not CPU-bound "
                "capacity; investigate downstream dependencies before scaling blindly.",
            )

    return DiagnosisResult(Diagnosis.HEALTHY, True, "no constraint detected")
