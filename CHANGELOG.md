# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions match `pyproject.toml`'s `[project].version`, surfaced at runtime via
`GET /api/source` and shown in the dashboard footer.

## [0.7.1]

### Changed

- Authentication remains available and recommended for exposed deployments,
  but is no longer mandatory when `SCALESCOPE_ACTUATE=true`. Removed the
  startup guard that refused to run without
  `SCALESCOPE_AUTH_USERNAME`/`SCALESCOPE_AUTH_PASSWORD`, because a hard
  refusal was too opinionated for a single-operator trusted-network tool.
  LAN-only operators can deliberately run open/no-auth; ScaleScope now logs
  the existing startup warning and continues.

## [0.7.0]

### Added

- **Authentication**: `SCALESCOPE_AUTH_USERNAME`/`SCALESCOPE_AUTH_PASSWORD`
  gate every route (API and dashboard) behind HTTP Basic Auth, except
  `/healthz` (new, unauthenticated liveness check — the Docker
  `HEALTHCHECK` was moved to it). Closes a gap flagged but not fixed in
  an earlier assurance review this session: every ScaleScope endpoint,
  including the write-capable actuation path, had zero authentication.
  Both env vars unset -> no auth (unchanged default for local demo use).
  `main.py` now refuses to start if `SCALESCOPE_ACTUATE=true` without
  both configured — unauthenticated write access to a real cluster has
  no safe default. `docker-compose.yml`/`.env.example` updated;
  `scripts/rebuild.sh`'s own smoke-test curls authenticate when
  configured, polling the new `/healthz` for readiness instead of a
  data endpoint.
- Dashboard: hover explainers (native `title` tooltips, no layout
  change) on diagnosis, data freshness, mode, actuation, and every
  model-comparison/replay-lab column and model name — the jargon a
  first-time viewer would otherwise have no context for.

## [0.6.2]

### Fixed

- `./scripts/rebuild.sh` crashed immediately with
  `COMPOSE_PROFILE_ARGS[@]: unbound variable` on this machine's shell.
  Root cause, confirmed by checking `bash --version` directly: macOS
  ships bash 3.2 (frozen there for licensing reasons), which treats
  `"${EMPTY_ARRAY[@]}"` as unbound under `set -u`; bash 4.4+ does not.
  Replaced the array with a `compose()` wrapper function that branches
  on `$WITH_OBSERVE` instead of expanding a possibly-empty array.
- `Dockerfile` had a duplicated `&&` in both `apt-get` `RUN` layers
  (`apt-get upgrade -qy && && apt-get install ...`), left over from an
  in-progress edit that added the `upgrade` step; both stages failed to
  build. Fixed the shell syntax.

### Changed

- Base image and toolchain moved to Python 3.14
  (`python:3.14-slim`, `requires-python = ">=3.14,<3.15"`, matching
  `ruff`/`black`/`mypy` target-versions). Verified clean: full
  dependency stack (`lightgbm`, `statsforecast`, `mlforecast`, `duckdb`,
  `pandas`, `scikit-learn`, `statsmodels`, ...) resolves and installs on
  3.14 with no version conflicts, `ruff`/`black --check`/`mypy`/`pytest`
  all pass unchanged, both DEMO and OBSERVE containers rebuilt and
  verified live on the new image.
- `scripts/rebuild.sh`'s local venv creation now tries `python3.14`
  first (was `python3.13`), matching the new `requires-python` floor.

## [0.6.1]

### Documentation

- README deep dive: four new Mermaid diagrams derived directly from the
  code they document, not from a description of it — `diagnosis.py`'s
  full rule ladder as a flowchart (exact branch order, including which
  checks require a 10-row window and which don't), the replay lab's
  anchor/backtest loop, the actuation sequence from `main.py`'s
  `_observe_loop`/`_actuate` through `k8s_actuator.scale`'s HPA-conflict
  check, and the `/trigger` DEMO-vs-OBSERVE branch. Plus a small sequence
  diagram of the dashboard's own fetch cadence (3s main poll, 12s
  other-model refetch, on-demand replay/trigger). New "Diagnosis logic"
  and "Replay lab" top-level sections; no code changes.

