"""Deterministic baseline forecasters. Every ML model must beat these."""

from __future__ import annotations

import numpy as np

from scalescope.models.base import Forecast

_MIN_HISTORY = 8
_SEASONAL_PERIOD = 150  # half of the simulator's ~300-tick daily cycle


def _spread(point: np.ndarray, residual_std: float) -> Forecast:
    widen = 1 + np.arange(len(point)) * 0.05
    band = 1.2816 * residual_std * widen  # ~80% interval
    return point, np.maximum(point - band, 0), np.maximum(point + band, 0)


def _residual_std(history: np.ndarray) -> float:
    if len(history) < 2:
        return float(np.std(history)) if len(history) else 1.0
    diffs = np.diff(history)
    return float(np.std(diffs)) or 1.0


class NaiveModel:
    """Repeats the last observed value."""

    name = "naive"

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        last = float(history[-1]) if len(history) else 0.0
        point = np.full(horizon, last)
        std = _residual_std(history)
        p50, p10, p90 = _spread(point, std)
        return Forecast(self.name, p10, p50, p90)


class SeasonalNaiveModel:
    """Repeats the value from one seasonal period ago; falls back to naive."""

    name = "seasonal_naive"

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        if len(history) < _SEASONAL_PERIOD + _MIN_HISTORY:
            return NaiveModel().predict(history, horizon)
        seasonal_slice = history[-_SEASONAL_PERIOD:]
        point = np.array([seasonal_slice[i % _SEASONAL_PERIOD] for i in range(horizon)])
        std = _residual_std(history)
        p50, p10, p90 = _spread(point, std)
        return Forecast(self.name, p10, p50, p90)


class EwmaModel:
    """Exponentially weighted moving average, flat-extrapolated."""

    name = "ewma"

    def __init__(self, alpha: float = 0.3) -> None:
        self.alpha = alpha

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        if len(history) == 0:
            point = np.zeros(horizon)
        else:
            level = float(history[0])
            for x in history[1:]:
                level = self.alpha * float(x) + (1 - self.alpha) * level
            point = np.full(horizon, level)
        std = _residual_std(history)
        p50, p10, p90 = _spread(point, std)
        return Forecast(self.name, p10, p50, p90)


class LinearTrendModel:
    """Least-squares linear extrapolation over the recent history window."""

    name = "linear_trend"

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        if len(history) < _MIN_HISTORY:
            return NaiveModel().predict(history, horizon)
        window = history[-60:]
        x = np.arange(len(window))
        slope, intercept = np.polyfit(x, window, 1)
        future_x = np.arange(len(window), len(window) + horizon)
        point = slope * future_x + intercept
        std = _residual_std(history)
        p50, p10, p90 = _spread(point, std)
        return Forecast(self.name, p10, p50, p90)
