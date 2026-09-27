"""Keeps every workload's long-memory model trained, and scores it live.

The collector's observations are rolled up into one row per minute and kept
for weeks (`Store.roll_up_minutes`). Every `retrain_minutes`, each workload's
seasonal model (`scalescope.models.seasonal`) is refitted on that history,
its latency curve is refitted on the same weeks of data, and its forecast
for the next `horizon_minutes` is logged. Once those minutes have passed,
the logged forecasts are scored against what actually happened, next to a
"no change" baseline: accuracy measured live, never on data the model was
trained with.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta

import numpy as np
import polars as pl

from scalescope.capacity import PodCapacity, latency_capacity
from scalescope.demand import DemandSignal
from scalescope.models.base import Forecast
from scalescope.models.seasonal import (
    MAX_TRAINING_MINUTES,
    MinuteSeries,
    SeasonalFit,
    fit_seasonal_model,
)
from scalescope.storage import Store

logger = logging.getLogger(__name__)

_MINUTE_COLUMN: dict[DemandSignal, str] = {
    "request_rate": "request_rate",
    "cpu_millicores": "cpu_usage_millicores",
}
# "No change" baseline: the mean of the last few minutes, held flat.
_BASELINE_MINUTES = 5
# A series whose newest minute is older than this is not the present.
_MAX_STALENESS_MINUTES = 10
ACCURACY_DAYS = 7


@dataclass(frozen=True)
class TrainedModel:
    signal: DemandSignal
    fit: SeasonalFit
    trained_at: datetime
    capacity: PodCapacity | None


@dataclass(frozen=True)
class LearningStatus:
    """How much a workload's long-memory model has learned, and how well."""

    history_minutes: int
    trained_at: datetime | None
    knows_daily_pattern: bool
    knows_weekly_pattern: bool
    scored_minutes: int
    model_error: float | None
    baseline_error: float | None
    p90_coverage: float | None
    # (UTC date, model error) per day, oldest first.
    daily_model_error: list[tuple[str, float]]


def _minute(ts: datetime) -> datetime:
    return ts.replace(second=0, microsecond=0, tzinfo=None)


