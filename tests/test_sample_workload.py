from __future__ import annotations

import importlib.util
import json
import sys
from dataclasses import replace
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from scalescope.api import routes


@pytest.fixture(scope="session")
def sample_workload() -> Any:
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
    if importlib.util.find_spec("prometheus_client") is not None:
        return

    prometheus_client: Any = ModuleType("prometheus_client")

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


@pytest.fixture(autouse=True)
def reset_sample_workload_state(sample_workload: Any) -> None:
    sample_workload._manual_override = None
    sample_workload._manual_override_until = 0.0
    with sample_workload._timeline_lock:
        sample_workload._timeline_paused = False
        sample_workload._timeline_paused_since = None
        sample_workload._timeline_paused_phase = None


def test_sample_workload_stress_trigger_uses_stress_runner(
    sample_workload: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    durations: list[int] = []

    class FakeStressRun:
        worker_count = 1

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
        "description": "saturating 1 CPU worker process",
        "duration_seconds": 5,
    }
    assert durations == [5]
    assert sample_workload._manual_override is None


def test_sample_workload_pi_computation_is_deterministic(
    sample_workload: Any,
) -> None:
    assert sample_workload._compute_pi_digits(20) == "3.14159265358979323846"


def test_timeline_pause_freezes_base_phase(sample_workload: Any) -> None:
    assert sample_workload._pause_timeline(now=70.0) == {
        "paused": True,
        "paused_since": "1970-01-01T00:01:10+00:00",
        "phase": "moderate",
    }

    assert sample_workload._current_phase(120.0).name == "moderate"
    assert sample_workload._timeline_status(now=120.0) == {
        "paused": True,
        "paused_since": "1970-01-01T00:01:10+00:00",
        "phase": "moderate",
    }


def test_timeline_resume_returns_to_wall_clock_phase(
    sample_workload: Any,
) -> None:
    sample_workload._pause_timeline(now=70.0)

    status = sample_workload._resume_timeline()

    assert status["paused"] is False
    assert status["paused_since"] is None
    assert sample_workload._current_phase(120.0).name == "traffic_spike"


def test_manual_override_still_wins_while_timeline_paused(
    sample_workload: Any,
) -> None:
    sample_workload._pause_timeline(now=70.0)
    sample_workload._manual_override = sample_workload._TRIGGER_PHASES["cpu"]
    sample_workload._manual_override_until = 200.0

    assert sample_workload._current_phase(120.0).name == "manual_cpu_spike"
    assert sample_workload._current_phase(220.0).name == "moderate"


def test_timeline_endpoints_report_pause_and_resume(
    sample_workload: Any,
) -> None:
    client = TestClient(sample_workload.app)

    pause_response = client.post("/timeline/pause")
    status_response = client.get("/timeline/status")
    resume_response = client.post("/timeline/resume")

    assert pause_response.status_code == 200
    assert pause_response.json()["paused"] is True
    assert status_response.json() == pause_response.json()
    assert resume_response.status_code == 200
    assert resume_response.json()["paused"] is False


def test_scalescope_demo_rejects_observe_only_stress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "settings", replace(routes.settings, mode="demo"))

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault("sample-app", kind="stress", duration_seconds=5)

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

    result = routes.trigger_fault(
        "scalescope-demo:sample-workload", kind="stress", duration_seconds=5
    )

    assert calls == [
        (
            "http://sample-workload/trigger",
            {"kind": "stress", "duration_seconds": 5},
            5.0,
        )
    ]
    assert result == {
        "workload": "scalescope-demo:sample-workload",
        "kind": "stress",
        "duration_seconds": 5,
        "target": "http://sample-workload",
        "phase": "manual_cpu_stress",
    }


def test_scalescope_observe_rejects_trigger_for_unconfigured_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        routes,
        "settings",
        replace(
            routes.settings,
            mode="observe",
            k8s_metrics_url="http://sample-workload/metrics",
        ),
    )

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault("other-ns:other-api", kind="stress", duration_seconds=5)

    assert exc_info.value.status_code == 501
    assert "no trigger route is configured" in exc_info.value.detail


def test_scalescope_observe_rejects_invalid_trigger_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            raise json.JSONDecodeError("bad json", "", 0)

    def fake_post(url: str, params: dict[str, object], timeout: float) -> FakeResponse:
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

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault(
            "scalescope-demo:sample-workload", kind="stress", duration_seconds=5
        )

    assert exc_info.value.status_code == 502
    assert "invalid JSON" in exc_info.value.detail
