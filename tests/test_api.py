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

from scalescope.capacity import CAPACITY_PER_POD_RPS
from scalescope.main import app, app_state
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
    }
    base.update(overrides)
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


def test_source_reports_stale_observe_collection_as_disconnected(
    client: TestClient,
) -> None:
    original_source = dict(app_state["source"])
    app_state["source"].update(
        {
            "mode": "observe",
            "connected": True,
            "last_success_ts": datetime.now(UTC) - timedelta(seconds=120),
            "last_error": None,
        }
    )
    try:
        resp = client.get("/api/source")
    finally:
        app_state["source"].clear()
        app_state["source"].update(original_source)

    assert resp.status_code == 200
    body = resp.json()
    assert body["connected"] is False
    assert "stale" in body["last_error"]


def test_observe_workload_list_prefers_visible_kubernetes_targets(
    client: TestClient, store: Store
) -> None:
    seed_observations(store, "sample-workload", 1)
    seed_observations(store, "scalescope-demo:sample-workload", 1)
    original_source = dict(app_state["source"])
    app_state["source"].update(
        {
            "mode": "observe",
            "targets": [
                {
                    "id": "scalescope-demo:sample-workload",
                    "namespace": "scalescope-demo",
                    "deployment": "sample-workload",
                    "metrics_url_configured": True,
                }
            ],
        }
    )
    try:
        resp = client.get("/api/workloads")
    finally:
        app_state["source"].clear()
        app_state["source"].update(original_source)

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
def test_default_model_is_ewma(
    client: TestClient, store: Store, path_suffix: str
) -> None:
    workload = _workload_name()
    seed_observations(store, workload, 40)

    resp = client.get(f"/api/workloads/{workload}/{path_suffix}")
    assert resp.status_code == 200
    assert resp.json()["model"] == "ewma"


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
    assert body["projected_utilization"] == pytest.approx(
        body["peak_forecast_p90"] / (3 * CAPACITY_PER_POD_RPS)
    )
