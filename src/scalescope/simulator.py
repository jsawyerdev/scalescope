"""Synthetic Kubernetes workload generator for DEMO mode.

Models one workload as a reactive-HPA-controlled deployment so that CPU-per-pod
genuinely responds to replica count (the scaling feedback loop the forecaster
must not be fooled by). Periodically injects one of a fixed set of fault
scenarios so the diagnosis engine has real conditions to classify.
"""

from __future__ import annotations

import logging
import math
import random
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

CAPACITY_PER_POD_RPS = 220.0
# One pod at 100% of this CPU request serves CAPACITY_PER_POD_RPS.
CPU_REQUEST_MILLICORES = 1000.0
TARGET_CPU_UTILIZATION = 0.60
MIN_REPLICAS = 3
MAX_REPLICAS = 30
BASE_LATENCY_MS = 20.0
# p95 of an M/M/1 response time is ln(20) / (mu - x) seconds; in ms * req/s.
QUEUEING_P95_COEFFICIENT = math.log(20) * 1000
# An overloaded pod's queue grows without bound; report a capped latency.
OVERLOAD_LATENCY_MS = 2000.0
BASE_MEMORY_MB = 180.0
FAULTS = ("traffic_spike", "memory_leak", "cpu_limit", "node_capacity")


def latency_p95_ms(
    load_per_pod: float, capacity_per_pod: float = CAPACITY_PER_POD_RPS
) -> float:
    """p95 latency of one pod as an M/M/1 queue, capped once it saturates."""
    headroom = capacity_per_pod - load_per_pod
    queueing_ms = (
        QUEUEING_P95_COEFFICIENT / headroom if headroom > 0 else OVERLOAD_LATENCY_MS
    )
    return min(OVERLOAD_LATENCY_MS, BASE_LATENCY_MS + queueing_ms)


@dataclass
class WorkloadState:
    """Mutable simulation state for one workload."""

    name: str = "sample-app"
    replicas: int = 8
    memory_leak_mb_per_tick: float = 0.0
    memory_baseline_mb: float = BASE_MEMORY_MB
    cpu_limit_constrained: bool = False
    node_capacity_constrained: bool = False
    tick: int = 0
    fault_ticks_remaining: int = 0
    active_fault: str | None = None
    # DEMO observations must be repeatable; this RNG never handles secret material.
    rng: random.Random = field(default_factory=lambda: random.Random(42))  # nosec B311


