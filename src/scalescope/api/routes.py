"""REST API for workload state, forecasts, and diagnosis."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from importlib.metadata import version as _package_version
from typing import Any

import httpx
import polars as pl
from fastapi import APIRouter, HTTPException, Query

from scalescope.capacity import (
    HPA_SCALE_DOWN_STABILIZATION_SECONDS,
    STARTUP_LEAD_STEPS,
    CapacityRecommendation,
    PodCapacity,
    recommend_replicas,
    resolve_capacity,
)
from scalescope.config import settings
from scalescope.demand import DemandSignal, demand_history, demand_signal
from scalescope.diagnosis import DIAGNOSIS_WINDOW_STEPS, DiagnosisResult, diagnose
from scalescope.k8s_collector import workload_id
from scalescope.models.base import Forecast
from scalescope.models.registry import ACTUATION_MODEL, MODELS
from scalescope.replay import REPLAY_MAX_OBSERVATIONS, REPLAY_MIN_HISTORY, replay_score
from scalescope.scaling_replay import capacity_training_rows, replay_scaling
from scalescope.state import app_state
from scalescope.storage import Store

router = APIRouter(prefix="/api")


_HORIZON_STEPS = settings.forecast_horizon_steps
_HISTORY_STEPS = settings.history_window_steps
_MAX_OBSERVATIONS_LIMIT = 5000
_SOURCE_STALE_AFTER_SECONDS = max(30.0, settings.simulation_tick_seconds * 5)
_VERSION = _package_version("scalescope")

# Keyed by (workload, model, signal) -> (latest observation ts, Forecast). A
# fixed-size history window keeps len(history) constant once it fills, so the
# cache must key on the newest timestamp rather than row count to invalidate.
# Bounded LRU: workloads come and go (Deployment churn, retention), and an
# unbounded dict would keep every one ever seen for the process lifetime.
_FORECAST_CACHE_MAX_ENTRIES = 2048
_forecast_cache_lock = threading.Lock()
_forecast_cache: dict[tuple[str, str, DemandSignal], tuple[Any, Forecast]] = {}


def get_store() -> Store:
    store: Store = app_state["store"]
    return store


@router.get("/source")
def get_source() -> dict[str, Any]:
    """What ScaleScope is actually observing right now: mode and cluster identity."""
    source: dict[str, Any] = dict(app_state["source"])
    last_success = source.get("last_success_ts")
    if (
        source.get("mode") == "observe"
        and source.get("connected")
        and isinstance(last_success, datetime)
    ):
        age_seconds = (datetime.now(UTC) - last_success).total_seconds()
        if age_seconds > _SOURCE_STALE_AFTER_SECONDS:
            source["connected"] = False
            source["last_error"] = (
                source.get("last_error")
                or f"last successful collection is stale ({int(age_seconds)}s old)"
            )
    source["version"] = _VERSION
    source["actuation_model"] = ACTUATION_MODEL
    return source


def _recent_observations_or_404(workload: str, limit: int) -> pl.DataFrame:
    # A workload exists exactly while it has observations (retention prunes
    # whole time ranges), so an empty result is exactly an unknown workload.
    df = get_store().recent_observations(workload, limit)
    if df.is_empty():
        raise HTTPException(status_code=404, detail=f"unknown workload: {workload}")
    return df


def _get_forecast(
    workload: str, model: str, df: pl.DataFrame, signal: DemandSignal
) -> Forecast:
    history = demand_history(df, signal)
    latest_ts = df["ts"][-1]
    cache_key = (workload, model, signal)
    with _forecast_cache_lock:
        cached = _forecast_cache.get(cache_key)
        if cached is not None and cached[0] == latest_ts:
            return cached[1]
    forecast = MODELS[model].predict(history, _HORIZON_STEPS)
    with _forecast_cache_lock:
        _forecast_cache.pop(cache_key, None)
        _forecast_cache[cache_key] = (latest_ts, forecast)
        while len(_forecast_cache) > _FORECAST_CACHE_MAX_ENTRIES:
            del _forecast_cache[next(iter(_forecast_cache))]
    return forecast


@router.get("/workloads")
def list_workloads() -> list[str]:
    """Stored workloads; in OBSERVE mode, only those currently visible, if any."""
    workloads = set(get_store().workloads())
    if settings.mode == "observe":
        visible = [
            target["id"]
            for target in app_state["source"]["targets"]
            if target["id"] in workloads
        ]
        if visible:
            return sorted(visible)
    return sorted(workloads)


@router.get("/workloads/{workload}/observations")
def get_observations(
    workload: str, limit: int = Query(default=300, ge=1, le=_MAX_OBSERVATIONS_LIMIT)
) -> list[dict[str, Any]]:
    return _recent_observations_or_404(workload, limit).to_dicts()


@router.get("/workloads/{workload}/forecast")
def get_forecast(workload: str, model: str = ACTUATION_MODEL) -> dict[str, Any]:
    if model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    df = _recent_observations_or_404(workload, _HISTORY_STEPS)
    signal = demand_signal(df)
    forecast = _get_forecast(workload, model, df, signal)
    return {
        "workload": workload,
        "model": forecast.model_name,
        "demand_signal": signal,
        "horizon_steps": _HORIZON_STEPS,
        "p10": forecast.p10.tolist(),
        "p50": forecast.p50.tolist(),
        "p90": forecast.p90.tolist(),
    }


@router.get("/workloads/{workload}/diagnosis")
def get_diagnosis(workload: str) -> dict[str, Any]:
    result = _diagnose(_recent_observations_or_404(workload, DIAGNOSIS_WINDOW_STEPS))
    return {
        "workload": workload,
        "diagnosis": result.diagnosis.value,
        "scaling_will_help": result.scaling_will_help,
        "explanation": result.explanation,
    }


def _diagnose(df: pl.DataFrame) -> DiagnosisResult:
    return diagnose(df, max_replicas=settings.max_replicas)


def _capacity(df: pl.DataFrame, signal: DemandSignal) -> PodCapacity:
    return resolve_capacity(
        df, signal, settings.capacity_per_pod_rps, settings.latency_slo_ms
    )


def _capacity_summary(capacity: PodCapacity) -> dict[str, Any] | None:
    model = capacity.latency_model
    if model is None:
        return None
    return {
        "base_ms": round(model.base_ms, 2),
        "saturation_rps_per_pod": round(model.saturation_rps, 2),
        "no_load_latency_ms": round(model.no_load_latency_ms, 2),
        "latency_target_ms": capacity.latency_target_ms,
        "r_squared": round(model.r_squared, 3),
    }


def _compute_recommendation(
    workload: str,
    model: str,
    df: pl.DataFrame,
    diag: DiagnosisResult,
    signal: DemandSignal,
    capacity: PodCapacity,
) -> dict[str, Any]:
    rec: CapacityRecommendation = recommend_replicas(
        # spec.replicas, not status: status lags a scale write by many seconds.
        int(df["desired_replicas"][-1]),
        _get_forecast(workload, model, df, signal),
        capacity,
        settings.scaling_policy,
        peak_step=STARTUP_LEAD_STEPS,
        scaling_will_help=diag.scaling_will_help,
    )
    return {
        "workload": workload,
        "model": model,
        "current_replicas": rec.current_replicas,
        "recommended_replicas": rec.recommended_replicas,
        "peak_forecast_p90": rec.peak_forecast_p90,
        "projected_utilization": rec.projected_utilization,
        "confidence": rec.confidence,
        "demand_signal": signal,
        "capacity_per_pod": capacity.per_pod,
        "capacity_source": capacity.source,
        "target_utilization": (
            capacity.target_utilization or settings.target_utilization
        ),
        "latency_model": _capacity_summary(capacity),
        "hold_reason": rec.hold_reason,
        "pods_needed": rec.pods_needed,
        "startup_lead_steps": STARTUP_LEAD_STEPS,
        "scaling_will_help": diag.scaling_will_help,
        "diagnosis": diag.diagnosis.value,
        "explanation": diag.explanation,
    }


@router.get("/workloads/{workload}/recommendation")
def get_recommendation(workload: str, model: str = ACTUATION_MODEL) -> dict[str, Any]:
    if model not in MODELS:
        raise HTTPException(status_code=400, detail=f"unknown model: {model}")
    df = _recent_observations_or_404(workload, _HISTORY_STEPS)
    signal = demand_signal(df)
    return _compute_recommendation(
        workload, model, df, _diagnose(df), signal, _capacity(df, signal)
    )


@router.get("/workloads/{workload}/recommendations")
def get_all_recommendations(workload: str) -> dict[str, Any]:
    """Recommendation from every registered model, for side-by-side comparison."""
    df = _recent_observations_or_404(workload, _HISTORY_STEPS)
    diag = _diagnose(df)
    signal = demand_signal(df)
    capacity = _capacity(df, signal)
    return {
        "workload": workload,
        "diagnosis": diag.diagnosis.value,
        "scaling_will_help": diag.scaling_will_help,
        "explanation": diag.explanation,
        "models": [
            _compute_recommendation(workload, name, df, diag, signal, capacity)
            for name in MODELS
        ],
    }


@router.get("/workloads/{workload}/replay")
def get_replay(workload: str) -> dict[str, Any]:
    """Backtest every model against this workload's real recorded history.

    Measured MAE/MAPE per model, not a stated preference - answers "which
    model actually performs best here" using only data available at each
    backtest point, the same discipline the diagnosis engine applies to
    scaling decisions.
    """
    df = _recent_observations_or_404(workload, REPLAY_MAX_OBSERVATIONS)
    signal = demand_signal(df)
    history = demand_history(df, signal)
    scores = replay_score(
        history, MODELS, min_history=REPLAY_MIN_HISTORY, horizon=_HORIZON_STEPS
    )
    return {
        "workload": workload,
        "n_observations": len(history),
        "demand_signal": signal,
        "horizon_steps": _HORIZON_STEPS,
        "scores": sorted(
            (
                {
                    "model": s.model_name,
                    "n_anchors": s.n_anchors,
                    "mean_absolute_error": s.mean_absolute_error,
                    "mean_absolute_pct_error": s.mean_absolute_pct_error,
                    "p90_pinball_loss": s.p90_pinball_loss,
                    "p90_coverage": s.p90_coverage,
                }
                for s in scores
            ),
            # p90 drives replica sizing, so rank on how well it is forecast.
            key=lambda s: s["p90_pinball_loss"],
        ),
    }


@router.get("/workloads/{workload}/scaling-replay")
def get_scaling_replay(workload: str) -> dict[str, Any]:
    """Replay this workload's recorded demand through ScaleScope and a reactive HPA.

    See `scalescope.scaling_replay` for the method. Returns `outcomes: []`
    with a `reason` when there is too little history or no pod capacity.
    """
    df = _recent_observations_or_404(workload, REPLAY_MAX_OBSERVATIONS)
    signal = demand_signal(df)
    capacity = _capacity(capacity_training_rows(df), signal)
    tick = settings.simulation_tick_seconds
    result = replay_scaling(
        df,
        signal,
        capacity,
        settings.scaling_policy,
        MODELS[ACTUATION_MODEL],
        _HORIZON_STEPS,
        _HISTORY_STEPS,
        hpa_stabilization_ticks=round(HPA_SCALE_DOWN_STABILIZATION_SECONDS / tick),
        scale_down_stabilization_ticks=round(
            settings.scale_down_stabilization_seconds / tick
        ),
    )
    if result is None:
        reason = (
            "pod capacity is not known yet"
            if capacity.per_pod is None
            else "not enough history yet"
        )
        return {"workload": workload, "outcomes": [], "reason": reason}
    return {
        "workload": workload,
        "replayed_seconds": round(result.ticks * tick),
        "decision_every_seconds": round(result.decision_every * tick, 1),
        "capacity_source": capacity.source,
        "outcomes": [vars(outcome) for outcome in result.outcomes],
    }


_TRIGGER_KINDS = {"cpu", "memory", "traffic", "stress"}
_DEMO_FAULT_MAP = {
    "cpu": "cpu_limit",
    "memory": "memory_leak",
    "traffic": "traffic_spike",
}


@router.post("/workloads/{workload}/trigger")
def trigger_fault(
    workload: str, kind: str, duration_seconds: int = Query(default=45, ge=5, le=300)
) -> dict[str, Any]:
    """Force a load pattern now, so the effect is visible within a few ticks.

    DEMO mode drives the local simulator directly. OBSERVE mode calls the
    real workload's own /trigger endpoint (derived from
    SCALESCOPE_K8S_METRICS_URL's base URL) - ScaleScope has no other route
    to the workload's process.
    """
    if kind not in _TRIGGER_KINDS:
        raise HTTPException(
            status_code=400,
            detail=f"unknown kind: {kind} (expected one of {sorted(_TRIGGER_KINDS)})",
        )

    if settings.mode == "demo":
        if kind == "stress":
            raise HTTPException(
                status_code=501,
                detail="stress trigger is only supported in OBSERVE mode",
            )

        simulator = app_state.get("simulator")
        if simulator is None:
            raise HTTPException(status_code=503, detail="simulator not running yet")
        if workload != simulator.state.name:
            raise HTTPException(
                status_code=501,
                detail=(
                    "no trigger route is configured for this workload; "
                    f"the demo simulator drives {simulator.state.name}"
                ),
            )
        duration_ticks = max(
            1, round(duration_seconds / settings.simulation_tick_seconds)
        )
        simulator.trigger_fault(_DEMO_FAULT_MAP[kind], duration_ticks=duration_ticks)
        return {
            "workload": workload,
            "kind": kind,
            "duration_seconds": duration_seconds,
            "target": "demo simulator",
        }

    if not settings.k8s_metrics_url:
        raise HTTPException(
            status_code=501,
            detail=(
                "SCALESCOPE_K8S_METRICS_URL is not configured; ScaleScope has no "
                "route to the workload to trigger a fault"
            ),
        )
    trigger_workload = workload_id(settings.k8s_namespace, settings.k8s_deployment)
    if workload != trigger_workload:
        raise HTTPException(
            status_code=501,
            detail=(
                "no trigger route is configured for this workload; "
                f"SCALESCOPE_K8S_METRICS_URL is attached to {trigger_workload}"
            ),
        )
    base_url = settings.k8s_metrics_url.removesuffix("/metrics")
    try:
        response = httpx.post(
            f"{base_url}/trigger",
            params={"kind": kind, "duration_seconds": duration_seconds},
            timeout=5.0,
        )
        response.raise_for_status()
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"could not reach workload: {exc}"
        ) from exc
    try:
        response_body = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502, detail=f"workload returned invalid JSON: {exc}"
        ) from exc
    if not isinstance(response_body, dict):
        raise HTTPException(
            status_code=502,
            detail="workload trigger response must be a JSON object",
        )
    # The workload's reply is untrusted: it may add detail but must not
    # overwrite what ScaleScope itself requested and where it sent it.
    return {
        **response_body,
        "workload": workload,
        "kind": kind,
        "duration_seconds": duration_seconds,
        "target": base_url,
    }
