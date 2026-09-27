#!/usr/bin/env python3
"""Measure the node-pool forecast: requested CPU and nodes, an hour ahead.

Three workloads share one pool of 4-vCPU nodes, each with its own traffic
(`eval_long_memory.generate`, different seeds and sizes) and an HPA-style
pod count (demand / per-pod target, never under its minimum). Other pods on
the pool request a steady amount. From points every 4 hours in the final
week, each workload's long-memory model is trained on the 21 days before
that point, and the pool's requests and nodes for the next hour are
forecast with `scalescope.nodes`, exactly as the running service does.

Prints requested-CPU error and p90 coverage next to "same as now", and how
often the busiest-minute node count was forecast right, one too few, or
one too many. The README's "Nodes" figures come from the default run
(three seeds, about 15 minutes).

Usage: scripts/eval_node_forecast.py [--seeds 1 2 3]
"""

from __future__ import annotations

import argparse
import sys
from datetime import timedelta
from pathlib import Path

import numpy as np
import polars as pl

sys.path.insert(0, str(Path(__file__).resolve().parent))

from eval_long_memory import _MONDAY, generate

from scalescope.models.seasonal import (
    MINUTES_PER_DAY,
    MinuteSeries,
    fit_seasonal_model,
)
from scalescope.nodes import (
    WorkloadDemand,
    fit_pod_scaling,
    forecast_requests,
    nodes_needed,
)

_TOTAL_DAYS = 35
_HISTORY_DAYS = 21
_HORIZON = 60
_ANCHOR_EVERY_MINUTES = 240
_NODE_CPU = 3920.0
_OTHER_CPU = 6000.0
_PACKING = 0.85
# (traffic scale, requests/s per pod the HPA targets, min pods, CPU per pod)
_WORKLOADS = (
    (1000.0, 120.0, 3, 1000.0),
    (400.0, 60.0, 2, 500.0),
    (2500.0, 250.0, 4, 2000.0),
)


def _pool(requested: float) -> dict[str, float]:
    """A pool row sized for `requested`, as the running pool would be."""
    nodes = float(np.ceil(requested / (_NODE_CPU * _PACKING)))
    return {
        "nodes": nodes,
        "allocatable_cpu_millicores": nodes * _NODE_CPU,
        "allocatable_memory_mb": nodes * 16000.0,
        "requested_cpu_millicores": requested,
        "requested_memory_mb": 0.0,
        "daemonset_cpu_millicores": 0.0,
        "daemonset_memory_mb": 0.0,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    args = parser.parse_args()

    model_error, baseline_error, coverage = [], [], []
    node_misses: list[int] = []
    baseline_misses: list[int] = []
    for seed in args.seeds:
        demands, pods = [], []
        for i, (scale, per_pod, floor, _) in enumerate(_WORKLOADS):
            demand = generate(_TOTAL_DAYS, seed * 10 + i, base=scale)
            demands.append(demand)
            pods.append(np.maximum(floor, np.ceil(demand / per_pod)))
        requested: np.ndarray = _OTHER_CPU + np.sum(
            [p * w[3] for p, w in zip(pods, _WORKLOADS, strict=True)], axis=0
        )
        anchors = range(
            (_TOTAL_DAYS - 7) * MINUTES_PER_DAY,
            _TOTAL_DAYS * MINUTES_PER_DAY - _HORIZON,
            _ANCHOR_EVERY_MINUTES,
        )
        for anchor in anchors:
            start = anchor - _HISTORY_DAYS * MINUTES_PER_DAY
            workloads = []
            for i, w in enumerate(_WORKLOADS):
                series = MinuteSeries(
                    _MONDAY + timedelta(minutes=start), demands[i][start:anchor]
                )
                fit = fit_seasonal_model(series)
                forecast = fit.predict(series, _HORIZON) if fit else None
                week = slice(anchor - 7 * MINUTES_PER_DAY, anchor)
                scaling = fit_pod_scaling(
                    pl.DataFrame(
                        {"replicas": pods[i][week], "request_rate": demands[i][week]}
                    ),
                    "request_rate",
                )
                if forecast is None or scaling is None:
                    continue
                workloads.append(
                    WorkloadDemand(
                        workload=str(i),
                        cpu_request=w[3],
                        memory_request=0.0,
                        demand_now=float(demands[i][anchor - 5 : anchor].mean()),
                        forecast=forecast,
                        scaling=scaling,
                    )
                )
            now = float(requested[anchor - 1])
            pool = _pool(now)
            p50, p90, memory = forecast_requests(pool, workloads, _HORIZON)
            actual = requested[anchor : anchor + _HORIZON]
            model_error.append(np.abs(actual - p50).sum() / actual.sum())
            baseline_error.append(np.abs(actual - now).sum() / actual.sum())
            coverage.append(float(np.mean(actual <= p90)))
            forecast_peak = nodes_needed(p90, memory, pool, _PACKING).max()
            actual_peak = nodes_needed(actual, np.zeros(_HORIZON), pool, _PACKING).max()
            node_misses.append(int(forecast_peak - actual_peak))
            baseline_misses.append(int(pool["nodes"] - actual_peak))

    print(
        f"seeds {args.seeds}, {len(node_misses)} forecasts of the next {_HORIZON} min"
    )
    print(f"requested CPU error   {100 * np.mean(model_error):5.1f}%")
    print(f"same as now           {100 * np.mean(baseline_error):5.1f}%")
    print(f"p90 covers            {100 * np.mean(coverage):5.0f}%")
    for label, values in (("forecast", node_misses), ("same as now", baseline_misses)):
        misses = np.array(values)
        print(
            f"busiest-minute nodes, {label}: "
            f"right {100 * np.mean(misses == 0):.0f}%, "
            f"too few {100 * np.mean(misses < 0):.0f}%, "
            f"one too many {100 * np.mean(misses == 1):.0f}%, "
            f"more too many {100 * np.mean(misses > 1):.0f}%"
        )


if __name__ == "__main__":
    main()
