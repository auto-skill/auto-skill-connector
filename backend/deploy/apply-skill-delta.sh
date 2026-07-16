#!/usr/bin/env bash
# Validate, back up, apply, audit, and activate a founder-supplied skill delta.
set -euo pipefail

if [ "$#" -lt 4 ] || [ "$4" != "--confirm" ]; then
  echo "usage: $0 <package-filename.zip> <actor-email> <reason> --confirm" >&2
  echo "package must already be under backend/data/skill-deltas/" >&2
  exit 2
fi

PACKAGE_NAME=$1
ACTOR_EMAIL=$2
REASON=$3
case "$PACKAGE_NAME" in
  */*|*\\*|*.zip) ;;
  *) echo "package must be a simple .zip filename" >&2; exit 2 ;;
esac
if [[ "$PACKAGE_NAME" == */* || "$PACKAGE_NAME" == *\\* ]]; then
  echo "package must be a simple filename, not a path" >&2
  exit 2
fi

DEPLOY_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
BACKEND_DIR=$(cd "$DEPLOY_DIR/.." && pwd)
HOST_PACKAGE="$BACKEND_DIR/data/skill-deltas/$PACKAGE_NAME"
CONTAINER_PACKAGE="/data/skill-deltas/$PACKAGE_NAME"
# validate/plan/apply hold the whole decompressed package in memory at once
# (unlike the collector's export, this side is not streamed); the weekly
# corpus keeps growing, so give api room here rather than production's leaner
# serving default.
export API_MEMORY_LIMIT="${API_MEMORY_LIMIT:-2560m}"
COMPOSE=(docker compose -f "$DEPLOY_DIR/docker-compose.yml")

if [ ! -f "$HOST_PACKAGE" ]; then
  echo "package not found: $HOST_PACKAGE" >&2
  exit 1
fi

echo "==> Validating constrained package"
"${COMPOSE[@]}" run --rm --no-deps api python skill_delta.py validate "$CONTAINER_PACKAGE"

echo "==> Planned changes"
"${COMPOSE[@]}" run --rm --no-deps api python skill_delta.py plan \
  "$CONTAINER_PACKAGE" --db /data/local_skills.db

echo "==> Applying with an online SQLite backup and append-only audit record"
"${COMPOSE[@]}" run --rm --no-deps api python skill_delta.py apply \
  "$CONTAINER_PACKAGE" \
  --db /data/local_skills.db \
  --library-dir /app/skills_library \
  --backup-root /data/backups \
  --actor-email "$ACTOR_EMAIL" \
  --reason "$REASON" \
  --confirm-apply

echo "==> Recreating readers once so their in-memory indexes see the import"
"${COMPOSE[@]}" up -d --force-recreate --no-deps api
for attempt in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:8000/readyz >/dev/null; then
    break
  fi
  if [ "$attempt" -eq 60 ]; then
    echo "API did not become ready after the import" >&2
    exit 1
  fi
  sleep 3
done
"${COMPOSE[@]}" up -d --force-recreate admin-local mcp

curl -fsS --max-time 15 https://skills.autoskill.dev/readyz >/dev/null
curl -fsS --max-time 15 https://mcp.autoskill.dev/healthz >/dev/null
echo "==> Skill delta active; public API and MCP checks passed"
