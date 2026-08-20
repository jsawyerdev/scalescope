# ScaleScope

Explainable predictive Kubernetes capacity intelligence lab. Forecasts near-term
demand for a workload, computes the replica count required to satisfy it, and
runs that alongside a deterministic diagnosis engine that flags when scaling is
the wrong response (CPU limit throttling, memory leak, node capacity exhaustion,
HPA ceiling, non-CPU bottleneck).

## Status: v0.1 (demo mode only)

This is a scaffold, not a finished product. It runs entirely self-contained
against a synthetic workload simulator so it can be evaluated with `docker run`
and no Kubernetes cluster. Real cluster observation (`SCALESCOPE_MODE=observe`
via the Kubernetes API / Prometheus / OpenTelemetry), the online-learning drift
detector (River), foundation-model forecasters (Chronos-2/TimesFM via Darts),
the model leaderboard, the replay lab, and KEDA-based actuation are on the
roadmap and not implemented yet — see "Not yet built" below.

## Architecture

```
simulator.py        synthetic workload (reactive-HPA-controlled, fault injection)
        |
        v
   storage.py        DuckDB: observations / forecasts / recommendations
        |
        v
  models/*.py         ForecastModel plugins, common Forecast(p10,p50,p90) interface
        |              - baselines: naive, seasonal_naive, ewma, linear_trend
        |              - auto_ets: Nixtla StatsForecast AutoETS
        |              - lightgbm_quantile: MLForecast + LightGBM quantile regression
        v
  capacity.py         forecast -> required replicas, rate-limited step, confidence
  diagnosis.py         deterministic rule engine (never calls a model)
        |
        v
   api/routes.py      FastAPI REST endpoints
        |
        v
   static/            vanilla HTML/CSS/JS dashboard (Chart.js via CDN, no build step)
```

The forecaster never predicts CPU-per-pod directly, because scaling changes
that signal (adding replicas lowers per-pod CPU, which would make the
forecast look self-correcting). It forecasts `request_rate` — a demand signal
that is not mechanically altered by the replica count — then derives
required capacity from a fixed per-pod throughput assumption.

## Run it

```
docker compose up --build
```

Then open http://localhost:8000. A synthetic workload (`payments-api`) starts
generating observations immediately; the dashboard begins populating within a
few seconds. Data persists in the `scalescope-data` volume across restarts.

Environment variables (see `src/scalescope/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `SCALESCOPE_MODE` | `demo` | `demo` runs the synthetic simulator; `observe` is not implemented yet |
| `SCALESCOPE_TICK_SECONDS` | `2.0` | Simulated seconds between observations |
| `SCALESCOPE_DB_PATH` | `/data/scalescope.duckdb` | DuckDB file path |
| `SCALESCOPE_LOG_LEVEL` | `INFO` | Python logging level |
| `SCALESCOPE_HORIZON_STEPS` | `30` | Forecast horizon, in simulation ticks |
| `SCALESCOPE_HISTORY_STEPS` | `600` | Observation history window fed to models |

To rebuild against the latest dependency versions `pyproject.toml` allows and
produce a fresh Docker image, run `./scripts/rebuild.sh`. It records the
resolved package set to `requirements-lock.txt` and smoke-tests the built
container before exiting 0.

## API

- `GET /api/workloads`
- `GET /api/workloads/{name}/observations?limit=300`
- `GET /api/workloads/{name}/forecast?model={naive|seasonal_naive|ewma|linear_trend|auto_ets|lightgbm_quantile}`
- `GET /api/workloads/{name}/diagnosis`
- `GET /api/workloads/{name}/recommendation?model=...`

## Develop locally

```
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn scalescope.main:app --reload
pytest
ruff check src tests
```

## Not yet built (roadmap, not implemented)

- **Kubernetes API / Prometheus / OpenTelemetry telemetry collector** for
  `SCALESCOPE_MODE=observe` against a real cluster.
- **Online drift detection** (River) to gate forecast confidence on regime change.
- **Foundation-model forecasters** (Chronos-2, TimesFM 2.5 via Darts) and a
  model leaderboard scoring every model on rolling out-of-sample accuracy.
- **Replay lab**: score ML recommendations against actual Kubernetes HPA
  behavior over recorded incident windows.
- **KEDA external-scaler integration** for shadow/control actuation modes
  (this app should never write `spec.replicas` directly — it should emit a
  metric KEDA/HPA consume, per the design doc).
- Multi-workload support (currently one synthetic workload).
