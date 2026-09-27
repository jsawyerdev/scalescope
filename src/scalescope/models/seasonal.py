"""Long-memory demand model: learns a workload's daily and weekly pattern.

The short-term models forecast the next minute from the last few minutes.
This one learns from weeks of minute-level history what demand usually does
at each time of day and day of week, so its forecasts improve the longer a
workload is monitored: after a day it knows the daily cycle, after a week
the weekly one, and more weeks refine both and calibrate its uncertainty.

It is a direct multi-horizon LightGBM quantile regressor. For a forecast
made at minute `o` for minute `o + h - 1`, the features are the horizon,
the time of day and day of week, what demand was at that time yesterday,
two days ago, last week and two weeks ago, and how today compares with
yesterday and last week so far. Everything is relative to the current
level (the mean of the last 15 minutes), so the model learns shape and
ratios, not absolute traffic, and follows growth without retraining.
Timestamps are UTC; a daylight-saving change shifts the learned pattern by
an hour until a week of history on the new time accumulates.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

import lightgbm as lgb
import numpy as np

from scalescope.models.base import Forecast

MINUTES_PER_DAY = 1440
MINUTES_PER_WEEK = 7 * MINUTES_PER_DAY
MIN_HISTORY_MINUTES = MINUTES_PER_DAY
# Older history adds little once four weekly cycles are known, and bounds
# training time.
MAX_TRAINING_MINUTES = 28 * MINUTES_PER_DAY

_LEVEL_MINUTES = 15
_CONTEXT_MINUTES = 60
_LAG_HALF_WIDTH = 2  # lags are 5-minute means, centred on the same minute
_ORIGIN_STRIDE = 5
_TRAINING_HORIZONS = (1, 2, 3, 5, 8, 12, 16, 20, 25, 30, 40, 50, 60, 90, 120)
_QUANTILES = (0.1, 0.5, 0.9)
_LAGS = (
    (MINUTES_PER_DAY, 1),
    (MINUTES_PER_DAY, 2),
    (MINUTES_PER_WEEK, 1),
    (MINUTES_PER_WEEK, 2),
)
_MODEL_NAME = "seasonal_lgbm"


@dataclass(frozen=True)
class MinuteSeries:
    """Demand per minute from `start` (naive UTC, minute-aligned); NaN marks gaps."""

    start: datetime
    values: np.ndarray

    @property
    def end(self) -> datetime:
        """The first minute after the series."""
        return self.start + timedelta(minutes=len(self.values))


@dataclass(frozen=True)
class SeasonalFit:
    """A trained model and what it was trained on."""

    models: tuple[lgb.LGBMRegressor, ...]
    history_minutes: int
    trained_through: datetime

    @property
    def history_days(self) -> float:
        return self.history_minutes / MINUTES_PER_DAY

    @property
    def knows_weekly_pattern(self) -> bool:
        return self.history_minutes >= MINUTES_PER_WEEK + _CONTEXT_MINUTES

    def predict(self, series: MinuteSeries, horizon_minutes: int) -> Forecast | None:
        """p10/p50/p90 demand for each of the next `horizon_minutes` minutes.

        Uses `series` up to its end as the present, so a fit made minutes
        ago forecasts from the latest data. None when the last
        `_LEVEL_MINUTES` minutes have no data.
        """
        y = np.asarray(series.values, dtype=float)
        origin = np.array([len(y)])
        horizons = np.arange(1, horizon_minutes + 1)
        x, level = _features(y, _minute_of_week(series.start), origin, horizons)
        if not np.isfinite(level).all() or (level <= 0).any():
            return None
        quantiles = np.vstack([m.predict(x) * level for m in self.models])
        # Independently fitted quantiles can cross; demand is never negative.
        p10, p50, p90 = np.maximum(np.sort(quantiles, axis=0), 0)
        return Forecast(_MODEL_NAME, p10, p50, p90)


def fit_seasonal_model(series: MinuteSeries) -> SeasonalFit | None:
    """Train on up to the last `MAX_TRAINING_MINUTES`, or None under a day of data."""
    values = np.asarray(series.values, dtype=float)
    start = series.start
    if len(values) > MAX_TRAINING_MINUTES:
        start += timedelta(minutes=len(values) - MAX_TRAINING_MINUTES)
        values = values[-MAX_TRAINING_MINUTES:]
    observed = int(np.isfinite(values).sum())
    if observed < MIN_HISTORY_MINUTES:
        return None

    max_horizon = max(_TRAINING_HORIZONS)
    origins = np.arange(_CONTEXT_MINUTES, len(values) - 1, _ORIGIN_STRIDE)
    horizons = np.array(_TRAINING_HORIZONS)
    x, level = _features(values, _minute_of_week(start), origins, horizons)
    targets = _target_indexes(origins, horizons)
    in_range = targets < len(values)
    y = np.full(len(targets), np.nan)
    y[in_range] = values[targets[in_range]] / level[in_range]
    usable = np.isfinite(y) & np.isfinite(level) & (level > 0)
    if usable.sum() < max_horizon:
        return None

    models = tuple(
        lgb.LGBMRegressor(
            objective="quantile",
            alpha=alpha,
            n_estimators=150,
            learning_rate=0.05,
            num_leaves=31,
            min_child_samples=20,
            deterministic=True,
            n_jobs=1,
            random_state=0,
            verbosity=-1,
        ).fit(x[usable], y[usable])
        for alpha in _QUANTILES
    )
    return SeasonalFit(
        models=models,
        history_minutes=observed,
        trained_through=series.end,
    )


def _minute_of_week(start: datetime) -> int:
    return start.weekday() * MINUTES_PER_DAY + start.hour * 60 + start.minute


def _target_indexes(origins: np.ndarray, horizons: np.ndarray) -> np.ndarray:
    """Minute index each (origin, horizon) row forecasts, origin-major."""
    indexes: np.ndarray = (origins[:, None] + horizons[None, :] - 1).ravel()
    return indexes


def _window_means(values: np.ndarray, width: int) -> np.ndarray:
    """means[i] = nanmean(values[i - width:i]); NaN where the window is empty."""
    finite = np.isfinite(values)
    sums = np.concatenate([[0.0], np.cumsum(np.where(finite, values, 0.0))])
    counts = np.concatenate([[0], np.cumsum(finite)])
    idx = np.arange(len(values) + 1)
    lo = np.maximum(idx - width, 0)
    n = counts[idx] - counts[lo]
    with np.errstate(invalid="ignore", divide="ignore"):
        means: np.ndarray = np.where(
            n > 0, (sums[idx] - sums[lo]) / np.maximum(n, 1), np.nan
        )
    return means


def _lookup(table: np.ndarray, index: np.ndarray, before: np.ndarray) -> np.ndarray:
    """table[index] where index is in range and ends before `before`, else NaN."""
    ok = (index >= 0) & (index < len(table)) & (index < before)
    out = np.full(index.shape, np.nan)
    out[ok] = table[index[ok]]
    return out


def _features(
    values: np.ndarray,
    week_offset: int,
    origins: np.ndarray,
    horizons: np.ndarray,
) -> tuple[np.ndarray, np.ndarray]:
    """Feature rows for every (origin, horizon) pair, and each row's level.

    Only data before each origin is used: lags whose window reaches the
    origin are NaN (LightGBM treats NaN as missing).
    """
    level_means = _window_means(values, _LEVEL_MINUTES)
    context_means = _window_means(values, _CONTEXT_MINUTES)
    # Mean of the 5 minutes centred on i sits at index i + half + 1 of the
    # trailing window table.
    lag_means = _window_means(values, 2 * _LAG_HALF_WIDTH + 1)

    level = np.repeat(level_means[origins], len(horizons))
    context = np.repeat(context_means[origins], len(horizons))
    origin = np.repeat(origins, len(horizons))
    horizon = np.tile(horizons, len(origins)).astype(float)
    target = _target_indexes(origins, horizons)
    minute_of_week = (week_offset + target) % MINUTES_PER_WEEK

    columns = [
        horizon,
        (minute_of_week % MINUTES_PER_DAY).astype(float),
        (minute_of_week // MINUTES_PER_DAY).astype(float),
    ]
    with np.errstate(invalid="ignore", divide="ignore"):
        for period, k in _LAGS:
            centre = target - k * period
            window_end = centre + _LAG_HALF_WIDTH + 1
            columns.append(_lookup(lag_means, window_end, origin + 1) / level)
        for period in (MINUTES_PER_DAY, MINUTES_PER_WEEK):
            then = _lookup(context_means, origin - period, origin + 1)
            columns.append(context / then)
    return np.column_stack(columns), level
