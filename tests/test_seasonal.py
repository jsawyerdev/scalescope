from __future__ import annotations

from datetime import timedelta

import numpy as np
import pytest
from _traffic import MINUTES_PER_DAY, MONDAY, weekly_traffic

from scalescope.models.seasonal import MinuteSeries, SeasonalFit, fit_seasonal_model

HORIZON = 60


def _series(values: np.ndarray, first_minute: int = 0) -> MinuteSeries:
    return MinuteSeries(MONDAY + timedelta(minutes=first_minute), values)


def _fit(values: np.ndarray, first_minute: int = 0) -> SeasonalFit:
    fit = fit_seasonal_model(_series(values, first_minute))
    assert fit is not None
    return fit


def _error(y: np.ndarray, days: int, anchors: list[int]) -> float:
    """Mean absolute error, as a fraction of demand, over the next HORIZON minutes."""
    errors = []
    for anchor in anchors:
        start = anchor - days * MINUTES_PER_DAY
        series = _series(y[start:anchor], start)
        forecast = _fit(y[start:anchor], start).predict(series, HORIZON)
        assert forecast is not None
        actual = y[anchor : anchor + HORIZON]
        errors.append(np.abs(actual - forecast.p50).sum() / actual.sum())
    return float(np.mean(errors))


def test_needs_a_day_of_history() -> None:
    y = weekly_traffic(2)
    assert fit_seasonal_model(_series(y[: MINUTES_PER_DAY - 1])) is None
    assert fit_seasonal_model(_series(y[:MINUTES_PER_DAY])) is not None


def test_anticipates_the_morning_ramp_that_same_as_now_misses() -> None:
    y = weekly_traffic(9)
    # Tuesday of the second week, 07:30: demand is about to triple.
    anchor = 8 * MINUTES_PER_DAY + 7 * 60 + 30
    forecast = _fit(y[:anchor]).predict(_series(y[:anchor]), HORIZON)
    assert forecast is not None
    actual = y[anchor : anchor + HORIZON]
    now = y[anchor - 5 : anchor].mean()

    model_error = np.abs(actual - forecast.p50).mean()
    same_as_now_error = np.abs(actual - now).mean()

    assert model_error < same_as_now_error / 2
    assert forecast.p90[-1] > now * 1.5


def test_more_history_forecasts_better() -> None:
    y = weekly_traffic(22, seed=3)
    # Mondays and Saturdays depend on knowing the week, not just yesterday.
    anchors = [
        21 * MINUTES_PER_DAY + 8 * 60,
        19 * MINUTES_PER_DAY + 11 * 60,
    ]
    assert _error(y, 14, anchors) < _error(y, 2, anchors)


def test_uses_no_data_after_the_forecast_origin() -> None:
    y = weekly_traffic(3)
    origin = 2 * MINUTES_PER_DAY + 600
    fit = _fit(y[:origin])
    changed = y.copy()
    changed[origin:] *= 5

    first = fit.predict(_series(y[:origin]), HORIZON)
    second = fit.predict(_series(changed[:origin]), HORIZON)

    assert first is not None and second is not None
    np.testing.assert_array_equal(first.p50, second.p50)


def test_tolerates_gaps_and_needs_recent_data() -> None:
    y = weekly_traffic(3).copy()
    y[MINUTES_PER_DAY : MINUTES_PER_DAY + 180] = np.nan  # collector down 3 hours
    fit = _fit(y)
    assert fit.predict(_series(y), HORIZON) is not None

    y[-20:] = np.nan
    assert fit.predict(_series(y), HORIZON) is None


def test_quantiles_are_ordered_and_non_negative() -> None:
    y = weekly_traffic(3)
    forecast = _fit(y).predict(_series(y), HORIZON)
    assert forecast is not None
    assert len(forecast.p50) == HORIZON
    assert (forecast.p10 >= 0).all()
    assert (forecast.p10 <= forecast.p50).all()
    assert (forecast.p50 <= forecast.p90).all()


def test_training_is_deterministic() -> None:
    y = weekly_traffic(2)
    first = _fit(y).predict(_series(y), HORIZON)
    second = _fit(y).predict(_series(y), HORIZON)
    assert first is not None and second is not None
    np.testing.assert_array_equal(first.p90, second.p90)


@pytest.mark.parametrize(("days", "weekly"), [(3, False), (9, True)])
def test_reports_whether_it_knows_the_weekly_pattern(days: int, weekly: bool) -> None:
    fit = _fit(weekly_traffic(days))
    assert fit.knows_weekly_pattern is weekly
    assert fit.history_days == pytest.approx(days)
