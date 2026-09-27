from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path
from typing import cast

import pytest
from fastapi import FastAPI

from scalescope import main


def test_lifespan_awaits_background_task_before_closing_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    original_state = dict(main.app_state)

    class FakeStore:
        def __init__(self, db_path: str) -> None:
            self.db_path = db_path

        def close(self) -> None:
            events.append("store_closed")

    async def fake_simulation_loop(store: FakeStore) -> None:
        try:
            await asyncio.Future()
        finally:
            events.append("task_cancelled")

    async def fake_history_loop(store: FakeStore, learner: object) -> None:
        await asyncio.Future()

    async def run_lifespan() -> None:
        async with main.lifespan(FastAPI()):
            await asyncio.sleep(0)

    monkeypatch.setattr(main, "settings", replace(main.settings, mode="demo"))
    monkeypatch.setattr(main, "Store", FakeStore)
    monkeypatch.setattr(main, "_simulation_loop", fake_simulation_loop)
    monkeypatch.setattr(main, "_history_loop", fake_history_loop)
    monkeypatch.setattr(main.demo_history, "backfill", lambda *args: 0)
    try:
        asyncio.run(run_lifespan())
    finally:
        main.app_state.clear()
        main.app_state.update(original_state)

    assert events == ["task_cancelled", "store_closed"]


def test_observe_loop_stores_rows_and_reports_partial_failures(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from datetime import UTC, datetime

    from scalescope.k8s_collector import CollectionResult, KubernetesWorkloadTarget
    from scalescope.learning import Learner
    from scalescope.storage import Store

    target = KubernetesWorkloadTarget(namespace="payments", deployment="api")
    row = {
        "ts": datetime.now(UTC),
        "workload": target.workload_id,
        "replicas": 2,
        "desired_replicas": 2,
        "request_rate": 0.0,
        "cpu_usage_pct": 50.0,
        "cpu_usage_millicores": 500.0,
        "cpu_request_millicores": 500.0,
        "cpu_throttled_pct": 0.0,
        "memory_usage_mb": 10.0,
        "latency_p95_ms": 0.0,
        "error_rate": 0.0,
        "pending_pods": 0,
        "restarts": 0,
    }

    class FakeCollector:
        cluster_server = "https://cluster"
        auth_type = "kubeconfig"
        auth_identity = "kubeconfig"

        def __init__(self, **kwargs: object) -> None:
            pass

        def collect(self, namespaces: tuple[str, ...]) -> CollectionResult:
            return CollectionResult(
                targets=[target], rows=[row], errors=["checkout: pods unreachable"]
            )

    store = Store(str(tmp_path / "observe.duckdb"))
    original_state = dict(main.app_state)
    monkeypatch.setattr(
        main, "settings", replace(main.settings, mode="observe", actuate=False)
    )
    monkeypatch.setattr(main, "KubernetesObservationCollector", FakeCollector)
    main.app_state["source"] = main._init_source_state()

    async def one_tick() -> None:
        task = asyncio.create_task(
            main._observe_loop(store, Learner(store, horizon_minutes=60))
        )
        while not main.app_state["source"]["connected"]:
            await asyncio.sleep(0.01)
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task

    try:
        asyncio.run(asyncio.wait_for(one_tick(), timeout=10))
        source = dict(main.app_state["source"])
    finally:
        main.app_state.clear()
        main.app_state.update(original_state)

    assert store.workloads() == [target.workload_id]
    assert source["targets"][0]["id"] == target.workload_id
    assert source["last_error"] == "1 target(s) failed: checkout: pods unreachable"


@pytest.mark.parametrize(
    ("done", "status_code"),
    [([], 200), ([False, False], 200), ([False, True], 503)],
)
def test_healthz_fails_once_a_background_task_stops(
    monkeypatch: pytest.MonkeyPatch, done: list[bool], status_code: int
) -> None:
    from types import SimpleNamespace

    from fastapi.testclient import TestClient

    tasks = [
        SimpleNamespace(done=lambda d=d: d, get_name=lambda i=i: f"task {i}")
        for i, d in enumerate(done)
    ]
    monkeypatch.setitem(main.app_state, "background_tasks", tasks)

    response = TestClient(main.app).get("/healthz")

    assert response.status_code == status_code
    if status_code == 503:
        assert response.json() == {"status": "task 1 stopped"}


def test_one_workloads_training_failure_does_not_stop_the_others(
    tmp_path: Path,
) -> None:
    from datetime import UTC, datetime

    from scalescope.learning import Learner
    from scalescope.storage import Store

    store = Store(str(tmp_path / "retrain.duckdb"))
    for workload in ("broken", "healthy"):
        store.insert_observation(
            {
                "ts": datetime.now(UTC),
                "workload": workload,
                "replicas": 2,
                "desired_replicas": 2,
                "request_rate": 10.0,
                "cpu_usage_pct": 50.0,
                "cpu_usage_millicores": 500.0,
                "cpu_request_millicores": 1000.0,
                "cpu_throttled_pct": 0.0,
                "memory_usage_mb": 10.0,
                "latency_p95_ms": 0.0,
                "error_rate": 0.0,
                "pending_pods": 0,
                "restarts": 0,
            }
        )
    trained: list[str] = []

    class FlakyLearner:
        def retrain(self, workload: str, signal: str, now: datetime) -> None:
            if workload == "broken":
                raise ValueError("bad history")
            trained.append(workload)

    main._retrain_all(store, cast(Learner, FlakyLearner()))

    assert trained == ["healthy"]
