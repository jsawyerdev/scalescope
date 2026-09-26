#!/usr/bin/env bash
# Full teardown and rebuild: destroys and recreates the ScaleScope
# container(s) from scratch against the latest package versions permitted
# by pyproject.toml's lower-bound constraints, validates the result, and
# leaves the service(s) running - this script is the entry point to get a
# working instance up, not just a CI check.
#
# Because the Dockerfile has no pinned versions, this script always pulls
# whatever is newest at run time (`pip install --upgrade`, `docker compose
# build --no-cache --pull`). Re-running it later can therefore produce a
# different, newer build even with no source changes; requirements-lock.txt
# records exactly what was resolved for the build that ran.
#
# Usage:
#   ./scripts/rebuild.sh              DEMO instance only (localhost:8000)
#   ./scripts/rebuild.sh --observe    also tear down/rebuild the OBSERVE
#                                     instance (localhost:8001) against a
#                                     real cluster - requires k8s/rbac/
#                                     already applied and a kubeconfig from
#                                     scripts/generate-observer-kubeconfig.sh
#   ./scripts/rebuild.sh --wipe-data  also drop the DuckDB data volume(s),
#                                     for a truly clean slate
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

WITH_OBSERVE=0
WIPE_DATA=0
for arg in "$@"; do
    case "$arg" in
        --observe) WITH_OBSERVE=1 ;;
        --wipe-data) WIPE_DATA=1 ;;
        *)
            echo "unknown argument: $arg (expected --observe and/or --wipe-data)" >&2
            exit 1
            ;;
    esac
done

VENV_DIR="$ROOT_DIR/.venv"
LOCK_FILE="$ROOT_DIR/requirements-lock.txt"
DEMO_URL="http://localhost:8000"
OBSERVE_URL="http://localhost:8001"
KUBECONFIG_PATH="${SCALESCOPE_OBSERVER_KUBECONFIG:-./scalescope-observer.kubeconfig}"

# Read directly from .env (not exported into this shell) so the smoke-test
# curls below can authenticate if SCALESCOPE_AUTH_USERNAME/PASSWORD are set.
AUTH_USERNAME=""
AUTH_PASSWORD=""
ENV_FILE="$ROOT_DIR/.env"
if [ -f "$ENV_FILE" ]; then
    AUTH_USERNAME="$(awk -F= '$1 == "SCALESCOPE_AUTH_USERNAME" { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE")"
    AUTH_PASSWORD="$(awk -F= '$1 == "SCALESCOPE_AUTH_PASSWORD" { sub(/^[^=]*=/, ""); print; exit }' "$ENV_FILE")"
fi

# Credentials reach curl as a config file on stdin, never argv, so they do
# not show up in the process list. Config strings escape \ and ".
curl_auth() {
    if [ -n "$AUTH_USERNAME" ] && [ -n "$AUTH_PASSWORD" ]; then
        credentials="$AUTH_USERNAME:$AUTH_PASSWORD"
        credentials="${credentials//\\/\\\\}"
        credentials="${credentials//\"/\\\"}"
        printf 'user = "%s"\n' "$credentials" | curl -K - "$@"
    else
        curl "$@"
    fi
}

json_field_true() {
    python -c 'import json, sys; raise SystemExit(0 if json.load(sys.stdin).get(sys.argv[1]) is True else 1)' "$1"
}

json_list_contains() {
    python -c 'import json, sys; raise SystemExit(0 if sys.argv[1] in json.load(sys.stdin) else 1)' "$1"
}

if [ "$WITH_OBSERVE" -eq 1 ]; then
    if [ ! -f "$KUBECONFIG_PATH" ]; then
        echo "ERROR: --observe requires a kubeconfig at $KUBECONFIG_PATH" >&2
        echo "  1. kubectl apply -f k8s/rbac/" >&2
        echo "  2. OUTPUT_PATH=$KUBECONFIG_PATH ./scripts/generate-observer-kubeconfig.sh" >&2
        exit 1
    fi
fi

# macOS ships bash 3.2 (frozen there for licensing reasons), which treats
# "${EMPTY_ARRAY[@]}" as unbound under `set -u` - bash 4.4+ does not. A
# wrapper function sidesteps the array entirely instead of relying on a
# bash version this script can't assume.
compose() {
    if [ "$WITH_OBSERVE" -eq 1 ]; then
        docker compose --profile observe "$@"
    else
        docker compose "$@"
    fi
}

echo "== tearing down existing containers =="
if [ "$WIPE_DATA" -eq 1 ]; then
    compose down --volumes
else
    compose down
fi

echo "== rebuilding venv: $VENV_DIR =="
rm -rf "$VENV_DIR"
python3.14 -m venv "$VENV_DIR" 2>/dev/null || python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "== installing latest allowed dependency versions =="
pip install --upgrade pip
pip install --upgrade -e ".[dev]"

echo "== recording resolved versions -> $LOCK_FILE =="
pip freeze --exclude-editable > "$LOCK_FILE"

echo "== formatting =="
black src tests sample-workload/app scripts/tune

echo "== lint =="
ruff check .

echo "== type check =="
mypy

echo "== tests =="
pytest -q

echo "== docker compose build (no cache, latest base image) =="
compose build --no-cache --pull

echo "== starting service(s) =="
compose up -d --force-recreate

on_failure() {
    echo "STARTUP FAILED" >&2
    compose logs >&2
    compose down
    exit 1
}
trap on_failure ERR

for _ in $(seq 1 30); do
    if curl -sf "$DEMO_URL/healthz" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
for _ in $(seq 1 30); do
    if curl_auth -sf "$DEMO_URL/api/workloads" | json_list_contains sample-app; then
        break
    fi
    sleep 1
done
curl_auth -sf "$DEMO_URL/api/workloads" | json_list_contains sample-app

if [ "$WITH_OBSERVE" -eq 1 ]; then
    for _ in $(seq 1 30); do
        if curl -sf "$OBSERVE_URL/healthz" >/dev/null 2>&1; then
            break
        fi
        sleep 1
    done
    for _ in $(seq 1 30); do
        if curl_auth -sf "$OBSERVE_URL/api/source" | json_field_true connected; then
            break
        fi
        sleep 1
    done
    curl_auth -sf "$OBSERVE_URL/api/source" | json_field_true connected
fi
trap - ERR

echo "== done =="
echo "DEMO:    $DEMO_URL"
if [ "$WITH_OBSERVE" -eq 1 ]; then
    echo "OBSERVE: $OBSERVE_URL"
fi
compose ps
