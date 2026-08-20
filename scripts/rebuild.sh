#!/usr/bin/env bash
# Rebuilds ScaleScope from scratch against the latest package versions
# permitted by pyproject.toml's lower-bound constraints, validates the
# result, and leaves the service running via docker compose — this script
# is the entry point to get a working instance up, not just a CI check.
#
# Because the Dockerfile has no pinned versions, this script always pulls
# whatever is newest at run time (`pip install --upgrade`, `docker compose
# build --no-cache --pull`). Re-running it later can therefore produce a
# different, newer build even with no source changes; requirements-lock.txt
# records exactly what was resolved for the build that ran.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

VENV_DIR="$ROOT_DIR/.venv"
LOCK_FILE="$ROOT_DIR/requirements-lock.txt"
APP_URL="http://localhost:8000"

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

echo "== docker compose build (no cache, latest base image) =="
docker compose build --no-cache --pull

echo "== starting service =="
docker compose up -d --force-recreate

on_failure() {
    echo "STARTUP FAILED: $APP_URL/api/workloads did not respond as expected" >&2
    docker compose logs >&2
    docker compose down
    exit 1
}
trap on_failure ERR

for _ in $(seq 1 30); do
    if curl -sf "$APP_URL/api/workloads" >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
curl -sf "$APP_URL/api/workloads" | grep -q payments-api
trap - ERR

echo "== done: ScaleScope is running at $APP_URL =="
docker compose ps
