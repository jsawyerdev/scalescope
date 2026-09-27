"""Process-wide runtime state shared by the lifespan loops and the API routes.

Keys, populated by `scalescope.main.lifespan`:

- `"store"`: the `Store` every loop and route reads and writes.
- `"source"`: the dict `GET /api/source` reports (mode, cluster identity,
  connection and actuation status).
- `"simulator"`: the running `WorkloadSimulator` (DEMO mode only).
- `"learner"`: the `Learner` that trains and serves long-memory forecasts.
- `"background_tasks"`: the data-source and history loop tasks; `/healthz`
  fails once any has stopped.
"""

from __future__ import annotations

from typing import Any

app_state: dict[str, Any] = {}
