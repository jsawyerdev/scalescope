"""The forecast models ScaleScope serves, and the one actuation uses.

The API, the dashboard's default view, and the actuation loop all read from
here, so what the dashboard shows by default is exactly what the autoscaler
would do.
"""

from __future__ import annotations

import json
from pathlib import Path

from scalescope.config import settings
from scalescope.models.base import ForecastModel
from scalescope.models.baselines import (
    EwmaModel,
    LinearTrendModel,
    NaiveModel,
    SeasonalNaiveModel,
)
from scalescope.models.lightgbm_model import (
    LightGbmHyperparameters,
    LightGbmQuantileModel,
    validate_lightgbm_hyperparameters,
)
from scalescope.models.statsforecast_model import AutoEtsModel


def load_lightgbm_config(path: str | None) -> LightGbmHyperparameters:
    if not path:
        return {}

    config_path = Path(path)
    try:
        with config_path.open(encoding="utf-8") as f:
            raw_config = json.load(f)
    except FileNotFoundError as exc:
        raise RuntimeError(
            "SCALESCOPE_LIGHTGBM_CONFIG_PATH points to a missing file: "
            f"{config_path}"
        ) from exc
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "SCALESCOPE_LIGHTGBM_CONFIG_PATH must contain valid JSON: "
            f"{config_path}: {exc}"
        ) from exc
    except OSError as exc:
        raise RuntimeError(
            f"SCALESCOPE_LIGHTGBM_CONFIG_PATH could not be read: {config_path}: {exc}"
        ) from exc

    try:
        return validate_lightgbm_hyperparameters(
            raw_config, source="SCALESCOPE_LIGHTGBM_CONFIG_PATH"
        )
    except (TypeError, ValueError) as exc:
        raise RuntimeError(str(exc)) from exc


MODELS: dict[str, ForecastModel] = {
    "naive": NaiveModel(),
    "seasonal_naive": SeasonalNaiveModel(),
    "ewma": EwmaModel(),
    "linear_trend": LinearTrendModel(),
    "auto_ets": AutoEtsModel(),
    "lightgbm_quantile": LightGbmQuantileModel(
        **load_lightgbm_config(settings.lightgbm_config_path)
    ),
}
# Best median error and the only calibrated p90 in the replay lab (README
# "Replay lab"); actuation and the dashboard's default view both use it.
ACTUATION_MODEL = "auto_ets"
