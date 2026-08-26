from __future__ import annotations

import pytest

from scalescope.config import Settings


def test_settings_reads_environment_when_instance_is_created(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "demo")
    monkeypatch.setenv("SCALESCOPE_DB_PATH", "/tmp/scalescope-test.duckdb")
    monkeypatch.setenv("SCALESCOPE_TICK_SECONDS", "3.5")
    monkeypatch.setenv("SCALESCOPE_ACTUATE", "YES")

    settings = Settings()

    assert settings.db_path == "/tmp/scalescope-test.duckdb"
    assert settings.simulation_tick_seconds == 3.5
    assert settings.actuate is True


def test_settings_rejects_unknown_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "sideways")

    with pytest.raises(ValueError, match="SCALESCOPE_MODE"):
        Settings()


def test_settings_rejects_partial_auth_config(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "demo")
    monkeypatch.setenv("SCALESCOPE_AUTH_USERNAME", "operator")
    monkeypatch.delenv("SCALESCOPE_AUTH_PASSWORD", raising=False)

    with pytest.raises(ValueError, match="must be set together"):
        Settings()


def test_settings_rejects_empty_observe_namespace_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "observe")
    monkeypatch.setenv("SCALESCOPE_K8S_NAMESPACES", ",")

    with pytest.raises(ValueError, match="SCALESCOPE_K8S_NAMESPACES"):
        Settings()


def test_settings_rejects_non_positive_numeric_values(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "demo")
    monkeypatch.setenv("SCALESCOPE_HORIZON_STEPS", "0")

    with pytest.raises(ValueError, match="SCALESCOPE_HORIZON_STEPS"):
        Settings()
