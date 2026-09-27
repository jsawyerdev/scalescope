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


@pytest.mark.parametrize("tick_seconds", ["0", "-1", "nan", "inf"])
def test_settings_rejects_non_positive_or_non_finite_tick(
    monkeypatch: pytest.MonkeyPatch, tick_seconds: str
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "demo")
    monkeypatch.setenv("SCALESCOPE_TICK_SECONDS", tick_seconds)

    with pytest.raises(ValueError, match="SCALESCOPE_TICK_SECONDS"):
        Settings()


def test_pod_startup_time_sets_both_forecast_leads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "demo")
    monkeypatch.setenv("SCALESCOPE_TICK_SECONDS", "2")
    monkeypatch.setenv("SCALESCOPE_POD_STARTUP_SECONDS", "150")

    settings = Settings()

    assert settings.startup_lead_steps == 75
    assert settings.startup_lead_minutes == 3


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SCALESCOPE_POD_STARTUP_SECONDS", "0"),
        ("SCALESCOPE_POD_STARTUP_SECONDS", "nan"),
        ("SCALESCOPE_LONG_HORIZON_MINUTES", "121"),
        ("SCALESCOPE_HISTORY_RETENTION_DAYS", "-1"),
        ("SCALESCOPE_RETRAIN_MINUTES", "0"),
    ],
)
def test_settings_rejects_invalid_long_memory_settings(
    monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "demo")
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError, match=name):
        Settings()


def test_forecast_horizon_must_cover_twice_the_pod_startup_time(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SCALESCOPE_MODE", "demo")
    # 40 minutes of startup needs an 80-minute horizon; the default is 60.
    monkeypatch.setenv("SCALESCOPE_POD_STARTUP_SECONDS", "2400")

    with pytest.raises(ValueError, match="SCALESCOPE_LONG_HORIZON_MINUTES"):
        Settings()
