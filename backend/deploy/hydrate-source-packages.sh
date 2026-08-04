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
RETRY_FALLBACK="${RETRY_FALLBACK:-0}"
SOURCES="${SOURCES:-}"
URLS_FILE="${URLS_FILE:-}"
AUDIT_REQUIRE_COMPLETE="${AUDIT_REQUIRE_COMPLETE:-0}"
STOP_SERVICES="${STOP_SERVICES:-1}"
HYDRATOR_SERVICE="${HYDRATOR_SERVICE:-hydrator}"
BUILD_HYDRATOR="${BUILD_HYDRATOR:-0}"
LOCK_FILE="${AUTOSKILL_HYDRATOR_LOCK:-$ROOT_DIR/backend/data/hydrator.global.lock}"
API_HEALTH_URL="${AUTOSKILL_API_HEALTH_URL:-http://127.0.0.1:8000/healthz}"

cd "$ROOT_DIR"

# All hydration lanes share this lock.  Their old per-lane locks allowed the
# main, closure-repair, and transient-retry supervisors to each start a
# one-shot container, which multiplied the memory ceiling and OOM-killed API.
mkdir -p "$(dirname "$LOCK_FILE")"
exec 9>"$LOCK_FILE"
if ! flock -n 9; then
  echo "another hydrator lane is already running; exiting without starting a container" >&2
  exit 0
fi

# Multiple Python workers multiply archive buffers and SQLite/package-store
# state.  Keep production hydration single-threaded; throughput comes from
# repository-level caching and sequential batches, not concurrent containers.
if [[ "$WORKERS" != "1" ]]; then
  echo "forcing WORKERS=1 for the production memory budget (requested=$WORKERS)" >&2
  WORKERS=1
fi

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
  if ! curl -fsS --max-time 5 "$API_HEALTH_URL" >/dev/null; then
    echo "API health check failed; refusing to start online hydration" >&2
    exit 2
  fi
fi

if [[ "$BUILD_HYDRATOR" == "1" ]]; then
  docker compose --profile hydrator -f "$COMPOSE_FILE" build "$HYDRATOR_SERVICE"
fi

retry_args=()
if [[ "$RETRY_FAILED" == "1" ]]; then
  retry_args+=(--retry-failed)
fi
if [[ "$RETRY_FALLBACK" == "1" ]]; then
  retry_args+=(--retry-fallback)
fi
source_args=()
if [[ -n "$SOURCES" ]]; then
  source_args+=(--sources "$SOURCES")
fi
urls_args=()
if [[ -n "$URLS_FILE" ]]; then
  urls_args+=(--urls-file "$URLS_FILE")
fi

for batch in $(seq 1 "$BATCHES"); do
  if [[ "$STOP_SERVICES" != "1" ]] && ! curl -fsS --max-time 5 "$API_HEALTH_URL" >/dev/null; then
    echo "API became unhealthy; stopping before the next hydration batch" >&2
    exit 2
  fi
  echo "==> Hydration batch $batch/$BATCHES (limit=$LIMIT)"
  docker compose --profile hydrator -f "$COMPOSE_FILE" run --rm --no-deps "$HYDRATOR_SERVICE" \
    python hydrate_github_packages.py \
    --db /data/local_skills.db \
    --library-dir /app/skills_library \
    --state "$STATE_PATH" \
    --limit "$LIMIT" \
    --workers "$WORKERS" \
    "${retry_args[@]}" \
    "${source_args[@]}" \
    "${urls_args[@]}"
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
