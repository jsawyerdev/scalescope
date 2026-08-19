"""Static configuration for ScaleScope."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Settings:
    """Runtime configuration, overridable via environment variables."""

    data_dir: str = os.environ.get("SCALESCOPE_DATA_DIR", "/data")
    db_path: str = os.environ.get("SCALESCOPE_DB_PATH", "/data/scalescope.duckdb")
    mode: str = os.environ.get("SCALESCOPE_MODE", "demo")  # demo | observe (roadmap)
    simulation_tick_seconds: float = float(os.environ.get("SCALESCOPE_TICK_SECONDS", "2.0"))
    forecast_horizon_steps: int = int(os.environ.get("SCALESCOPE_HORIZON_STEPS", "30"))
    history_window_steps: int = int(os.environ.get("SCALESCOPE_HISTORY_STEPS", "600"))
    log_level: str = os.environ.get("SCALESCOPE_LOG_LEVEL", "INFO")


settings = Settings()
