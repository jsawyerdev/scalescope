import numpy as np

from scalescope.models.baselines import EwmaModel, LinearTrendModel, NaiveModel, SeasonalNaiveModel


def test_naive_repeats_last_value():
    history = np.array([10.0, 20.0, 30.0])
    forecast = NaiveModel().predict(history, horizon=5)
    assert np.all(forecast.p50 == 30.0)
    assert np.all(forecast.p10 <= forecast.p50)
    assert np.all(forecast.p90 >= forecast.p50)


def test_linear_trend_extrapolates_upward_trend():
    history = np.arange(0.0, 60.0)
    forecast = LinearTrendModel().predict(history, horizon=5)
    assert forecast.p50[-1] > forecast.p50[0]


def test_ewma_smooths_noise():
    history = np.array([100.0, 10.0, 100.0, 10.0, 100.0, 10.0] * 5)
    forecast = EwmaModel().predict(history, horizon=1)
    assert 10.0 < forecast.p50[0] < 100.0


def test_seasonal_naive_falls_back_on_short_history():
    history = np.array([1.0, 2.0, 3.0])
    forecast = SeasonalNaiveModel().predict(history, horizon=3)
    assert forecast.model_name == "naive"
