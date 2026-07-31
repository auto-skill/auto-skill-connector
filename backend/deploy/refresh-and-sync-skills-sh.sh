#!/usr/bin/env bash
set -euo pipefail

# Idempotent operator/cron entrypoint. It refreshes the short-lived OIDC file,
# indexes the complete skills.sh listing metadata, hydrates only the bounded
# top slice, then fails if the shared mirror is still empty.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
cd "$ROOT_DIR"

bash backend/deploy/refresh-skills-sh-oidc.sh
bash backend/deploy/sync-skills-sh.sh \
  --all-listings \
  --view "${SKILLS_SH_INDEX_VIEW:-all-time}" \
  --per-page "${SKILLS_SH_INDEX_PER_PAGE:-500}" \
  --hydrate-top "${SKILLS_SH_HYDRATE_TOP:-100}" \
  --batch-size "${SKILLS_SH_HYDRATE_BATCH_SIZE:-8}" \
  --delay-seconds "${SKILLS_SH_HYDRATE_DELAY_SECONDS:-0.25}"
bash backend/deploy/verify-skills-sh-mirror.sh
