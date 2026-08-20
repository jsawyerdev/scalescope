"""FastAPI application entry point: serves the API and the dashboard."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from scalescope.api.routes import router
from scalescope.config import settings
from scalescope.k8s_collector import (
    KubernetesObservationCollector,
    KubernetesUnavailableError,
)
from scalescope.logging_config import configure_logging
from scalescope.simulator import WorkloadSimulator
from scalescope.storage import Store

configure_logging()
logger = logging.getLogger(__name__)

app_state: dict[str, Any] = {}


async def _simulation_loop(store: Store) -> None:
    simulator = WorkloadSimulator()
    while True:
        row = simulator.step()
        store.insert_observation(row)
        await asyncio.sleep(settings.simulation_tick_seconds)


async def _observe_loop(store: Store) -> None:
    try:
        collector = await asyncio.to_thread(
            KubernetesObservationCollector,
            settings.k8s_namespace,
            settings.k8s_deployment,
            settings.k8s_kubeconfig,
            settings.k8s_metrics_url,
        )
    except Exception:
        logger.exception(
            "could not initialize Kubernetes client for %s/%s; observe loop not started",
            settings.k8s_namespace,
            settings.k8s_deployment,
        )
        return

    while True:
        try:
            row = await asyncio.to_thread(collector.collect)
            store.insert_observation(row)
        except KubernetesUnavailableError:
            logger.warning(
                "deployment %s/%s unreachable this tick",
                settings.k8s_namespace,
                settings.k8s_deployment,
            )
        await asyncio.sleep(settings.simulation_tick_seconds)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = Store(settings.db_path)
    app_state["store"] = store

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
