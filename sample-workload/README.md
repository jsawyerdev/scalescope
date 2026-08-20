# sample-workload

A minimal, self-contained CPU-bound HTTP service to deploy into a real
Kubernetes cluster as a test target - something for a real HPA (and later
ScaleScope's OBSERVE mode) to actually scale in response to.

Independent of the ScaleScope app itself (`src/scalescope/`): no shared code,
config, or image.

## What it does

`app/main.py` is a small FastAPI app:

- `GET /` and `GET /healthz` - liveness/readiness, returns `200 ok`.
- `GET /work?iterations=N` - performs `N` rounds of SHA-256 hashing
  (default 200,000) before responding. This is real, bounded CPU work, not
  a sleep, so request volume against it shows up as genuine CPU usage in
  `kubectl top pods` and in the metrics a real HPA (or ScaleScope) reads.
- `GET /metrics` - Prometheus-format request count/latency metrics
  (`prometheus-client`).

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

## Drive load against it

Port-forward the Service locally:

```
kubectl port-forward -n scalescope-demo svc/sample-workload 8080:80
```

Then run the bundled load generator, which ramps concurrency up, holds it
sustained, then drops it back to idle - a pattern with enough shape for an
HPA (or ScaleScope) to react to:

```
sample-workload/scripts/generate-load.sh http://localhost:8080/work 20 120
```

Arguments: target URL, max concurrent requests (default 20), seconds per
ramp/sustained/drop phase (default 120). Set `WORK_ITERATIONS` to change how
much CPU each request burns (default 300000).

Watch it react:

```
kubectl get hpa -n scalescope-demo -w
kubectl top pods -n scalescope-demo
```
