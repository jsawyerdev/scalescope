"""Replays a workload's recorded demand through two scaling policies.

Answers "would ScaleScope have scaled this workload better than a standard
Kubernetes HPA?" from its own history. Both policies see the same demand,
the same per-pod capacity, the same replica bounds, and the same pod
start-up delay; neither sees the future.

- ScaleScope: the same forecast, diagnosis, and recommendation code that
  drives actuation, deciding every `decision_every` ticks.
- Reactive HPA: pods for the demand it has just observed, at the same target
  utilization, scaling down only to the highest count of the last
  `hpa_stabilization_ticks` (the HPA's default window is 300s).

Capacity is resolved from the first third of the history and the policies
are replayed over the rest, so capacity never uses data from the replayed
period. A tick is under-provisioned when demand exceeds what the ready pods
can serve at saturation.
"""

from __future__ import annotations

import math
from collections import deque
from dataclasses import dataclass

import polars as pl

from scalescope.capacity import (
    STARTUP_LEAD_STEPS,
    PodCapacity,
    ScaleDownStabilizer,
    ScalingPolicy,
    recommend_replicas,
)
from scalescope.demand import DemandSignal, demand_history
from scalescope.diagnosis import diagnose
from scalescope.models.base import ForecastModel

MIN_REPLAY_TICKS = 60
MAX_DECISIONS = 150


@dataclass(frozen=True)
class PolicyOutcome:
    name: str
    under_provisioned_pct: float
    average_pods: float
    scale_changes: int


@dataclass(frozen=True)
class ScalingReplay:
    ticks: int
    decision_every: int
    outcomes: list[PolicyOutcome]


class _Cluster:
    """Pods that become ready `lead` ticks after they are requested."""

    def __init__(self, replicas: int, lead: int) -> None:
        self.target = replicas
        self.ready = replicas
        self._lead = lead
        self._pending: deque[tuple[int, int]] = deque()
        self.scale_changes = 0

    def advance(self, tick: int) -> None:
        while self._pending and self._pending[0][0] <= tick:
            self.ready += self._pending.popleft()[1]

    def scale_to(self, tick: int, replicas: int) -> None:
        if replicas == self.target:
            return
        self.scale_changes += 1
        if replicas > self.target:
            self._pending.append((tick + self._lead, replicas - self.target))
        else:
            remove = self.target - replicas
            # Pods still starting are cancelled before running ones stop.
            while remove and self._pending:
                ready_at, count = self._pending.pop()
                cancelled = min(count, remove)
                remove -= cancelled
                if count > cancelled:
                    self._pending.append((ready_at, count - cancelled))
            self.ready -= remove
        self.target = replicas


def replay_scaling(
    observations: pl.DataFrame,
    signal: DemandSignal,
    capacity: PodCapacity,
    policy: ScalingPolicy,
    model: ForecastModel,
    horizon: int,
    history_steps: int,
    hpa_stabilization_ticks: int,
    scale_down_stabilization_ticks: int,
) -> ScalingReplay | None:
    """Both policies over the last two thirds of `observations`, or None if too short.

    `capacity` must come from data before the replayed period (see module
    docstring); returns None without it.
    """
    start = len(observations) // 3
    if capacity.per_pod is None or len(observations) - start < MIN_REPLAY_TICKS:
        return None

    demand = demand_history(observations, signal)
    replicas = observations["replicas"].to_numpy()
    ticks = len(demand) - start
    decision_every = max(1, math.ceil(ticks / MAX_DECISIONS))
    utilization = capacity.target_utilization or policy.target_utilization
    safe_per_pod = capacity.per_pod * utilization

    def bounded(pods: int) -> int:
        return max(policy.min_replicas, min(policy.max_replicas, pods))

    initial = bounded(int(replicas[start]))
    clusters = {
        "ScaleScope": _Cluster(initial, STARTUP_LEAD_STEPS),
        "Reactive HPA": _Cluster(initial, STARTUP_LEAD_STEPS),
    }
    scalescope_stabilizer = ScaleDownStabilizer(scale_down_stabilization_ticks)
    hpa_stabilizer = ScaleDownStabilizer(hpa_stabilization_ticks)
    under = dict.fromkeys(clusters, 0)
    pod_ticks = dict.fromkeys(clusters, 0)

    for tick in range(start, len(demand)):
        for cluster in clusters.values():
            cluster.advance(tick)

        if (tick - start) % decision_every == 0:
            first = max(0, tick - history_steps)
            ours = clusters["ScaleScope"]
            forecast = model.predict(demand[first:tick], horizon)
            recommended = recommend_replicas(
                ours.target,
                forecast,
                capacity,
                policy,
                peak_step=STARTUP_LEAD_STEPS,
                scaling_will_help=diagnose(
                    observations.slice(first, tick - first),
                    max_replicas=policy.max_replicas,
                ).scaling_will_help,
            ).recommended_replicas
            ours.scale_to(
                tick, scalescope_stabilizer.stabilize(tick, ours.target, recommended)
            )

            hpa = clusters["Reactive HPA"]
            wanted = bounded(math.ceil(float(demand[tick - 1]) / safe_per_pod))
            hpa.scale_to(tick, hpa_stabilizer.stabilize(tick, hpa.target, wanted))

        for name, cluster in clusters.items():
            under[name] += bool(demand[tick] > cluster.ready * capacity.per_pod)
            pod_ticks[name] += cluster.target

    return ScalingReplay(
        ticks=ticks,
        decision_every=decision_every,
        outcomes=[
            PolicyOutcome(
                name=name,
                under_provisioned_pct=round(100 * under[name] / ticks, 2),
                average_pods=round(pod_ticks[name] / ticks, 2),
                scale_changes=cluster.scale_changes,
            )
            for name, cluster in clusters.items()
        ],
    )


def capacity_training_rows(observations: pl.DataFrame) -> pl.DataFrame:
    """The rows capacity may be resolved from: those before the replayed period."""
    return observations.head(len(observations) // 3)
