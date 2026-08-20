#!/usr/bin/env bash
# Renders a standalone kubeconfig for the least-privilege scalescope-actuator
# ServiceAccount (k8s/rbac/), so ScaleScope authenticates as that scoped
# identity instead of whatever admin kubeconfig is on the operator's machine.
# Requires k8s/rbac/*.yaml already applied. This script itself only reads
# (`kubectl get`/`config view`) - it does not grant or change any permission,
# only exports credentials for the identity k8s/rbac/role.yaml already
# defines (read-only unless SCALESCOPE_ACTUATE=true is also set).
set -euo pipefail

NAMESPACE="scalescope-demo"
SECRET_NAME="scalescope-actuator-token"
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
  - name: scalescope-actuator
    context:
      cluster: scalescope-demo
      user: scalescope-actuator
      namespace: ${NAMESPACE}
current-context: scalescope-actuator
users:
  - name: scalescope-actuator
    user:
      token: ${TOKEN}
EOF

chmod 600 "$OUTPUT_PATH"
echo "wrote scoped kubeconfig to $OUTPUT_PATH"
