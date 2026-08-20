from __future__ import annotations

import importlib.util
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from scalescope.api import routes


@pytest.fixture(scope="session")
def sample_workload() -> ModuleType:
    _install_prometheus_stub()
    module_path = Path(__file__).parents[1] / "sample-workload" / "app" / "main.py"
    spec = importlib.util.spec_from_file_location("sample_workload_main", module_path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _install_prometheus_stub() -> None:
    try:
        import prometheus_client

        return
    except ModuleNotFoundError:
        pass

    prometheus_client = ModuleType("prometheus_client")

    class Metric:
        def __init__(self, *args: object, **kwargs: object) -> None:
            self.value = 0.0

        def labels(self, *args: object, **kwargs: object) -> Metric:
            return self

        def inc(self) -> None:
            self.value += 1.0

        def observe(self, value: float) -> None:
            self.value = value

        def set(self, value: float) -> None:
            self.value = value

    prometheus_client.CONTENT_TYPE_LATEST = "text/plain; version=0.0.4"
    prometheus_client.Counter = Metric
    prometheus_client.Gauge = Metric
    prometheus_client.Histogram = Metric
    prometheus_client.generate_latest = lambda: b""
    sys.modules["prometheus_client"] = prometheus_client


def test_sample_workload_stress_trigger_uses_stress_runner(
    sample_workload: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    durations: list[int] = []

    class FakeStressRun:
        worker_count = 4

    def fake_start_stress(duration_seconds: int) -> FakeStressRun:
        durations.append(duration_seconds)
        return FakeStressRun()

    monkeypatch.setattr(sample_workload, "_start_stress", fake_start_stress)
    client = TestClient(sample_workload.app)

    response = client.post("/trigger", params={"kind": "stress", "duration_seconds": 5})

    assert response.status_code == 200
    assert response.json() == {
        "kind": "stress",
        "phase": "manual_cpu_stress",
        "description": "saturating 4 CPU worker processes",
        "duration_seconds": 5,
    }
    assert durations == [5]
    assert sample_workload._manual_override is None


def test_sample_workload_pi_computation_is_deterministic(
    sample_workload: ModuleType,
) -> None:
    assert sample_workload._compute_pi_digits(20) == "3.14159265358979323846"


def test_scalescope_demo_rejects_observe_only_stress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "settings", replace(routes.settings, mode="demo"))

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault("payments-api", kind="stress", duration_seconds=5)

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "stress trigger is only supported in OBSERVE mode"


def test_scalescope_observe_proxies_stress_trigger(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, dict[str, object], float]] = []

    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"phase": "manual_cpu_stress", "duration_seconds": 5}

    def fake_post(url: str, params: dict[str, object], timeout: float) -> FakeResponse:
        calls.append((url, params, timeout))
        return FakeResponse()

    monkeypatch.setattr(
        routes,
        "settings",
        replace(
            routes.settings,
            mode="observe",
            k8s_metrics_url="http://sample-workload/metrics",
        ),
    )
    monkeypatch.setattr(routes.httpx, "post", fake_post)

    result = routes.trigger_fault("payments-api", kind="stress", duration_seconds=5)

    assert calls == [
        (
            "http://sample-workload/trigger",
            {"kind": "stress", "duration_seconds": 5},
            5.0,
        )
    ]
    assert result == {
        "workload": "payments-api",
        "kind": "stress",
        "duration_seconds": 5,
        "target": "http://sample-workload",
        "phase": "manual_cpu_stress",
    }
