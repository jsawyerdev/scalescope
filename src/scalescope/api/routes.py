"""REST API for workload state, forecasts, and diagnosis."""

from __future__ import annotations

import threading
from importlib.metadata import version as _package_version
from typing import Any

import polars as pl
from fastapi import APIRouter, HTTPException, Query

from scalescope.capacity import STARTUP_LEAD_STEPS, recommend_replicas
from scalescope.config import settings
from scalescope.diagnosis import DiagnosisResult, diagnose
from scalescope.models.base import Forecast, ForecastModel
from scalescope.models.baselines import (
    EwmaModel,
    LinearTrendModel,
    NaiveModel,
    SeasonalNaiveModel,
)
from scalescope.models.lightgbm_model import LightGbmQuantileModel
from scalescope.models.statsforecast_model import AutoEtsModel
from scalescope.storage import Store

router = APIRouter(prefix="/api")

_MODELS: dict[str, ForecastModel] = {
    "naive": NaiveModel(),
    "seasonal_naive": SeasonalNaiveModel(),
    "ewma": EwmaModel(),
    "linear_trend": LinearTrendModel(),
    "auto_ets": AutoEtsModel(),
    "lightgbm_quantile": LightGbmQuantileModel(),
}
_DEFAULT_MODEL = "auto_ets"
_HORIZON_STEPS = settings.forecast_horizon_steps
_HISTORY_STEPS = settings.history_window_steps
_MAX_OBSERVATIONS_LIMIT = 5000
_VERSION = _package_version("scalescope")

# Keyed by (workload, model) -> (latest observation ts, Forecast). A fixed-size
# history window keeps len(history) constant once it fills, so the cache must
# key on the newest timestamp rather than row count to invalidate correctly.
_forecast_cache_lock = threading.Lock()
_forecast_cache: dict[tuple[str, str], tuple[Any, Forecast]] = {}


def get_store() -> Store:
    # Deferred import: main.py imports this router at module load time, and
    # app_state is only populated once the FastAPI lifespan starts.
    from scalescope.main import app_state

    store: Store = app_state["store"]
    return store


@router.get("/source")
def get_source() -> dict[str, Any]:
    """What ScaleScope is actually observing right now: mode and cluster identity."""
    from scalescope.main import app_state

    source: dict[str, Any] = dict(app_state["source"])
    source["version"] = _VERSION
    return source


def _require_known_workload(store: Store, workload: str) -> None:
    if workload not in store.workloads():
        raise HTTPException(status_code=404, detail=f"unknown workload: {workload}")


def _get_forecast(workload: str, model: str, df: pl.DataFrame) -> Forecast:
    history = df["request_rate"].to_numpy()
    latest_ts = df["ts"][-1]
    cache_key = (workload, model)
    with _forecast_cache_lock:
        cached = _forecast_cache.get(cache_key)
        if cached is not None and cached[0] == latest_ts:
            return cached[1]
    forecast = _MODELS[model].predict(history, _HORIZON_STEPS)
    with _forecast_cache_lock:
        _forecast_cache[cache_key] = (latest_ts, forecast)
    return forecast


@router.get("/workloads")
def list_workloads() -> list[str]:
    return get_store().workloads()


@router.get("/workloads/{workload}/observations")
def get_observations(
    workload: str, limit: int = Query(default=300, ge=1, le=_MAX_OBSERVATIONS_LIMIT)
) -> list[dict[str, Any]]:
    store = get_store()
    _require_known_workload(store, workload)
    return store.recent_observations(workload, limit).to_dicts()


@router.get("/workloads/{workload}/forecast")
def get_forecast(workload: str, model: str = _DEFAULT_MODEL) -> dict[str, Any]:
    if model not in _MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    store = get_store()
    _require_known_workload(store, workload)
    df = store.recent_observations(workload, _HISTORY_STEPS)
    if df.is_empty():
        raise HTTPException(
            status_code=409, detail=f"no observations yet for workload: {workload}"
        )

    forecast = _get_forecast(workload, model, df)
    return {
        "workload": workload,
        "model": forecast.model_name,
        "horizon_steps": _HORIZON_STEPS,
        "p10": forecast.p10.tolist(),
        "p50": forecast.p50.tolist(),
        "p90": forecast.p90.tolist(),
    }


@router.get("/workloads/{workload}/diagnosis")
def get_diagnosis(workload: str) -> dict[str, Any]:
    store = get_store()
    _require_known_workload(store, workload)
    df = store.recent_observations(workload, 30)
    if df.is_empty():
        raise HTTPException(
            status_code=409, detail=f"no observations yet for workload: {workload}"
        )
    result = diagnose(df)
    return {
        "workload": workload,
        "diagnosis": result.diagnosis.value,
        "scaling_will_help": result.scaling_will_help,
        "explanation": result.explanation,
    }


def _compute_recommendation(
    workload: str, model: str, df: pl.DataFrame, diag: DiagnosisResult
) -> dict[str, Any]:
    current_replicas = int(df["replicas"][-1])
    forecast = _get_forecast(workload, model, df)
    rec = recommend_replicas(current_replicas, forecast, peak_step=STARTUP_LEAD_STEPS)
    recommended_replicas = (
        rec.recommended_replicas if diag.scaling_will_help else current_replicas
    )

    return {
        "workload": workload,
        "model": model,
        "current_replicas": rec.current_replicas,
        "recommended_replicas": recommended_replicas,
        "peak_forecast_p90": rec.peak_forecast_p90,
        "projected_utilization": rec.projected_utilization,
        "confidence": rec.confidence,
        "scaling_will_help": diag.scaling_will_help,
        "diagnosis": diag.diagnosis.value,
        "explanation": diag.explanation,
    }


@router.get("/workloads/{workload}/recommendation")
def get_recommendation(workload: str, model: str = _DEFAULT_MODEL) -> dict[str, Any]:
    if model not in _MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    store = get_store()
    _require_known_workload(store, workload)
    df = store.recent_observations(workload, _HISTORY_STEPS)
    if df.is_empty():
        raise HTTPException(
            status_code=409, detail=f"no observations yet for workload: {workload}"
        )

    diag = diagnose(df.tail(30))
    return _compute_recommendation(workload, model, df, diag)


@router.get("/workloads/{workload}/recommendations")
def get_all_recommendations(workload: str) -> dict[str, Any]:
    """Recommendation from every registered model, for side-by-side comparison."""
    store = get_store()
    _require_known_workload(store, workload)
    df = store.recent_observations(workload, _HISTORY_STEPS)
    if df.is_empty():
        raise HTTPException(
            status_code=409, detail=f"no observations yet for workload: {workload}"
        )

    diag = diagnose(df.tail(30))
    return {
        "workload": workload,
        "diagnosis": diag.diagnosis.value,
        "scaling_will_help": diag.scaling_will_help,
        "explanation": diag.explanation,
        "models": [
            _compute_recommendation(workload, model_name, df, diag)
            for model_name in _MODELS
        ],
    }
