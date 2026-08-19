"""FastAPI application entry point: serves the API and the dashboard."""

from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from scalescope.api.routes import router
from scalescope.config import settings
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    store = Store(settings.db_path)
    app_state["store"] = store

    task: asyncio.Task | None = None
    if settings.mode == "demo":
        task = asyncio.create_task(_simulation_loop(store))
        logger.info("demo simulation loop started")
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
