"""API/integration tests for the FastAPI routes in scalescope.api.routes.

These exercise the app through FastAPI's TestClient (over the real ASGI
lifespan, so the same `Store` instance the routes use in production is what
gets seeded and queried here), rather than unit-testing the pure functions
directly.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from typing import cast

import pytest
from fastapi.testclient import TestClient

from scalescope.main import app, app_state
from scalescope.models.registry import ACTUATION_MODEL
from scalescope.storage import Store

_MODEL_NAMES = [
    "naive",
    "seasonal_naive",
    "ewma",
    "linear_trend",
    "auto_ets",
    "lightgbm_quantile",
]


@pytest.fixture(scope="session")
def client() -> Iterator[TestClient]:
    """One TestClient for the whole session: runs the app's real lifespan."""
    with TestClient(app) as c:
        yield c


@pytest.fixture
def store(client: TestClient) -> Store:
    """The same Store instance the running app's routes read/write."""
    return cast(Store, app_state["store"])


def _workload_name() -> str:
    return f"wl-{uuid.uuid4().hex[:12]}"


def _observation(ts: datetime, workload: str, **overrides: object) -> dict[str, object]:
    base = {
        "ts": ts,
        "workload": workload,
        "replicas": 8,
        "request_rate": 1000.0,
        "cpu_usage_pct": 50.0,
        "cpu_throttled_pct": 0.0,
        "memory_usage_mb": 200.0,
        "latency_p95_ms": 30.0,
        "error_rate": 0.0,
        "pending_pods": 0,
        "restarts": 0,
        "cpu_usage_millicores": 4000.0,
        "cpu_request_millicores": 1000.0,
    }
    base.update(overrides)
    base.setdefault("desired_replicas", base["replicas"])
    return base


def seed_observations(store: Store, workload: str, n: int) -> None:
    """Insert `n` monotonically increasing observation rows for `workload`."""
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(n):
        store.insert_observation(
            _observation(
                start + timedelta(seconds=i),
                workload,
                request_rate=1000.0 + i,
            )
        )


def _isolated_source(monkeypatch: pytest.MonkeyPatch, **fields: object) -> None:
    """Swap in a private copy of app_state["source"] for this test.

    The app's background observe loop keeps a reference to the original dict
    and writes to it concurrently (e.g. its kubeconfig error), so mutating
    that dict in place races with it.
    """
    monkeypatch.setitem(app_state, "source", {**app_state["source"], **fields})


def test_source_reports_stale_observe_collection_as_disconnected(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _isolated_source(
        monkeypatch,
        mode="observe",
        connected=True,
        last_success_ts=datetime.now(UTC) - timedelta(seconds=120),
        last_error=None,
    )

    resp = client.get("/api/source")

    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert "stale" in body["last_error"]


def test_observe_workload_list_prefers_visible_kubernetes_targets(
    client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    seed_observations(store, "sample-workload", 1)
    seed_observations(store, "scalescope-demo:sample-workload", 1)
    _isolated_source(
        monkeypatch,
        mode="observe",
        targets=[
            {
                "id": "scalescope-demo:sample-workload",
                "namespace": "scalescope-demo",
                "deployment": "sample-workload",
                "metrics_url_configured": True,
            }
        ],
    )

    resp = client.get("/api/workloads")

    assert resp.status_code == 200
    assert resp.json() == ["scalescope-demo:sample-workload"]


# --- unknown workload -> 404 -------------------------------------------------

_WORKLOAD_ENDPOINTS = [
    "/api/workloads/{w}/observations",
    "/api/workloads/{w}/forecast",
    "/api/workloads/{w}/diagnosis",
    "/api/workloads/{w}/recommendation",
    "/api/workloads/{w}/recommendations",
    "/api/workloads/{w}/replay",
]


@pytest.mark.parametrize("path_template", _WORKLOAD_ENDPOINTS)
def test_unknown_workload_returns_404(client: TestClient, path_template: str) -> None:
    resp = client.get(path_template.format(w=_workload_name()))
    assert resp.status_code == 404
    assert "unknown workload" in resp.json()["detail"]


# --- unknown model -> 400 ----------------------------------------------------


@pytest.mark.parametrize("path_suffix", ["forecast", "recommendation"])
def test_unknown_model_returns_400(
    client: TestClient, store: Store, path_suffix: str
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 40)
    resp = client.get(
        f"/api/workloads/{workload}/{path_suffix}", params={"model": "not-a-model"}
    )
    assert resp.status_code == 400
    assert "unknown model" in resp.json()["detail"]


@pytest.mark.parametrize("path_suffix", ["forecast", "recommendation"])
def test_default_model_is_the_actuation_model(
    client: TestClient, store: Store, path_suffix: str
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 40)

    resp = client.get(f"/api/workloads/{workload}/{path_suffix}")
    assert resp.status_code == 200
    assert resp.json()["model"] == ACTUATION_MODEL == "auto_ets"


# --- limit validation ---------------------------------------------------------


@pytest.mark.parametrize("limit", [-1, 0])
def test_negative_or_zero_limit_returns_422(
    client: TestClient, store: Store, limit: int
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 5)
    resp = client.get(
        f"/api/workloads/{workload}/observations", params={"limit": limit}
    )
    assert resp.status_code == 422


def test_valid_limit_returns_requested_rows(client: TestClient, store: Store) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 20)
    resp = client.get(f"/api/workloads/{workload}/observations", params={"limit": 5})
    assert resp.status_code == 200
    assert len(resp.json()) == 5


def test_limit_over_cap_returns_422(client: TestClient, store: Store) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 5)
    resp = client.get(f"/api/workloads/{workload}/observations", params={"limit": 5001})
    assert resp.status_code == 422


