"""Process-wide runtime state shared by the lifespan loops and the API routes.

Keys, populated by `scalescope.main.lifespan`:

- `"store"`: the `Store` every loop and route reads and writes.
- `"source"`: the dict `GET /api/source` reports (mode, cluster identity,
  connection and actuation status).
- `"simulator"`: the running `WorkloadSimulator` (DEMO mode only).
- `"data_source_task"`: the simulation or observe loop task; `/healthz`
  fails once it has stopped.
"""

from __future__ import annotations

from typing import Any

app_state: dict[str, Any] = {}
