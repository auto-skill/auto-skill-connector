#!/usr/bin/env bash
set -euo pipefail

# Rehydrate packages that were previously accepted with an incomplete
# dependency closure.  The lock makes this safe to launch from a cron/systemd
# retry without creating duplicate Docker workers or SQLite writers.
ROOT_DIR="${AUTOSKILL_ROOT_DIR:-/opt/auto-skill-connector}"
COMPOSE_FILE="$ROOT_DIR/backend/deploy/docker-compose.yml"
STATE_FILE="${AUTOSKILL_CLOSURE_STATE:-$ROOT_DIR/backend/data/closure-repair.json}"
LOCK_FILE="${AUTOSKILL_CLOSURE_LOCK:-$ROOT_DIR/backend/data/closure-repair.lock}"
LIMIT="${AUTOSKILL_CLOSURE_BATCH:-100}"
MAX_BATCHES="${AUTOSKILL_CLOSURE_MAX_BATCHES:-2000}"

mkdir -p "$(dirname "$STATE_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
    echo "closure repair already running" >&2
    exit 0
fi

cd "$ROOT_DIR"
for _ in $(seq 1 "$MAX_BATCHES"); do
    output="$(docker compose -f "$COMPOSE_FILE" --profile hydrator run --rm --no-deps hydrator \
        python3 hydrate_github_packages.py \
        --db /data/local_skills.db \
        --library-dir /app/skills_library \
        --state "/data/$(basename "$STATE_FILE")" \
        --limit "$LIMIT" \
        --workers 1 \
        --retry-closure)"
    printf '%s\n' "$output"
    if printf '%s\n' "$output" | grep -q '"selected": 0'; then
        break
    fi
done