class WorkloadSimulator:
    """Advances one synthetic workload by one observation per call to `step`."""

    def __init__(
        self,
        state: WorkloadState | None = None,
        demand_level: Callable[[], float] | None = None,
    ) -> None:
        """`demand_level`, if given, replaces the built-in ten-minute cycle."""
        self.state = state or WorkloadState()
        self._demand_level = demand_level

    def _demand(self) -> float:
        t = self.state.tick
        daily = (
            self._demand_level()
            if self._demand_level is not None
            else math.sin(2 * math.pi * t / 300) * 400 + 700
        )
        noise = self.state.rng.gauss(0, 25)
        spike = 600 if self.state.active_fault == "traffic_spike" else 0
        return max(50.0, daily + noise + spike)

    def _clear_fault(self) -> None:
        self.state.active_fault = None
        self.state.memory_leak_mb_per_tick = 0.0
        self.state.cpu_limit_constrained = False
        self.state.node_capacity_constrained = False

    def _start_fault(self, fault: str, duration_ticks: int) -> None:
        self.state.active_fault = fault
        self.state.fault_ticks_remaining = duration_ticks
        if fault == "memory_leak":
            self.state.memory_leak_mb_per_tick = self.state.rng.uniform(0.5, 2.0)
        elif fault == "cpu_limit":
            self.state.cpu_limit_constrained = True
        elif fault == "node_capacity":
            self.state.node_capacity_constrained = True
        logger.info("fault started: %s", fault)

    def _maybe_start_fault(self) -> None:
        if self.state.active_fault is not None:
            return
        if self.state.rng.random() > 0.01:
            return
        fault = self.state.rng.choice(FAULTS)
        self._start_fault(fault, self.state.rng.randint(60, 180))

    def trigger_fault(self, fault: str, duration_ticks: int = 60) -> None:
        """Manually start `fault` (one of `FAULTS`) now, replacing any running fault."""
        if fault not in FAULTS:
            raise ValueError(f"unknown fault: {fault}")
        self._clear_fault()
        self._start_fault(fault, duration_ticks)

    def _maybe_end_fault(self) -> None:
        if self.state.active_fault is None:
            return
        self.state.fault_ticks_remaining -= 1
        if self.state.fault_ticks_remaining > 0:
            return
        logger.info("fault ended: %s", self.state.active_fault)
        self._clear_fault()

    def _reactive_hpa(self, cpu_utilization: float) -> tuple[int, int]:
        """Reactive controller: mimics Kubernetes HPA scaling on CPU target.

        Returns (new_replicas, pending_pods).
        """
        desired = math.ceil(
            self.state.replicas * (cpu_utilization / TARGET_CPU_UTILIZATION)
        )
        desired = max(MIN_REPLICAS, min(MAX_REPLICAS, desired))
        step = max(-2, min(2, desired - self.state.replicas))
        new_replicas = self.state.replicas + step
        pending = 0
        if self.state.node_capacity_constrained:
            max_schedulable = self.state.replicas + 1
            if new_replicas > max_schedulable:
                pending = new_replicas - max_schedulable
                new_replicas = max_schedulable
        return new_replicas, pending

    def step(self) -> dict[str, Any]:
        """Advance simulation by one tick and return one observation row."""
        self.state.tick += 1
        self._maybe_start_fault()
        self._maybe_end_fault()

        demand = self._demand()
        effective_capacity_per_pod = (
            CAPACITY_PER_POD_RPS * 0.5
            if self.state.cpu_limit_constrained
            else CAPACITY_PER_POD_RPS
        )
        utilization_per_pod = demand / (
            self.state.replicas * effective_capacity_per_pod
        )
        cpu_usage_pct = min(1.0, utilization_per_pod) * 100
        cpu_throttled_pct = (
            max(0.0, utilization_per_pod - 1.0) * 100
            if self.state.cpu_limit_constrained
            else 0.0
        )

        self.state.memory_baseline_mb += self.state.memory_leak_mb_per_tick
        memory_usage_mb = self.state.memory_baseline_mb + self.state.rng.gauss(0, 5)

        # Each pod is a queue: latency climbs as its load nears capacity.
        load_per_pod = demand / self.state.replicas
        latency = latency_p95_ms(
            load_per_pod, effective_capacity_per_pod
        ) * self.state.rng.gauss(1.0, 0.05)
        error_rate = (
            max(0.0, min(1.0, (utilization_per_pod - 0.95) * 2))
            if utilization_per_pod > 0.95
            else 0.0
        )

        restarts = 1 if memory_usage_mb > 900 else 0
        if restarts:
            self.state.memory_baseline_mb = BASE_MEMORY_MB

        new_replicas, pending_pods = self._reactive_hpa(utilization_per_pod)
        current_replicas = self.state.replicas
        self.state.replicas = new_replicas

        return {
            "ts": datetime.now(UTC),
            "workload": self.state.name,
            "replicas": current_replicas,
            "desired_replicas": new_replicas,
            "cpu_usage_millicores": round(
                cpu_usage_pct / 100 * CPU_REQUEST_MILLICORES * current_replicas, 1
            ),
            "cpu_request_millicores": CPU_REQUEST_MILLICORES,
            "request_rate": round(demand, 2),
            "cpu_usage_pct": round(cpu_usage_pct, 2),
            "cpu_throttled_pct": round(cpu_throttled_pct, 2),
            "memory_usage_mb": round(memory_usage_mb, 2),
            "latency_p95_ms": round(max(0.0, latency), 2),
            "error_rate": round(error_rate, 4),
            "pending_pods": pending_pods,
            "restarts": restarts,
        }