## [0.6.0]

### Added

- **Replay lab**: `GET /api/workloads/{name}/replay` backtests every
  registered model against the workload's own recorded history -
  `src/scalescope/replay.py` trains each model only on data strictly
  before a set of past anchor points and measures its forecast error
  (MAE/MAPE) against what actually happened next, so "which model
  performs best here" is a measurement over real stored data rather than
  a stated preference. Dashboard gained a "Replay lab" panel with an
  on-demand "Run replay" button (a full 6-model backtest pass takes ~2s
  against 5000 rows of real history, too slow for the 3s poll cycle) and
  a results table sorted by measured error.
- Scope note: this backtests against the workload's own subsequent
  observed values, not against a real Kubernetes HPA's decisions - the
  original roadmap wording ("score ML recommendations against actual
  Kubernetes HPA behavior") overstated the near-term scope; corrected in
  the README roadmap section along with a stale KEDA bullet that no
  longer reflects actuation as it was actually built (direct RBAC write,
  not a KEDA external scaler).

## [0.5.5]

### Fixed

- No Kubernetes API call anywhere in the codebase had an explicit timeout
  configured - `kubernetes.client.Configuration.retries` is `None` and
  there is no default socket timeout, confirmed by inspecting the
  installed client at runtime. An unresponsive/partitioned API server
  could hang the calling thread indefinitely. Added
  `K8S_REQUEST_TIMEOUT_SECONDS = 10` in `k8s_collector.py`, passed via
  `_request_timeout` to all 5 call sites across `k8s_collector.py` and
  `k8s_actuator.py` (verified as a real, supported parameter on the
  generated client methods, not guessed). Found during a structured
  assurance review, not previously reported.

## [0.5.4]

### Fixed

- Actuation state (`actuate`/`last_actuation_ts`/`last_actuation_replicas`/
  `last_actuation_error`) was computed by the backend and exposed via
  `GET /api/source` but never read anywhere in the dashboard - a fully
  wired feature with zero UI visibility, found during a cleanup pass.
  Sidebar now shows an "actuation" row (hidden unless `actuate=true` in
  observe mode) with the last write's outcome or the reason it was
  refused.

### Documentation

- Noted a real drift between `sample-workload/k8s/service.yaml`
  (`ClusterIP`) and the actual live demo cluster (`LoadBalancer`, patched
  live for a stable metrics/trigger address) so a future `kubectl apply`
  doesn't silently break connectivity; same note for the HPA (deleted
  live to unblock actuation).
- Documented `SIM_TICK_SECONDS`/`LOG_LEVEL`, read by
  `sample-workload/app/main.py` but previously undocumented.

## [0.5.3]

### Changed

- `--radius-sm`/`--radius`/`--radius-lg` all set to `0`, plus the badge,
  confidence-bar, and freshness-dot elements that had hardcoded `9999px`/
  `50%` values outside those tokens - sharp corners everywhere, no pills,
  no circles.

## [0.5.2]

### Changed

- Palette switched from the 0.5.1 warm-off-white/indigo scheme to navy/
  blue/orange/red - reuses the same verified color values as
  services_to_deploy/consensus-trading-stack's dashboard (dark navy
  sidebar `#102d5c`, blue accent `#2563eb`, orange warning `#e98b2a`, red
  danger `#dc2626`) rather than a separately invented scheme. Sidebar is
  now a solid dark navy panel with light text, distinct from the light
  content area.
- Tightened spacing throughout: smaller paddings/gaps in the sidebar,
  panels, stat cards, and table cells; denser table rows; smaller chart
  height (380px -> 300px) and log panel height (320px -> 260px); base
  font size 14px -> 13px. Radii/shadows/type scale from 0.5.1 kept.

## [0.5.1]

