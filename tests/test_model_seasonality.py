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


def test_lightgbm_appends_detected_period_lag(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_lightgbm_fakes(monkeypatch)
    monkeypatch.setattr(lightgbm_model, "detect_period", lambda history: 50)

    lightgbm_model.LightGbmQuantileModel().predict(
        np.arange(120, dtype=float), horizon=3
    )

    assert captured == [[1, 2, 3, 5, 10, 50]] * 3


def test_lightgbm_keeps_base_lags_without_detected_period(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = _install_lightgbm_fakes(monkeypatch)
    monkeypatch.setattr(lightgbm_model, "detect_period", lambda history: 1)

    lightgbm_model.LightGbmQuantileModel().predict(
        np.arange(120, dtype=float), horizon=3
    )

    assert captured == [[1, 2, 3, 5, 10]] * 3


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


def _install_lightgbm_fakes(monkeypatch: pytest.MonkeyPatch) -> list[list[int]]:
    captured: list[list[int]] = []

    class FakeLGBMRegressor:
        def __init__(self, **kwargs: object) -> None:
            pass

    class FakeMLForecast:
        def __init__(
            self,
            models: dict[str, FakeLGBMRegressor],
            freq: int,
            lags: list[int],
            lag_transforms: dict[int, list[object]],
        ) -> None:
            self._label = next(iter(models))
            captured.append(list(lags))

        def fit(self, df: pd.DataFrame) -> None:
            pass

        def predict(self, horizon: int) -> pd.DataFrame:
            return pd.DataFrame({self._label: np.full(horizon, 100.0)})

    monkeypatch.setattr(lightgbm_model.lgb, "LGBMRegressor", FakeLGBMRegressor)
    monkeypatch.setattr(lightgbm_model, "MLForecast", FakeMLForecast)
    return captured
