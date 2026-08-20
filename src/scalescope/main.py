"""FastAPI application entry point: serves the API and the dashboard."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from scalescope.api.routes import router
from scalescope.capacity import STARTUP_LEAD_STEPS, recommend_replicas
from scalescope.config import settings
from scalescope.diagnosis import diagnose
from scalescope.k8s_actuator import ActuationError, HpaConflictError, KubernetesActuator
from scalescope.k8s_collector import (
    KubernetesObservationCollector,
    KubernetesUnavailableError,
)
from scalescope.logging_config import configure_logging
from scalescope.models.statsforecast_model import AutoEtsModel
from scalescope.simulator import WorkloadSimulator
from scalescope.storage import Store

configure_logging()
logger = logging.getLogger(__name__)

app_state: dict[str, Any] = {}


def _init_source_state() -> dict[str, Any]:
    return {
        "mode": settings.mode,
        "k8s_namespace": settings.k8s_namespace if settings.mode == "observe" else None,
        "k8s_deployment": (
            settings.k8s_deployment if settings.mode == "observe" else None
        ),
        "cluster_server": None,
        "connected": settings.mode == "demo",
        "last_success_ts": None,
        "last_error": None,
        "actuate": settings.actuate,
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


def _actuate(store: Store, actuator: KubernetesActuator) -> None:
    source = app_state["source"]
    df = store.recent_observations(
        settings.k8s_deployment, settings.history_window_steps
    )
    if df.is_empty():
        return

    diag = diagnose(df.tail(30))
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
        actuator.scale(settings.k8s_deployment, rec.recommended_replicas)
        source["last_actuation_ts"] = datetime.now(UTC)
        source["last_actuation_replicas"] = rec.recommended_replicas
        source["last_actuation_error"] = None
    except (HpaConflictError, ActuationError) as exc:
        logger.warning("actuation skipped: %s", exc)
        source["last_actuation_error"] = str(exc)


async def _observe_loop(store: Store) -> None:
    source = app_state["source"]
    try:
        collector = await asyncio.to_thread(
            KubernetesObservationCollector,
            settings.k8s_namespace,
            settings.k8s_deployment,
            settings.k8s_kubeconfig,
            settings.k8s_metrics_url,
        )
        source["cluster_server"] = collector.cluster_server
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
            row = await asyncio.to_thread(collector.collect)
            store.insert_observation(row)
            source["connected"] = True
            source["last_success_ts"] = datetime.now(UTC)
            source["last_error"] = None
            if actuator is not None:
                await asyncio.to_thread(_actuate, store, actuator)
        except KubernetesUnavailableError as exc:
            logger.warning(
                "deployment %s/%s unreachable this tick",
                settings.k8s_namespace,
                settings.k8s_deployment,
            )
            source["connected"] = False
            source["last_error"] = str(exc)
        await asyncio.sleep(settings.simulation_tick_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = Store(settings.db_path)
    app_state["store"] = store
    app_state["source"] = _init_source_state()

    task: asyncio.Task | None = None
    if settings.mode == "demo":
        task = asyncio.create_task(_simulation_loop(store))
        logger.info("demo simulation loop started")
    elif settings.mode == "observe":
        task = asyncio.create_task(_observe_loop(store))
        logger.info(
            "observe loop started for %s/%s",
            settings.k8s_namespace,
            settings.k8s_deployment,
        )
    else:
        logger.warning("mode=%s not implemented; no data source running", settings.mode)

    yield

    if task is not None:
        task.cancel()
    store.close()


app = FastAPI(title="ScaleScope", lifespan=lifespan)
app.include_router(router)

static_dir = Path(__file__).parent / "static"
app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
