"""FastAPI application entry point: serves the API and the dashboard."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from scalescope.api.routes import router
from scalescope.auth import BasicAuthMiddleware
from scalescope.capacity import STARTUP_LEAD_STEPS, recommend_replicas
from scalescope.config import settings
from scalescope.diagnosis import diagnose
from scalescope.k8s_actuator import ActuationError, HpaConflictError, KubernetesActuator
from scalescope.k8s_collector import (
    KubernetesObservationCollector,
    KubernetesUnavailableError,
    KubernetesWorkloadTarget,
    workload_id,
)
from scalescope.logging_config import configure_logging
from scalescope.models.statsforecast_model import AutoEtsModel
from scalescope.simulator import WorkloadSimulator
from scalescope.state import app_state
from scalescope.storage import Store

configure_logging()
logger = logging.getLogger(__name__)


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


async def _simulation_loop(store: Store) -> None:
    simulator = WorkloadSimulator()
    app_state["simulator"] = simulator
    while True:
        row = simulator.step()
        store.insert_observation(row)
        app_state["source"]["last_success_ts"] = datetime.now(UTC)
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
    store: Store, actuator: KubernetesActuator, target: KubernetesWorkloadTarget
) -> None:
    source = app_state["source"]
    df = store.recent_observations(target.workload_id, settings.history_window_steps)
    if df.is_empty():
        return

    diag = diagnose(df)
    if not diag.scaling_will_help:
        source["last_actuation_error"] = f"skipped: {diag.explanation}"
        return

    current_replicas = int(df["replicas"][-1])
    forecast = AutoEtsModel().predict(
        df["request_rate"].to_numpy(), settings.forecast_horizon_steps
    )
    rec = recommend_replicas(current_replicas, forecast, peak_step=STARTUP_LEAD_STEPS)
    if rec.recommended_replicas == current_replicas:
        return

    try:
        actuator.scale(target.deployment, rec.recommended_replicas)
        source["last_actuation_ts"] = datetime.now(UTC)
        source["last_actuation_replicas"] = rec.recommended_replicas
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

    while True:
        try:
            targets = await asyncio.to_thread(
                collector.list_targets, settings.k8s_namespaces
            )
            source["targets"] = _target_dicts(targets, metrics_urls)
            if not targets:
                raise KubernetesUnavailableError(
                    f"no deployments visible in namespaces {settings.k8s_namespaces}"
                )

            target_errors: list[str] = []
            rows_inserted = 0
            for target in targets:
                try:
                    row = await asyncio.to_thread(collector.collect, target)
                except KubernetesUnavailableError as exc:
                    target_errors.append(str(exc))
                    continue
                store.insert_observation(row)
                rows_inserted += 1
                if actuator is not None and target == primary_target:
                    await asyncio.to_thread(_actuate, store, actuator, target)

            if rows_inserted == 0:
                raise KubernetesUnavailableError(
                    "; ".join(target_errors) or "no observations collected this tick"
                )

            source["connected"] = True
            source["last_success_ts"] = datetime.now(UTC)
            source["last_error"] = (
                f"{len(target_errors)} target(s) failed: {target_errors[0]}"
                if target_errors
                else None
            )
        except KubernetesUnavailableError as exc:
            logger.warning(
                "Kubernetes observation failed for namespaces %s",
                settings.k8s_namespaces,
            )
            source["connected"] = False
            source["last_error"] = str(exc)
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

    try:
        yield
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
        store.close()


app = FastAPI(title="ScaleScope", lifespan=lifespan)


@app.get("/healthz")
def healthz() -> dict[str, str]:
    """Unauthenticated liveness check - the only route BasicAuthMiddleware exempts."""
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
