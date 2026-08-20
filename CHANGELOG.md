# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions match `pyproject.toml`'s `[project].version`, surfaced at runtime via
`GET /api/source` and shown in the dashboard footer.

## [0.3.0]

### Fixed

- `sample-workload` only ever scaled up, never down. Root cause: each replica
  ran an independently random load simulator, so the cross-replica average
  CPU the HPA and ScaleScope both read barely moved. Replaced with a
  wall-clock-synchronized timeline so all replicas move through the same
  phase together, producing real synchronized idle periods to scale down
  into. Verified live: `SuccessfulRescale ... All metrics below target`
  events now occur, which never happened in 50+ minutes before the fix.
- `sample-workload`'s Deployment used `imagePullPolicy: IfNotPresent` with a
  mutable `:latest` tag, so a node that had already cached an older `:latest`
  silently kept running it after a rebuild+push+rollout-restart — `kubectl`
  reported a successful rollout while every pod stayed on the stale image.
  Switched to `Always`.
- `k8s/rbac/hpa.yaml`'s comment incorrectly claimed HPA CPU utilization is
  computed against the container's limit; it's always the request.

### Changed

- Dashboard palette now matches `services_to_deploy/consensus-trading-stack`'s
  design tokens (brand blue scale, status colors, Inter/JetBrains Mono) so
  operator tools in this environment share one visual language.
- App version (from `pyproject.toml`) is now exposed via `GET /api/source`
  and shown in the dashboard footer.

## [0.2.0]

### Added

- **OBSERVE mode**: `k8s_collector.py` reads a real Deployment's
  replicas/CPU/memory from the Kubernetes API and metrics-server, read-only.
  `request_rate`/`latency_p95_ms`/`error_rate` are scraped from the
  workload's own Prometheus `/metrics` when `SCALESCOPE_K8S_METRICS_URL` is
  configured, else reported as `0.0` rather than fabricated.
- Least-privilege RBAC manifests (`k8s/rbac/`): a namespace-scoped
  `ServiceAccount`/`Role`/`RoleBinding` with only the read verbs OBSERVE mode
  needs, plus a script to render a scoped kubeconfig for it.
- `sample-workload/`: a self-contained FastAPI test target with a background
  load simulator (CPU/memory/error variation) and a real
  `autoscaling/v2` HPA, so OBSERVE mode has something real to watch.
- `GET /api/source`: reports mode, cluster identity, and live connection
  status, so DEMO and OBSERVE are never visually ambiguous.
- `GET /api/workloads/{name}/recommendations` (plural): every registered
  model's recommendation side by side, replacing single-model-only viewing.
- Dashboard: cluster identity panel, multi-model forecast overlay, raw
  observations log panel.
- `## Forecast models` section in the README documenting each model's fit
  characteristics, minimum history, and fallback behavior.

### Fixed (carried over from the code review that gated this release)

- `storage.py`'s shared `duckdb.Connection` was accessed from multiple
  threads without synchronization (FastAPI's thread pool for sync route
  handlers, concurrent with the simulation/observe loop), intermittently
  raising `polars.exceptions.ColumnNotFoundError` under load. Fixed with a
  `threading.Lock`.
- Negative/zero `limit` query params crashed with a raw 500 or returned a
  misleading 404; now validated via FastAPI `Query(ge=1, le=5000)` with
  workload-existence (404) separated from empty-result (409) states.
- `GET /recommendation` wrote a row to the database on every call — a GET
  with a side effect, called every 3s by the dashboard indefinitely. Removed
  the write; the now-dead `recommendations` table and its insert method were
  removed with it.
- `MAX_REPLICAS` was independently defined in three places
  (`simulator.py`, `capacity.py`, and an unused `diagnosis.py` default
  parameter); `diagnosis.py` now imports `capacity.MAX_REPLICAS`.

## [0.1.0]

Initial scaffold. DEMO-mode only: a synthetic Kubernetes workload simulator
(reactive-HPA-controlled, with injected faults), six pluggable forecast
models behind a common interface (naive/seasonal_naive/ewma/linear_trend
baselines, `AutoETS` via Nixtla StatsForecast, LightGBM quantile regression
via MLForecast), a deterministic diagnosis engine that never calls a model,
a FastAPI backend, a vanilla HTML/CSS/JS dashboard, and a multi-stage
Docker build. `mypy`, an API/concurrency integration test suite, and the
first analyst-grade dashboard redesign landed within this version before
OBSERVE mode existed.
