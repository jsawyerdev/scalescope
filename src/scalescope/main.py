"""FastAPI application entry point: serves the API and the dashboard."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from scalescope.api.routes import router
from scalescope.auth import BasicAuthMiddleware
from scalescope.capacity import (
    STARTUP_LEAD_STEPS,
    ScaleDownStabilizer,
    recommend_replicas,
    resolve_capacity,
)
from scalescope.config import settings
from scalescope.demand import demand_history, demand_signal
from scalescope.diagnosis import diagnose
from scalescope.k8s_actuator import ActuationError, HpaConflictError, KubernetesActuator
from scalescope.k8s_collector import (
    KubernetesObservationCollector,
    KubernetesUnavailableError,
    KubernetesWorkloadTarget,
    PrometheusQueries,
    workload_id,
)
from scalescope.logging_config import configure_logging
from scalescope.models.registry import ACTUATION_MODEL, MODELS
from scalescope.simulator import WorkloadSimulator
from scalescope.state import app_state
from scalescope.storage import Store

configure_logging()
logger = logging.getLogger(__name__)

# Retention is enforced this often rather than every tick: a DELETE scans
# the table, and data only needs to stay roughly within the window.
_PRUNE_EVERY_TICKS = 300


def _init_source_state() -> dict[str, Any]:
    return {
        "mode": settings.mode,
        "k8s_namespace": settings.k8s_namespace if settings.mode == "observe" else None,
        "k8s_deployment": (
            settings.k8s_deployment if settings.mode == "observe" else None
        ),
        "cluster_server": None,
        "cluster_auth_type": None,
        "cluster_auth_identity": None,
        "tick_seconds": settings.simulation_tick_seconds,
        "connected": settings.mode == "demo",
        "last_success_ts": None,
        "last_error": None,
        "k8s_namespaces": (
            list(settings.k8s_namespaces) if settings.mode == "observe" else []
        ),
        "targets": [],
        "actuate": settings.actuate,
        "actuation_target": (
            workload_id(settings.k8s_namespace, settings.k8s_deployment)
            if settings.mode == "observe" and settings.actuate
            else None
        ),
        "last_actuation_ts": None,
        "last_actuation_replicas": None,
        "last_actuation_error": None,
    }


async def _prune_if_due(store: Store, tick: int) -> None:
    if tick % _PRUNE_EVERY_TICKS:
        return
    cutoff = datetime.now(UTC) - timedelta(hours=settings.retention_hours)
    deleted = await asyncio.to_thread(store.prune, cutoff)
    if deleted:
        logger.info("pruned %d observations older than %s", deleted, cutoff)


async def _simulation_loop(store: Store) -> None:
    simulator = WorkloadSimulator()
    app_state["simulator"] = simulator
    tick = 0
    while True:
        row = simulator.step()
        # Store calls block on its lock, which API reads can hold; keep them
        # off the event loop so health checks never stall behind a query.
        await asyncio.to_thread(store.insert_observation, row)
        app_state["source"]["last_success_ts"] = datetime.now(UTC)
        await _prune_if_due(store, tick)
        tick += 1
        await asyncio.sleep(settings.simulation_tick_seconds)


def _metrics_urls_by_target() -> dict[str, str]:
    if not settings.k8s_metrics_url:
        return {}
    return {
        workload_id(
            settings.k8s_namespace,
            settings.k8s_deployment,
        ): settings.k8s_metrics_url
    }


def _target_dicts(
    targets: list[KubernetesWorkloadTarget], metrics_urls: dict[str, str]
) -> list[dict[str, str | bool]]:
    return [
        target.to_dict(metrics_url_configured=target.workload_id in metrics_urls)
        for target in targets
    ]


def _actuate(
    store: Store,
    actuator: KubernetesActuator,
    target: KubernetesWorkloadTarget,
    stabilizer: ScaleDownStabilizer,
) -> None:
    source = app_state["source"]
    df = store.recent_observations(target.workload_id, settings.history_window_steps)
    if df.is_empty():
        return

    diag = diagnose(df, max_replicas=settings.max_replicas)
    if not diag.scaling_will_help:
        source["last_actuation_error"] = f"skipped: {diag.explanation}"
        return

    signal = demand_signal(df)
    capacity = resolve_capacity(
        df, signal, settings.capacity_per_pod_rps, settings.latency_slo_ms
    )
    if capacity.per_pod is None:
        source["last_actuation_error"] = (
            "skipped: per-pod capacity unknown; set SCALESCOPE_CAPACITY_PER_POD_RPS, "
            "give the Deployment a CPU request, or wait for enough history to "
            "estimate it"
        )
        return

    # spec.replicas, not status: status lags a scale write by many seconds,
    # and planning from it would apply the same step twice.
    current_replicas = int(df["desired_replicas"][-1])
    forecast = MODELS[ACTUATION_MODEL].predict(
        demand_history(df, signal), settings.forecast_horizon_steps
    )
    recommended = stabilizer.stabilize(
        time.monotonic(),
        current_replicas,
        recommend_replicas(
            current_replicas,
            forecast,
            capacity,
            settings.scaling_policy,
            peak_step=STARTUP_LEAD_STEPS,
        ).recommended_replicas,
    )
    if recommended == current_replicas:
        return

    try:
        actuator.scale(target.deployment, recommended)
        source["last_actuation_ts"] = datetime.now(UTC)
        source["last_actuation_replicas"] = recommended
        source["last_actuation_error"] = None
    except (HpaConflictError, ActuationError) as exc:
        logger.warning("actuation skipped: %s", exc)
        source["last_actuation_error"] = str(exc)


async def _observe_loop(store: Store) -> None:
    source = app_state["source"]
    metrics_urls = _metrics_urls_by_target()
    primary_target = KubernetesWorkloadTarget(
        namespace=settings.k8s_namespace,
        deployment=settings.k8s_deployment,
    )
    try:
        collector = await asyncio.to_thread(
            KubernetesObservationCollector,
            kubeconfig_path=settings.k8s_kubeconfig,
            metrics_urls=metrics_urls,
            prometheus_url=settings.prometheus_url,
            prometheus_queries=PrometheusQueries(
                request_rate=settings.prometheus_rps_query,
                throttled_fraction=settings.prometheus_throttling_query,
                latency_p95_ms=settings.prometheus_latency_query,
                error_rate=settings.prometheus_error_rate_query,
            ),
        )
        source["cluster_server"] = collector.cluster_server
        source["cluster_auth_type"] = collector.auth_type
        source["cluster_auth_identity"] = collector.auth_identity
    except Exception as exc:
        logger.exception(
            "could not initialize Kubernetes client for %s/%s; observe loop not started",
            settings.k8s_namespace,
            settings.k8s_deployment,
        )
        source["last_error"] = str(exc)
        return

    actuator: KubernetesActuator | None = None
    if settings.actuate:
        actuator = await asyncio.to_thread(
            KubernetesActuator, settings.k8s_namespace, settings.k8s_kubeconfig
        )
        logger.warning(
            "actuation enabled: will write replicas to %s/%s",
            settings.k8s_namespace,
            settings.k8s_deployment,
        )
    stabilizer = ScaleDownStabilizer(settings.scale_down_stabilization_seconds)

    tick = 0
    while True:
        try:
            result = await asyncio.to_thread(collector.collect, settings.k8s_namespaces)
            source["targets"] = _target_dicts(result.targets, metrics_urls)
            if not result.targets:
                raise KubernetesUnavailableError(
                    f"no deployments visible in namespaces {settings.k8s_namespaces}"
                )
            if not result.rows:
                raise KubernetesUnavailableError(
                    "; ".join(result.errors) or "no observations collected this tick"
                )

            for row in result.rows:
                await asyncio.to_thread(store.insert_observation, row)
            if actuator is not None and primary_target.workload_id in {
                row["workload"] for row in result.rows
            }:
                await asyncio.to_thread(
                    _actuate, store, actuator, primary_target, stabilizer
                )

            source["connected"] = True
            source["last_success_ts"] = datetime.now(UTC)
            source["last_error"] = (
                f"{len(result.errors)} target(s) failed: {result.errors[0]}"
                if result.errors
                else None
            )
        except KubernetesUnavailableError as exc:
            logger.warning(
                "Kubernetes observation failed for namespaces %s",
                settings.k8s_namespaces,
            )
            source["connected"] = False
            source["last_error"] = str(exc)
        await _prune_if_due(store, tick)
        tick += 1
        await asyncio.sleep(settings.simulation_tick_seconds)


def _record_background_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.exception("data source task stopped unexpectedly")
        source = app_state.get("source")
        if source is not None:
            source["connected"] = False
            source["last_error"] = f"data source task stopped: {exc}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = Store(settings.db_path)
    app_state["store"] = store
    app_state["source"] = _init_source_state()

    if settings.mode == "demo":
        task = asyncio.create_task(_simulation_loop(store))
        logger.info("demo simulation loop started")
    else:
        task = asyncio.create_task(_observe_loop(store))
        logger.info(
            "observe loop started for %s/%s",
            settings.k8s_namespace,
            settings.k8s_deployment,
        )
    task.add_done_callback(_record_background_failure)
    app_state["data_source_task"] = task

    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        app_state.pop("data_source_task", None)
        store.close()


app = FastAPI(title="ScaleScope", lifespan=lifespan)


@app.get("/healthz", response_model=None)
def healthz() -> dict[str, str] | JSONResponse:
    """Unauthenticated liveness check - the only route BasicAuthMiddleware exempts.

    503 once the data-source loop has stopped (it failed to start or crashed):
    nothing restarts it in-process, so the kubelet must restart the pod.
    """
    task = app_state.get("data_source_task")
    if task is not None and task.done():
        return JSONResponse({"status": "data source stopped"}, status_code=503)
    return {"status": "ok"}


app.include_router(router)

static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")

if settings.auth_username and settings.auth_password:
    app.add_middleware(
        BasicAuthMiddleware,
        username=settings.auth_username,
        password=settings.auth_password,
    )
    logger.info("HTTP Basic Auth enabled for all routes except /healthz")
else:
    logger.warning(
        "no SCALESCOPE_AUTH_USERNAME/SCALESCOPE_AUTH_PASSWORD configured - "
        "running with no authentication"
    )
