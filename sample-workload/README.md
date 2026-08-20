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
- `POST /trigger?kind={cpu|memory|traffic}&duration_seconds=45` - override
  the background timeline for `duration_seconds`, forcing that load
  pattern immediately. This is what ScaleScope's dashboard "Generate
  load" buttons call in OBSERVE mode (proxied through
  `POST /api/workloads/{name}/trigger` on the ScaleScope side); call it
  directly here for a quick manual check without going through ScaleScope
  at all: `curl -X POST 'http://<service-ip>/trigger?kind=cpu&duration_seconds=30'`.

## Build the image

```
docker build -t scalescope-sample-workload:latest sample-workload/
```

## Load it into a cluster

**Local dev cluster (kind/k3d)** - load the image directly, no registry
needed:

```
kind load docker-image scalescope-sample-workload:latest
# or
k3d image import scalescope-sample-workload:latest
```

**Any other cluster reached via `kubectl`** (a managed cluster, a bare-metal
cluster, a homelab cluster) - kind/k3d's image-load shortcut doesn't apply.
Push the image to a registry that cluster can pull from, then point the
Deployment at it:

```
docker tag scalescope-sample-workload:latest <your-registry>/scalescope-sample-workload:latest
docker push <your-registry>/scalescope-sample-workload:latest
```

Then edit `k8s/deployment.yaml` and replace the placeholder `image:` value
with `<your-registry>/scalescope-sample-workload:latest` (and set
`imagePullPolicy: Always` if using a mutable tag). Any registry your
cluster's nodes can reach works - a cloud registry (ECR/GCR/Docker Hub) or
a self-hosted one already running in your cluster.

## Deploy

Not run as part of building these files - apply manually once the image
reference is set:

```
kubectl apply -f sample-workload/k8s/
```

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

**Live-cluster drift note**: the demo cluster this repo was built against
currently differs from a fresh `kubectl apply` of these manifests - the
Service was patched to `LoadBalancer` (`kubectl patch svc sample-workload
-n scalescope-demo -p '{"spec":{"type":"LoadBalancer"}}'`) for a stable
metrics/`/trigger` address instead of a fragile `kubectl port-forward`,
and the HPA was deleted (`kubectl delete hpa sample-workload -n
scalescope-demo`) so ScaleScope's actuator isn't refused by the
HPA-conflict check - see the main README's "Actuation" section. A fresh
`kubectl apply -f sample-workload/k8s/` restores `ClusterIP` and the HPA,
which will then compete with actuation until the HPA is removed again.

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
