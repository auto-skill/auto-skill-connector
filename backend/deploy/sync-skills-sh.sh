#!/usr/bin/env bash
set -euo pipefail

# Run from the deployed checkout. The one-shot container shares /data with
# the API, so hydrated records land in the same durable local_skills.db.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/backend/deploy/docker-compose.yml"
cd "$ROOT_DIR"
exec docker compose -f "$COMPOSE_FILE" run --rm --no-deps api \
  python sync_skills_sh_mirror.py "$@"
