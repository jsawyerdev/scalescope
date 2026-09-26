"""Runtime configuration, read from `SCALESCOPE_*` environment variables."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Literal, cast

Mode = Literal["demo", "observe"]

_VALID_MODES = frozenset({"demo", "observe"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default)


def _optional_env(name: str) -> str | None:
    return os.environ.get(name) or None


def _mode_env(name: str, default: Mode) -> Mode:
    value = os.environ.get(name, default)
    if value not in _VALID_MODES:
        raise ValueError(f"{name} must be one of {sorted(_VALID_MODES)}, got {value!r}")
    return cast(Mode, value)


def _float_env(name: str, default: float) -> float:
    raw = os.environ.get(name, str(default))
    try:
        return float(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be a number, got {raw!r}") from exc


def _int_env(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    try:
        return int(raw)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer, got {raw!r}") from exc


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    if normalized in _TRUE_VALUES:
        return True
    if normalized in _FALSE_VALUES:
        return False
    raise ValueError(f"{name} must be true or false, got {raw!r}")


def _csv_env(name: str, default: str) -> tuple[str, ...]:
    values = tuple(part.strip() for part in os.environ.get(name, default).split(","))
    return tuple(value for value in values if value)


def _k8s_namespaces() -> tuple[str, ...]:
    return _csv_env(
        "SCALESCOPE_K8S_NAMESPACES",
        os.environ.get("SCALESCOPE_K8S_NAMESPACE", "scalescope-demo"),
    )


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, overridable via environment variables."""

    db_path: str = field(
        default_factory=lambda: _env("SCALESCOPE_DB_PATH", "/data/scalescope.duckdb")
    )
    mode: Mode = field(default_factory=lambda: _mode_env("SCALESCOPE_MODE", "demo"))
    simulation_tick_seconds: float = field(
        default_factory=lambda: _float_env("SCALESCOPE_TICK_SECONDS", 2.0)
    )
    forecast_horizon_steps: int = field(
        default_factory=lambda: _int_env("SCALESCOPE_HORIZON_STEPS", 30)
    )
    history_window_steps: int = field(
        default_factory=lambda: _int_env("SCALESCOPE_HISTORY_STEPS", 600)
    )
    lightgbm_config_path: str | None = field(
        default_factory=lambda: _optional_env("SCALESCOPE_LIGHTGBM_CONFIG_PATH")
    )
    log_level: str = field(default_factory=lambda: _env("SCALESCOPE_LOG_LEVEL", "INFO"))
    k8s_namespace: str = field(
        default_factory=lambda: _env("SCALESCOPE_K8S_NAMESPACE", "scalescope-demo")
    )
    k8s_deployment: str = field(
        default_factory=lambda: _env("SCALESCOPE_K8S_DEPLOYMENT", "sample-workload")
    )
    k8s_namespaces: tuple[str, ...] = field(default_factory=_k8s_namespaces)
    k8s_kubeconfig: str | None = field(
        default_factory=lambda: _optional_env("SCALESCOPE_K8S_KUBECONFIG")
    )
    k8s_metrics_url: str | None = field(
        default_factory=lambda: _optional_env("SCALESCOPE_K8S_METRICS_URL")
    )
    # Opt-in on top of mode=observe: observing a cluster must never imply
    # writing to it by default.
    actuate: bool = field(default_factory=lambda: _bool_env("SCALESCOPE_ACTUATE"))
    # Both unset -> no auth (default, e.g. local zero-config DEMO). Both set
    # -> HTTP Basic Auth required for every request except /healthz.
    auth_username: str | None = field(
        default_factory=lambda: _optional_env("SCALESCOPE_AUTH_USERNAME")
    )
    auth_password: str | None = field(
        default_factory=lambda: _optional_env("SCALESCOPE_AUTH_PASSWORD")
    )

    def __post_init__(self) -> None:
        if not (
            math.isfinite(self.simulation_tick_seconds)
            and self.simulation_tick_seconds > 0
        ):
            raise ValueError(
                "SCALESCOPE_TICK_SECONDS must be a finite number greater than 0"
            )
        if self.forecast_horizon_steps <= 0:
            raise ValueError("SCALESCOPE_HORIZON_STEPS must be greater than 0")
        if self.history_window_steps <= 0:
            raise ValueError("SCALESCOPE_HISTORY_STEPS must be greater than 0")
        if self.mode == "observe" and not self.k8s_namespaces:
            raise ValueError(
                "SCALESCOPE_K8S_NAMESPACES must not be empty in observe mode"
            )
        if bool(self.auth_username) != bool(self.auth_password):
            raise ValueError(
                "SCALESCOPE_AUTH_USERNAME and SCALESCOPE_AUTH_PASSWORD must be set together"
            )


settings = Settings()
