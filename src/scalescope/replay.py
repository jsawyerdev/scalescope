"""Backtests every registered model against real recorded history.

Answers "which model actually performs best on this workload's data" with
measured error, not a stated preference - the "HPA vs ML" comparison
principle from the original design doc, applied to model selection itself.
Walks backward through stored observations, forecasts forward from several
past points using only the data available at that point, and compares
against what actually happened next.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from scalescope.models.base import ForecastModel

DEFAULT_NUM_ANCHORS = 5


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
    if last_valid == min_history:
        return [last_valid]
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
    forecast against the real values that actually followed. Models with
    too little history to fit at any anchor are omitted from the result,
    not scored as failing.
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
