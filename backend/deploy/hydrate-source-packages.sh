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
RETRY_FAILED="${RETRY_FAILED:-0}"
AUDIT_REQUIRE_COMPLETE="${AUDIT_REQUIRE_COMPLETE:-0}"

cd "$ROOT_DIR"

restart_services() {
  docker compose -f "$COMPOSE_FILE" up -d api admin-local mcp litestream >/dev/null
}

cleanup() {
  restart_services || true
}
trap cleanup EXIT

docker compose -f "$COMPOSE_FILE" stop api admin-local mcp litestream >/dev/null

retry_args=()
if [[ "$RETRY_FAILED" == "1" ]]; then
  retry_args+=(--retry-failed)
fi

docker compose -f "$COMPOSE_FILE" run --rm --no-deps api \
  python hydrate_github_packages.py \
  --db /data/local_skills.db \
  --library-dir /app/skills_library \
  --state "$STATE_PATH" \
  --limit "$LIMIT" \
  "${retry_args[@]}"

if ! docker compose -f "$COMPOSE_FILE" run --rm --no-deps api \
  python audit_package_integrity.py \
  --db /data/local_skills.db \
  --package-root /app/skills_library/packages; then
  if [[ "$AUDIT_REQUIRE_COMPLETE" == "1" ]]; then
    exit 1
  fi
  echo "warning: source package audit is not complete yet; rerun with more batches" >&2
fi
