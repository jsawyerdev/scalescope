# Deploying ScaleScope

ScaleScope is **advisory by default**: it reads the cluster and recommends pod
counts. It changes a Deployment only when you turn on actuation (step 5).

- [1. Before you start](#1-before-you-start)
- [2. Install](#2-install)
- [3. Open the dashboard](#3-open-the-dashboard)
- [4. Configure](#4-configure)
- [5. Turn on autoscaling](#5-turn-on-autoscaling-optional)
- [6. Expose the dashboard safely](#6-expose-the-dashboard-safely)
- [7. Try it with the sample workload](#7-try-it-with-the-sample-workload)
- [8. Run it locally instead](#8-run-it-locally-instead)
- [9. Upgrade and uninstall](#9-upgrade-and-uninstall)
- [10. Troubleshooting](#10-troubleshooting)

## 1. Before you start

- `kubectl` 1.27 or newer (for `apply -k` with remote sources), with a
  context that may create a namespace and cluster-wide RBAC.
- **metrics-server** in the cluster. Most managed clusters ship it; check with:

  ```sh
  kubectl top pods -A | head
  ```

- **CPU requests** on the Deployments you want recommendations for. Without
  a request rate from Prometheus or the workload itself, ScaleScope sizes
  pods against their CPU request.
- The images are public, multi-arch (amd64/arm64), and need no pull secret:
  `ghcr.io/jsawyerdev/scalescope` and `ghcr.io/jsawyerdev/scalescope-sample-workload`.

## 2. Install

Straight from GitHub, no clone needed:

```sh
kubectl apply -k "https://github.com/jsawyerdev/scalescope//k8s/scalescope?ref=v0.16.1"
kubectl -n scalescope-system rollout status deployment/scalescope
```

Or from a clone: `kubectl apply -k k8s/scalescope`.

This creates the `scalescope-system` namespace, a ServiceAccount with
**read-only** cluster-wide access to Deployments, Pods, and pod metrics, a
1Gi volume for history, the Deployment (one replica), and a `ClusterIP`
Service. It observes every Deployment the ServiceAccount can read.

## 3. Open the dashboard

```sh
kubectl -n scalescope-system port-forward svc/scalescope 8000:80
```

Open http://localhost:8000 and pick a workload. From the top:

1. **Status**: green means the current pods cover the forecast, amber means a
   change is recommended or data is slow, red means data stopped or adding
   pods would not fix the problem.
2. **Recommendation**: what the autoscaler would do ("Add 1 pod now: 4 → 5"),
   why, and the numbers behind it. It stays advisory until step 5.
3. **What it has learned**: how much history it has for this workload
   (a day for the daily pattern, a week for the weekly one), how accurate
   its forecasts have been against assuming nothing changes, and the last
   6 hours with the next hour's forecast.
4. **Demand forecast and pods**: recent demand, the forecast, and pods
   running vs needed.

Give each workload a few minutes of history. When demand is a request rate,
ScaleScope first measures how much one pod handles: from the workload's
latency curve once it has seen a range of loads (the footnote then says
"Pod capacity comes from this workload's latency curve"), or from CPU after
at least ten observations with traffic and moderate CPU. Until then it holds
the current pod count and says so. Workloads sized by CPU use the CPU
request at once.

Under **Engineering details → Scaling replay**, "Run scaling replay" replays
the workload's recorded demand through ScaleScope and a reactive HPA and
compares time short of pods, average pods, and scale changes.

Check the connection from the command line:

```sh
curl -fs http://localhost:8000/api/source
curl -fs http://localhost:8000/api/workloads
```

`/api/source` should show `"mode": "observe"` and `"connected": true`.
Workload IDs are `namespace:deployment`.

## 4. Configure

Change settings with `kubectl set env`; the pod restarts with them.

```sh
kubectl -n scalescope-system set env deployment/scalescope \
  SCALESCOPE_PROMETHEUS_URL=http://prometheus-server.monitoring.svc:9090
```

| Setting | What it does |
|---|---|
| `SCALESCOPE_PROMETHEUS_URL` | Read per-pod request rate (used as demand, sharper than CPU), p95 latency (used to size pods), CPU throttling, and error rate. Each query must return series labelled `namespace` and `pod`. The defaults assume `http_requests_total` with a `code` label and an `http_request_duration_seconds` histogram, plus cAdvisor for throttling; change `SCALESCOPE_PROMETHEUS_RPS_QUERY`, `_LATENCY_QUERY`, `_ERROR_RATE_QUERY`, or `_THROTTLING_QUERY` to match your metric names, or set one empty to skip it. |
| `SCALESCOPE_LATENCY_SLO_MS` | The p95 latency (ms) pods are sized to keep. Unset, twice the workload's own no-load latency. |
| `SCALESCOPE_CAPACITY_PER_POD_RPS` | Requests/s one pod serves at 100% of its CPU request, from a load test. Overrides the measured capacity. |
| `SCALESCOPE_POD_STARTUP_SECONDS` | How long a new pod takes to serve traffic (default 30), including a new node if the cluster autoscaler must add one. Set it to what you actually see: ScaleScope starts pods this far ahead of the forecast need, which is where it beats a reactive autoscaler. |
| `SCALESCOPE_MIN_REPLICAS`, `SCALESCOPE_MAX_REPLICAS` | Bounds on every recommendation (default 1 and 30). |
| `SCALESCOPE_TARGET_UTILIZATION` | How full each pod may run (default 0.70). |
| `SCALESCOPE_K8S_NAMESPACES` | Comma-separated namespaces to observe; `*` (the default in the manifest) means all readable ones. |

The full list is in the main README's "Run it" table.

**Narrower visibility.** To limit ScaleScope to some namespaces, replace the
cluster-wide binding with one RoleBinding per namespace:

```sh
kubectl delete clusterrolebinding scalescope-observer
# once per namespace: edit metadata.namespace in the file first
kubectl apply -f examples/k8s/observe-namespace-rolebinding.yaml
kubectl -n scalescope-system set env deployment/scalescope \
  SCALESCOPE_K8S_NAMESPACES=payments,checkout
```

## 5. Turn on autoscaling (optional)

Actuation writes the recommended pod count for **one** Deployment, only when
the diagnosis says more pods would help, and never while a
HorizontalPodAutoscaler manages the same Deployment.

```sh
# write access to deployments/scale (cluster-wide; see below to narrow it)
kubectl apply -f k8s/scalescope-actuation/

# a competing HPA blocks actuation; remove it if there is one
kubectl -n my-namespace get hpa

kubectl -n scalescope-system set env deployment/scalescope \
  SCALESCOPE_K8S_NAMESPACE=my-namespace \
  SCALESCOPE_K8S_DEPLOYMENT=my-deployment \
  SCALESCOPE_ACTUATE=true
kubectl -n scalescope-system rollout status deployment/scalescope
```

The top bar then shows **Autoscaling** with the last change or the reason it
was skipped, and the recommendation footnote says "Autoscaling is on".

To grant write access in one namespace only, apply
`k8s/scalescope-actuation/clusterrole-actuator.yaml` and
`examples/k8s/actuation-namespace-rolebinding.yaml` (edit its namespace)
instead of the whole `k8s/scalescope-actuation/` directory.

Verify the write boundary:

```sh
kubectl auth can-i patch deployments/scale \
  --as=system:serviceaccount:scalescope-system:scalescope -n my-namespace   # yes
kubectl auth can-i delete pods \
  --as=system:serviceaccount:scalescope-system:scalescope -n my-namespace   # no
```

Optional: `SCALESCOPE_SCALE_DOWN_STABILIZATION_SECONDS=300` holds
scale-downs like the Kubernetes HPA does. It is off by default because the
forecast already refuses to remove pods it will need again soon; see the
main README's "Scaling policy" for the measurements.

## 6. Expose the dashboard safely

The Service is `ClusterIP`. Before exposing it through an ingress or load
balancer, turn on Basic Auth (or put it behind your SSO/VPN):

```sh
kubectl -n scalescope-system create secret generic scalescope-auth \
  --from-literal=username="$SCALESCOPE_AUTH_USERNAME" \
  --from-literal=password="$SCALESCOPE_AUTH_PASSWORD"
kubectl -n scalescope-system rollout restart deployment/scalescope
```

Every page and API route then asks for the credentials, except `/healthz`.

## 7. Try it with the sample workload

`sample-workload/` is a small app that generates its own varying load and
exposes request-rate, latency, and error metrics, so you can watch every
part of ScaleScope work.

```sh
kubectl apply -k "https://github.com/jsawyerdev/scalescope//sample-workload/k8s?ref=v0.16.1"

# give ScaleScope the sample's own metrics (request rate, latency, errors)
kubectl -n scalescope-system set env deployment/scalescope \
  SCALESCOPE_K8S_NAMESPACE=scalescope-demo \
  SCALESCOPE_K8S_DEPLOYMENT=sample-workload \
  SCALESCOPE_K8S_METRICS_URL=http://sample-workload.scalescope-demo.svc.cluster.local/metrics
```

Select `scalescope-demo/sample-workload` in the dashboard and use **Load triggers**
("Spike traffic", "Stress CPU") to force a load pattern: the recommendation
should turn to "Add N pods" within a few ticks and back once it ends.

The sample installs its own HPA as a baseline to compare against. Delete it
(`kubectl -n scalescope-demo delete hpa sample-workload`) before turning on
actuation for it.

## 8. Run it locally instead

**Demo, no cluster:**

```sh
docker compose up --build
```

Open http://localhost:8000. A simulated workload (`sample-app`) starts
producing data immediately.

**Local Docker reading a real cluster** (a scoped kubeconfig instead of an
in-cluster ServiceAccount):

```sh
kubectl apply -f k8s/rbac/
OUTPUT_PATH=./scalescope-observer.kubeconfig ./scripts/generate-observer-kubeconfig.sh
cp .env.example .env    # set SCALESCOPE_K8S_* for your cluster
docker compose --profile observe up --build
```

The OBSERVE instance serves on http://localhost:8001. The generated
kubeconfig holds a live token: keep it out of version control (it is
already in `.gitignore`).

## 9. Upgrade and uninstall

Upgrade by applying a newer tag:

```sh
kubectl apply -k "https://github.com/jsawyerdev/scalescope//k8s/scalescope?ref=vX.Y.Z"
```

Uninstall (this also deletes the namespace and its history volume):

```sh
kubectl delete -k "https://github.com/jsawyerdev/scalescope//k8s/scalescope?ref=v0.16.1"
kubectl delete -f k8s/scalescope-actuation/ --ignore-not-found
```

## 10. Troubleshooting

| You see | Cause and fix |
|---|---|
| Red status "Not receiving data from the cluster" | The ServiceAccount cannot list Deployments; check the ClusterRoleBinding (or RoleBindings) and `kubectl -n scalescope-system logs deploy/scalescope`. |
| The pod keeps restarting; `/healthz` returns 503 | The data-source loop stopped, most often because the Kubernetes client could not be created at startup. `kubectl -n scalescope-system logs deploy/scalescope --previous` shows why. |
| A workload is missing from the list | It is outside `SCALESCOPE_K8S_NAMESPACES` or the ServiceAccount's RBAC. |
| "Hold at N pods: how much one pod can handle is not known yet" | No CPU request on the Deployment and no request-rate history to measure from. Add a CPU request, or set `SCALESCOPE_CAPACITY_PER_POD_RPS`. |
| Capacity never comes from the latency curve | No latency per pod (Prometheus latency query returns nothing: check the metric name), or the workload has only run in a narrow band of load per pod, so the curve's bend is not visible. ScaleScope then sizes from CPU. |
| "Learning this workload's daily pattern" | Normal for the first day: the long-memory forecast starts after a day of history, and the weekly pattern after a week. Until then scaling uses the short-term forecast. History survives restarts (it is on the volume). |
| The volume fills up | Minute history takes about 7 MB per workload at the default 35 days; raw observations are kept 24 hours. Lower `SCALESCOPE_HISTORY_RETENTION_DAYS` or give the PVC more space. |
| Scaling replay says "not enough history yet" | It needs about 90 observations (a third to measure capacity, the rest to replay). |
| Demand stays at 0 | metrics-server is missing or cannot be read (logs say `metrics.k8s.io unavailable`); `kubectl top pods` must work. |
| "Scaling will not fix this" | The diagnosis found a cause more pods would not solve (CPU throttling, a probable memory leak, pods stuck pending). The status line says which. |
| Autoscaling shows "refusing to write" | A HorizontalPodAutoscaler targets the Deployment. Delete it or turn actuation off. |
| `ImagePullBackOff` | The cluster cannot reach `ghcr.io`; mirror the image to a reachable registry and set it with a kustomize overlay (`kustomize edit set image ghcr.io/jsawyerdev/scalescope=<your-registry>/scalescope:0.16.1`). |
