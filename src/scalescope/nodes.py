"""Node pools: what they hold, what they will need, and how long nodes take.

Node CPU is no more a demand signal than per-pod CPU: it falls when nodes
are added. What decides how many nodes a pool needs is what its pods
*request*. So a pool's future requests are built from the workloads'
demand forecasts:

    requested(t) = requested now
                   + sum over forecast workloads of
                     (pods(t) - pods now) x that workload's per-pod request

where pods(t) follows the workload's long-memory demand forecast the way
its pod count has followed its demand over the last week, whatever
autoscaler runs it (`PodScaling`). Everything not forecast (workloads
without a trained model, pods ScaleScope cannot see) is held at what it
requests now. Nodes needed divides by what one node offers once its
DaemonSet pods are placed, and by how tightly this pool has actually
packed pods (`packing_factor`): the scheduler never fills every node.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from scalescope.demand import DemandSignal
from scalescope.learning import ACCURACY_DAYS, Learner
from scalescope.models.base import Forecast
from scalescope.storage import Store

# How far back a workload's pod count is compared with its demand, and a
# pool's packing measured.
FIT_DAYS = 7
_MIN_FIT_MINUTES = 60
# Packing: the 95th percentile of (fewest nodes the requests fit on /
# nodes running): how tightly the pool packs when it is busy. Never below
# 70%: a pool that has only ever run mostly empty (a fixed node count, say)
# must not teach that idleness is how tightly pods pack.
_PACKING_QUANTILE = 0.95
_PACKING_BOUNDS = (0.7, 1.0)
DEFAULT_PACKING = 0.85
# A pool whose newest minute is older than this has no current state.
_MAX_STALENESS_MINUTES = 5
_BASELINE_MINUTES = 5
_CHART_HOURS = 6
_STARTUP_SAMPLES = 20

_DEMAND_COLUMN: dict[DemandSignal, str] = {
    "request_rate": "request_rate",
    "cpu_millicores": "cpu_usage_millicores",
}


@dataclass(frozen=True)
class PodScaling:
    """How a workload's pod count has followed its demand.

    `demand_per_pod` is None when the pod count did not change with demand
    over the fit window (a fixed replica count): its pods are then held.
    """

    demand_per_pod: float | None
    floor: float
    ceiling: float

    def pods(self, demand: np.ndarray) -> np.ndarray:
        if self.demand_per_pod is None:
            return np.full(len(demand), self.floor)
        pods: np.ndarray = np.clip(
            demand / self.demand_per_pod, self.floor, self.ceiling
        )
        return pods


def fit_pod_scaling(minutes: pl.DataFrame, signal: DemandSignal) -> PodScaling | None:
    """Fit `PodScaling` on a workload's minute history; None if too short.

    The floor is the fewest pods seen (an autoscaler's minimum); demand per
    pod is the median over minutes when the pod count sat above that floor,
    so an autoscaler's reaction to demand is what is measured. The ceiling
    allows twice the most pods seen: past that is no longer this history.
    """
    if minutes.height < _MIN_FIT_MINUTES:
        return None
    replicas = minutes["replicas"].to_numpy().astype(float)
    demand = minutes[_DEMAND_COLUMN[signal]].to_numpy().astype(float)
    floor, most = float(replicas.min()), float(replicas.max())
    scaling = (replicas >= floor + 1) & (demand > 0)
    if most - floor < 1 or scaling.sum() < _MIN_FIT_MINUTES // 2:
        return PodScaling(None, float(replicas[-1]), float(replicas[-1]))
    per_pod = float(np.median(demand[scaling] / replicas[scaling]))
    return PodScaling(per_pod, floor, 2 * most)


def _fewest_nodes(
    cpu: np.ndarray, memory: np.ndarray, pool: dict[str, Any] | pl.DataFrame
) -> np.ndarray:
    """Nodes the non-DaemonSet requests would fill if packed perfectly.

    `pool` is one minute (a row dict) or a frame of minutes aligned with
    `cpu` and `memory`. A node offers its allocatable resources less its
    share of the pool's DaemonSet requests, which every node carries.
    """

    def value(column: str) -> Any:
        return (
            pool[column].to_numpy().astype(float)
            if isinstance(pool, pl.DataFrame)
            else float(pool[column])
        )

    nodes = np.maximum(value("nodes"), 1.0)
    ds_cpu, ds_memory = value("daemonset_cpu_millicores"), value("daemonset_memory_mb")
    usable_cpu = np.maximum((value("allocatable_cpu_millicores") - ds_cpu) / nodes, 1.0)
    usable_memory = np.maximum(
        (value("allocatable_memory_mb") - ds_memory) / nodes, 1.0
    )
    fewest: np.ndarray = np.maximum(
        (cpu - ds_cpu) / usable_cpu, (memory - ds_memory) / usable_memory
    )
    return fewest


def _requested(history: pl.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    return (
        history["requested_cpu_millicores"].to_numpy().astype(float),
        history["requested_memory_mb"].to_numpy().astype(float),
    )


def packing_factor(history: pl.DataFrame) -> float:
    """How full this pool's nodes get when it is busy, from its minute history."""
    rows = history.filter(pl.col("nodes") > 0)
    if rows.height < _MIN_FIT_MINUTES:
        return DEFAULT_PACKING
    ratios = _fewest_nodes(*_requested(rows), rows) / rows["nodes"].to_numpy()
    low, high = _PACKING_BOUNDS
    return float(np.clip(np.quantile(ratios, _PACKING_QUANTILE), low, high))


def nodes_needed(
    cpu: np.ndarray,
    memory: np.ndarray,
    pool: dict[str, Any] | pl.DataFrame,
    packing: float,
) -> np.ndarray:
    """Nodes a pool needs for these requests at its usual packing (at least 1)."""
    fewest = _fewest_nodes(cpu, memory, pool) / packing
    needed: np.ndarray = np.maximum(1, np.ceil(fewest - 1e-9)).astype(int)
    return needed


def nodes_needed_history(history: pl.DataFrame, packing: float) -> np.ndarray:
    """Nodes each recorded minute's requests needed at usual packing."""
    if history.is_empty():
        return np.array([], dtype=int)
    return nodes_needed(*_requested(history), history, packing)


def idle_node_hours(history: pl.DataFrame, packing: float) -> float:
    """Node-hours run beyond what the pods' requests needed at usual packing."""
    if history.is_empty():
        return 0.0
    idle = history["nodes"].to_numpy() - nodes_needed_history(history, packing)
    return float(np.maximum(idle, 0).sum() / 60)


@dataclass(frozen=True)
class WorkloadDemand:
    """One workload's part in its pool's forecast."""

    workload: str
    cpu_request: float
    memory_request: float
    demand_now: float
    forecast: Forecast
    scaling: PodScaling


def forecast_requests(
    pool: dict[str, Any], workloads: list[WorkloadDemand], horizon: int
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(CPU p50, CPU p90, memory p90) the pool will request, per minute ahead.

    Each workload moves by how its pods would change from demand now to the
    forecast demand; a workload whose pods did not follow demand adds 0.
    """
    cpu50 = np.full(horizon, float(pool["requested_cpu_millicores"]))
    cpu90 = cpu50.copy()
    memory90 = np.full(horizon, float(pool["requested_memory_mb"]))
    for w in workloads:
        now = w.scaling.pods(np.array([w.demand_now]))[0]
        extra50 = w.scaling.pods(w.forecast.p50[:horizon]) - now
        extra90 = w.scaling.pods(w.forecast.p90[:horizon]) - now
        cpu50 += extra50 * w.cpu_request
        cpu90 += extra90 * w.cpu_request
        memory90 += extra90 * w.memory_request
    return (
        np.maximum(cpu50, 0.0),
        np.maximum(cpu90, 0.0),
        np.maximum(memory90, 0.0),
    )


@dataclass(frozen=True)
class PoolPlan:
    """One pool's state now, its history, and what it will need."""

    pool: str
    now: dict[str, Any]
    packing: float
    nodes_needed_now: int
    workloads: list[str]
    workloads_forecast: list[str]
    forecast_start: datetime | None
    requested_cpu_p50: list[float]
    requested_cpu_p90: list[float]
    nodes_needed: list[int]
    history: pl.DataFrame = field(repr=False)


def _minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0, tzinfo=None)


class NodePlanner:
    """Forecasts each node pool's requests and nodes, and scores it live.

    `refresh` runs once a minute from the history loop; API handlers read
    the latest plans. Thread-safe.
    """

    def __init__(self, store: Store, learner: Learner, horizon_minutes: int) -> None:
        self._store = store
        self._learner = learner
        self._horizon = horizon_minutes
        self._lock = threading.Lock()
        self._scaling: dict[str, PodScaling] = {}
        self._plans: dict[str, PoolPlan] = {}

    def refit(self, now: datetime) -> None:
        """Refit how each workload's pods follow its demand (on retrain)."""
        since = _minute(now) - timedelta(days=FIT_DAYS)
        scaling: dict[str, PodScaling] = {}
        for workload in self._store.workloads():
            signal = self._learner.trained_signal(workload)
            if signal is None:
                continue
            fit = fit_pod_scaling(self._store.minute_history(workload, since), signal)
            if fit is not None:
                scaling[workload] = fit
        with self._lock:
            self._scaling = scaling

    def refresh(self, now: datetime, log: bool = False) -> None:
        """Recompute every pool's plan; `log` records the forecasts for scoring."""
        minute = _minute(now)
        history = self._store.node_history(minute - timedelta(days=FIT_DAYS))
        if history.is_empty():
            with self._lock:
                self._plans = {}
            return
        # Workloads seen recently: a deleted Deployment is no longer a member.
        latest = self._store.latest_observations(
            minute - timedelta(minutes=_MAX_STALENESS_MINUTES)
        )
        with self._lock:
            scaling = dict(self._scaling)
        plans: dict[str, PoolPlan] = {}
        for pool_name in history["pool"].unique().sort().to_list():
            pool_history = history.filter(pl.col("pool") == pool_name)
            plan = self._plan(pool_name, pool_history, latest, scaling, minute)
            if plan is None:
                continue
            plans[pool_name] = plan
            if log and plan.forecast_start is not None:
                self._store.log_node_forecast(
                    pool_name,
                    made_at=minute,
                    minutes=[
                        plan.forecast_start + timedelta(minutes=i)
                        for i in range(len(plan.requested_cpu_p50))
                    ],
                    p50=plan.requested_cpu_p50,
                    p90=plan.requested_cpu_p90,
                    baseline=float(plan.now["requested_cpu_millicores"]),
                )
        with self._lock:
            self._plans = plans

    def plans(self) -> dict[str, PoolPlan]:
        with self._lock:
            return dict(self._plans)

    def startup_seconds(self, pool: str, now: datetime) -> list[float]:
        since = _minute(now) - timedelta(days=FIT_DAYS * 5)
        return self._store.node_startup_seconds(pool, since)[:_STARTUP_SAMPLES]

    def accuracy(self, pool: str, now: datetime) -> pl.DataFrame:
        since = _minute(now) - timedelta(days=ACCURACY_DAYS)
        return self._store.node_forecast_accuracy(pool, since).drop_nulls()

    def _plan(
        self,
        pool_name: str,
        history: pl.DataFrame,
        latest: pl.DataFrame,
        scaling: dict[str, PodScaling],
        minute: datetime,
    ) -> PoolPlan | None:
        now = history.row(history.height - 1, named=True)
        if (minute - now["minute"]).total_seconds() > _MAX_STALENESS_MINUTES * 60:
            return None
        packing = packing_factor(history)
        in_pool = latest.filter(pl.col("node_pool") == pool_name)
        members = in_pool["workload"].to_list()
        demands = []
        for row in in_pool.iter_rows(named=True):
            demand = self._workload_demand(row, scaling, minute)
            if demand is not None:
                demands.append(demand)
        needed_now = int(
            nodes_needed(
                np.array([now["requested_cpu_millicores"]]),
                np.array([now["requested_memory_mb"]]),
                now,
                packing,
            )[0]
        )
        cpu50: list[float] = []
        cpu90: list[float] = []
        needed: list[int] = []
        if demands:
            horizon = min(self._horizon, *(len(d.forecast.p50) for d in demands))
            p50, p90, memory90 = forecast_requests(now, demands, horizon)
            cpu50 = [float(v) for v in p50]
            cpu90 = [float(v) for v in p90]
            needed = [int(v) for v in nodes_needed(p90, memory90, now, packing)]
        return PoolPlan(
            pool=pool_name,
            now=now,
            packing=packing,
            nodes_needed_now=needed_now,
            workloads=members,
            workloads_forecast=[d.workload for d in demands],
            forecast_start=minute if demands else None,
            requested_cpu_p50=cpu50,
            requested_cpu_p90=cpu90,
            nodes_needed=needed,
            history=history.filter(
                pl.col("minute") >= minute - timedelta(hours=_CHART_HOURS)
            ),
        )

    def _workload_demand(
        self, row: dict[str, Any], scaling: dict[str, PodScaling], minute: datetime
    ) -> WorkloadDemand | None:
        workload = row["workload"]
        fit = scaling.get(workload)
        signal = self._learner.trained_signal(workload)
        if fit is None or fit.demand_per_pod is None or signal is None:
            return None
        forecast = self._learner.forecast(workload, signal, minute)
        if forecast is None:
            return None
        recent = self._store.minute_history(
            workload, minute - timedelta(minutes=_BASELINE_MINUTES)
        )
        if recent.is_empty():
            return None
        return WorkloadDemand(
            workload=workload,
            cpu_request=float(row["cpu_request_millicores"] or 0.0),
            memory_request=float(row["memory_request_mb"] or 0.0),
            demand_now=float(np.mean(recent[_DEMAND_COLUMN[signal]].to_numpy())),
            forecast=forecast,
            scaling=fit,
        )


def median_or_none(values: list[float]) -> float | None:
    return float(np.median(values)) if values else None
