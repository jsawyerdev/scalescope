"""AutoETS wrapper via Nixtla StatsForecast, with a naive fallback on short history."""

from __future__ import annotations

import logging

import numpy as np
import pandas as pd
import polars as pl
from statsforecast import StatsForecast
from statsforecast.models import AutoETS

from scalescope.models.base import Forecast
from scalescope.models.baselines import NaiveModel
from scalescope.models.seasonality import detect_period

logger = logging.getLogger(__name__)

_MIN_HISTORY = 30


class AutoEtsModel:
    """Statistical ETS forecaster with 80% prediction intervals."""

    name = "auto_ets"

    def predict(self, history: np.ndarray, horizon: int) -> Forecast:
        if len(history) < _MIN_HISTORY:
            return NaiveModel().predict(history, horizon)

        # StatsForecast requires a "ds" index; an integer step index is sufficient
        # since our series is evenly spaced and freq is passed as an integer step.
        df = pl.DataFrame(
            {
                "unique_id": ["series"] * len(history),
                "ds": list(range(len(history))),
                "y": history,
            }
        )

        try:
            season_length = detect_period(history)
            sf = StatsForecast(
                models=[AutoETS(season_length=season_length)], freq=1, n_jobs=1
            )
            forecast_df = sf.forecast(df=df.to_pandas(), h=horizon, level=[80])
        except Exception:
            logger.exception("AutoETS failed, falling back to naive")
            return NaiveModel().predict(history, horizon)

        if not isinstance(forecast_df, pd.DataFrame):
            logger.error(
                "AutoETS returned unexpected forecast type: %s",
                type(forecast_df).__name__,
            )
            return NaiveModel().predict(history, horizon)
        point = forecast_df["AutoETS"].to_numpy()
        lo = forecast_df.get("AutoETS-lo-80", forecast_df["AutoETS"]).to_numpy()
        hi = forecast_df.get("AutoETS-hi-80", forecast_df["AutoETS"]).to_numpy()
        return Forecast(
            self.name,
            np.maximum(lo, 0),
            np.maximum(point, 0),
            np.maximum(hi, 0),
        )
