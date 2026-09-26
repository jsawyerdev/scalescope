"""Which observed series a workload's replica needs are forecast from.

Request rate is preferred: it is pure demand. Without it (no metrics URL or
Prometheus query), a workload's *total* CPU usage across all its pods is
the demand signal every cluster exposes through metrics-server. Unlike
per-pod CPU %, which falls when replicas are added, total CPU tracks the
work being done regardless of how many pods share it.
"""

from __future__ import annotations

from typing import Literal

import numpy as np
import polars as pl

DemandSignal = Literal["request_rate", "cpu_millicores"]

_COLUMN: dict[DemandSignal, str] = {
    "request_rate": "request_rate",
    "cpu_millicores": "cpu_usage_millicores",
}


def demand_signal(observations: pl.DataFrame) -> DemandSignal:
    """`request_rate` if the workload ever reported traffic, else total CPU."""
    if (observations["request_rate"] > 0).any():
        return "request_rate"
    if (observations["cpu_usage_millicores"] > 0).any():
        return "cpu_millicores"
    return "request_rate"


def demand_history(observations: pl.DataFrame, signal: DemandSignal) -> np.ndarray:
    return observations[_COLUMN[signal]].to_numpy()
