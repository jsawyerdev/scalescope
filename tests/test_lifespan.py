from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import replace
from pathlib import Path

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

    async def run_lifespan() -> None:
        async with main.lifespan(FastAPI()):
            await asyncio.sleep(0)

    monkeypatch.setattr(main, "settings", replace(main.settings, mode="demo"))
    monkeypatch.setattr(main, "Store", FakeStore)
    monkeypatch.setattr(main, "_simulation_loop", fake_simulation_loop)
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
        task = asyncio.create_task(main._observe_loop(store))
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
