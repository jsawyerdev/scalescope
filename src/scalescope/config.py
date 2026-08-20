"""Static configuration for ScaleScope."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, overridable via environment variables."""

    db_path: str = os.environ.get("SCALESCOPE_DB_PATH", "/data/scalescope.duckdb")
    mode: str = os.environ.get("SCALESCOPE_MODE", "demo")  # demo | observe
    simulation_tick_seconds: float = float(
        os.environ.get("SCALESCOPE_TICK_SECONDS", "2.0")
    )
    forecast_horizon_steps: int = int(os.environ.get("SCALESCOPE_HORIZON_STEPS", "30"))
    history_window_steps: int = int(os.environ.get("SCALESCOPE_HISTORY_STEPS", "600"))
    log_level: str = os.environ.get("SCALESCOPE_LOG_LEVEL", "INFO")
    k8s_namespace: str = os.environ.get("SCALESCOPE_K8S_NAMESPACE", "scalescope-demo")
    k8s_deployment: str = os.environ.get("SCALESCOPE_K8S_DEPLOYMENT", "sample-workload")
    k8s_kubeconfig: str | None = os.environ.get("SCALESCOPE_K8S_KUBECONFIG")
    k8s_metrics_url: str | None = os.environ.get("SCALESCOPE_K8S_METRICS_URL")
    # Opt-in on top of mode=observe: observing a cluster must never imply
    # writing to it by default.
    actuate: bool = os.environ.get("SCALESCOPE_ACTUATE", "false").lower() == "true"


settings = Settings()
