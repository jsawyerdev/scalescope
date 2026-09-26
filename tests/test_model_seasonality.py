from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from scalescope.models import lightgbm_model, statsforecast_model


def test_auto_ets_uses_detected_period(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = _install_auto_ets_fakes(monkeypatch)
    monkeypatch.setattr(statsforecast_model, "detect_period", lambda history: 300)

    statsforecast_model.AutoEtsModel().predict(np.arange(120, dtype=float), horizon=3)

    assert captured == [300]


def test_auto_ets_keeps_season_length_one_without_detected_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_auto_ets_fakes(monkeypatch)
    monkeypatch.setattr(statsforecast_model, "detect_period", lambda history: 1)

    statsforecast_model.AutoEtsModel().predict(np.arange(120, dtype=float), horizon=3)

    assert captured == [1]


def test_auto_ets_falls_back_when_forecast_type_is_unexpected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAutoETS:
        def __init__(self, season_length: int) -> None:
            pass

    class FakeStatsForecast:
        def __init__(self, **kwargs: object) -> None:
            pass

        def forecast(self, df: pd.DataFrame, h: int, level: list[int]) -> object:
            return object()

    monkeypatch.setattr(statsforecast_model, "AutoETS", FakeAutoETS)
    monkeypatch.setattr(statsforecast_model, "StatsForecast", FakeStatsForecast)

    forecast = statsforecast_model.AutoEtsModel().predict(
        np.arange(120, dtype=float), horizon=3
    )

    assert forecast.model_name == "naive"


def test_lightgbm_appends_detected_period_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_lags, _ = _install_lightgbm_fakes(monkeypatch)
    monkeypatch.setattr(lightgbm_model, "detect_period", lambda history: 50)

    lightgbm_model.LightGbmQuantileModel().predict(
        np.arange(120, dtype=float), horizon=3
    )

    assert captured_lags == [[1, 2, 3, 5, 10, 50]] * 3


def test_lightgbm_keeps_base_lags_without_detected_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_lags, _ = _install_lightgbm_fakes(monkeypatch)
    monkeypatch.setattr(lightgbm_model, "detect_period", lambda history: 1)

    lightgbm_model.LightGbmQuantileModel().predict(
        np.arange(120, dtype=float), horizon=3
    )

    assert captured_lags == [[1, 2, 3, 5, 10]] * 3


def test_lightgbm_default_regressor_kwargs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, captured_kwargs = _install_lightgbm_fakes(monkeypatch)
    monkeypatch.setattr(lightgbm_model, "detect_period", lambda history: 1)

    lightgbm_model.LightGbmQuantileModel().predict(
        np.arange(120, dtype=float), horizon=3
    )

    assert captured_kwargs == [
        {
            "objective": "quantile",
            "alpha": 0.10,
            "n_estimators": 100,
            "num_leaves": 15,
            "min_child_samples": 5,
            "verbosity": -1,
        },
        {
            "objective": "quantile",
            "alpha": 0.50,
            "n_estimators": 100,
            "num_leaves": 15,
            "min_child_samples": 5,
            "verbosity": -1,
        },
        {
            "objective": "quantile",
            "alpha": 0.90,
            "n_estimators": 100,
            "num_leaves": 15,
            "min_child_samples": 5,
            "verbosity": -1,
        },
    ]


def test_lightgbm_forwards_tuned_hyperparameters(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, captured_kwargs = _install_lightgbm_fakes(monkeypatch)
    monkeypatch.setattr(lightgbm_model, "detect_period", lambda history: 1)

    lightgbm_model.LightGbmQuantileModel(
        n_estimators=180, num_leaves=31, min_child_samples=8, learning_rate=0.08
    ).predict(np.arange(120, dtype=float), horizon=3)

    assert [kwargs["alpha"] for kwargs in captured_kwargs] == [0.10, 0.50, 0.90]
    for kwargs in captured_kwargs:
        assert kwargs["n_estimators"] == 180
        assert kwargs["num_leaves"] == 31
        assert kwargs["min_child_samples"] == 8
        assert kwargs["learning_rate"] == 0.08


def _install_auto_ets_fakes(monkeypatch: pytest.MonkeyPatch) -> list[int]:
    captured: list[int] = []

    class FakeAutoETS:
        def __init__(self, season_length: int) -> None:
            captured.append(season_length)

    class FakeStatsForecast:
        def __init__(self, **kwargs: object) -> None:
            pass

        def forecast(self, df: pd.DataFrame, h: int, level: list[int]) -> pd.DataFrame:
            return pd.DataFrame(
                {
                    "AutoETS": np.full(h, 100.0),
                    "AutoETS-lo-80": np.full(h, 90.0),
                    "AutoETS-hi-80": np.full(h, 110.0),
                }
            )

    monkeypatch.setattr(statsforecast_model, "AutoETS", FakeAutoETS)
    monkeypatch.setattr(statsforecast_model, "StatsForecast", FakeStatsForecast)
    return captured


def _install_lightgbm_fakes(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[list[int]], list[dict[str, object]]]:
    captured_lags: list[list[int]] = []
    captured_kwargs: list[dict[str, object]] = []

    class FakeLGBMRegressor:
        def __init__(self, **kwargs: object) -> None:
            captured_kwargs.append(dict(kwargs))

    class FakeMLForecast:
        def __init__(
            self,
            models: dict[str, FakeLGBMRegressor],
            freq: int,
            lags: list[int],
            lag_transforms: dict[int, list[object]],
        ) -> None:
            self._label = next(iter(models))
            captured_lags.append(list(lags))

        def fit(self, df: pd.DataFrame) -> None:
            pass

        def predict(self, horizon: int) -> pd.DataFrame:
            return pd.DataFrame({self._label: np.full(horizon, 100.0)})

    monkeypatch.setattr(lightgbm_model.lgb, "LGBMRegressor", FakeLGBMRegressor)
    monkeypatch.setattr(lightgbm_model, "MLForecast", FakeMLForecast)
    return captured_lags, captured_kwargs
