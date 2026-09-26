"""ScaleScope's POST /api/workloads/{name}/trigger in DEMO and OBSERVE modes."""

from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from scalescope.api import routes
from scalescope.simulator import FAULTS
from scalescope.state import app_state

_OBSERVE_TARGET = "scalescope-demo:sample-workload"


def _observe_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        routes,
        "settings",
        replace(
            routes.settings,
            mode="observe",
            k8s_metrics_url="http://sample-workload/metrics",
        ),
    )


class _FakeSimulator:
    def __init__(self) -> None:
        self.state = SimpleNamespace(name="sample-app")
        self.faults: list[tuple[str, int]] = []

    def trigger_fault(self, fault: str, duration_ticks: int) -> None:
        self.faults.append((fault, duration_ticks))


def _demo_simulator(monkeypatch: pytest.MonkeyPatch) -> _FakeSimulator:
    simulator = _FakeSimulator()
    monkeypatch.setattr(routes, "settings", replace(routes.settings, mode="demo"))
    monkeypatch.setitem(app_state, "simulator", simulator)
    return simulator


def test_demo_trigger_kinds_map_to_simulator_faults() -> None:
    assert set(routes._DEMO_FAULT_MAP.values()) <= set(FAULTS)


def test_demo_trigger_drives_the_simulator(monkeypatch: pytest.MonkeyPatch) -> None:
    simulator = _demo_simulator(monkeypatch)

    result = routes.trigger_fault("sample-app", kind="memory", duration_seconds=10)

    assert simulator.faults == [
        ("memory_leak", round(10 / routes.settings.simulation_tick_seconds))
    ]
    assert result["target"] == "demo simulator"


def test_demo_trigger_rejects_workload_the_simulator_does_not_drive(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    simulator = _demo_simulator(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault("not-a-workload", kind="cpu", duration_seconds=10)

    assert exc_info.value.status_code == 501
    assert "sample-app" in exc_info.value.detail
    assert simulator.faults == []


def test_demo_rejects_observe_only_stress(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(routes, "settings", replace(routes.settings, mode="demo"))

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault("sample-app", kind="stress", duration_seconds=5)

    assert exc_info.value.status_code == 501
    assert exc_info.value.detail == "stress trigger is only supported in OBSERVE mode"


def test_observe_proxies_stress_trigger(
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

    _observe_settings(monkeypatch)
    monkeypatch.setattr(routes.httpx, "post", fake_post)

    result = routes.trigger_fault(_OBSERVE_TARGET, kind="stress", duration_seconds=5)

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


def test_observe_rejects_trigger_for_unconfigured_target(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _observe_settings(monkeypatch)

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault("other-ns:other-api", kind="stress", duration_seconds=5)

    assert exc_info.value.status_code == 501
    assert "no trigger route is configured" in exc_info.value.detail


def test_observe_rejects_invalid_trigger_json(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            raise json.JSONDecodeError("bad json", "", 0)

    def fake_post(url: str, params: dict[str, object], timeout: float) -> FakeResponse:
        return FakeResponse()

    _observe_settings(monkeypatch)
    monkeypatch.setattr(routes.httpx, "post", fake_post)

    with pytest.raises(HTTPException) as exc_info:
        routes.trigger_fault(_OBSERVE_TARGET, kind="stress", duration_seconds=5)

    assert exc_info.value.status_code == 502
    assert "invalid JSON" in exc_info.value.detail


def test_observe_response_cannot_override_scalescope_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        def raise_for_status(self) -> None:
            pass

        def json(self) -> dict[str, object]:
            return {"target": "http://elsewhere", "kind": "cpu", "phase": "x"}

    def fake_post(url: str, params: dict[str, object], timeout: float) -> FakeResponse:
        return FakeResponse()

    _observe_settings(monkeypatch)
    monkeypatch.setattr(routes.httpx, "post", fake_post)

    result = routes.trigger_fault(_OBSERVE_TARGET, kind="stress", duration_seconds=5)

    assert result["target"] == "http://sample-workload"
    assert result["kind"] == "stress"
    assert result["phase"] == "x"
