"""Backtests every registered model against real recorded history.

Answers "which model actually performs best on this workload's data" with
measured error, not a stated preference. Picks several evenly spaced past
anchor points, forecasts forward from each using only the data available at
that point, and compares against what actually happened next.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from scalescope.models.base import ForecastModel

DEFAULT_NUM_ANCHORS = 5
# Shared by the replay API and scripts/tune so the tuner optimizes exactly
# the score the dashboard's replay lab reports.
REPLAY_MAX_OBSERVATIONS = 5000
# The baselines' minimum history: below it every model except naive and ewma
# falls back to a naive forecast, so earlier anchors would mostly score naive
# against itself.
REPLAY_MIN_HISTORY = 8


@dataclass(frozen=True)
class ReplayScore:
    """One model's measured accuracy over `n_anchors` backtest points."""

    model_name: str
    n_anchors: int
    mean_absolute_error: float
    mean_absolute_pct_error: float


def _anchors(
    history_len: int, min_history: int, horizon: int, num_anchors: int
) -> list[int]:
    last_valid = history_len - horizon
    if last_valid < min_history:
        return []
    step = max(1, (last_valid - min_history) // max(1, num_anchors - 1))
    points = list(range(min_history, last_valid + 1, step))
    return points[-num_anchors:]


def replay_score(
    history: np.ndarray,
    models: dict[str, ForecastModel],
    min_history: int,
    horizon: int,
    num_anchors: int = DEFAULT_NUM_ANCHORS,
) -> list[ReplayScore]:
    """Backtest every model in `models` over up to `num_anchors` points in `history`.

    Each anchor trains only on data strictly before it and compares the
    forecast's p50 against the real values that actually followed. Returns
    an empty list when `history` is too short for any anchor; a model whose
    forecasts are empty at every anchor is omitted rather than scored.
    """
    anchors = _anchors(len(history), min_history, horizon, num_anchors)
    if not anchors:
        return []

    results: list[ReplayScore] = []
    for name, model in models.items():
        errors: list[float] = []
        pct_errors: list[float] = []
        for anchor in anchors:
            train = history[:anchor]
            actual = history[anchor : anchor + horizon]
            forecast = model.predict(train, horizon)
            predicted = forecast.p50[: len(actual)]
            actual = actual[: len(predicted)]
            if len(actual) == 0:
                continue
            errors.append(float(np.mean(np.abs(actual - predicted))))
            pct_errors.append(
                float(
                    np.mean(np.abs((actual - predicted) / np.maximum(actual, 1.0)))
                    * 100
                )
            )
        if not errors:
            continue
        results.append(
            ReplayScore(
                model_name=name,
                n_anchors=len(errors),
                mean_absolute_error=round(float(np.mean(errors)), 2),
                mean_absolute_pct_error=round(float(np.mean(pct_errors)), 2),
            )
        )
    return results
