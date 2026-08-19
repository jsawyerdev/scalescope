"""Turns a demand forecast into a required-replica recommendation."""

from __future__ import annotations

import math
from dataclasses import dataclass

from scalescope.models.base import Forecast

CAPACITY_PER_POD_RPS = 220.0
TARGET_UTILIZATION = 0.70
MIN_REPLICAS = 3
MAX_REPLICAS = 30
MAX_SCALE_UP_PER_STEP = 4
MAX_SCALE_DOWN_PER_STEP = 2


@dataclass(frozen=True)
class CapacityRecommendation:
    """Recommended replica count derived from a P90 demand forecast."""

    current_replicas: int
    recommended_replicas: int
    peak_forecast_p90: float
    projected_utilization: float
    confidence: float


def recommend_replicas(
    current_replicas: int, forecast: Forecast, peak_step: int | None = None
) -> CapacityRecommendation:
    """Recommend replicas to satisfy the P90 forecast at target utilization.

    `peak_step` restricts the lookahead to the model's known-reliable horizon
    (e.g. pod startup + readiness lag); defaults to the full forecast.
    """
    p90_window = forecast.p90[: peak_step + 1] if peak_step is not None else forecast.p90
    peak_demand = float(p90_window.max()) if len(p90_window) else 0.0

    safe_capacity_per_pod = CAPACITY_PER_POD_RPS * TARGET_UTILIZATION
    raw_required = math.ceil(peak_demand / safe_capacity_per_pod) if safe_capacity_per_pod else current_replicas
    required = max(MIN_REPLICAS, min(MAX_REPLICAS, raw_required))

    step = required - current_replicas
    step = max(-MAX_SCALE_DOWN_PER_STEP, min(MAX_SCALE_UP_PER_STEP, step))
    recommended = current_replicas + step

    projected_utilization = (
        peak_demand / (recommended * CAPACITY_PER_POD_RPS) if recommended else 0.0
    )

    band_width = float((forecast.p90 - forecast.p10).mean()) if len(forecast.p90) else 0.0
    confidence = max(0.0, min(1.0, 1 - (band_width / max(peak_demand, 1.0))))

    return CapacityRecommendation(
        current_replicas=current_replicas,
        recommended_replicas=recommended,
        peak_forecast_p90=peak_demand,
        projected_utilization=projected_utilization,
        confidence=round(confidence, 3),
    )
