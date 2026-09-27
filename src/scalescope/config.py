"""Runtime configuration, read from `SCALESCOPE_*` environment variables."""

from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from typing import Literal, cast

from scalescope.capacity import (
    DEFAULT_MAX_REPLICAS,
    DEFAULT_MIN_REPLICAS,
    DEFAULT_TARGET_UTILIZATION,
    ScalingPolicy,
)

# Observations older than this are pruned. The forecasters read at most
# SCALESCOPE_HISTORY_STEPS rows and replay at most 5000, so a day covers
# both at any tick of 2s or more.
DEFAULT_RETENTION_HOURS = 24.0
# Minute rollups for the long-memory model: four weekly cycles plus margin.
DEFAULT_HISTORY_RETENTION_DAYS = 35.0
# The long-memory model is trained for horizons up to this.
MAX_LONG_HORIZON_MINUTES = 120

Mode = Literal["demo", "observe"]

_VALID_MODES = frozenset({"demo", "observe"})
_TRUE_VALUES = frozenset({"1", "true", "yes", "on"})
_FALSE_VALUES = frozenset({"0", "false", "no", "off", ""})

# One query per tick, returning requests/s per pod; ScaleScope attributes
# pods to Deployments itself through their label selectors.
DEFAULT_PROMETHEUS_RPS_QUERY = "sum by (namespace, pod) (rate(http_requests_total[2m]))"
# cAdvisor metrics, which Prometheus scrapes from the kubelet in most setups.
DEFAULT_PROMETHEUS_THROTTLING_QUERY = (
    "sum by (namespace, pod) "
    '(rate(container_cpu_cfs_throttled_periods_total{container!=""}[2m]))'
    " / sum by (namespace, pod) "
    '(rate(container_cpu_cfs_periods_total{container!=""}[2m]))'
)
DEFAULT_PROMETHEUS_LATENCY_QUERY = (
    "1000 * histogram_quantile(0.95, sum by (namespace, pod, le) "
    "(rate(http_request_duration_seconds_bucket[2m])))"
)
DEFAULT_PROMETHEUS_ERROR_RATE_QUERY = (
    'sum by (namespace, pod) (rate(http_requests_total{code=~"5.."}[2m]))'
    " / sum by (namespace, pod) (rate(http_requests_total[2m]))"
)


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


