#!/usr/bin/env bash
set -euo pipefail

# Emergency cleanup for containers started by the old, uncoordinated
# supervisors.  The label is restricted to the compose hydrator service; no
# API, database, volume, image, or source package is removed.
ROOT_DIR="${AUTOSKILL_ROOT_DIR:-/opt/auto-skill-connector}"

cd "$ROOT_DIR"
ids="$(docker ps -aq --filter 'label=com.docker.compose.service=hydrator')"
if [[ -z "$ids" ]]; then
  echo "no hydrator containers to drain"
  exit 0
fi

echo "draining hydrator containers: $ids" >&2
docker rm -f $ids
echo "hydrator containers drained; database and package volumes were preserved"
