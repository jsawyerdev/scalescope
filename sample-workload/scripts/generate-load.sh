#!/usr/bin/env bash
# Lightweight load generator for the sample-workload service. Drives a
# ramp-up / sustained / drop-off pattern of concurrent curl requests against
# /work so a real HPA (and later ScaleScope) has something to react to.
#
# Usage:
#   ./generate-load.sh <url> [max_concurrency] [phase_seconds]
#
# Example (after `kubectl port-forward -n scalescope-demo svc/sample-workload 8080:80`):
#   ./generate-load.sh http://localhost:8080/work 20 120
set -euo pipefail

URL="${1:?usage: generate-load.sh <url> [max_concurrency] [phase_seconds]}"
MAX_CONCURRENCY="${2:-20}"
PHASE_SECONDS="${3:-120}"
ITERATIONS="${WORK_ITERATIONS:-300000}"

fire_requests() {
  local concurrency="$1"
  local duration="$2"
  local end=$((SECONDS + duration))
  echo "[$(date +%T)] concurrency=${concurrency} for ${duration}s"
  while [ "${SECONDS}" -lt "${end}" ]; do
    for _ in $(seq 1 "${concurrency}"); do
      curl -s -o /dev/null "${URL}?iterations=${ITERATIONS}" &
    done
    wait
    sleep 1
  done
}

# Ramp up: 1 -> max_concurrency in steps.
STEPS=5
for i in $(seq 1 "${STEPS}"); do
  step_concurrency=$(( MAX_CONCURRENCY * i / STEPS ))
  [ "${step_concurrency}" -lt 1 ] && step_concurrency=1
  fire_requests "${step_concurrency}" $(( PHASE_SECONDS / STEPS ))
done

# Sustained at max concurrency.
fire_requests "${MAX_CONCURRENCY}" "${PHASE_SECONDS}"

# Drop off back to idle.
fire_requests 1 "${PHASE_SECONDS}"

echo "[$(date +%T)] load generation complete"