def _optional_float_env(name: str) -> float | None:
    raw = _optional_env(name)
    return None if raw is None else _float_env(name, 0.0)


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
    # Requests/s one pod serves at 100% of its CPU request. Unset -> measured
    # per workload from its latency curve or CPU history.
    capacity_per_pod_rps: float | None = field(
        default_factory=lambda: _optional_float_env("SCALESCOPE_CAPACITY_PER_POD_RPS")
    )
    min_replicas: int = field(
        default_factory=lambda: _int_env(
            "SCALESCOPE_MIN_REPLICAS", DEFAULT_MIN_REPLICAS
        )
    )
    max_replicas: int = field(
        default_factory=lambda: _int_env(
            "SCALESCOPE_MAX_REPLICAS", DEFAULT_MAX_REPLICAS
        )
    )
    target_utilization: float = field(
        default_factory=lambda: _float_env(
            "SCALESCOPE_TARGET_UTILIZATION", DEFAULT_TARGET_UTILIZATION
        )
    )
    # 0 disables it: the asymmetric scale-down rule already refuses to drop
    # pods the forecast horizon still needs. A longer window trades pod cost
    # for fewer scale changes, like the HPA's default 300s.
    scale_down_stabilization_seconds: float = field(
        default_factory=lambda: _float_env(
            "SCALESCOPE_SCALE_DOWN_STABILIZATION_SECONDS", 0.0
        )
    )
    # How long a new pod takes to serve traffic, including any node the
    # cluster autoscaler must add first. Pods are requested this far ahead.
    pod_startup_seconds: float = field(
        default_factory=lambda: _float_env("SCALESCOPE_POD_STARTUP_SECONDS", 30.0)
    )
    long_horizon_minutes: int = field(
        default_factory=lambda: _int_env("SCALESCOPE_LONG_HORIZON_MINUTES", 60)
    )
    history_retention_days: float = field(
        default_factory=lambda: _float_env(
            "SCALESCOPE_HISTORY_RETENTION_DAYS", DEFAULT_HISTORY_RETENTION_DAYS
        )
    )
    retrain_minutes: float = field(
        default_factory=lambda: _float_env("SCALESCOPE_RETRAIN_MINUTES", 15.0)
    )
    retention_hours: float = field(
        default_factory=lambda: _float_env(
            "SCALESCOPE_RETENTION_HOURS", DEFAULT_RETENTION_HOURS
        )
    )
    # p95 latency (ms) the latency model sizes pods for. Unset -> twice each
    # workload's own no-load latency.
    latency_slo_ms: float | None = field(
        default_factory=lambda: _optional_float_env("SCALESCOPE_LATENCY_SLO_MS")
    )
    # Optional per-pod signals for OBSERVE mode: one instant query each, whose
    # series carry `namespace` and `pod` labels; an empty query is skipped.
    node_pool_label: str | None = field(
        default_factory=lambda: _optional_env("SCALESCOPE_NODE_POOL_LABEL")
    )
    prometheus_url: str | None = field(
        default_factory=lambda: _optional_env("SCALESCOPE_PROMETHEUS_URL")
    )
    prometheus_rps_query: str = field(
        default_factory=lambda: _env(
            "SCALESCOPE_PROMETHEUS_RPS_QUERY", DEFAULT_PROMETHEUS_RPS_QUERY
        )
    )
    prometheus_throttling_query: str = field(
        default_factory=lambda: _env(
            "SCALESCOPE_PROMETHEUS_THROTTLING_QUERY",
            DEFAULT_PROMETHEUS_THROTTLING_QUERY,
        )
    )
    prometheus_latency_query: str = field(
        default_factory=lambda: _env(
            "SCALESCOPE_PROMETHEUS_LATENCY_QUERY", DEFAULT_PROMETHEUS_LATENCY_QUERY
        )
    )
    prometheus_error_rate_query: str = field(
        default_factory=lambda: _env(
            "SCALESCOPE_PROMETHEUS_ERROR_RATE_QUERY",
            DEFAULT_PROMETHEUS_ERROR_RATE_QUERY,
        )
    )
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
        if self.capacity_per_pod_rps is not None and not (
            math.isfinite(self.capacity_per_pod_rps) and self.capacity_per_pod_rps > 0
        ):
            raise ValueError(
                "SCALESCOPE_CAPACITY_PER_POD_RPS must be a finite number greater than 0"
            )
        if self.latency_slo_ms is not None and not (
            math.isfinite(self.latency_slo_ms) and self.latency_slo_ms > 0
        ):
            raise ValueError(
                "SCALESCOPE_LATENCY_SLO_MS must be a finite number greater than 0"
            )
        if not 1 <= self.min_replicas <= self.max_replicas:
            raise ValueError(
                "SCALESCOPE_MIN_REPLICAS must be at least 1 and no greater than "
                "SCALESCOPE_MAX_REPLICAS"
            )
        if not (
            math.isfinite(self.scale_down_stabilization_seconds)
            and self.scale_down_stabilization_seconds >= 0
        ):
            raise ValueError(
                "SCALESCOPE_SCALE_DOWN_STABILIZATION_SECONDS must be a finite "
                "number of seconds, 0 or more"
            )
        if not (math.isfinite(self.retention_hours) and self.retention_hours > 0):
            raise ValueError(
                "SCALESCOPE_RETENTION_HOURS must be a finite number greater than 0"
            )
        if not (
            math.isfinite(self.pod_startup_seconds) and self.pod_startup_seconds > 0
        ):
            raise ValueError(
                "SCALESCOPE_POD_STARTUP_SECONDS must be a finite number greater than 0"
            )
        if not (
            2 * self.startup_lead_minutes
            <= self.long_horizon_minutes
            <= MAX_LONG_HORIZON_MINUTES
        ):
            raise ValueError(
                "SCALESCOPE_LONG_HORIZON_MINUTES must be between twice the pod "
                f"startup time in minutes and {MAX_LONG_HORIZON_MINUTES}"
            )
        if not (
            math.isfinite(self.history_retention_days)
            and self.history_retention_days > 0
        ):
            raise ValueError(
                "SCALESCOPE_HISTORY_RETENTION_DAYS must be a finite number greater "
                "than 0"
            )
        if not (math.isfinite(self.retrain_minutes) and self.retrain_minutes > 0):
            raise ValueError(
                "SCALESCOPE_RETRAIN_MINUTES must be a finite number greater than 0"
            )
        if not 0 < self.target_utilization <= 1:
            raise ValueError("SCALESCOPE_TARGET_UTILIZATION must be in (0, 1]")
        if bool(self.auth_username) != bool(self.auth_password):
            raise ValueError(
                "SCALESCOPE_AUTH_USERNAME and SCALESCOPE_AUTH_PASSWORD must be set together"
            )

    @property
    def startup_lead_steps(self) -> int:
        """Pod startup time in ticks: how far ahead the short-term forecast looks."""
        return max(
            1, math.ceil(self.pod_startup_seconds / self.simulation_tick_seconds)
        )

    @property
    def startup_lead_minutes(self) -> int:
        """Pod startup time in whole minutes, the long-memory forecast's step."""
        return max(1, math.ceil(self.pod_startup_seconds / 60))

    @property
    def scaling_policy(self) -> ScalingPolicy:
        return ScalingPolicy(
            min_replicas=self.min_replicas,
            max_replicas=self.max_replicas,
            target_utilization=self.target_utilization,
        )


settings = Settings()
