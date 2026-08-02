#!/usr/bin/env bash
set -euo pipefail

# Repair active GitHub/SkillsMP/curated-list rows by capturing complete,
# immutable GitHub package trees. The API and direct DB readers are stopped so
# SQLite and the package CAS are updated as one operator-controlled window.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$DEPLOY_DIR/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$DEPLOY_DIR/docker-compose.yml}"
STATE_PATH="${STATE_PATH:-/data/github_package_hydration_state.json}"
LIMIT="${LIMIT:-100}"
BATCHES="${BATCHES:-1}"
WORKERS="${WORKERS:-1}"
RETRY_FAILED="${RETRY_FAILED:-0}"
SOURCES="${SOURCES:-}"
AUDIT_REQUIRE_COMPLETE="${AUDIT_REQUIRE_COMPLETE:-0}"
STOP_SERVICES="${STOP_SERVICES:-1}"
HYDRATOR_SERVICE="${HYDRATOR_SERVICE:-hydrator}"
BUILD_HYDRATOR="${BUILD_HYDRATOR:-0}"

cd "$ROOT_DIR"

restart_services() {
  docker compose -f "$COMPOSE_FILE" up -d api admin-local mcp litestream >/dev/null
}

cleanup() {
  restart_services || true
}
if [[ "$STOP_SERVICES" == "1" ]]; then
  trap cleanup EXIT
  docker compose -f "$COMPOSE_FILE" stop api admin-local mcp litestream >/dev/null
else
  echo "warning: running with readers online; SQLite writes remain transactional but API caches refresh only after restart" >&2
fi

if [[ "$BUILD_HYDRATOR" == "1" ]]; then
  docker compose --profile hydrator -f "$COMPOSE_FILE" build "$HYDRATOR_SERVICE"
fi

retry_args=()
if [[ "$RETRY_FAILED" == "1" ]]; then
  retry_args+=(--retry-failed)
fi
source_args=()
if [[ -n "$SOURCES" ]]; then
  source_args+=(--sources "$SOURCES")
fi

for batch in $(seq 1 "$BATCHES"); do
  echo "==> Hydration batch $batch/$BATCHES (limit=$LIMIT)"
  docker compose --profile hydrator -f "$COMPOSE_FILE" run --rm --no-deps "$HYDRATOR_SERVICE" \
    python hydrate_github_packages.py \
    --db /data/local_skills.db \
    --library-dir /app/skills_library \
    --state "$STATE_PATH" \
    --limit "$LIMIT" \
    --workers "$WORKERS" \
    "${retry_args[@]}" \
    "${source_args[@]}"
done

if [[ "$STOP_SERVICES" != "1" ]]; then
  # Do not restart or disrupt the live API in online mode. The operator can
  # run backfill-source-embeddings.sh afterward, which refreshes caches.
  trap - EXIT
fi

if ! docker compose -f "$COMPOSE_FILE" run --rm --no-deps api \
  python audit_package_integrity.py \
  --db /data/local_skills.db \
  --package-root /app/skills_library/packages; then
  if [[ "$AUDIT_REQUIRE_COMPLETE" == "1" ]]; then
    exit 1
  fi
  echo "warning: source package audit is not complete yet; rerun with more batches" >&2
fi
