"""Node pools: collection, the forecast of what they need, and the demo pool."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import polars as pl
import pytest

from scalescope import demo_history
from scalescope.k8s_collector import DEFAULT_POOL, node_pool, summarize_nodes
from scalescope.learning import Learner
from scalescope.models.base import Forecast
from scalescope.nodes import (
    DEFAULT_PACKING,
    NodePlanner,
    PodScaling,
    WorkloadDemand,
    fit_pod_scaling,
    forecast_requests,
    idle_node_hours,
    nodes_needed,
    packing_factor,
)
from scalescope.storage import NODE_MINUTE_COLUMNS, Store

MINUTE = datetime(2026, 1, 7, 12, 0, tzinfo=UTC).replace(tzinfo=None)


# ---------- collection ----------


def test_node_pool_reads_the_managed_platform_labels() -> None:
    assert node_pool({"karpenter.sh/nodepool": "spot"}) == "spot"
    assert node_pool({"cloud.google.com/gke-nodepool": "pool-1"}) == "pool-1"
    assert node_pool({"team": "a"}, label="team") == "a"
    assert node_pool({"kubernetes.io/os": "linux"}) == DEFAULT_POOL


def _node(
    name: str,
    pool: str,
    cpu: str = "4",
    memory: str = "16Gi",
    ready: bool = True,
    unschedulable: bool = False,
    control_plane: bool = False,
    startup_seconds: float = 90.0,
) -> SimpleNamespace:
    labels = {"eks.amazonaws.com/nodegroup": pool}
    if control_plane:
        labels["node-role.kubernetes.io/control-plane"] = ""
    created = datetime(2026, 1, 7, 11, 0, tzinfo=UTC)
    return SimpleNamespace(
        metadata=SimpleNamespace(name=name, labels=labels, creation_timestamp=created),
        spec=SimpleNamespace(unschedulable=unschedulable),
        status=SimpleNamespace(
            allocatable={"cpu": cpu, "memory": memory},
            conditions=[
                SimpleNamespace(
                    type="Ready",
                    status="True" if ready else "False",
                    last_transition_time=created + timedelta(seconds=startup_seconds),
                )
            ],
        ),
    )


def _container(cpu: str, memory: str) -> SimpleNamespace:
    return SimpleNamespace(
        resources=SimpleNamespace(requests={"cpu": cpu, "memory": memory})
    )


def _pod(
    node: str | None,
    cpu: str = "500m",
    memory: str = "256Mi",
    phase: str = "Running",
    daemonset: bool = False,
    init: list[SimpleNamespace] | None = None,
    node_selector: dict[str, str] | None = None,
) -> SimpleNamespace:
    owners = [SimpleNamespace(kind="DaemonSet")] if daemonset else []
    return SimpleNamespace(
        metadata=SimpleNamespace(owner_references=owners),
        spec=SimpleNamespace(
            node_name=node,
            containers=[_container(cpu, memory)],
            init_containers=init,
            node_selector=node_selector,
        ),
        status=SimpleNamespace(phase=phase),
    )


def test_summarize_nodes_counts_schedulable_nodes_and_what_their_pods_request() -> None:
    nodes = [
        _node("a1", "apps"),
        _node("a2", "apps"),
        _node("a3", "apps", ready=False),
        _node("a4", "apps", unschedulable=True),
        _node("b1", "batch", cpu="8", memory="32Gi", startup_seconds=7200),
        _node("cp", "apps", control_plane=True),
    ]
    pods = [
        _pod("a1"),
        # An init container larger than the app containers sets the request.
        _pod("a2", init=[_container("2", "1Gi")]),
        _pod("a1", cpu="100m", memory="64Mi", daemonset=True),
        _pod("a3"),  # on a node that is not Ready: not counted
        _pod("cp"),  # on the control plane: not counted
        _pod(
            None,
            phase="Pending",
            node_selector={"eks.amazonaws.com/nodegroup": "batch"},
        ),
        _pod(None, phase="Pending"),  # two pools, no selector: unattributed
    ]

    collected = summarize_nodes(nodes, pods, MINUTE, pool_label=None)

    pools = {row["pool"]: row for row in collected.pools}
    apps, batch = pools["apps"], pools["batch"]
    assert apps["nodes"] == 2
    assert apps["allocatable_cpu_millicores"] == 8000
    assert apps["allocatable_memory_mb"] == 32768
    assert apps["requested_cpu_millicores"] == pytest.approx(500 + 2000 + 100)
    assert apps["requested_memory_mb"] == pytest.approx(256 + 1024 + 64)
    assert apps["daemonset_cpu_millicores"] == pytest.approx(100)
    assert apps["pending_pods"] == 0
    assert batch["pending_pods"] == 1
    assert batch["requested_cpu_millicores"] == pytest.approx(500)
    # A Ready transition two hours after creation is a restart, not a start.
    assert {name for name, *_ in collected.startups} == {"a1", "a2"}
    assert collected.startups[0][3] == pytest.approx(90.0)
    assert collected.pool_by_node["a3"] == "apps"
    assert "cp" not in collected.pool_by_node


def test_summarize_nodes_puts_pending_pods_on_the_only_pool() -> None:
    collected = summarize_nodes(
        [_node("a1", "apps")], [_pod(None, phase="Pending")], MINUTE, None
    )

    assert collected.pools[0]["pending_pods"] == 1
    assert collected.pools[0]["requested_cpu_millicores"] == pytest.approx(500)


# ---------- the arithmetic ----------


def _pool_minute(**overrides: Any) -> dict[str, Any]:
    row: dict[str, Any] = {
        "pool": "apps",
        "minute": MINUTE,
        "nodes": 4,
        "allocatable_cpu_millicores": 4 * 4000.0,
        "allocatable_memory_mb": 4 * 16000.0,
        "requested_cpu_millicores": 10000.0,
        "requested_memory_mb": 20000.0,
        "daemonset_cpu_millicores": 4 * 200.0,
        "daemonset_memory_mb": 4 * 500.0,
        "pending_pods": 0,
    }
    row.update(overrides)
    return row


def test_nodes_needed_leaves_room_for_daemonsets_and_packing() -> None:
    pool = _pool_minute()
    # Each node offers 3800m after its DaemonSets; 9200m of other requests.
    perfect = nodes_needed(np.array([10000.0]), np.array([20000.0]), pool, 1.0)
    packed = nodes_needed(np.array([10000.0]), np.array([20000.0]), pool, 0.7)
    by_memory = nodes_needed(np.array([1000.0]), np.array([47000.0]), pool, 1.0)

    assert perfect.tolist() == [3]  # 9200 / 3800 = 2.4
    assert packed.tolist() == [4]  # 2.4 / 0.7 = 3.5
    assert by_memory.tolist() == [3]  # 45000 / 15500 = 2.9


def _pool_history(nodes: list[int], cpu: list[float]) -> pl.DataFrame:
    return pl.DataFrame(
        [
            _pool_minute(
                minute=MINUTE + timedelta(minutes=i),
                nodes=n,
                allocatable_cpu_millicores=n * 4000.0,
                allocatable_memory_mb=n * 16000.0,
                requested_cpu_millicores=c,
                requested_memory_mb=1000.0,
                daemonset_cpu_millicores=0.0,
                daemonset_memory_mb=0.0,
            )
            for i, (n, c) in enumerate(zip(nodes, cpu, strict=True))
        ]
    )


def test_packing_factor_is_how_full_the_pool_gets_when_busy() -> None:
    # 4 nodes; requests fill them to 50% most of the time, 80% when busy.
    cpu = [8000.0] * 100 + [12800.0] * 20

    assert packing_factor(_pool_history([4] * 120, cpu)) == pytest.approx(0.8)
    assert packing_factor(_pool_history([4] * 10, cpu[:10])) == DEFAULT_PACKING
    # Never below 70%, however idle the pool has been.
    assert packing_factor(_pool_history([4] * 120, [100.0] * 120)) == 0.7


def test_idle_node_hours_counts_nodes_beyond_what_requests_needed() -> None:
    # 60 minutes at 4 nodes whose requests fit on 2 at packing 1.0.
    history = _pool_history([4] * 60, [8000.0] * 60)

    assert idle_node_hours(history, 1.0) == pytest.approx(2.0)


def _minutes(replicas: np.ndarray, demand: np.ndarray) -> pl.DataFrame:
    return pl.DataFrame({"replicas": replicas, "request_rate": demand})


def test_fit_pod_scaling_learns_how_pods_follow_demand() -> None:
    demand = np.linspace(100, 1000, 600)
    # An autoscaler: 100 req/s per pod, never under 3 pods.
    replicas = np.maximum(3, np.ceil(demand / 100))

    fit = fit_pod_scaling(_minutes(replicas, demand), "request_rate")

    assert fit is not None and fit.demand_per_pod is not None
    assert fit.demand_per_pod == pytest.approx(95, rel=0.1)
    assert fit.floor == 3
    assert fit.pods(np.array([50.0, 1500.0, 5000.0])).tolist() == pytest.approx(
        # Never under the floor; never over twice the most pods seen.
        [3, 1500 / fit.demand_per_pod, 20]
    )


def test_fit_pod_scaling_holds_a_fixed_replica_count() -> None:
    fit = fit_pod_scaling(
        _minutes(np.full(600, 5.0), np.linspace(100, 1000, 600)), "request_rate"
    )

    assert fit == PodScaling(None, 5.0, 5.0)
    assert fit is not None and fit.pods(np.array([10_000.0])).tolist() == [5.0]
    assert (
        fit_pod_scaling(_minutes(np.full(10, 5.0), np.ones(10)), "request_rate") is None
    )


def _forecast(p50: list[float], p90: list[float]) -> Forecast:
    return Forecast("seasonal", np.array(p50), np.array(p50), np.array(p90))


def test_forecast_requests_moves_with_each_workloads_pods() -> None:
    pool = _pool_minute(requested_cpu_millicores=10000.0, requested_memory_mb=20000.0)
    growing = WorkloadDemand(
        workload="api",
        cpu_request=500.0,
        memory_request=256.0,
        demand_now=1000.0,
        # 100 req/s per pod: 10 pods now, 20 at the forecast peak.
        forecast=_forecast([1000.0, 1500.0], [1200.0, 2000.0]),
        scaling=PodScaling(100.0, 1.0, 50.0),
    )

    cpu50, cpu90, memory90 = forecast_requests(pool, [growing], horizon=2)

    assert cpu50.tolist() == pytest.approx([10000, 10000 + 5 * 500])
    assert cpu90.tolist() == pytest.approx([10000 + 2 * 500, 10000 + 10 * 500])
    assert memory90.tolist() == pytest.approx([20000 + 2 * 256, 20000 + 10 * 256])
    assert forecast_requests(pool, [], horizon=2)[1].tolist() == [10000, 10000]


# ---------- storage, planner, and demo pool ----------


def test_node_minutes_keep_one_sample_per_minute_and_prune(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "nodes.duckdb"))
    rows = pl.DataFrame([_pool_minute()]).select(NODE_MINUTE_COLUMNS)

    assert store.insert_node_minutes(rows) == 1
    assert store.insert_node_minutes(rows) == 0
    assert store.node_history(MINUTE - timedelta(hours=1)).height == 1
    assert store.node_history(MINUTE, pool="other").is_empty()
    assert store.prune_minutes(MINUTE + timedelta(minutes=1)) == 1

    # The collector's minutes are timezone-aware; they are stored as UTC.
    in_paris = datetime(2026, 1, 7, 13, 0, tzinfo=ZoneInfo("Europe/Paris"))
    store.insert_node_minutes(
        pl.DataFrame([_pool_minute(minute=in_paris)]).select(NODE_MINUTE_COLUMNS)
    )
    assert store.node_history(MINUTE - timedelta(hours=1))["minute"][0] == MINUTE


def test_demo_pool_adds_nodes_late_and_removes_them_slowly() -> None:
    pool = demo_history.DemoNodePool(nodes=3)
    start = datetime(2026, 1, 7, 8, 0, tzinfo=UTC)

    busy = [pool.step(start + timedelta(minutes=i), 12.0) for i in range(4)]
    # Pods do not fit at once: pending until the new node is Ready.
    assert busy[0][0]["pending_pods"] > 0
    assert busy[0][0]["nodes"] == 3
    assert busy[3][0]["nodes"] > 3 and busy[3][0]["pending_pods"] == 0
    assert busy[3][1] or busy[2][1]  # a start-up was recorded

    peak = busy[3][0]["nodes"]
    quiet = [pool.step(start + timedelta(minutes=10 + i), 3.0) for i in range(12)]
    assert quiet[5][0]["nodes"] == peak  # the cluster autoscaler waits
    assert quiet[-1][0]["nodes"] == peak - 1


@pytest.fixture(scope="module")
def demo_store(tmp_path_factory: pytest.TempPathFactory) -> tuple[Store, datetime]:
    """Three weeks of demo history, and the minute it ends."""
    store = Store(str(tmp_path_factory.mktemp("demo") / "demo.duckdb"))
    end = datetime(2026, 1, 7, 7, 0, tzinfo=UTC)  # a Wednesday, before the ramp
    demo_history.backfill(store, "sample-app", end)
    store.insert_observation(
        {
            "ts": end,
            "workload": "sample-app",
            "replicas": 3,
            "desired_replicas": 3,
            "request_rate": 300.0,
            "cpu_usage_pct": 50.0,
            "cpu_throttled_pct": 0.0,
            "memory_usage_mb": 200.0,
            "latency_p95_ms": 30.0,
            "error_rate": 0.0,
            "pending_pods": 0,
            "restarts": 0,
            "cpu_usage_millicores": 1500.0,
            "cpu_request_millicores": demo_history.CPU_REQUEST_MILLICORES,
            "memory_request_mb": demo_history.POD_MEMORY_REQUEST_MB,
            "node_pool": demo_history.DEMO_POOL,
        }
    )
    return store, end


def test_backfill_generates_node_history_alongside_the_workload(
    demo_store: tuple[Store, datetime],
) -> None:
    store, end = demo_store
    history = store.node_history(end - timedelta(days=30), demo_history.DEMO_POOL)

    assert history.height == demo_history.DEMO_HISTORY_DAYS * 1440
    nodes = history["nodes"].to_numpy()
    assert nodes.min() < nodes.max()
    assert store.node_startup_seconds(demo_history.DEMO_POOL, end - timedelta(days=30))


def test_planner_forecasts_the_morning_ramp_and_scores_itself(
    demo_store: tuple[Store, datetime],
) -> None:
    store, end = demo_store
    learner = Learner(store, horizon_minutes=60)
    assert learner.retrain("sample-app", "request_rate", end) is not None
    planner = NodePlanner(store, learner, horizon_minutes=60)
    planner.refit(end)
    planner.refresh(end, log=True)

    plan = planner.plans()[demo_history.DEMO_POOL]
    assert plan.workloads_forecast == ["sample-app"]
    assert len(plan.nodes_needed) == 60
    # 07:00 on a weekday: requests climb into the morning peak.
    assert plan.requested_cpu_p50[-1] > plan.requested_cpu_p50[0]
    assert plan.requested_cpu_p90[-1] >= plan.requested_cpu_p50[-1]
    assert max(plan.nodes_needed) >= plan.nodes_needed_now

    # Once the forecast minutes have happened, the forecast is scored.
    pool = demo_history.DemoNodePool(nodes=int(plan.now["nodes"]))
    rows = [pool.step(end + timedelta(minutes=i), 4.0)[0] for i in range(30)]
    store.insert_node_minutes(pl.DataFrame(rows).select(NODE_MINUTE_COLUMNS))
    accuracy = planner.accuracy(demo_history.DEMO_POOL, end + timedelta(minutes=30))
    assert int(accuracy["minutes"].sum()) == 30
    assert accuracy["model_error"][0] is not None


def test_planner_has_no_plan_without_recent_node_data(tmp_path: Path) -> None:
    store = Store(str(tmp_path / "empty.duckdb"))
    planner = NodePlanner(store, Learner(store, horizon_minutes=60), 60)
    planner.refresh(MINUTE)
    assert planner.plans() == {}

    store.insert_node_minutes(
        pl.DataFrame([_pool_minute()]).select(NODE_MINUTE_COLUMNS)
    )
    planner.refresh(MINUTE + timedelta(hours=1))  # stale: an hour old
    assert planner.plans() == {}

    planner.refresh(MINUTE)
    plan = planner.plans()["apps"]
    assert plan.forecast_start is None  # no workload has a learned pattern
    assert plan.nodes_needed_now == 3
