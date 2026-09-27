#!/usr/bin/env python3
"""Measure how the long-memory forecast improves with history length.

Generates minute-level traffic with the structure real services show (a
business-day cycle, quieter weekends, growth, slow "busier than usual"
drift, random spikes, a holiday), then forecasts from several points in
the final week using only the history available at each point, trained on
1 to 28 days of it. Prints error and p90 coverage next to a "same as now"
baseline. The README's "How it learns" table comes from the default run
(three seeds, about 20 minutes).

Usage: scripts/eval_long_memory.py [--seeds 1 2 3] [--horizon 60]
"""

from __future__ import annotations

import argparse
import math
from datetime import UTC, datetime, timedelta

import numpy as np

from scalescope.models.seasonal import (
    MINUTES_PER_DAY,
    MinuteSeries,
    fit_seasonal_model,
)

_MONDAY = datetime(2026, 1, 5, tzinfo=UTC).replace(tzinfo=None)
_TOTAL_DAYS = 35
_HISTORY_DAYS = (1, 2, 3, 7, 14, 21, 28)
_ANCHOR_EVERY_MINUTES = 240


def _bump(hour: float, centre: float, width: float, height: float) -> float:
    return height * math.exp(-0.5 * ((hour - centre) / width) ** 2)


def _daily_shape(hour: float, weekend: bool) -> float:
    if weekend:
        return 0.25 + _bump(hour, 13.5, 3.5, 0.45) + _bump(hour, 20.5, 2.0, 0.25)
    return (
        0.2
        + _bump(hour, 10.0, 1.8, 0.75)
        + _bump(hour, 14.5, 2.2, 0.7)
        + _bump(hour, 20.0, 1.5, 0.3)
    )


def generate(days: int, seed: int, base: float = 1000.0) -> np.ndarray:
    """Demand per minute from a Monday 00:00 UTC."""
    rng = np.random.default_rng(seed)
    n = days * MINUTES_PER_DAY
    holiday = int(rng.integers(7, days - 7))
    values = np.empty(n)
    drift = 0.0
    spike_left, spike_multiplier = 0, 1.0
    for t in range(n):
        day, minute = divmod(t, MINUTES_PER_DAY)
        weekend = day % 7 >= 5 or day == holiday
        drift = 0.995 * drift + rng.normal(0, 0.006)
        if spike_left == 0 and rng.random() < 1 / (3 * MINUTES_PER_DAY):
            spike_left = int(rng.integers(20, 60))
            spike_multiplier = float(rng.uniform(1.3, 1.8))
        spike = spike_multiplier if spike_left else 1.0
        spike_left = max(0, spike_left - 1)
        level = (
            base
            * _daily_shape(minute / 60, weekend)
            * (1 + 0.10 * t / n)
            * (1 + drift)
            * spike
        )
        values[t] = max(1.0, level * math.exp(rng.normal(0, 0.05)))
    return values


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--seeds", type=int, nargs="+", default=[1, 2, 3])
    parser.add_argument("--horizon", type=int, default=60)
    args = parser.parse_args()
    horizon: int = args.horizon

    model_error: dict[int, list[float]] = {days: [] for days in _HISTORY_DAYS}
    coverage: dict[int, list[float]] = {days: [] for days in _HISTORY_DAYS}
    baseline_error: list[float] = []
    for seed in args.seeds:
        demand = generate(_TOTAL_DAYS, seed)
        anchors = range(
            (_TOTAL_DAYS - 7) * MINUTES_PER_DAY,
            _TOTAL_DAYS * MINUTES_PER_DAY - horizon,
            _ANCHOR_EVERY_MINUTES,
        )
        for anchor in anchors:
            actual = demand[anchor : anchor + horizon]
            now = demand[anchor - 5 : anchor].mean()
            baseline_error.append(np.abs(actual - now).sum() / actual.sum())
            for days in _HISTORY_DAYS:
                start = anchor - days * MINUTES_PER_DAY
                series = MinuteSeries(
                    _MONDAY + timedelta(minutes=start), demand[start:anchor]
                )
                fit = fit_seasonal_model(series)
                forecast = fit.predict(series, horizon) if fit else None
                if forecast is None:
                    continue
                model_error[days].append(
                    np.abs(actual - forecast.p50).sum() / actual.sum()
                )
                coverage[days].append(float(np.mean(actual <= forecast.p90)))

    print(f"seeds {args.seeds}, forecasting the next {horizon} minutes")
    print("history      " + "".join(f"{days:>7}d" for days in _HISTORY_DAYS))
    print(
        "model error  "
        + "".join(f"{100 * np.mean(model_error[d]):7.1f}%" for d in _HISTORY_DAYS)
    )
    print(f"same as now  {100 * np.mean(baseline_error):7.1f}% (every history)")
    print(
        "p90 covers   "
        + "".join(f"{100 * np.mean(coverage[d]):7.0f}%" for d in _HISTORY_DAYS)
    )


if __name__ == "__main__":
    main()
