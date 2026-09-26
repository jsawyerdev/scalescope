import numpy as np

from scalescope.models.baselines import (
    EwmaModel,
    LinearTrendModel,
    NaiveModel,
    SeasonalNaiveModel,
)


def test_naive_repeats_last_value() -> None:
    history = np.array([10.0, 20.0, 30.0])
    forecast = NaiveModel().predict(history, horizon=5)
    assert np.all(forecast.p50 == 30.0)
    assert np.all(forecast.p10 <= forecast.p50)
    assert np.all(forecast.p90 >= forecast.p50)


def test_linear_trend_extrapolates_upward_trend() -> None:
    history = np.arange(0.0, 60.0)
    forecast = LinearTrendModel().predict(history, horizon=5)
    assert forecast.p50[-1] > forecast.p50[0]


def test_ewma_smooths_noise() -> None:
    history = np.array([100.0, 10.0, 100.0, 10.0, 100.0, 10.0] * 5)
    forecast = EwmaModel().predict(history, horizon=1)
    assert 10.0 < forecast.p50[0] < 100.0


def test_seasonal_naive_falls_back_on_short_history() -> None:
    history = np.array([1.0, 2.0, 3.0])
    forecast = SeasonalNaiveModel().predict(history, horizon=3)
    assert forecast.model_name == "naive"


def test_seasonal_naive_repeats_detected_cycle_in_phase() -> None:
    period = 300
    t = np.arange(period * 3)
    series = 700.0 + 400.0 * np.sin(2 * np.pi * t / period)
    history, future = series[: period * 2], series[period * 2 : period * 2 + 30]

    forecast = SeasonalNaiveModel().predict(history, horizon=30)

    assert forecast.model_name == "seasonal_naive"
    assert np.allclose(forecast.p50, future)


def test_seasonal_naive_falls_back_without_detected_period() -> None:
    history = np.full(400, 250.0)
    forecast = SeasonalNaiveModel().predict(history, horizon=3)
    assert forecast.model_name == "naive"


def test_single_observation_still_gets_a_nonzero_band() -> None:
    forecast = NaiveModel().predict(np.array([500.0]), horizon=3)
    assert np.all(forecast.p90 > forecast.p50)
    assert np.all(forecast.p10 < forecast.p50)
