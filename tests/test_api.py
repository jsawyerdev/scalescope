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

import pytest
from fastapi.testclient import TestClient

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
    return app_state["store"]


def _workload_name() -> str:
    return f"wl-{uuid.uuid4().hex[:12]}"


def _observation(ts: datetime, workload: str, **overrides: object) -> dict:
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


# --- unknown workload -> 404 -------------------------------------------------

_WORKLOAD_ENDPOINTS = [
    "/api/workloads/{w}/observations",
    "/api/workloads/{w}/forecast",
    "/api/workloads/{w}/diagnosis",
    "/api/workloads/{w}/recommendation",
    "/api/workloads/{w}/recommendations",
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


# --- workload exists but has zero observations -------------------------------
#
# `store.workloads()` is `SELECT DISTINCT workload FROM observations`, and
# every read path (`recent_observations`) filters on the same `workload`
# column with no delete method anywhere in `Store`. So a workload can only
# ever appear in `workloads()` once at least one observation row for it
# exists, which means the "known workload, zero observations" 409 branches
# in forecast/diagnosis/recommendation/recommendations are currently
# unreachable through the real `Store` API. Not tested here per the brief:
# forcing it would require substituting a fake store, which would prove
# nothing about the real integration.


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
        return resp.status_code

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
    return {r[0] for r in rows}


def _row_count(store: Store, workload: str) -> int:
    with store._lock:
        (count,) = store._conn.execute(
            "SELECT COUNT(*) FROM observations WHERE workload = ?", [workload]
        ).fetchone()
    return count


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
