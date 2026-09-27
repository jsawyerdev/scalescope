# sample-workload

A minimal, self-contained CPU-bound HTTP service to deploy into a real
Kubernetes cluster as a test target - something for a real HPA (and
ScaleScope's OBSERVE mode) to actually scale in response to.

Independent of the ScaleScope app itself (`src/scalescope/`): no shared code,
config, or image.

Environment variables (none set in `k8s/deployment.yaml` - all default):
`SIM_TICK_SECONDS` (default `1.0`, seconds between background-simulator
ticks) and `LOG_LEVEL` (default `INFO`).

## What it does

`app/main.py` is a small FastAPI app that generates its own load - no
external traffic required. A background task randomly cycles through
phases (idle, moderate, traffic_spike, memory_leak, error_burst), each
driving real SHA-256-hashing CPU work (not a sleep), a bounded
self-releasing simulated memory leak, and synthetic error injection. This
is what makes the deployment alone - `kubectl apply -f k8s/`, nothing else -
produce a real, varying CPU/memory pattern a Kubernetes HPA reacts to.

Routes:

- `GET /` and `GET /healthz` - liveness/readiness, returns `200 ok`.
- `GET /work?iterations=N` - on-demand extra CPU work (SHA-256 hashing,
  default 200,000 rounds), independent of the background simulator, for
  driving additional load manually if you want to.
- `GET /metrics` - Prometheus-format metrics: standard request
  count/latency, plus `sample_workload_demand_rps`,
  `_latency_p95_ms`, `_error_rate`, `_simulated_fault`, and `_leak_bytes`
  gauges reflecting the background simulator's current state. ScaleScope's
  OBSERVE-mode collector reads the first three when
  `SCALESCOPE_K8S_METRICS_URL` points here (see the main README's "Wiring
  in a real cluster" section).
- `POST /timeline/pause`, `POST /timeline/resume`, and
  `GET /timeline/status` - freeze or resume the background timeline's base
  phase at runtime. Manual `/trigger` overrides still run for their own
  duration while the base timeline remains frozen underneath.
- `POST /trigger?kind={cpu|memory|traffic|stress}&duration_seconds=45` - override
  the background timeline for `duration_seconds`, forcing that load
  pattern immediately. `stress` bypasses the timeline and starts one
  bounded multiprocessing worker repeatedly computing Pi digits until the
  bounded duration expires. This is what ScaleScope's dashboard
  "Load triggers" buttons call in OBSERVE mode (proxied through
  `POST /api/workloads/{name}/trigger` on the ScaleScope side); call it
  directly here for a quick manual check without going through ScaleScope
  at all: `curl -X POST 'http://<service-ip>/trigger?kind=stress&duration_seconds=30'`.
  The manual traffic trigger intentionally exceeds the safe capacity of
  the default 3-replica deployment so a sustained demo produces visible
  scale-up recommendations.

## Deploy

The image is published (amd64/arm64) as
`ghcr.io/jsawyerdev/scalescope-sample-workload`, so no build is needed:

```
kubectl apply -k "https://github.com/jsawyerdev/scalescope//sample-workload/k8s?ref=v0.16.1"
# or, from a clone:
kubectl apply -k sample-workload/k8s
```

To build it yourself instead: `docker build -t <your-registry>/scalescope-sample-workload sample-workload/`,
push it, and point the Deployment at it with a kustomize overlay.

This creates the `scalescope-demo` namespace, a 3-replica Deployment (CPU
request 100m / limit 200m, memory request 64Mi / limit 128Mi - deliberately
tight so sustained load reaches the CPU limit and produces real throttling,
not just clean scale-up), a ClusterIP Service, and an
`autoscaling/v2` HorizontalPodAutoscaler (target 60% CPU utilization,
2-8 replicas) so there's a real HPA baseline to compare against ScaleScope's
own recommendations later.

Requires a metrics-server (or equivalent) in the cluster for the HPA to read
CPU utilization; most clusters, including a standard Talos setup, already
run one.

To let ScaleScope actuate this workload, delete its HPA first
(`kubectl delete hpa sample-workload -n scalescope-demo`): ScaleScope refuses
to compete with another controller. See
[examples/README.md](../examples/README.md) section 7 for connecting it to
ScaleScope.

## Watch it react

Nothing else to run - the background simulator starts producing varying
load as soon as the pods are up:

```
kubectl get hpa -n scalescope-demo -w
kubectl top pods -n scalescope-demo
```

To add extra load on top of the self-generated pattern (optional),
port-forward the Service and run the bundled generator, which ramps
concurrency up, holds it sustained, then drops back to idle:

```
kubectl port-forward -n scalescope-demo svc/sample-workload 8080:80
sample-workload/scripts/generate-load.sh http://localhost:8080/work 20 120
```

Arguments: target URL, max concurrent requests (default 20), seconds per
ramp/sustained/drop phase (default 120). Set `WORK_ITERATIONS` to change how
much CPU each request burns (default 300000).
