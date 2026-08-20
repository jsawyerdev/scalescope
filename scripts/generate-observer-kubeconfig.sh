#!/usr/bin/env bash
# Renders a standalone kubeconfig for the least-privilege scalescope-observer
# ServiceAccount (k8s/rbac/), so ScaleScope authenticates as that scoped
# identity instead of whatever admin kubeconfig is on the operator's machine.
# Requires k8s/rbac/*.yaml already applied. Uses only `kubectl get`/`config
# view` (read-only against the cluster).
set -euo pipefail

NAMESPACE="scalescope-demo"
SECRET_NAME="scalescope-observer-token"
OUTPUT_PATH="${OUTPUT_PATH:-./scalescope-observer.kubeconfig}"

SERVER_URL=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.server}')
CA_DATA=$(kubectl config view --raw --minify -o jsonpath='{.clusters[0].cluster.certificate-authority-data}')

for _ in $(seq 1 10); do
    TOKEN=$(kubectl get secret "$SECRET_NAME" -n "$NAMESPACE" -o jsonpath='{.data.token}' 2>/dev/null | base64 --decode || true)
    if [ -n "$TOKEN" ]; then
        break
    fi
    echo "waiting for $SECRET_NAME token to populate..." >&2
    sleep 1
done

if [ -z "$TOKEN" ]; then
    echo "ERROR: token never populated on secret $NAMESPACE/$SECRET_NAME" >&2
    exit 1
fi

cat <<EOF > "$OUTPUT_PATH"
apiVersion: v1
kind: Config
clusters:
  - name: scalescope-demo
    cluster:
      server: ${SERVER_URL}
      certificate-authority-data: ${CA_DATA}
contexts:
  - name: scalescope-observer
    context:
      cluster: scalescope-demo
      user: scalescope-observer
      namespace: ${NAMESPACE}
current-context: scalescope-observer
users:
  - name: scalescope-observer
    user:
      token: ${TOKEN}
EOF

chmod 600 "$OUTPUT_PATH"
echo "wrote scoped kubeconfig to $OUTPUT_PATH"
