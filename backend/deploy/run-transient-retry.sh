#!/usr/bin/env bash
set -euo pipefail

# Retry only rows that the normal lanes left pending after a transient GitHub
# or transport failure.  A separate state file and lock prevent this lane from
# racing the main URL-partition supervisors.
ROOT_DIR="${AUTOSKILL_ROOT_DIR:-/opt/auto-skill-connector}"
COMPOSE_FILE="$ROOT_DIR/backend/deploy/docker-compose.yml"
STATE_FILE="${AUTOSKILL_TRANSIENT_STATE:-$ROOT_DIR/backend/data/transient-retry.json}"
LOCK_FILE="${AUTOSKILL_TRANSIENT_LOCK:-$ROOT_DIR/backend/data/transient-retry.lock}"
LIMIT="${AUTOSKILL_TRANSIENT_BATCH:-100}"
MAX_ROUNDS="${AUTOSKILL_TRANSIENT_MAX_ROUNDS:-3}"

mkdir -p "$(dirname "$STATE_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "transient retry already running" >&2
    exit 0
fi

cd "$ROOT_DIR"
for _ in $(seq 1 "$MAX_ROUNDS"); do
    output="$(docker compose -f "$COMPOSE_FILE" --profile hydrator run --rm --no-deps hydrator \
        python3 hydrate_github_packages.py \
        --db /data/local_skills.db \
        --library-dir /app/skills_library \
        --state "/data/$(basename "$STATE_FILE")" \
        --limit "$LIMIT" \
        --workers 1 \
        --retry-failed \
        --only-pending)"
    printf '%s\n' "$output"
    if printf '%s\n' "$output" | grep -q '"selected": 0'; then
        break
    fi
done
