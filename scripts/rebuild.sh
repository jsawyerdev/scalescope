#!/usr/bin/env bash
# Rebuilds ScaleScope from scratch against the latest package versions
# permitted by pyproject.toml's lower-bound constraints, validates the
# result, and produces a fresh Docker image plus a version audit trail.
#
# Because the Dockerfile has no pinned versions, this script always pulls
# whatever is newest at run time (`pip install --upgrade`, `docker build
# --no-cache --pull`). Re-running it later can therefore produce a
# different, newer build even with no source changes; requirements-lock.txt
# records exactly what was resolved for the build that ran.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

IMAGE_TAG="${IMAGE_TAG:-scalescope:dev}"
VENV_DIR="$ROOT_DIR/.venv"
LOCK_FILE="$ROOT_DIR/requirements-lock.txt"

echo "== rebuilding venv: $VENV_DIR =="
rm -rf "$VENV_DIR"
python3.13 -m venv "$VENV_DIR" 2>/dev/null || python3 -m venv "$VENV_DIR"
# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

echo "== installing latest allowed dependency versions =="
pip install --upgrade pip
pip install --upgrade -e ".[dev]"

echo "== recording resolved versions -> $LOCK_FILE =="
pip freeze --exclude-editable > "$LOCK_FILE"

echo "== formatting =="
black src tests

echo "== lint =="
ruff check src tests

echo "== tests =="
pytest -q

echo "== docker build (no cache, latest base image) =="
docker build --no-cache --pull -t "$IMAGE_TAG" .

echo "== container smoke test =="
CONTAINER_NAME="scalescope-rebuild-smoke"
docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
docker run -d --name "$CONTAINER_NAME" -p 18123:8000 -e SCALESCOPE_TICK_SECONDS=0.3 "$IMAGE_TAG" >/dev/null

cleanup() { docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true; }
trap cleanup EXIT

for _ in $(seq 1 20); do
    if curl -sf http://localhost:18123/api/workloads >/dev/null; then
        break
    fi
    sleep 1
done

if ! curl -sf http://localhost:18123/api/workloads | grep -q payments-api; then
    echo "SMOKE TEST FAILED: /api/workloads did not return the expected workload" >&2
    docker logs "$CONTAINER_NAME" >&2
    exit 1
fi

echo "== done: $IMAGE_TAG built and verified =="
docker images "$IMAGE_TAG" --format "image size: {{.Size}}"
