"""Quantile-regression forecaster via MLForecast + LightGBM."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from typing import Any, TypedDict

import lightgbm as lgb
import numpy as np
import polars as pl
from mlforecast import MLForecast
from mlforecast.lag_transforms import RollingMean, RollingStd

from scalescope.models.base import Forecast
from scalescope.models.baselines import NaiveModel
from scalescope.models.seasonality import detect_period

logger = logging.getLogger(__name__)

_MIN_HISTORY = 60
_QUANTILES = {"p10": 0.10, "p50": 0.50, "p90": 0.90}
_BASE_LAGS = [1, 2, 3, 5, 10]
_LIGHTGBM_INT_CONFIG_KEYS = frozenset(
    {"n_estimators", "num_leaves", "min_child_samples"}
)
_LIGHTGBM_CONFIG_KEYS = _LIGHTGBM_INT_CONFIG_KEYS | {"learning_rate"}


class LightGbmHyperparameters(TypedDict, total=False):
    """Constructor parameters operators may tune offline."""

    n_estimators: int
    num_leaves: int
    min_child_samples: int
    learning_rate: float


def _positive_int_config(raw_config: Mapping[object, object], key: str) -> int:
    value = raw_config[key]
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"value for {key!r} must be a positive integer")
    return value


def validate_lightgbm_hyperparameters(
    raw_config: object, source: str
) -> LightGbmHyperparameters:
    """Validate operator-supplied LightGBM hyperparameters."""
    if not isinstance(raw_config, Mapping):
        raise TypeError(
            f"{source} must contain a JSON object of LightGBM hyperparameters"
        )

    unknown_keys = sorted(set(raw_config) - _LIGHTGBM_CONFIG_KEYS)
    if unknown_keys:
        raise ValueError(
            f"{source} contains unsupported LightGBM hyperparameter(s): {unknown_keys}"
        )

    config: LightGbmHyperparameters = {}
    if "n_estimators" in raw_config:
        config["n_estimators"] = _positive_int_config(raw_config, "n_estimators")
    if "num_leaves" in raw_config:
        config["num_leaves"] = _positive_int_config(raw_config, "num_leaves")
    if "min_child_samples" in raw_config:
        config["min_child_samples"] = _positive_int_config(
            raw_config, "min_child_samples"
        )
    if "learning_rate" in raw_config:
        value = raw_config["learning_rate"]
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(float(value))
            or value <= 0
        ):
            raise ValueError(
                "value for 'learning_rate' must be a positive finite number"
            )
        config["learning_rate"] = float(value)
    return config


class LightGbmQuantileModel:
    """Lag/rolling-feature LightGBM quantile regressor, one model per quantile."""

    name = "lightgbm_quantile"

    def __init__(
        self,
        n_estimators: int = 100,
        num_leaves: int = 15,
        min_child_samples: int = 5,
        learning_rate: float | None = None,
    ) -> None:
        self.n_estimators = n_estimators
        self.num_leaves = num_leaves
        self.min_child_samples = min_child_samples
        self.learning_rate = learning_rate

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        if len(history) < _MIN_HISTORY:
            return NaiveModel().predict(history, horizon)

        df = pl.DataFrame(
            {
                "unique_id": ["series"] * len(history),
                "ds": list(range(len(history))),
                "y": history,
            }
        ).to_pandas()

        try:
            season_length = detect_period(history)
            lags = list(_BASE_LAGS)
            if (
                season_length > 1
                and season_length < len(history)
                and season_length not in lags
            ):
                lags.append(season_length)

            regressor_params: dict[str, Any] = {
                "objective": "quantile",
                "n_estimators": self.n_estimators,
                "num_leaves": self.num_leaves,
                "min_child_samples": self.min_child_samples,
                "verbosity": -1,
            }
            # Only passed when tuned, so the default tracks LightGBM's own.
            if self.learning_rate is not None:
                regressor_params["learning_rate"] = self.learning_rate

            quantile_forecasts: dict[str, np.ndarray] = {}
            for label, alpha in _QUANTILES.items():
                model = lgb.LGBMRegressor(alpha=alpha, **regressor_params)
                mlf = MLForecast(
                    models={label: model},
                    freq=1,
                    lags=lags,
                    lag_transforms={
                        1: [RollingMean(window_size=5), RollingStd(window_size=5)],
                    },
                )
                mlf.fit(df)
                fcst = mlf.predict(horizon)
                quantile_forecasts[label] = fcst[label].to_numpy()
        except Exception:
            logger.exception("LightGBM quantile forecast failed, falling back to naive")
            return NaiveModel().predict(history, horizon)

        p10, p50, p90 = (
            quantile_forecasts["p10"],
            quantile_forecasts["p50"],
            quantile_forecasts["p90"],
        )
        # Quantile models are trained independently and can cross; enforce order.
        stacked = np.sort(np.vstack([p10, p50, p90]), axis=0)
        return Forecast(
            self.name,
            np.maximum(stacked[0], 0),
            np.maximum(stacked[1], 0),
            np.maximum(stacked[2], 0),
        )
