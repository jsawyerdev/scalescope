import numpy as np
import polars as pl

from scalescope.capacity import PodCapacity, ScalingPolicy
from scalescope.models.base import Forecast
from scalescope.scaling_replay import _Cluster, replay_scaling


class _Perfect:
    """Forecasts exactly the demand that follows, from a shared series.

    Assumes the history it gets starts at tick 0 (a history window longer
    than the series), so its length is the current tick.
    """

    name = "perfect"

    def __init__(self, demand: np.ndarray) -> None:
        self._demand = demand

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        start = len(history)
        future = self._demand[start : start + horizon]
        future = np.pad(future, (0, horizon - len(future)), mode="edge")
        return Forecast(self.name, future, future, future)


def _frame(demand: np.ndarray) -> pl.DataFrame:
    n = len(demand)
    return pl.DataFrame(
        {
            "request_rate": demand,
            "cpu_usage_millicores": np.zeros(n),
            "replicas": np.full(n, 2),
            "cpu_usage_pct": np.full(n, 50.0),
            "cpu_throttled_pct": np.zeros(n),
            "memory_usage_mb": np.full(n, 100.0),
            "latency_p95_ms": np.full(n, 30.0),
            "pending_pods": np.zeros(n, dtype=int),
        }
    )


def test_cluster_starts_pods_after_the_lead_and_stops_them_at_once() -> None:
    cluster = _Cluster(replicas=2, lead=3)
    cluster.scale_to(0, 5)
    cluster.advance(2)
    assert cluster.ready == 2
    cluster.advance(3)
    assert cluster.ready == 5
    cluster.scale_to(4, 1)
    assert cluster.ready == 1
    assert cluster.scale_changes == 2


def test_cluster_cancels_starting_pods_before_running_ones() -> None:
    cluster = _Cluster(replicas=2, lead=10)
    cluster.scale_to(0, 6)
    cluster.scale_to(1, 3)
    assert cluster.ready == 2
    cluster.advance(10)
    assert cluster.ready == 3


def test_forecasting_ahead_beats_reacting_to_a_step_in_demand() -> None:
    # Flat, then a step up: the reactive HPA only starts pods after it sees
    # the step; a correct forecast starts them before it.
    demand = np.concatenate([np.full(200, 200.0), np.full(200, 1400.0)])
    capacity = PodCapacity(100.0, "configured")

    result = replay_scaling(
        _frame(demand),
        "request_rate",
        capacity,
        ScalingPolicy(min_replicas=1, max_replicas=30),
        _Perfect(demand),
        horizon=30,
        history_steps=len(demand),
        hpa_stabilization_ticks=150,
        scale_down_stabilization_ticks=0,
        lead_steps=15,
    )

    assert result is not None
    outcomes = {o.name: o for o in result.outcomes}
    # The HPA is short for at least the whole start-up delay (15 of the 267
    # replayed ticks). ScaleScope starts pods before the step; the +4 pods
    # per decision cap still leaves it briefly short of a 7x jump.
    assert outcomes["Reactive HPA"].under_provisioned_pct >= 100 * 15 / 267
    assert (
        outcomes["ScaleScope"].under_provisioned_pct
        < outcomes["Reactive HPA"].under_provisioned_pct / 2
    )


def _replay(frame: pl.DataFrame, capacity: PodCapacity, demand: np.ndarray) -> object:
    return replay_scaling(
        frame,
        "request_rate",
        capacity,
        ScalingPolicy(),
        _Perfect(demand),
        horizon=30,
        history_steps=100,
        hpa_stabilization_ticks=150,
        scale_down_stabilization_ticks=0,
        lead_steps=15,
    )


def test_replay_needs_capacity_and_history() -> None:
    demand = np.full(400, 100.0)
    unknown = PodCapacity(None, "unavailable")
    known = PodCapacity(100.0, "configured")
    assert _replay(_frame(demand), unknown, demand) is None
    assert _replay(_frame(demand[:50]), known, demand) is None