### Changed

- Full dashboard visual redesign toward a "modern clean SaaS" look
  (previous style was explicitly rejected as too flat/analyst-tool). Grounded
  in real production CSS pulled from Linear and Vercel: restrained 4-8px
  border radii (previously 0 everywhere), pill shapes reserved for
  badges/tags only. Warm off-white background and a single indigo accent
  (`#5b5bd6`) replacing the previous cool navy/white scheme. Soft low-opacity
  shadows alongside thin borders instead of flat border-only panels. Real
  Inter/JetBrains Mono loaded via Google Fonts (previously referenced in CSS
  but never actually loaded - silently fell back to system fonts). Added a
  persistent left sidebar for identity/workload-selection/load-trigger
  controls, main content area for evidence/metrics/tables/chart/log.
  No functional or API changes - all existing panels, the model comparison
  table, multi-model chart overlay, raw log, and the load-trigger buttons
  from 0.5.0 are unchanged in behavior, confirmed live after rebuild.

## [0.5.0]

### Added

- **On-demand load triggers**: `POST /api/workloads/{name}/trigger?kind={cpu|memory|traffic}`
  forces a load pattern immediately - DEMO mode drives the local simulator
  directly (`WorkloadSimulator.trigger_fault`); OBSERVE mode proxies to the
  real workload's own new `POST /trigger` endpoint
  (`sample-workload/app/main.py`). Dashboard gained three "Generate load"
  buttons that call this and show a live countdown.

### Fixed

- The documented `kubectl port-forward` convention for reaching a
  workload's `/metrics` from outside the cluster is a foreground/background
  host process with no supervision - it died silently multiple times this
  session, breaking metrics scraping and the new /trigger calls with no
  visible cause until manually noticed. `.env.example` and
  `docker-compose.yml` now recommend a LoadBalancer Service's stable IP
  instead where the cluster can provision one (this cluster already runs
  MetalLB or equivalent - confirmed via other Services with real external
  IPs) - not a fragile local process.

## [0.4.0]

### Added

- **Actuation**: `SCALESCOPE_ACTUATE=true` (opt-in on top of `SCALESCOPE_MODE=observe`)
  makes ScaleScope actually write recommended replica counts to the cluster,
  via `k8s_actuator.py`'s `patch` on the `deployments/scale` subresource
  only. Refuses to write if a `HorizontalPodAutoscaler` already targets the
  same Deployment (two controllers writing the same replica count fight
  each other), surfaced via `GET /api/source`'s new
  `actuate`/`last_actuation_ts`/`last_actuation_replicas`/`last_actuation_error`
  fields rather than failing silently.
- `k8s/rbac/`: added `patch`/`update` on `deployments/scale` and `get`/`list`
  on `horizontalpodautoscalers` to the Role. Renamed the identity
  `scalescope-observer` → `scalescope-actuator` throughout (RBAC object
  names should describe what an identity can actually do).

### Verified

- Against the real cluster: removed `sample-workload`'s HPA, enabled
  actuation, and confirmed via `kubectl`'s own event log
  (`Scaled down replica set ... from 8 to 6`) that ScaleScope's write
  actually changed the Deployment - not self-reported.

## [0.3.1]

### Added

- `docker-compose.yml`: formalized the OBSERVE instance as a real service
  (`scalescope-observe`, opt-in via `--profile observe`) instead of a
  manually-run `docker run` command that only existed in shell history.
  Configurable via `.env` (see `.env.example`) for the namespace/deployment/
  kubeconfig path/metrics URL.
- `scripts/rebuild.sh`: now does a real teardown (`docker compose down`)
  before rebuilding, not just `up -d --force-recreate`. New flags:
  `--observe` (also tear down/rebuild/verify the OBSERVE instance; fails
  fast with setup instructions if no kubeconfig is present rather than
  silently skipping) and `--wipe-data` (drop the DuckDB volume(s) for a
  clean-slate rebuild instead of preserving history across it).

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
