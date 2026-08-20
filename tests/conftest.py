"""Session-wide test environment: point ScaleScope at an isolated DuckDB file.

`scalescope.config.Settings` reads environment variables at import time (it is
a frozen dataclass instantiated once as a module-level singleton), and
`scalescope.api.routes` in turn captures `settings.forecast_horizon_steps` /
`settings.history_window_steps` into module-level constants at import time
too. Env vars therefore must be set before `scalescope` is imported anywhere,
which is why this happens at conftest module scope rather than in a fixture.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

_tmp_dir = tempfile.mkdtemp(prefix="scalescope-test-")
os.environ["SCALESCOPE_DB_PATH"] = str(Path(_tmp_dir) / "test.duckdb")
# "observe" mode is a no-op data source in main.py's lifespan, so no
# background simulator competes with test-controlled inserts.
os.environ["SCALESCOPE_MODE"] = "observe"