class Learner:
    """Trains, serves, and scores each workload's long-memory model.

    Thread-safe: training runs in a worker thread while API handlers read.
    """

    def __init__(
        self, store: Store, horizon_minutes: int, latency_slo_ms: float | None = None
    ) -> None:
        self._store = store
        self._horizon = horizon_minutes
        self._latency_slo_ms = latency_slo_ms
        self._lock = threading.Lock()
        # Predictions take milliseconds; serializing them avoids depending on
        # concurrent-predict guarantees of the model library.
        self._predict_lock = threading.Lock()
        self._models: dict[str, TrainedModel] = {}
        # One cached forecast per workload, for the current minute.
        self._forecasts: dict[str, tuple[DemandSignal, datetime, Forecast | None]] = {}

    def minute_history(self, workload: str, now: datetime) -> pl.DataFrame:
        since = _minute(now) - timedelta(minutes=MAX_TRAINING_MINUTES)
        return self._store.minute_history(workload, since)

    def minute_series(
        self, history: pl.DataFrame, signal: DemandSignal
    ) -> MinuteSeries | None:
        """`history` on a regular one-minute grid, NaN where minutes are missing."""
        if history.is_empty():
            return None
        start: datetime = history["minute"][0]
        offsets = (
            ((history["minute"] - start).dt.total_seconds() // 60).cast(int).to_numpy()
        )
        values = np.full(int(offsets[-1]) + 1, np.nan)
        values[offsets] = history[_MINUTE_COLUMN[signal]].to_numpy()
        return MinuteSeries(start, values)

    def retrain(
        self, workload: str, signal: DemandSignal, now: datetime
    ) -> TrainedModel | None:
        """Refit `workload`'s model on its minute history and log a forecast."""
        history = self.minute_history(workload, now)
        series = self.minute_series(history, signal)
        fit = fit_seasonal_model(series) if series is not None else None
        if series is None or fit is None:
            return None
        capacity = (
            latency_capacity(history, self._latency_slo_ms)
            if signal == "request_rate"
            else None
        )
        trained = TrainedModel(signal, fit, _minute(now), capacity)
        with self._lock:
            self._models[workload] = trained
            self._forecasts.pop(workload, None)
        forecast = self._predict(trained, series, now)
        if forecast is not None:
            self._log(workload, signal, series, forecast, now)
        return trained

    def forecast(
        self, workload: str, signal: DemandSignal, now: datetime
    ) -> Forecast | None:
        """Per-minute forecast starting at the current minute, or None.

        None until a model is trained for this workload and signal, or when
        its minute history is stale (collection stopped).
        """
        minute = _minute(now)
        with self._lock:
            trained = self._models.get(workload)
            cached = self._forecasts.get(workload)
        if cached is not None and cached[:2] == (signal, minute):
            return cached[2]
        if trained is None or trained.signal != signal:
            return None
        series = self.minute_series(self.minute_history(workload, now), signal)
        forecast = self._predict(trained, series, now) if series else None
        with self._lock:
            self._forecasts[workload] = (signal, minute, forecast)
        return forecast

    def learned_capacity(
        self, workload: str, signal: DemandSignal
    ) -> PodCapacity | None:
        """The latency-curve capacity fitted on weeks of minute data, if any."""
        with self._lock:
            trained = self._models.get(workload)
        if trained is None or trained.signal != signal:
            return None
        return trained.capacity

    def status(self, workload: str, now: datetime) -> LearningStatus:
        history = self.minute_history(workload, now)
        with self._lock:
            trained = self._models.get(workload)
        accuracy = self._store.forecast_accuracy(
            workload, _minute(now) - timedelta(days=ACCURACY_DAYS)
        ).drop_nulls()
        scored = int(accuracy["minutes"].sum()) if not accuracy.is_empty() else 0

        def weighted(column: str) -> float | None:
            if not scored:
                return None
            return float((accuracy[column] * accuracy["minutes"]).sum() / scored)

        return LearningStatus(
            history_minutes=history.height,
            trained_at=trained.trained_at if trained else None,
            knows_daily_pattern=trained is not None,
            knows_weekly_pattern=trained is not None
            and trained.fit.knows_weekly_pattern,
            scored_minutes=scored,
            model_error=weighted("model_error"),
            baseline_error=weighted("baseline_error"),
            p90_coverage=weighted("p90_coverage"),
            daily_model_error=[
                (str(day), float(error))
                for day, error in zip(
                    accuracy["day"], accuracy["model_error"], strict=True
                )
            ],
        )

    def _predict(
        self, trained: TrainedModel, series: MinuteSeries, now: datetime
    ) -> Forecast | None:
        # Minutes between the newest rolled-up minute and now have no data
        # yet; forecast through them and return from the current minute on.
        gap = int((_minute(now) - series.end).total_seconds() // 60)
        if not 0 <= gap <= _MAX_STALENESS_MINUTES:
            return None
        with self._predict_lock:
            forecast = trained.fit.predict(series, gap + self._horizon)
        if forecast is None:
            return None
        return Forecast(
            forecast.model_name,
            forecast.p10[gap:],
            forecast.p50[gap:],
            forecast.p90[gap:],
        )

    def _log(
        self,
        workload: str,
        signal: DemandSignal,
        series: MinuteSeries,
        forecast: Forecast,
        now: datetime,
    ) -> None:
        recent = series.values[-_BASELINE_MINUTES:]
        if not np.isfinite(recent).any():
            return
        first = _minute(now)
        self._store.log_forecast(
            workload,
            made_at=first,
            signal=signal,
            minutes=[first + timedelta(minutes=i) for i in range(len(forecast.p50))],
            p50=[float(v) for v in forecast.p50],
            p90=[float(v) for v in forecast.p90],
            baseline=float(np.nanmean(recent)),
        )
