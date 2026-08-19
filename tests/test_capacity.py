import numpy as np

from scalescope.capacity import MAX_SCALE_UP_PER_STEP, recommend_replicas
from scalescope.models.base import Forecast


def _forecast(p50: list[float]) -> Forecast:
    arr = np.array(p50, dtype=float)
    return Forecast("test", arr * 0.8, arr, arr * 1.2)


def test_recommends_more_replicas_when_demand_rises():
    forecast = _forecast([1800.0] * 10)
    rec = recommend_replicas(current_replicas=8, forecast=forecast, peak_step=9)
    assert rec.recommended_replicas > 8


def test_recommendation_step_is_rate_limited():
    forecast = _forecast([5000.0] * 10)
    rec = recommend_replicas(current_replicas=3, forecast=forecast, peak_step=9)
    assert rec.recommended_replicas - rec.current_replicas <= MAX_SCALE_UP_PER_STEP


def test_stable_demand_keeps_replicas_flat():
    # current replicas already sized for this demand at target utilization
    forecast = _forecast([8 * 220.0 * 0.70] * 10)
    rec = recommend_replicas(current_replicas=8, forecast=forecast, peak_step=9)
    assert rec.recommended_replicas == 8