# --- concurrency regression test ----------------------------------------------


def test_concurrent_reads_and_writes_do_not_500(
    client: TestClient, store: Store
) -> None:
    """Regression test for the unsynchronized-connection bug in Store.

    Before the `threading.Lock` fix, concurrent access to the single shared
    `duckdb.Connection` from FastAPI's thread pool (readers) and a writer
    thread intermittently raised `polars.exceptions.ColumnNotFoundError` (or
    other DuckDB errors) because DuckDB connections are not thread-safe.
    """
    workload = _workload_name()
    seed_observations(store, workload, 100)

    stop_writing = False

    def writer() -> None:
        ts = datetime(2026, 6, 1, tzinfo=UTC)
        i = 0
        while not stop_writing:
            store.insert_observation(
                _observation(
                    ts + timedelta(seconds=i), workload, request_rate=500.0 + i
                )
            )
            i += 1

    def reader() -> int:
        resp = client.get(
            f"/api/workloads/{workload}/observations", params={"limit": 50}
        )
        return int(resp.status_code)

    with ThreadPoolExecutor(max_workers=4) as writer_pool:
        write_future = writer_pool.submit(writer)
        try:
            with ThreadPoolExecutor(max_workers=16) as reader_pool:
                futures = [reader_pool.submit(reader) for _ in range(60)]
                statuses = [f.result() for f in as_completed(futures)]
        finally:
            stop_writing = True
            write_future.result()

    assert all(status == 200 for status in statuses)
    assert not any(status >= 500 for status in statuses)


# --- recommendation has no persistence side effect ----------------------------


def test_recommendation_does_not_write_to_database(
    client: TestClient, store: Store
) -> None:
    """There is no recommendations table; GET must not create one or add rows."""
    workload = _workload_name()
    seed_observations(store, workload, 40)

    tables_before = _table_names(store)
    count_before = _row_count(store, workload)

    resp = client.get(f"/api/workloads/{workload}/recommendation")
    assert resp.status_code == 200

    assert _table_names(store) == tables_before
    assert _row_count(store, workload) == count_before


def _table_names(store: Store) -> set[str]:
    with store._lock:
        rows = store._conn.execute("SHOW TABLES").fetchall()
    return {str(row[0]) for row in rows}


def _row_count(store: Store, workload: str) -> int:
    with store._lock:
        row = store._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE workload = ?", [workload]
        ).fetchone()
    assert row is not None
    return int(row[0])


# --- GET /recommendations (plural) --------------------------------------------


def test_all_recommendations_returns_six_consistent_models(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 40)

    resp = client.get(f"/api/workloads/{workload}/recommendations")
    assert resp.status_code == 200
    body = resp.json()

    assert body["workload"] == workload
    assert "diagnosis" in body
    assert "scaling_will_help" in body
    assert "explanation" in body

    models = body["models"]
    assert len(models) == 6
    assert {m["model"] for m in models} == set(_MODEL_NAMES)
    for m in models:
        assert m["workload"] == workload
        assert m["diagnosis"] == body["diagnosis"]
        assert m["scaling_will_help"] == body["scaling_will_help"]
        assert m["explanation"] == body["explanation"]


