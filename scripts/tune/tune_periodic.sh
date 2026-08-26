#!/usr/bin/env bash
# Periodic LightGBM re-tuning driver for cron.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

WORKLOAD="sample-app"
SERVICE="scalescope"
TRIALS=30
CONTAINER_DB_PATH="/data/scalescope.duckdb"
TUNE_DIR="$ROOT_DIR/scripts/tune"
VENV_DIR="$TUNE_DIR/.venv-tune"
OUTPUT_DIR="$TUNE_DIR/output"
TUNER="$TUNE_DIR/tune_lightgbm.py"
TMP_DIR=""
PROMOTED="no"

timestamp() {
    date -u +"%Y-%m-%dT%H:%M:%SZ"
}

fail() {
    echo "$(timestamp) ERROR $*" >&2
    exit 1
}

usage() {
    cat <<'EOF'
usage: scripts/tune/tune_periodic.sh [--workload NAME] [--service NAME] [--trials N]

Runs SMAC3 LightGBM tuning against a DuckDB copy from a running compose service.
The isolated scripts/tune/.venv-tune must already exist; see scripts/tune/README.md.
EOF
}

cleanup() {
    if [ -n "$TMP_DIR" ] && [ -d "$TMP_DIR" ]; then
        rm -rf "$TMP_DIR"
    fi
    if [ "$PROMOTED" != "yes" ]; then
        rm -f "$OUTPUT_DIR/$WORKLOAD.json.tmp"
    fi
}

on_error() {
    status=$?
    echo "$(timestamp) ERROR tune_periodic failed status=$status line=$1" >&2
    exit "$status"
}

trap cleanup EXIT
trap 'on_error $LINENO' ERR

while [ "$#" -gt 0 ]; do
    case "$1" in
        --workload)
            if [ "$#" -lt 2 ]; then
                fail "--workload requires a value"
            fi
            WORKLOAD="$2"
            shift 2
            ;;
        --service)
            if [ "$#" -lt 2 ]; then
                fail "--service requires a value"
            fi
            SERVICE="$2"
            shift 2
            ;;
        --trials)
            if [ "$#" -lt 2 ]; then
                fail "--trials requires a value"
            fi
            TRIALS="$2"
            shift 2
            ;;
        --help|-h)
            usage
            exit 0
            ;;
        *)
            fail "unknown argument: $1"
            ;;
    esac
done

