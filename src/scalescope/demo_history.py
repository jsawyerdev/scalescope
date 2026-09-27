"""DEMO mode's daily and weekly traffic shape, and its generated history.

A real workload teaches the long-memory model its pattern over days and
weeks of monitoring. DEMO mode cannot wait that long, so at first start it
generates `DEMO_HISTORY_DAYS` of minute history with the same shape the live
simulator then follows (`demand_level`), and the dashboard says so. Everything
else (training, forecasting, scoring) runs exactly as on a real cluster.
"""

from __future__ import annotations

import math
from datetime import datetime, timedelta

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
from scalescope.storage import Store

DEMO_HISTORY_DAYS = 21
MEAN_DEMAND_RPS = 700.0
# The simulator's ten-minute wave, kept small next to the daily pattern.
_WAVE_AMPLITUDE = 0.1
_WAVE_SECONDS = 600
_NOISE = 0.04


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
    return store.insert_minutes(rows)
