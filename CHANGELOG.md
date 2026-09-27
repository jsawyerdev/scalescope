# Changelog

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).
Versions match `pyproject.toml`'s `[project].version`, surfaced at runtime via
`GET /api/source` and shown in the dashboard footer.

## [Unreleased]

### Fixed

- Docs caught up with 0.16.x: the README status no longer names an old
  version; the dashboard-fetch diagram includes the learning panel's 60 s
  poll; `/healthz` documents the history loop; the forecast-model section
  says DEMO now follows a daily and weekly shape (the measurements still use
  the simulator's built-in cycle); the replay endpoint's ranking and the
  "Autoscaling" top-bar item are described as they are; troubleshooting
  quotes the dashboard's current messages.

## [0.16.1]

### Changed

- Dashboard typography: prose is set in the platform's sans-serif font; the
  bundled DejaVu Sans Mono is kept for values, identifiers, and tables.
  Headings, labels, and table headers are sentence case instead of spaced
  uppercase, on one type scale with one control height.
- The status line says each thing once: its detail appears only when it adds
  something the recommendation does not (a connection error, stale data, a
  diagnosis). "All good" is a neutral banner; amber and red are tinted.
- Plain text instead of badges: the mode reads "Demo" or "Observe", the
  version "v0.16.1". The data-connection item is shown only in Observe mode,
  where it can change. Idle placeholders ("no trigger active", "-") are gone;
  replay panels say "Not run yet."
- "Try it" is now "Load triggers", matching what the buttons do.
- Colour carries meaning only: orange for adding pods and the busy case,
  blue for interaction, selection, and observed/expected data, traffic-light
  colours for status. Neutral decisions and headlines use the text colour.

### Fixed

- Model comparison rows can be selected from the keyboard (Enter or Space)
  and keep focus across the 3-second refresh; every focusable element has a
  visible focus outline.
- Replay buttons are no longer inside their headings.
- While the first long-memory training runs, the learning panel says it is
  training instead of "a day is needed" beside weeks of history.
- The status square aligns with the headline when the detail wraps; values
  in the facts grid line up when a label wraps; mobile chart time labels no
  longer overlap.

## [0.16.0]

### Added

- **Long-memory forecasting.** Each workload's demand is rolled up into
  minute history kept for `SCALESCOPE_HISTORY_RETENTION_DAYS` (35), and a
  LightGBM quantile model (`scalescope.models.seasonal`) learns its daily
  and weekly pattern from up to 28 days of it, retrained every
  `SCALESCOPE_RETRAIN_MINUTES` (15), forecasting the next
  `SCALESCOPE_LONG_HORIZON_MINUTES` (60). It improves with monitoring time:
  on realistic synthetic traffic, next-hour error falls from 9.2% with a
  day of history to 5.9% with three weeks, against 11.1% for assuming
  nothing changes (`scripts/eval_long_memory.py`).
- **Scaling ahead of the pattern.** `SCALESCOPE_POD_STARTUP_SECONDS` (30)
  sets how far ahead pods are started. Scale-ups cover the busiest minute
  either forecast expects within that time; pods the long-memory forecast
  needs back within twice that time are kept. Recommendations report
  `anticipated` when the learned pattern changed the decision.
- **Live accuracy.** Every long-memory forecast is logged and scored against
  what then happened, next to a "same as now" baseline, over the last week.
- **"What it has learned"** dashboard panel and
  `GET /api/workloads/{name}/learning`: history, whether the daily and
  weekly patterns are known, live accuracy, and the last six hours with the
  next hour's forecast.
- The latency curve is refitted on weeks of minute history, covering quiet
  and busy periods, once a day of it exists.
- DEMO mode generates 21 days of history at first start (labelled as such
  in the dashboard) and its simulated traffic follows the same daily and
  weekly shape.
- README: who ScaleScope is for, what it does, and how it learns.

### Changed

- `/healthz` fails when either background loop (data source or history)
  has stopped.
- The pod startup lead is `SCALESCOPE_POD_STARTUP_SECONDS` instead of a
  fixed 15 ticks; the default reproduces it at the default 2s tick.

## [0.15.1]

### Changed

- Every dependency is on its newest stable release that the rest of the
  stack supports, and `pyproject.toml` requires those versions, so a fresh
  install or image build cannot resolve anything older: FastAPI 0.141.1,
  Starlette 1.7.0, uvicorn 0.54.0, DuckDB 1.5.5, polars 1.44.2, NumPy
  2.5.3, LightGBM 4.7.0, statsforecast 2.1.1, mlforecast 1.1.0,
  Kubernetes client 36.0.3, urllib3 2.8.0; pytest 9.1.1, ruff 0.16.9,
  black 26.5.1, mypy 2.3.1. `requirements-lock.txt` pins the full
  resolution CI installs (statsmodels 0.15, SQLAlchemy 2.1, optuna 5 among
  the transitive updates).
- pandas stays on 2.3.3: statsforecast 2.1 and mlforecast 1.1 declare
  `pandas<3`. The forecasts and replay scores were checked identical on
  both pandas lines before choosing the newest forecasting libraries.
- GitHub Actions on their latest majors (checkout v7, setup-python v7,
  Docker login/setup v4, metadata v6, build-push v7), all on Node 24.
- The sample workload requires FastAPI 0.141.1, uvicorn 0.54.0, and
  prometheus-client 0.26.0.

## [0.15.0]

### Added

- Open-source project files: `CONTRIBUTING.md` (setup, checks, layout,
  release process), `SECURITY.md` (private vulnerability reporting and the
  trust boundaries), `CODE_OF_CONDUCT.md` (Contributor Covenant 2.1), issue
  and pull request templates, and `CODEOWNERS`.
- Automated releases: merging a version bump to `main` tags `v<version>`,
  creates the GitHub Release from that version's section of this file
  (`scripts/release_notes.py`), and publishes the `<version>` and `latest`
  images. `tests/test_release.py` fails if the version in `pyproject.toml`,
  this file, the Kubernetes manifests, and the install commands disagree.
- Images carry SBOM and provenance attestations.
- CodeQL scanning (Python, JavaScript, workflows) and weekly Dependabot
  updates for Python, Docker, and GitHub Actions.
- Package metadata: project URLs, classifiers, keywords.

### Changed

- NOTICE lists the vendored Chart.js alongside DejaVu Sans Mono.
- Local development uses `SCALESCOPE_DB_PATH=./scalescope.duckdb` and the
  lock file, matching CI.

### Note

0.14.0 and 0.14.1 were merged but never tagged, so no images were published
for them; 0.15.0 is the first release that includes their changes.

## [0.14.1]

### Fixed

- `/healthz` returns 503 once the data-source loop has stopped (the
  Kubernetes client could not be created at startup, or the loop crashed),
  so the kubelet restarts the pod. It previously kept answering 200, leaving
  a pod that collected nothing and never restarted.
- The memory-leak diagnosis judges "flat" against the workload's demand
  signal. Workloads without request metrics report a request rate of 0,
  which always read as flat, so memory growing with CPU load was diagnosed
  as a leak and blocked actuation.
- Switching workloads clears the replay panels, and responses for a
  workload no longer selected are discarded instead of rendered.
- Baseline forecasts never go below 0, so p10 can no longer exceed p50.
- Several Prometheus series for one pod (one per container, say) are
  combined by each signal's rule; latencies and ratios were summed.
- The sample workload's `/trigger?kind=memory` releases its memory when the
  trigger ends; only the scheduled leak phase did.
- `scripts/tune` tunes on the series the app forecasts (total CPU when a
  workload has no request rate), not always on request rate.

### Changed

- The sample workload image runs Python 3.14, the version it is tested on.
- `starlette` and `urllib3`, imported directly, are declared dependencies.
- Removed stale mypy overrides for packages that ship type information, an
  unused element id, and outdated comments and docstrings.

## [0.14.0]

### Added

- **Performance model** (`scalescope.performance`): each workload's p95
  latency is fitted as a queueing curve, `p95(x) = B + c / (mu - x)` over
  load per pod, and pods are sized so the planned load keeps p95 at
  `SCALESCOPE_LATENCY_SLO_MS` (default: twice the no-load latency). The fit
  is deterministic (grid over `mu`, exact least squares on relative error),
  robust to incidents (least trimmed squares), used only when the data
  identify it, and never plans past the highest load seen meeting the
  target. Capacity resolution is now configured → latency model → CPU
  estimate; recommendations report the model (`latency_model`) and the
  effective `target_utilization`.
- **Scaling replay**: `GET /api/workloads/{name}/scaling-replay` and a
  dashboard panel replay a workload's recorded demand through ScaleScope
  and a reactive HPA (300s scale-down stabilization) with the same capacity
  and pod start-up delay, and report time short of pods, average pods, and
  scale changes. Capacity is taken from the first third of the history only.
- Prometheus now also supplies per-pod CPU throttling (cAdvisor), p95
  latency, and error rate for every workload
  (`SCALESCOPE_PROMETHEUS_THROTTLING_QUERY`, `_LATENCY_QUERY`,
  `_ERROR_RATE_QUERY`; empty skips a signal). Previously throttling was
  always 0 in OBSERVE mode and latency/errors came only from the primary
  target's metrics URL.

### Changed

- The simulator's latency follows the M/M/1 queueing shape, so DEMO mode
  exercises the performance model.
- README: removed the internal "Concurrency and consistency" section, added
  "Performance model" and "Scaling replay" with measured results, and
  rewrote "Known limitations".

## [0.13.0]

### Changed

- The dashboard leads with the answer: a traffic-light status (is anything
  wrong?), a plain-language recommendation ("Add 1 pod now: 14 → 15") with
  its reason and the four numbers behind it, and a demand chart over a pod
  strip (running now vs needed for the busy case). Statistical terms moved to
  tooltips; model comparison, replay, raw data, and cluster identity moved
  under a collapsed "Engineering details". Light theme, straight lines,
  blues with orange for the busy case.
- The dashboard defaults to the model actuation uses (`auto_ets`), from one
  shared registry (`scalescope.models.registry`), so it shows exactly what
  the autoscaler would do; it previously defaulted to `ewma`, which could
  disagree with actuation.
- Recommendations report `hold_reason` (why the count is held),
  `pods_needed` per forecast step, `startup_lead_steps`, and
  `target_utilization`; `/api/source` reports `actuation_model`.
- The chart shows three forecast horizons of history, so the forecast gets a
  readable share of the axis, and no longer refetches every other model's
  forecast every 12 seconds.
- Chart.js 4.5.1 is vendored (byte-identical to the previously pinned CDN
  file), so the dashboard works on clusters without internet access.

## [0.12.0]

### Added

- Deploy to any cluster: `kubectl apply -k k8s/scalescope` installs the
  published multi-arch (amd64/arm64) `ghcr.io/jsawyerdev/scalescope` image;
  `.github/workflows/image.yml` publishes it and the sample workload image,
  and `.github/workflows/ci.yml` runs format, lint, type, test, shellcheck
  and script checks on every pull request.
- Workloads without request metrics are forecast on their **total CPU**
  (metrics-server) and sized against their pods' CPU request, so every
  Deployment gets a recommendation with no Prometheus and no
  instrumentation. Responses report `demand_signal`.
- Optional per-pod request rates from one Prometheus instant query per tick
  (`SCALESCOPE_PROMETHEUS_URL`, `SCALESCOPE_PROMETHEUS_RPS_QUERY`), attributed
  to Deployments through their label selectors.
- Per-pod capacity is configured (`SCALESCOPE_CAPACITY_PER_POD_RPS`),
  estimated per workload from request-rate and CPU history, or the CPU
  request; responses report `capacity_per_pod` and `capacity_source`, and an
  unknown capacity holds replicas instead of guessing.
- `SCALESCOPE_MIN_REPLICAS`, `SCALESCOPE_MAX_REPLICAS`,
  `SCALESCOPE_TARGET_UTILIZATION`, `SCALESCOPE_RETENTION_HOURS`, and
  `SCALESCOPE_SCALE_DOWN_STABILIZATION_SECONDS` (actuation, default 0).
- The replay lab scores the p90 forecast that sizes replicas (pinball loss
  and coverage), ranks models by it, and backtests 10 anchors instead of 5.

### Changed

- Scaling is asymmetric: scale up for the p90 peak within the pod startup
  lead, scale down only when the whole horizon's p90 fits in fewer pods.
  Offline on DEMO demand this cut scale-direction reversals 5-7x for about
  2% more pods (see README "Scaling policy").
- Recommendations and actuation plan from `spec.replicas`; `status.replicas`
  lags a scale write and made actuation repeat a step instead of taking the
  next one.
- Collection lists Pods and PodMetrics once per namespace instead of three
  API calls per Deployment.
- Observations are pruned after 24 hours by default; the forecast cache is
  a bounded LRU; store writes run off the event loop.
- The default minimum replica count is 1 (was a fixed 3), and the simulator's
  220 req/s per pod is no longer used to size real workloads.
- Manifests use the published images instead of a private registry.

### Fixed

- `auto_ets` no longer passes seasonal periods over 24 to StatsForecast's
  ETS, which silently skips every seasonal model above 24 after trying them:
  identical forecasts, about 100x faster (a full recommendation went from
  ~4.5s to ~0.5s).

### Fixed

- HTTP Basic Auth no longer returns a 500 for non-ASCII usernames or
  passwords, and always compares both fields so response timing does not
  reveal whether the username alone was correct.
- Observation timestamps are stored as UTC regardless of the host timezone;
  previously DuckDB converted them to local wall-clock time on non-UTC hosts,
  shifting the API's `ts` values and the dashboard's freshness indicator.
- `seasonal_naive` now repeats the last cycle of the period detected in the
  workload's own history. It previously used a fixed 150-tick lag, half the
  simulator's 300-tick cycle, so it forecast the daily pattern inverted.
- OBSERVE mode drops `NaN`/`Inf` Prometheus gauge values instead of storing
  them, which previously broke every recommendation for that workload.
- OBSERVE mode parses every Kubernetes quantity suffix (`k`, `P`, `E`, `Pi`,
  `Ei`, ...) and reports malformed pod metrics as a per-target collection
  error instead of stopping the observe loop.
- The diagnosis engine measures memory growth per tick (over the window's
  steps, not its row count), matching the `MB/tick` threshold it reports.
- `SCALESCOPE_TICK_SECONDS` must be finite; `nan`/`inf` previously passed
  validation and stopped or stalled the data loop.
- DEMO load triggers reject workloads the simulator does not drive, and
  OBSERVE triggers no longer let the workload's JSON reply overwrite
  ScaleScope's own `workload`/`kind`/`duration_seconds`/`target` fields.
- The dashboard retries its initial workload load after a connection error
  instead of staying blank.
- `scripts/generate-observer-kubeconfig.sh` creates the kubeconfig
  owner-only from the start instead of briefly exposing the token under the
  default umask.
- The dashboard's actuation row is hidden outside actuation mode as its
  tooltip promises (component CSS was overriding the `hidden` attribute),
  and the chart uses the bundled DejaVu Sans Mono font like the rest of the
  page.
- Baseline forecasts from a single observation get the same 1.0 minimum
  band as every other history instead of a zero-width one.

### Changed

- Actuation RBAC grants only `patch` on `deployments/scale`; the unused
  `update` verb was removed.
- The Docker builder stage no longer sets `PYTHONOPTIMIZE`, which made pip
  precompile only bytecode the runtime stage never loads; unused builder-only
  `TZ`/`PYTHONHASHSEED` settings and `setuptools`/`wheel` upgrades were
  removed.
- Removed unreachable code paths: the 409 "no observations yet" responses
  (an unknown workload is exactly one with no observations, still 404),
  unsupported-mode branches, and a dead capacity guard.
- Replay constants are shared between the API and `scripts/tune`, and
  diagnosis window/threshold literals are named constants.
- `scripts/rebuild.sh` runs the configured `mypy` gate alongside formatting,
  lint, and tests.
- `scripts/rebuild.sh` passes Basic Auth credentials to its smoke-test curls
  on stdin instead of the command line, keeping them out of `ps`.

## [0.11.1]

### Fixed

- Runtime settings now read environment variables when each `Settings`
  instance is created, validate `SCALESCOPE_MODE`, require tick/history
  values to be positive, reject an empty OBSERVE namespace scope, and refuse
  partial Basic Auth configuration instead of silently running unauthenticated.
- Recommendation responses now report projected pod load against the replica
  count actually returned to the caller, including low-confidence forecasts and
  diagnosis-gated "do not scale" decisions.
- FastAPI lifespan shutdown now awaits the cancelled data-source task before
  closing DuckDB, avoiding shutdown races between background collection and
  store teardown.
- OBSERVE-mode load triggers now return a 502 when the target workload returns
  invalid or non-object JSON, instead of surfacing a server-side 500.
- AutoETS now falls back to the naive model if the forecasting library returns
  an unexpected frame type, instead of depending on a production `assert`.
- HTTP Basic Auth parsing now accepts case-insensitive auth schemes and rejects
  malformed base64 or missing `username:password` separators explicitly.
- Docker builds include `README.md` in the metadata install layer so package
  `readme` metadata and container builds stay in sync.
- The in-cluster Deployment can now read Basic Auth credentials from an
  optional `scalescope-auth` Secret instead of requiring manifest edits.

### Changed

- Added Apache-2.0 licensing, root `NOTICE` attribution for James Sawyer, and
  package author/license metadata for releasable open-source artifacts.
- Added `examples/` with copy-paste paths for local advisory mode, local
  OBSERVE mode, in-cluster advisory mode, in-cluster actuation, Basic Auth, and
  sustained spike demos, plus namespace-scoped RBAC examples for restricted
  production deployments.
- The README now states the product contract up front: ScaleScope is advisory
  by default and becomes an autoscaler only with OBSERVE mode, explicit
  actuation config, write RBAC, and no competing HPA on the same Deployment.
- Centralized LightGBM hyperparameter JSON validation for the app startup path
  and the SMAC tuning script.
- Added package `readme` metadata and removed obsolete README roadmap language
  that described speculative integrations rather than current ScaleScope
  behaviour.
- The dashboard predictive ramp now shows projected pod load beside the
  selected model's replica and confidence values.
- `scripts/rebuild.sh` now formats the sample workload and tuning script and
  lints the whole repository with `ruff check .`.
- Refreshed `requirements-lock.txt` against the current dependency resolution,
  including `polars==1.44.1`.
- The configured mypy gate now covers the application, tests, sample workload,
  and tuning scripts; `prometheus-client` is included in dev dependencies so
  sample-workload imports are visible during local checks.

## [0.11.0]

### Added

- OBSERVE mode now discovers Deployments across
  `SCALESCOPE_K8S_NAMESPACES` and stores each workload as
  `namespace:deployment`, so duplicate deployment names in different
  namespaces remain selectable and unambiguous in the dashboard.
- Added `k8s/scalescope/` manifests for an in-cluster observe-only install
  with a ServiceAccount, read ClusterRole/ClusterRoleBinding, PVC,
  Deployment, and Service. Optional write permissions live separately in
  `k8s/scalescope-actuation/`.
- Observer RBAC now grants only the Deployment, Pod, and PodMetrics
  `get`/`list` permissions the collector actually uses; actuation write
  permissions stay in their own optional manifests.
- Bundled DejaVu Sans Mono Regular for the dashboard and made it the single
  UI font, so labels, tables, chart legends, and raw metrics render
  consistently without Google Fonts.

### Fixed

- Transient Kubernetes transport failures from the generated client
  (`urllib3` timeouts/connection refusals) are now wrapped as
  availability errors instead of escaping the observe task and silently
  stopping data collection while `/healthz` remains healthy.
- `GET /api/source` now marks OBSERVE mode disconnected when the last
  successful collection timestamp is stale, so dashboards and rebuild
  checks cannot report days-old data as connected.
- Low-confidence forecasts now keep the current replica count instead of
  driving a scale change, preventing zero-confidence model outliers from
  triggering actuation.
- OBSERVE-mode trigger calls now refuse selected workloads that do not have
  a configured trigger/metrics URL instead of sending the request to the
  single configured primary workload by accident.
- The dashboard now opens on the configured metrics-enabled OBSERVE target
  instead of the first alphabetically sorted cluster workload, while still
  preserving explicit user selections.
- Chart labels now keep the selected model visible when a model falls back
  to another forecaster because there is not enough history yet.
- `scripts/rebuild.sh --observe` now polls parsed JSON readiness for both
  DEMO and OBSERVE startup checks, avoiding false startup failures while
  containers are still opening sockets or before the first observe tick.

### Changed

- Dashboard labels and colors were tightened around one neutral/operator
  palette, with clearer workload/source/load-test wording and a duration
  selector for 45-second, 2-minute, and 5-minute load triggers.
- The dashboard and default forecast/recommendation endpoints now start on
  `ewma` and omit low-confidence
  non-selected model overlays from the chart while leaving every model in
  the comparison table.
- sample-workload's Kubernetes manifest now points at the same registry image
  used by the live demo cluster, with docs calling out where to replace it
  for another cluster.
- sample-workload's CPU stress trigger now uses a bounded single-worker
  stressor and 5-second health probe timeouts, so sustained demo spikes show
  pressure without causing liveness flaps under a 200m CPU limit.
- sample-workload's manual traffic trigger now emits enough synthetic demand
  to cross the default 3-replica safe-capacity threshold and make scale-up
  recommendations visible during sustained demos.

## [0.10.0]

### Added

- Added `scripts/tune/tune_periodic.sh`, a bash 3.2-safe cron driver for
  periodic LightGBM SMAC3 re-tuning. It copies the live DuckDB file from a
  running compose service, scores the hardcoded defaults, the currently
  deployed tuned config, and the new SMAC candidate against the current data,
  then atomically promotes and restarts services only when the new candidate
  beats the deployed config.
- `docker-compose.yml` now exposes `scripts/tune/output/` read-only at
  `/tune-output` and passes opt-in
  `SCALESCOPE_LIGHTGBM_CONFIG_PATH="${SCALESCOPE_LIGHTGBM_CONFIG_PATH:-}"` to
  both DEMO and OBSERVE services, so operators can point containers at
  `/tune-output/<workload>.json` after a successful promotion.

### Fixed

- Empty `SCALESCOPE_LIGHTGBM_CONFIG_PATH` values now behave like an unset value,
  preserving the zero-config DEMO path while keeping strict startup failure for
  real configured paths that are missing, malformed, or invalid.

## [0.9.0]

### Added

- LightGBM quantile forecasts can now consume an opt-in tuned hyperparameter
  JSON file via `SCALESCOPE_LIGHTGBM_CONFIG_PATH`, with strict startup
  validation for missing, malformed, or unsupported config values.
- Added `scripts/tune/`, an operator-run SMAC3 tuning lab that minimizes the
  existing replay-lab MAE against recorded workload history and writes the
  winning LightGBM config. `smac` is deliberately not a project dependency:
  it lives only in an isolated tuning venv with `scikit-learn<1.9` (SMAC's
  RandomForest surrogate imports `sklearn.tree._tree.DTYPE`, a private
  internal removed in scikit-learn 1.9.0, which the app needs via
  `mlforecast` - confirmed by a real install attempt, not assumed), outside
  the FastAPI runtime and Docker image.
- Verified live: built the isolated tuning venv for real and ran 30 SMAC
  trials against 5000 real observations copied from the running DEMO
  container (`payments-api`) - default hardcoded hyperparameters scored
  127.27 MAE via the replay lab, the SMAC incumbent found 126.34 (a modest,
  real ~0.7% improvement, not a fabricated headline number). Confirmed the
  opt-in config actually overrides the model's hyperparameters
  (`n_estimators`/`num_leaves`/`min_child_samples`/`learning_rate` =
  193/60/18/0.019 vs the unset-env-var default of 100/15/5/None) by
  constructing `_MODELS` with and without `SCALESCOPE_LIGHTGBM_CONFIG_PATH`
  set and comparing directly, and by running it end-to-end in a throwaway
  container.

## [0.8.0]

### Added

- Forecast models now auto-detect a dominant seasonal period from each
  workload's own request-rate history. AutoETS passes the detected period
  to StatsForecast, and LightGBM adds the detected period as a lag feature
  when the history supports it. Histories without a confident ACF peak keep
  the previous no-seasonality behavior.
- sample-workload now supports an OBSERVE-only `stress` trigger that
  saturates available CPU cores with bounded multiprocessing Pi
  computation, while keeping the FastAPI event loop responsive and ending
  the worker processes at `duration_seconds`.
- sample-workload now exposes `/timeline/pause`, `/timeline/resume`, and
  `/timeline/status` so operators can freeze and observe the base
  background phase progression during controlled tests without breaking
  temporary manual load triggers.

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
