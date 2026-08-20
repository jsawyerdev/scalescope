"""Quantile-regression forecaster via MLForecast + LightGBM."""

from __future__ import annotations

import logging

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


class LightGbmQuantileModel:
    """Lag/rolling-feature LightGBM quantile regressor, one model per quantile."""

    name = "lightgbm_quantile"

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

            quantile_forecasts: dict[str, np.ndarray] = {}
            for label, alpha in _QUANTILES.items():
                model = lgb.LGBMRegressor(
                    objective="quantile",
                    alpha=alpha,
                    n_estimators=100,
                    num_leaves=15,
                    min_child_samples=5,
                    verbosity=-1,
                )
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
