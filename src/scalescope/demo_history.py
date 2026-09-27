"""DEMO mode's daily and weekly traffic shape, and its generated history.

A real workload teaches the long-memory model its pattern over days and
weeks of monitoring. DEMO mode cannot wait that long, so at first start it
generates `DEMO_HISTORY_DAYS` of minute history with the same shape the live
simulator then follows (`demand_level`), and the dashboard says so. Everything
else (training, forecasting, scoring) runs exactly as on a real cluster.

The demo workload runs on a node pool (`DemoNodePool`) scaled by a
reactive cluster autoscaler, next to other workloads whose requests stay
steady; its node history is generated the same way.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

import numpy as np
import polars as pl

from scalescope.simulator import (
    CAPACITY_PER_POD_RPS,
    CPU_REQUEST_MILLICORES,
    MAX_REPLICAS,
    MIN_REPLICAS,
    TARGET_CPU_UTILIZATION,
    latency_p95_ms,
)
from scalescope.storage import NODE_MINUTE_COLUMNS, Store

DEMO_HISTORY_DAYS = 21
MEAN_DEMAND_RPS = 700.0
# The simulator's ten-minute wave, kept small next to the daily pattern.
_WAVE_AMPLITUDE = 0.1
_WAVE_SECONDS = 600
_NOISE = 0.04

DEMO_POOL = "general"
POD_MEMORY_REQUEST_MB = 512.0
# 4 vCPU / 16 GiB nodes, as the kubelet reports them allocatable.
_NODE_CPU_MILLICORES = 3920.0
_NODE_MEMORY_MB = 14800.0
_DAEMONSET_CPU_MILLICORES = 150.0
_DAEMONSET_MEMORY_MB = 300.0
# The other workloads on the pool, which do not change.
_OTHER_CPU_MILLICORES = 6500.0
_OTHER_MEMORY_MB = 14000.0
_MIN_NODES = 2
# The scheduler never fills nodes completely.
_SCHEDULER_PACKING = 0.9
# Cluster-autoscaler behaviour: a node is Ready about two minutes after it
# is requested; a node is removed once the pods would have fitted on one
# fewer for ten minutes.
_NODE_STARTUP_SECONDS = (100.0, 140.0)
_SCALE_DOWN_UNNEEDED_MINUTES = 10


def _daily_shape(hour: float, weekend: bool) -> float:
    def bump(centre: float, width: float, height: float) -> float:
        return height * math.exp(-0.5 * ((hour - centre) / width) ** 2)

    weekday = 0.2 + bump(10.0, 1.8, 1.0) + bump(15.0, 2.2, 0.8) + bump(20.0, 1.5, 0.25)
    return 0.3 + 0.5 * weekday if weekend else weekday


def _mean_shape() -> float:
    samples = [
        _daily_shape(minute / 60, day >= 5)
        for day in range(7)
        for minute in range(1440)
    ]
    return sum(samples) / len(samples)


_MEAN_SHAPE = _mean_shape()


def demand_level(ts: datetime) -> float:
    """Requests/s the demo workload receives at `ts` (UTC), before noise."""
    hour = ts.hour + ts.minute / 60 + ts.second / 3600
    daily = _daily_shape(hour, ts.weekday() >= 5) / _MEAN_SHAPE
    wave = 1 + _WAVE_AMPLITUDE * math.sin(2 * math.pi * ts.timestamp() / _WAVE_SECONDS)
    return MEAN_DEMAND_RPS * daily * wave


@dataclass
class DemoNodePool:
    """The demo's node pool, run by a reactive cluster autoscaler.

    `step` advances one minute: nodes are added only once pods do not fit
    (pending), and become Ready after a start-up delay; a node is removed
    only after the pods have fitted on one fewer for ten minutes.
    """

    nodes: int = 4
    # (ready at, node name, requested at, seconds to Ready)
    starting: list[tuple[datetime, str, datetime, float]] = field(default_factory=list)
    unneeded_minutes: int = 0
    rng: np.random.Generator = field(default_factory=lambda: np.random.default_rng(1))

    def _usable(self, nodes: int) -> float:
        return nodes * (_NODE_CPU_MILLICORES - _DAEMONSET_CPU_MILLICORES)

    def step(
        self, minute: datetime, demo_pods: float
    ) -> tuple[dict[str, Any], list[tuple[str, str, datetime, float]]]:
        """One minute of pool state, and the nodes that became Ready in it."""
        ready_now = [s for s in self.starting if s[0] <= minute]
        self.starting = [s for s in self.starting if s[0] > minute]
        self.nodes += len(ready_now)
        startups = [
            (name, DEMO_POOL, asked, secs) for _, name, asked, secs in ready_now
        ]

        wanted = _OTHER_CPU_MILLICORES + demo_pods * CPU_REQUEST_MILLICORES
        fits = self._usable(self.nodes) * _SCHEDULER_PACKING
        pending = max(0, math.ceil((wanted - fits) / CPU_REQUEST_MILLICORES))
        if pending and not self.starting:
            per_node = (_NODE_CPU_MILLICORES - _DAEMONSET_CPU_MILLICORES) * (
                _SCHEDULER_PACKING
            )
            for i in range(math.ceil((wanted - fits) / per_node)):
                seconds = float(self.rng.uniform(*_NODE_STARTUP_SECONDS))
                self.starting.append(
                    (
                        minute + timedelta(seconds=seconds),
                        f"demo-node-{minute:%Y%m%d%H%M}-{i}",
                        minute,
                        seconds,
                    )
                )
        if (
            self.nodes > _MIN_NODES
            and not self.starting
            and wanted <= self._usable(self.nodes - 1) * _SCHEDULER_PACKING
        ):
            self.unneeded_minutes += 1
            if self.unneeded_minutes >= _SCALE_DOWN_UNNEEDED_MINUTES:
                self.nodes -= 1
                self.unneeded_minutes = 0
        else:
            self.unneeded_minutes = 0

        placed = min(wanted, fits) if pending else wanted
        # Pending pods count as requested, as the collector reports them.
        requested = placed + pending * CPU_REQUEST_MILLICORES
        demo_memory = demo_pods * POD_MEMORY_REQUEST_MB
        row = {
            "pool": DEMO_POOL,
            "minute": minute.replace(tzinfo=None),
            "nodes": self.nodes,
            "allocatable_cpu_millicores": self.nodes * _NODE_CPU_MILLICORES,
            "allocatable_memory_mb": self.nodes * _NODE_MEMORY_MB,
            "requested_cpu_millicores": requested
            + self.nodes * _DAEMONSET_CPU_MILLICORES,
            "requested_memory_mb": _OTHER_MEMORY_MB
            + demo_memory
            + self.nodes * _DAEMONSET_MEMORY_MB,
            "daemonset_cpu_millicores": self.nodes * _DAEMONSET_CPU_MILLICORES,
            "daemonset_memory_mb": self.nodes * _DAEMONSET_MEMORY_MB,
            "pending_pods": pending,
        }
        return row, startups


def backfill_nodes(
    store: Store, replicas: np.ndarray, minutes: list[datetime]
) -> DemoNodePool:
    """Generate the pool's node history for the demo workload's pod counts."""
    pool = DemoNodePool()
    rows = []
    for minute, pods in zip(minutes, replicas, strict=True):
        row, startups = pool.step(minute, float(pods))
        rows.append(row)
        store.record_node_startups(startups)
    store.insert_node_minutes(pl.DataFrame(rows).select(NODE_MINUTE_COLUMNS))
    return pool


def backfill(store: Store, workload: str, now: datetime) -> int:
    """Generate the demo's minute history up to `now` if it has none yet.

    Returns the minutes added; 0 when history already exists, so restarts
    keep what the demo has since recorded.
    """
    first_minute = now.replace(second=0, microsecond=0) - timedelta(
        days=DEMO_HISTORY_DAYS
    )
    if not store.minute_history(workload, first_minute).is_empty():
        return 0
    rng = np.random.default_rng(0)
    minutes = [
        first_minute + timedelta(minutes=i) for i in range(DEMO_HISTORY_DAYS * 1440)
    ]
    demand = np.array([demand_level(m + timedelta(seconds=30)) for m in minutes])
    demand *= np.exp(rng.normal(0, _NOISE, len(demand)))
    replicas = np.clip(
        np.ceil(demand / (CAPACITY_PER_POD_RPS * TARGET_CPU_UTILIZATION)),
        MIN_REPLICAS,
        MAX_REPLICAS,
    )
    per_pod = demand / replicas
    rows = pl.DataFrame(
        {
            "workload": [workload] * len(minutes),
            "minute": [m.replace(tzinfo=None) for m in minutes],
            "samples": [30] * len(minutes),
            "request_rate": demand,
            "cpu_usage_millicores": np.minimum(per_pod / CAPACITY_PER_POD_RPS, 1.0)
            * CPU_REQUEST_MILLICORES
            * replicas,
            "replicas": replicas,
            "latency_p95_ms": [latency_p95_ms(load) for load in per_pod],
        }
    )
    added = store.insert_minutes(rows)
    backfill_nodes(store, replicas, minutes)
    return added
