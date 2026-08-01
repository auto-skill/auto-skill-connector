#!/usr/bin/env bash
set -euo pipefail

# Re-embed active skills after package hydration without loading a second ONNX
# model into the serving API. The API is restarted by the EXIT trap, refreshing
# its in-memory vector/lexical caches.
DEPLOY_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ROOT_DIR="$(cd "$DEPLOY_DIR/../.." && pwd)"
COMPOSE_FILE="${COMPOSE_FILE:-$DEPLOY_DIR/docker-compose.yml}"
LIMIT="${LIMIT:-0}"
cd "$ROOT_DIR"

restart_services() {
  docker compose -f "$COMPOSE_FILE" up -d api admin-local mcp litestream >/dev/null
}
trap 'restart_services || true' EXIT

docker compose -f "$COMPOSE_FILE" stop api admin-local mcp litestream >/dev/null
docker compose -f "$COMPOSE_FILE" run --rm --no-deps api \
  python backfill_embeddings_local.py \
  --db /data/local_skills.db \
  --library-dir /app/skills_library \
  --limit "$LIMIT"