case "$WORKLOAD" in
    ""|*/*)
        fail "--workload must be a non-empty name without '/'"
        ;;
esac

case "$SERVICE" in
    ""|*:*)
        fail "--service must be a non-empty docker compose service name without ':'"
        ;;
esac

case "$TRIALS" in
    ""|*[!0-9]*)
        fail "--trials must be a positive integer"
        ;;
esac
if [ "$TRIALS" -le 0 ]; then
    fail "--trials must be a positive integer"
fi

# Keep this wrapper instead of compose-argument arrays: macOS bash 3.2 treats
# empty arrays as unbound under set -u.
compose() {
    docker compose "$@"
}

run_tuner() {
    if [ -f "$OUTPUT_PATH" ]; then
        python "$TUNER" \
            --db-path "$DB_COPY" \
            --workload "$WORKLOAD" \
            --trials "$TRIALS" \
            --out "$CANDIDATE_PATH" \
            --baseline-config "$OUTPUT_PATH"
    else
        python "$TUNER" \
            --db-path "$DB_COPY" \
            --workload "$WORKLOAD" \
            --trials "$TRIALS" \
            --out "$CANDIDATE_PATH"
    fi
}

read_metric() {
    metric="$1"
    value="$(awk -F= -v key="$metric" '
        $1 == key { found = 1; print $2; exit }
        END { if (!found) exit 1 }
    ' "$TUNER_LOG")" || fail "tuner output did not include $metric"
    printf '%s\n' "$value"
}

promotion_decision() {
    python - "$1" "$2" <<'PY'
import math
import sys

new_mae = float(sys.argv[1])
old_mae = float(sys.argv[2])
if not math.isfinite(new_mae) or not math.isfinite(old_mae):
    raise SystemExit("MAE values must be finite")
print("yes" if new_mae < old_mae else "no")
PY
}

is_running_service() {
    printf '%s\n' "$RUNNING_SERVICES" | grep -qx "$1"
}

lightgbm_config_path_is_set() {
    # docker-compose.yml wires the same SCALESCOPE_LIGHTGBM_CONFIG_PATH into
    # both services, so this is a single all-or-nothing check against .env
    # rather than a per-service one - restarting a container that was never
    # configured to read the tuned config would be a gratuitous outage for
    # zero effect.
    [ -f "$ROOT_DIR/.env" ] || return 1
    value="$(awk -F= '$1 == "SCALESCOPE_LIGHTGBM_CONFIG_PATH" { sub(/^[^=]*=/, ""); print; exit }' "$ROOT_DIR/.env")"
    [ -n "$value" ]
}

restart_configured_services() {
    restarted=0
    RUNNING_SERVICES="$(compose ps --services --status running)"
    for candidate_service in scalescope scalescope-observe; do
        if is_running_service "$candidate_service"; then
            compose restart "$candidate_service"
            restarted=1
        fi
    done
    if [ "$restarted" -eq 0 ]; then
        compose restart "$SERVICE"
        restarted=1
    fi
}

mkdir -p "$OUTPUT_DIR"
OUTPUT_PATH="$OUTPUT_DIR/$WORKLOAD.json"
CANDIDATE_PATH="$OUTPUT_PATH.tmp"
TMP_DIR="$(mktemp -d "${TMPDIR:-/tmp}/scalescope-tune.XXXXXX")"
DB_COPY="$TMP_DIR/scalescope.duckdb"
TUNER_LOG="$TMP_DIR/tuner.log"

echo "$(timestamp) workload=$WORKLOAD service=$SERVICE trials=$TRIALS started"
echo "$(timestamp) copying $SERVICE:$CONTAINER_DB_PATH to $DB_COPY"
compose cp "$SERVICE:$CONTAINER_DB_PATH" "$DB_COPY"

if [ ! -x "$VENV_DIR/bin/python" ]; then
    fail "missing $VENV_DIR; create it with the setup steps in scripts/tune/README.md"
fi

# shellcheck disable=SC1091
source "$VENV_DIR/bin/activate"

run_tuner 2>&1 | tee "$TUNER_LOG"

if [ ! -s "$CANDIDATE_PATH" ]; then
    fail "tuner did not write candidate config: $CANDIDATE_PATH"
fi

DEFAULT_MAE="$(read_metric default_mae)"
NEW_MAE="$(read_metric best_mae)"
if [ -f "$OUTPUT_PATH" ]; then
    OLD_MAE="$(read_metric baseline_mae)"
else
    OLD_MAE="$DEFAULT_MAE"
fi

SHOULD_PROMOTE="$(promotion_decision "$NEW_MAE" "$OLD_MAE")" || \
    fail "could not compare MAE values old=$OLD_MAE new=$NEW_MAE"

RESTART_PERFORMED="no"
if [ "$SHOULD_PROMOTE" = "yes" ]; then
    mv "$CANDIDATE_PATH" "$OUTPUT_PATH"
    PROMOTED="yes"
    if lightgbm_config_path_is_set; then
        restart_configured_services
        RESTART_PERFORMED="yes"
    else
        echo "$(timestamp) SCALESCOPE_LIGHTGBM_CONFIG_PATH not set in .env; skipping restart"
    fi
fi

echo "$(timestamp) workload=$WORKLOAD old_mae=$OLD_MAE new_mae=$NEW_MAE promoted=$PROMOTED restart_performed=$RESTART_PERFORMED"
