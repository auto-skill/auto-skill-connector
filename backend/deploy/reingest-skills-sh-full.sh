#!/usr/bin/env bash
set -euo pipefail

# Explicit operator job for a complete, resumable hydration pass. Keep this
# separate from the normal bounded refresh so a cron job cannot unexpectedly
# spend the full upstream quota.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

bash backend/deploy/refresh-skills-sh-oidc.sh
bash backend/deploy/sync-skills-sh.sh \
  --all-listings \
  --views "${SKILLS_SH_INDEX_VIEWS:-all-time,trending,hot}" \
  --per-page "${SKILLS_SH_INDEX_PER_PAGE:-500}" \
  --hydrate-all \
  --retry-failed \
  --batch-size "${SKILLS_SH_HYDRATE_BATCH_SIZE:-8}" \
  --delay-seconds "${SKILLS_SH_HYDRATE_DELAY_SECONDS:-0.25}"
bash backend/deploy/verify-skills-sh-mirror.sh
