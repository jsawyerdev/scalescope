# ScaleScope Deployment Examples

ScaleScope is advisory by default. It becomes an autoscaler only when OBSERVE
mode, write RBAC, and `SCALESCOPE_ACTUATE=true` are all enabled.

## 1. Local advisory demo

No cluster required.

```sh
./scripts/rebuild.sh
curl -fs http://localhost:8000/healthz
curl -fs http://localhost:8000/api/source
curl -fs http://localhost:8000/api/workloads
curl -fs "http://localhost:8000/api/workloads/sample-app/recommendations"
```

Open http://localhost:8000. The dashboard should show `DEMO`, `no cluster`,
forecast bands, recommendations, diagnosis, and replay results.

## 2. Local OBSERVE advisory mode

This runs ScaleScope in Docker while it reads a real Kubernetes cluster through
a generated kubeconfig. It does not write replica counts unless
`SCALESCOPE_ACTUATE=true`.

```sh
kubectl apply -f sample-workload/k8s/
kubectl apply -f k8s/rbac/
OUTPUT_PATH=./scalescope-observer.kubeconfig ./scripts/generate-observer-kubeconfig.sh
kubectl -n scalescope-demo port-forward svc/sample-workload 18080:80
```

Use these `.env` values for the observe service:

```sh
SCALESCOPE_K8S_NAMESPACE=scalescope-demo
SCALESCOPE_K8S_DEPLOYMENT=sample-workload
SCALESCOPE_K8S_NAMESPACES=scalescope-demo
SCALESCOPE_OBSERVER_KUBECONFIG=./scalescope-observer.kubeconfig
SCALESCOPE_K8S_METRICS_URL=http://host.docker.internal:18080/metrics
SCALESCOPE_ACTUATE=false
```

Then rebuild both local services:

```sh
./scripts/rebuild.sh --observe
curl -fs http://localhost:8001/api/source
curl -fs http://localhost:8001/api/workloads
```

The `/api/source` response should show `mode: observe`, `connected: true`, the
cluster server, the auth identity, and `actuate: false`.

## 3. In-cluster advisory mode

Build an image your cluster can pull, update
`k8s/scalescope/deployment.yaml`, then deploy:

```sh
docker buildx build --platform linux/amd64 \
  -t <your-registry>/scalescope:0.12.0 \
  --push .

kubectl apply -f k8s/scalescope/
kubectl -n scalescope-system rollout status deployment/scalescope
kubectl -n scalescope-system port-forward svc/scalescope 8000:80
curl -fs http://localhost:8000/api/source
```

The checked-in `k8s/scalescope/clusterrolebinding-observer.yaml` grants
cluster-wide read visibility to the ScaleScope ServiceAccount. That is useful
for an operator demo or platform-wide install. For a narrower production
install, do not apply that ClusterRoleBinding; instead apply
`examples/k8s/observe-namespace-rolebinding.yaml` once per namespace that
users are allowed to inspect, and set `SCALESCOPE_K8S_NAMESPACES` to that same
comma-separated namespace list.

## 4. In-cluster actuation mode

Actuation is the autoscaling option. It patches only the
`deployments/scale` subresource for the configured primary target, and refuses
to write if a HorizontalPodAutoscaler already targets the same Deployment.

```sh
kubectl apply -f k8s/scalescope-actuation/
kubectl -n scalescope-system set env deployment/scalescope SCALESCOPE_ACTUATE=true
kubectl -n scalescope-system rollout status deployment/scalescope
```

For namespace-limited actuation, do not apply
`k8s/scalescope-actuation/clusterrolebinding-actuator.yaml`; bind the
`scalescope-actuator` ClusterRole in only the target namespace instead:

```sh
kubectl apply -f k8s/scalescope-actuation/clusterrole-actuator.yaml
kubectl apply -f examples/k8s/actuation-namespace-rolebinding.yaml
kubectl -n scalescope-system set env deployment/scalescope SCALESCOPE_ACTUATE=true
```

If the sample workload HPA is installed, remove it before actuation:

```sh
kubectl delete hpa sample-workload -n scalescope-demo
```

Verify the write boundary before trusting the deployment:

```sh
kubectl auth can-i patch deployments/scale \
  --as=system:serviceaccount:scalescope-system:scalescope \
  -n scalescope-demo

kubectl auth can-i delete pods \
  --as=system:serviceaccount:scalescope-system:scalescope \
  -n scalescope-demo
```

The first command should be `yes` only where actuation is intended. The second
command should be `no`.

## 5. Exposed dashboard with Basic Auth

The in-cluster Service is `ClusterIP`; keep it internal unless an ingress,
gateway, VPN, SSO proxy, mTLS policy, or equivalent control protects it.

For built-in Basic Auth:

```sh
kubectl -n scalescope-system create secret generic scalescope-auth \
  --from-literal=username="$SCALESCOPE_AUTH_USERNAME" \
  --from-literal=password="$SCALESCOPE_AUTH_PASSWORD"

kubectl -n scalescope-system rollout restart deployment/scalescope
kubectl -n scalescope-system rollout status deployment/scalescope
```

Then verify through the exposed route or port-forward:

```sh
curl -fs -u "$SCALESCOPE_AUTH_USERNAME:$SCALESCOPE_AUTH_PASSWORD" \
  http://localhost:8000/api/source
```

## 6. Sustained spike demo

Deploy `sample-workload/`, expose its Service to ScaleScope through
`SCALESCOPE_K8S_METRICS_URL`, then use the dashboard's load buttons or call:

```sh
curl -fs -X POST \
  "http://localhost:8000/api/workloads/scalescope-demo:sample-workload/trigger?kind=traffic&duration_seconds=300"

curl -fs -X POST \
  "http://localhost:8000/api/workloads/scalescope-demo:sample-workload/trigger?kind=stress&duration_seconds=300"
```

The forecast ramp should show rising P50/P90 demand over time, projected pod
load, recommended replicas, and whether diagnosis allows scaling.
