from __future__ import annotations

import importlib.util
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any

import numpy as np
import pytest

from scalescope.storage import Store


def _stub_optional_tuning_modules(monkeypatch: pytest.MonkeyPatch) -> None:
    """SMAC3 and ConfigSpace are operator-installed; stub them when absent."""
    names = {
        "ConfigSpace": ["Configuration", "ConfigurationSpace"],
        "ConfigSpace.hyperparameters": [
            "UniformFloatHyperparameter",
            "UniformIntegerHyperparameter",
        ],
        "smac": ["HyperparameterOptimizationFacade", "Scenario"],
    }
    installed = {
        package: importlib.util.find_spec(package) is not None
        for package in ("ConfigSpace", "smac")
    }
    for module_name, attributes in names.items():
        if installed[module_name.split(".")[0]]:
            continue
        module: Any = ModuleType(module_name)
        for attribute in attributes:
            setattr(module, attribute, object)
        monkeypatch.setitem(sys.modules, module_name, module)


def _load_tuner(monkeypatch: pytest.MonkeyPatch) -> Any:
    _stub_optional_tuning_modules(monkeypatch)
    path = Path(__file__).parents[1] / "scripts" / "tune" / "tune_lightgbm.py"
    spec = importlib.util.spec_from_file_location("tune_lightgbm", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_tuner_uses_cpu_demand_when_a_workload_has_no_request_rate(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    db_path = tmp_path / "tune.duckdb"
    store = Store(str(db_path))
    start = datetime(2026, 1, 1, tzinfo=UTC)
    for i in range(5):
        store.insert_observation(
            {
                "ts": start + timedelta(seconds=2 * i),
                "workload": "payments:api",
                "replicas": 2,
                "desired_replicas": 2,
                "request_rate": 0.0,
                "cpu_usage_pct": 50.0,
                "cpu_usage_millicores": 1000.0 + i,
                "cpu_request_millicores": 1000.0,
                "cpu_throttled_pct": 0.0,
                "memory_usage_mb": 100.0,
                "latency_p95_ms": 0.0,
                "error_rate": 0.0,
                "pending_pods": 0,
                "restarts": 0,
            }
        )
    store.close()

    history = _load_tuner(monkeypatch)._load_history(db_path, "payments:api")

    np.testing.assert_array_equal(history, [1000.0, 1001.0, 1002.0, 1003.0, 1004.0])
