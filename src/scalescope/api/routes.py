"""REST API for workload state, forecasts, and diagnosis."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, HTTPException

from scalescope.capacity import recommend_replicas
from scalescope.config import settings
from scalescope.diagnosis import diagnose
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

_MODELS = {
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
_STARTUP_LEAD_STEPS = 15  # models pod-startup + readiness lag in simulation ticks


def get_store() -> Store:
    # Deferred import: main.py imports this router at module load time, and
    # app_state is only populated once the FastAPI lifespan starts.
    from scalescope.main import app_state

    return app_state["store"]


@router.get("/workloads")
def list_workloads() -> list[str]:
    return get_store().workloads()


@router.get("/workloads/{workload}/observations")
def get_observations(workload: str, limit: int = 300) -> list[dict[str, Any]]:
    df = get_store().recent_observations(workload, limit)
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"unknown workload: {workload}")
    return df.to_dicts()


@router.get("/workloads/{workload}/forecast")
def get_forecast(workload: str, model: str = _DEFAULT_MODEL) -> dict[str, Any]:
    if model not in _MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    df = get_store().recent_observations(workload, _HISTORY_STEPS)
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"unknown workload: {workload}")

    history = df["request_rate"].to_numpy()
    forecast = _MODELS[model].predict(history, _HORIZON_STEPS)
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
    df = get_store().recent_observations(workload, 30)
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"unknown workload: {workload}")
    result = diagnose(df)
    return {
        "workload": workload,
        "diagnosis": result.diagnosis.value,
        "scaling_will_help": result.scaling_will_help,
        "explanation": result.explanation,
    }


@router.get("/workloads/{workload}/recommendation")
def get_recommendation(workload: str, model: str = _DEFAULT_MODEL) -> dict[str, Any]:
    if model not in _MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    store = get_store()
    df = store.recent_observations(workload, _HISTORY_STEPS)
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"unknown workload: {workload}")

    current_replicas = int(df["replicas"][-1])
    history = df["request_rate"].to_numpy()
    forecast = _MODELS[model].predict(history, _HORIZON_STEPS)
    diag = diagnose(df.tail(30))
    rec = recommend_replicas(current_replicas, forecast, peak_step=_STARTUP_LEAD_STEPS)

    row = {
        "ts": datetime.now(UTC),
        "workload": workload,
        "current_replicas": rec.current_replicas,
        "recommended_replicas": (
            rec.recommended_replicas if diag.scaling_will_help else current_replicas
        ),
        "reason": diag.explanation,
        "confidence": rec.confidence,
    }
    store.insert_recommendation(row)

    return {
        "workload": workload,
        "model": model,
        "current_replicas": rec.current_replicas,
        "recommended_replicas": row["recommended_replicas"],
        "peak_forecast_p90": rec.peak_forecast_p90,
        "projected_utilization": rec.projected_utilization,
        "confidence": rec.confidence,
        "scaling_will_help": diag.scaling_will_help,
        "diagnosis": diag.diagnosis.value,
        "explanation": diag.explanation,
    }