def test_recommendation_projection_matches_diagnosis_gated_replicas(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(40):
        store.insert_observation(
            _observation(
                start + timedelta(seconds=i),
                workload,
                replicas=3,
                request_rate=1800.0,
                cpu_throttled_pct=12.0,
            )
        )

    resp = client.get(f"/api/workloads/{workload}/recommendation?model=naive")
    assert resp.status_code == 200
    body = resp.json()

    assert body["scaling_will_help"] is False
    assert body["recommended_replicas"] == body["current_replicas"] == 3
    # 1800 req/s over 3 pods at 50% CPU -> 1200 req/s per pod at 100%.
    assert body["capacity_source"] == "estimated"
    assert body["capacity_per_pod"] == pytest.approx(1200.0)
    assert body["projected_utilization"] == pytest.approx(
        body["peak_forecast_p90"] / (3 * 1200.0)
    )


def test_recommendation_without_capacity_signal_keeps_replicas(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(40):
        store.insert_observation(
            _observation(
                start + timedelta(seconds=i),
                workload,
                replicas=4,
                request_rate=5000.0,
                cpu_usage_pct=0.0,
            )
        )

    resp = client.get(f"/api/workloads/{workload}/recommendation?model=naive")
    assert resp.status_code == 200
    body = resp.json()

    assert body["demand_signal"] == "request_rate"
    assert body["capacity_source"] == "unavailable"
    assert body["capacity_per_pod"] is None
    assert body["recommended_replicas"] == body["current_replicas"] == 4
    assert body["projected_utilization"] is None


def test_workload_without_request_metrics_is_forecast_on_total_cpu(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(40):
        store.insert_observation(
            _observation(
                start + timedelta(seconds=i),
                workload,
                replicas=2,
                request_rate=0.0,
                cpu_usage_millicores=3500.0,
                cpu_request_millicores=500.0,
            )
        )

    forecast = client.get(f"/api/workloads/{workload}/forecast").json()
    rec = client.get(f"/api/workloads/{workload}/recommendation?model=naive").json()

    assert forecast["demand_signal"] == "cpu_millicores"
    assert forecast["p50"][0] == pytest.approx(3500.0)
    assert rec["demand_signal"] == "cpu_millicores"
    assert rec["capacity_source"] == "cpu_request"
    assert rec["capacity_per_pod"] == 500.0
    # 3500m at 70% of 500m per pod needs 10 pods; one step is at most +4.
    assert rec["recommended_replicas"] == 6


def test_forecast_cache_is_bounded(
    client: TestClient, store: Store, monkeypatch: pytest.MonkeyPatch
) -> None:
    from scalescope.api import routes

    monkeypatch.setattr(routes, "_FORECAST_CACHE_MAX_ENTRIES", 2)
    for _ in range(4):
        workload = _workload_name()
        seed_observations(store, workload, 5)
        assert client.get(f"/api/workloads/{workload}/forecast").status_code == 200

    assert len(routes._forecast_cache) <= 2


def test_recommendation_explains_itself_for_the_dashboard(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 40)

    source = client.get("/api/source").json()
    rec = client.get(f"/api/workloads/{workload}/recommendation").json()

    assert source["actuation_model"] == rec["model"] == ACTUATION_MODEL
    assert rec["hold_reason"] is None
    assert rec["startup_lead_steps"] > 0
    assert rec["target_utilization"] == pytest.approx(0.7)
    assert len(rec["pods_needed"]) == 30
    assert all(isinstance(n, int) and n >= 1 for n in rec["pods_needed"])


def test_scaling_replay_compares_scalescope_with_a_reactive_hpa(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(300):
        store.insert_observation(
            _observation(
                start + timedelta(seconds=2 * i),
                workload,
                replicas=4,
                request_rate=0.0,
                cpu_usage_millicores=2000.0 + 1500.0 * (i // 100),
                cpu_request_millicores=500.0,
            )
        )

    body = client.get(f"/api/workloads/{workload}/scaling-replay").json()

    assert body["capacity_source"] == "cpu_request"
    assert [o["name"] for o in body["outcomes"]] == ["ScaleScope", "Reactive HPA"]
    for outcome in body["outcomes"]:
        assert 0 <= outcome["under_provisioned_pct"] <= 100
        assert outcome["average_pods"] >= 1


def test_scaling_replay_explains_when_it_cannot_run(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 20)

    body = client.get(f"/api/workloads/{workload}/scaling-replay").json()

    assert body["outcomes"] == []
    assert body["reason"]


def test_recommendation_without_long_memory_is_not_anticipated(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 40)

    rec = client.get(f"/api/workloads/{workload}/recommendation").json()

    assert rec["anticipated"] is False
    assert rec["long_term_peak_p90"] is None


def test_learning_reports_progress_before_a_day_of_history(
    client: TestClient, store: Store
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 40)

    learning = client.get(f"/api/workloads/{workload}/learning").json()

    assert learning["knows_daily_pattern"] is False
    assert learning["forecast"] is None
    assert learning["accuracy"]["scored_minutes"] == 0
    assert learning["retrain_minutes"] > 0


def test_learning_serves_the_day_and_the_forecast_once_trained(
    client: TestClient, store: Store
) -> None:
    import polars as pl

    from scalescope.config import settings
    from scalescope.learning import Learner

    workload = _workload_name()
    seed_observations(store, workload, 40)
    now = datetime.now(UTC)
    last = now.replace(second=0, microsecond=0, tzinfo=None) - timedelta(minutes=1)
    minutes = [last - timedelta(minutes=i) for i in range(3 * 1440)][::-1]
    hour = pl.Series([m.hour + m.minute / 60 for m in minutes])
    demand = 500 + 400 * ((hour - 12) / 12) ** 2
    store.insert_minutes(
        pl.DataFrame(
            {
                "workload": [workload] * len(minutes),
                "minute": minutes,
                "samples": [30] * len(minutes),
                "request_rate": demand,
                "cpu_usage_millicores": demand,
                "replicas": [8.0] * len(minutes),
                "latency_p95_ms": [30.0] * len(minutes),
            }
        )
    )
    learner = cast(Learner, app_state["learner"])
    assert learner.retrain(workload, "request_rate", now) is not None

    learning = client.get(f"/api/workloads/{workload}/learning").json()

    assert learning["knows_daily_pattern"] is True
    assert learning["knows_weekly_pattern"] is False
    assert learning["history_days"] == pytest.approx(3, abs=0.01)
    assert len(learning["history"]["values"]) >= 6 * 60 - 1
    assert len(learning["forecast"]["p90"]) == settings.long_horizon_minutes


def test_nodes_explain_why_there_is_nothing_to_show(client: TestClient) -> None:
    from scalescope.nodes import NodePlanner

    cast(NodePlanner, app_state["node_planner"]).refresh(datetime.now(UTC))

    nodes = client.get("/api/nodes").json()

    assert nodes["available"] is False
    assert "No node data yet" in nodes["reason"]


def test_nodes_serve_each_pool_with_its_history(
    client: TestClient, store: Store
) -> None:
    import polars as pl

    from scalescope.nodes import NodePlanner
    from scalescope.storage import NODE_MINUTE_COLUMNS

    pool = f"pool-{uuid.uuid4().hex[:8]}"
    now = datetime.now(UTC).replace(second=0, microsecond=0, tzinfo=None)
    store.insert_node_minutes(
        pl.DataFrame(
            [
                {
                    "pool": pool,
                    "minute": now - timedelta(minutes=i),
                    "nodes": 3,
                    "allocatable_cpu_millicores": 12000.0,
                    "allocatable_memory_mb": 48000.0,
                    # Fits on 2 of the 3 nodes, even at the lowest packing.
                    "requested_cpu_millicores": 5000.0,
                    "requested_memory_mb": 9000.0,
                    "daemonset_cpu_millicores": 300.0,
                    "daemonset_memory_mb": 600.0,
                    "pending_pods": 0,
                }
                for i in range(90)
            ]
        ).select(NODE_MINUTE_COLUMNS)
    )
    cast(NodePlanner, app_state["node_planner"]).refresh(datetime.now(UTC))

    nodes = client.get("/api/nodes").json()

    served = {p["pool"]: p for p in nodes["pools"]}[pool]
    assert nodes["available"] is True
    assert served["nodes"] == 3
    assert served["nodes_needed_now"] == 2
    assert served["forecast"] is None
    assert len(served["history"]["nodes"]) == 90
    assert served["idle_node_hours_24h"] == pytest.approx(1.5)
