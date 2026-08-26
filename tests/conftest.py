"""Session-wide test environment: point ScaleScope at an isolated DuckDB file.

`scalescope.config.settings` is instantiated once as a module-level singleton,
and `scalescope.api.routes` captures `settings.forecast_horizon_steps` /
`settings.history_window_steps` into module-level constants at import time.
Env vars therefore must be set before `scalescope` is imported anywhere, which
is why this happens at conftest module scope rather than in a fixture.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp(prefix="scalescope-test-")
os.environ["SCALESCOPE_DB_PATH"] = str(Path(_tmp_dir) / "test.duckdb")
# Observe mode avoids the background simulator, so test-controlled inserts are
# the only stored observations once the Kubernetes client initialization fails.
os.environ["SCALESCOPE_MODE"] = "observe"
