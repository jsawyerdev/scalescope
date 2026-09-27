"""FastAPI application entry point: serves the API and the dashboard."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import polars as pl
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

from scalescope import demo_history
from scalescope.api.routes import router
from scalescope.auth import BasicAuthMiddleware
from scalescope.capacity import (
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
from scalescope.learning import ACCURACY_DAYS, Learner
from scalescope.logging_config import configure_logging
from scalescope.models.registry import ACTUATION_MODEL, MODELS
from scalescope.nodes import NodePlanner
from scalescope.simulator import WorkloadSimulator, WorkloadState
from scalescope.state import app_state
from scalescope.storage import NODE_MINUTE_COLUMNS, Store

configure_logging()
logger = logging.getLogger(__name__)

# Retention is enforced this often rather than every tick: a DELETE scans
# the table, and data only needs to stay roughly within the window.
_PRUNE_EVERY_TICKS = 300
# Minute rollups run each minute and rescan this far back, so a late or
# missed run still rolls up every complete minute.
_ROLLUP_EVERY_SECONDS = 60.0
_ROLLUP_LOOKBACK = timedelta(hours=1)
# Node pools are sampled once a minute.
_NODE_EVERY_SECONDS = 60.0


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
        # Why node pools cannot be read, if they cannot (OBSERVE mode).
        "nodes_error": None,
        # DEMO starts with generated history so the long-memory model has
        # weeks to learn from; the dashboard says so.
        "demo_history_days": (
            demo_history.DEMO_HISTORY_DAYS if settings.mode == "demo" else None
        ),
    }


async def _prune_if_due(store: Store, tick: int) -> None:
    if tick % _PRUNE_EVERY_TICKS:
        return
    cutoff = datetime.now(UTC) - timedelta(hours=settings.retention_hours)
    deleted = await asyncio.to_thread(store.prune, cutoff)
    if deleted:
        logger.info("pruned %d observations older than %s", deleted, cutoff)


def _record_nodes(
    store: Store,
    rows: list[dict[str, Any]],
    startups: list[tuple[str, str, datetime, float]],
) -> None:
    if rows:
        store.insert_node_minutes(pl.DataFrame(rows).select(NODE_MINUTE_COLUMNS))
    store.record_node_startups(startups)


def _node_sampler(sample: Callable[[datetime], None]) -> Callable[[], None]:
    """Calls `sample(minute)` at most once per `_NODE_EVERY_SECONDS`."""
    last = -_NODE_EVERY_SECONDS

    def maybe_sample() -> None:
        nonlocal last
        if time.monotonic() - last < _NODE_EVERY_SECONDS:
            return
        last = time.monotonic()
        sample(datetime.now(UTC).replace(second=0, microsecond=0))

    return maybe_sample


def _demo_node_pool(store: Store) -> demo_history.DemoNodePool:
    """The demo pool, continuing from its recorded node count."""
    recent = store.node_history(
        datetime.now(UTC) - timedelta(hours=1), demo_history.DEMO_POOL
    )
    if recent.is_empty():
        return demo_history.DemoNodePool()
    return demo_history.DemoNodePool(nodes=int(recent["nodes"][-1]))


async def _simulation_loop(store: Store) -> None:
    simulator = WorkloadSimulator(
        demand_level=lambda: demo_history.demand_level(datetime.now(UTC))
    )
    app_state["simulator"] = simulator
    node_pool = await asyncio.to_thread(_demo_node_pool, store)

    def sample_nodes(minute: datetime) -> None:
        row, startups = node_pool.step(minute, simulator.state.replicas)
        _record_nodes(store, [row], startups)

    maybe_sample_nodes = _node_sampler(sample_nodes)
    tick = 0
    while True:
        row = simulator.step()
        row["memory_request_mb"] = demo_history.POD_MEMORY_REQUEST_MB
        row["node_pool"] = demo_history.DEMO_POOL
        # Store calls block on its lock, which API reads can hold; keep them
        # off the event loop so health checks never stall behind a query.
        await asyncio.to_thread(store.insert_observation, row)
        await asyncio.to_thread(maybe_sample_nodes)
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
    learner: Learner,
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
    now = datetime.now(UTC)
    capacity = resolve_capacity(
        df,
        signal,
        settings.capacity_per_pod_rps,
        settings.latency_slo_ms,
        learner.learned_capacity(target.workload_id, signal),
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
            peak_step=settings.startup_lead_steps,
            long_forecast=learner.forecast(target.workload_id, signal, now),
            lead_minutes=settings.startup_lead_minutes,
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


async def _observe_loop(store: Store, learner: Learner) -> None:
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
            node_pool_label=settings.node_pool_label,
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

    def sample_nodes(minute: datetime) -> None:
        # Node pools are advisory: no failure here may stop observing workloads.
        try:
            nodes = collector.collect_nodes(minute)
            _record_nodes(store, nodes.pools, nodes.startups)
        except KubernetesUnavailableError as exc:
            if source["nodes_error"] is None:
                logger.warning("node pools unavailable: %s", exc)
            source["nodes_error"] = str(exc)
            return
        except Exception as exc:
            logger.exception("node pool sampling failed")
            source["nodes_error"] = f"node pool sampling failed: {exc}"
            return
        source["nodes_error"] = None

    maybe_sample_nodes = _node_sampler(sample_nodes)
    tick = 0
    while True:
        try:
            # Nodes first, so this tick's rows name each workload's pool.
            await asyncio.to_thread(maybe_sample_nodes)
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
                    _actuate, store, learner, actuator, primary_target, stabilizer
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


def _retrain_all(store: Store, learner: Learner, planner: NodePlanner) -> None:
    now = datetime.now(UTC)
    for workload in store.workloads():
        recent = store.recent_observations(workload, settings.history_window_steps)
        if recent.is_empty():
            continue
        try:
            learner.retrain(workload, demand_signal(recent), now)
        except Exception:
            # One workload's bad history must not stop the others learning.
            logger.exception("long-memory training failed for %s", workload)
    planner.refit(now)
    _plan_nodes(planner, log=True)


def _plan_nodes(planner: NodePlanner, log: bool = False) -> None:
    try:
        planner.refresh(datetime.now(UTC), log=log)
    except Exception:
        # Node plans are advisory; a failure must not stop the history loop.
        logger.exception("node pool forecast failed")


async def _history_loop(store: Store, learner: Learner, planner: NodePlanner) -> None:
    """Roll observations up into minute history, retrain on schedule, and
    re-plan node pools every minute."""
    await asyncio.to_thread(store.roll_up_minutes, datetime.now(UTC))
    await asyncio.to_thread(_retrain_all, store, learner, planner)
    last_trained = time.monotonic()
    while True:
        await asyncio.sleep(_ROLLUP_EVERY_SECONDS)
        now = datetime.now(UTC)
        await asyncio.to_thread(store.roll_up_minutes, now, now - _ROLLUP_LOOKBACK)
        await asyncio.to_thread(
            store.prune_minutes,
            now - timedelta(days=settings.history_retention_days),
        )
        await asyncio.to_thread(
            store.prune_forecast_log, now - timedelta(days=ACCURACY_DAYS)
        )
        if time.monotonic() - last_trained >= settings.retrain_minutes * 60:
            await asyncio.to_thread(_retrain_all, store, learner, planner)
            last_trained = time.monotonic()
        else:
            await asyncio.to_thread(_plan_nodes, planner)


def _record_background_failure(task: asyncio.Task[None]) -> None:
    if task.cancelled():
        return
    try:
        task.result()
    except Exception as exc:
        logger.exception("background task %s stopped unexpectedly", task.get_name())
        source = app_state.get("source")
        if source is not None:
            source["connected"] = False
            source["last_error"] = f"{task.get_name()} stopped: {exc}"


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    store = Store(settings.db_path)
    learner = Learner(store, settings.long_horizon_minutes, settings.latency_slo_ms)
    planner = NodePlanner(store, learner, settings.long_horizon_minutes)
    app_state["store"] = store
    app_state["learner"] = learner
    app_state["node_planner"] = planner
    app_state["source"] = _init_source_state()

    if settings.mode == "demo":
        await asyncio.to_thread(
            demo_history.backfill, store, WorkloadState().name, datetime.now(UTC)
        )
        source_loop = _simulation_loop(store)
        logger.info("demo simulation loop started")
    else:
        source_loop = _observe_loop(store, learner)
        logger.info(
            "observe loop started for %s/%s",
            settings.k8s_namespace,
            settings.k8s_deployment,
        )
    tasks = [
        asyncio.create_task(source_loop, name="data source"),
        asyncio.create_task(_history_loop(store, learner, planner), name="history"),
    ]
    for task in tasks:
        task.add_done_callback(_record_background_failure)
    app_state["background_tasks"] = tasks

    try:
        yield
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        app_state.pop("background_tasks", None)
        store.close()


app = FastAPI(title="ScaleScope", lifespan=lifespan)


@app.get("/healthz", response_model=None)
def healthz() -> dict[str, str] | JSONResponse:
    """Unauthenticated liveness check - the only route BasicAuthMiddleware exempts.

    503 once a background loop (data source or history) has stopped, having
    failed to start or crashed: nothing restarts it in-process, so the
    kubelet must restart the pod.
    """
    stopped = [
        task.get_name() for task in app_state.get("background_tasks", []) if task.done()
    ]
    if stopped:
        return JSONResponse(
            {"status": f"{', '.join(stopped)} stopped"}, status_code=503
        )
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
