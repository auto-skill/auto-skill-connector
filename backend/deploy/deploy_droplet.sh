#!/usr/bin/env bash
# Deploys the current git HEAD to the droplet running backend/deploy/docker-compose.yml.
#
# The droplet checkout at /opt/auto-skill-connector has no relationship to git
# -- it was rsynced there once by hand and never updated again, which is how
# it silently drifted out of date until it was missing whole endpoints. This
# script replaces that manual process: archive HEAD, ship it over, sync it
# into place without touching the live DB/secrets, and rebuild.
#
# Required env vars: DEPLOY_HOST, DEPLOY_USER, DEPLOY_SSH_KEY_PATH.
# Runs identically from GitHub Actions or a developer machine.
set -euo pipefail

: "${DEPLOY_HOST:?DEPLOY_HOST is required}"
: "${DEPLOY_USER:?DEPLOY_USER is required}"
: "${DEPLOY_SSH_KEY_PATH:?DEPLOY_SSH_KEY_PATH is required}"

if python3 -c 'import sys' >/dev/null 2>&1; then
  PYTHON_BIN=python3
else
  PYTHON_BIN=python
fi

REMOTE_DIR="/opt/auto-skill-connector"
SSH=(ssh -i "$DEPLOY_SSH_KEY_PATH" -o StrictHostKeyChecking=accept-new "${DEPLOY_USER}@${DEPLOY_HOST}")
SCP=(scp -i "$DEPLOY_SSH_KEY_PATH" -o StrictHostKeyChecking=accept-new)

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
ARCHIVE_NAME="auto-skill-deploy-$(git rev-parse --short HEAD)-$$.tar.gz"
REMOTE_ARCHIVE="/tmp/${ARCHIVE_NAME}"

echo "==> Archiving $(git rev-parse HEAD)"
git archive --format=tar.gz -o "$WORKDIR/deploy.tar.gz" HEAD

echo "==> Shipping archive to ${DEPLOY_HOST}"
"${SCP[@]}" "$WORKDIR/deploy.tar.gz" "${DEPLOY_USER}@${DEPLOY_HOST}:${REMOTE_ARCHIVE}"

echo "==> Extracting and syncing into ${REMOTE_DIR}"
# cloudflared has nothing in-repo to redeploy (it's the official image, auth'd
# purely by CLOUDFLARED_TOKEN in .env) -- litestream.yml and
# backup-library.sh, though, are repo files bind-mounted into their
# containers, which docker compose won't notice changed on its own (it only
# tracks its own service definitions, not bind-mounted file contents). This
# hashes each before and after the sync so only the service whose bind-mounted
# config changed is recreated. In particular, a normal API deploy must not
# force a multi-hundred-MiB library backup upload onto the tiny VPS.
LITESTREAM_HASH_BEFORE=$("${SSH[@]}" "sha256sum ${REMOTE_DIR}/backend/deploy/litestream.yml 2>/dev/null" || true)
LIBRARY_BACKUP_HASH_BEFORE=$("${SSH[@]}" "sha256sum ${REMOTE_DIR}/backend/deploy/backup-library.sh 2>/dev/null" || true)

"${SSH[@]}" "REMOTE_ARCHIVE='${REMOTE_ARCHIVE}' bash -s" <<'REMOTE'
set -euo pipefail
rm -rf /tmp/deploy-extract
mkdir -p /tmp/deploy-extract
tar -xzf "$REMOTE_ARCHIVE" -C /tmp/deploy-extract
rsync -a --delete \
  --exclude 'backend/deploy/.env' \
  --exclude 'backend/deploy/.env.ci' \
  --exclude 'backend/data/' \
  --exclude 'backend/skills_library/' \
  --exclude 'backend/content_blobs/' \
  --exclude '.git/' \
  /tmp/deploy-extract/ /opt/auto-skill-connector/
rm -rf /tmp/deploy-extract "$REMOTE_ARCHIVE"
REMOTE

LITESTREAM_HASH_AFTER=$("${SSH[@]}" "sha256sum ${REMOTE_DIR}/backend/deploy/litestream.yml 2>/dev/null" || true)
LIBRARY_BACKUP_HASH_AFTER=$("${SSH[@]}" "sha256sum ${REMOTE_DIR}/backend/deploy/backup-library.sh 2>/dev/null" || true)

echo "==> Ensuring bind-mounted data dirs are owned by the container's non-root user (uid 10001)"
"${SSH[@]}" "mkdir -p ${REMOTE_DIR}/backend/data ${REMOTE_DIR}/backend/skills_library && chown -R 10001:10001 ${REMOTE_DIR}/backend/data ${REMOTE_DIR}/backend/skills_library"

# Tag whatever is currently running as :previous before rebuilding, so a
# failed smoke check can restore it. `|| true` covers the very first deploy,
# when no :latest image exists yet to tag.
echo "==> Tagging current images as :previous for rollback"
"${SSH[@]}" "for img in deploy-api deploy-mcp deploy-worker; do docker tag \$img:latest \$img:previous 2>/dev/null || true; done"

PRIVACY_SCRUBBED=0
PRIVACY_MARKER="$REMOTE_DIR/backend/data/.route-privacy-scrub-v1.complete"
rollback() {
  if [ "$PRIVACY_SCRUBBED" -eq 1 ] || "${SSH[@]}" "test -f '$PRIVACY_MARKER'"; then
    echo "==> Privacy scrub is complete; refusing to restore pre-privacy images" >&2
    "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --no-build api mcp worker litestream" || true
    return
  fi
  echo "==> Rolling back to the previous images" >&2
  "${SSH[@]}" "cd ${REMOTE_DIR} && for img in deploy-api deploy-mcp deploy-worker; do docker tag \$img:previous \$img:latest 2>/dev/null || true; done && docker compose -f backend/deploy/docker-compose.yml rm -sf api mcp worker && docker compose -f backend/deploy/docker-compose.yml up -d --no-build api mcp worker"
}

echo "==> Building replacement images before touching live services"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml build api mcp worker"; then
  echo "FAILED: image build failed" >&2
  rollback
  exit 1
fi

# A worker can be cancelled mid-scrape by Docker recreation. Remove it first,
# mark its single SQLite lease stale through the still-running local API, then
# start a fresh worker only after the replacement API has passed readiness.
echo "==> Draining worker scrape lease"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && removed=0; for attempt in \$(seq 1 15); do docker compose -f backend/deploy/docker-compose.yml rm -sf worker >/dev/null 2>&1 || true; if test -z \"\$(docker compose -f backend/deploy/docker-compose.yml ps -q worker)\"; then removed=1; break; fi; sleep 2; done; test \"\$removed\" -eq 1 && curl -fsS -X PATCH 'http://127.0.0.1:8000/rest/v1/scrape_runs?status=eq.running' -H 'Content-Type: application/json' --data '{\"status\":\"stale\",\"error\":\"Marked stale during deploy before worker restart.\"}' -o /dev/null"; then
  echo "FAILED: could not drain the worker lease" >&2
  rollback
  exit 1
fi

PRIVACY_MARKER="${REMOTE_DIR}/backend/data/.route-privacy-scrub-v1.complete"
if ! "${SSH[@]}" "test -f '${PRIVACY_MARKER}'"; then
  echo "==> Stopping database users for the one-time route privacy scrub"
  if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml stop api mcp litestream"; then
    echo "FAILED: could not stop database services for privacy scrub" >&2
    rollback
    exit 1
  fi

  echo "==> Physically scrubbing retained route text and verifying SQLite integrity"
  if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml run --rm --no-deps api python scrub_route_privacy.py --apply --timeout-seconds 30"; then
    echo "FAILED: route privacy scrub did not complete" >&2
    rollback
    exit 1
  fi
  PRIVACY_SCRUBBED=1
  echo "==> Removing pre-scrub Litestream local tracking state and rollback tags"
  if ! "${SSH[@]}" "rm -rf '${REMOTE_DIR}/backend/data/local_skills.db-litestream' '${REMOTE_DIR}/backend/data/.local_skills.db-litestream' && for img in deploy-api deploy-mcp deploy-worker; do docker image rm \$img:previous 2>/dev/null || true; done"; then
    echo "FAILED: could not clear pre-scrub Litestream/rollback state" >&2
    rollback
    exit 1
  fi

  echo "==> Physically scrubbing and removing local database backup copies"
  if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml run --rm --no-deps api python scrub_route_privacy.py --purge-backups-only --purge-backup-root /data/backups --purge-backup-root /data/seed-packets --timeout-seconds 30"; then
    echo "FAILED: local database backup scrub did not complete" >&2
    rollback
    exit 1
  fi

  echo "==> Purging pre-scrub database generations from R2"
  if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --force-recreate library-backup && docker compose -f backend/deploy/docker-compose.yml exec -T library-backup sh -lc 'command -v aws >/dev/null 2>&1 || apk add --no-cache aws-cli >/dev/null; sh /scripts/purge-route-db-backups.sh --purge'"; then
    echo "FAILED: could not purge pre-scrub database backups" >&2
    rollback
    exit 1
  fi

fi

echo "==> Recreating API"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && removed=0; for attempt in \$(seq 1 15); do docker compose -f backend/deploy/docker-compose.yml rm -sf api mcp >/dev/null 2>&1 || true; docker rm -f deploy-api-1 deploy-mcp-1 >/dev/null 2>&1 || true; if test -z \"\$(docker ps -aq --filter name=^deploy-api-1$)\" && test -z \"\$(docker ps -aq --filter name=^deploy-mcp-1$)\"; then removed=1; break; fi; sleep 2; done; test \"\$removed\" -eq 1 && docker compose -f backend/deploy/docker-compose.yml up -d --no-build api"; then
  echo "FAILED: API recreate failed" >&2
  rollback
  exit 1
fi

echo "==> Waiting for local semantic readiness"
ready=0
for attempt in $(seq 1 40); do
  if "${SSH[@]}" "curl -fsS --max-time 10 http://127.0.0.1:8000/readyz -o /dev/null"; then
    ready=1
    break
  fi
  sleep 3
done
if [ "$ready" -ne 1 ]; then
  echo "FAILED: local /readyz did not pass after API replacement" >&2
  rollback
  exit 1
fi

echo "==> Starting MCP after API readiness"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && started=0; for attempt in \$(seq 1 8); do if docker compose -f backend/deploy/docker-compose.yml up -d --no-build mcp; then started=1; break; fi; sleep 2; done; test \"\$started\" -eq 1"; then
  echo "FAILED: MCP recreate failed" >&2
  rollback
  exit 1
fi

echo "==> Starting worker after API/MCP readiness"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && started=0; for attempt in \$(seq 1 8); do if docker compose -f backend/deploy/docker-compose.yml up -d --no-build worker; then started=1; break; fi; sleep 2; done; test \"\$started\" -eq 1"; then
  echo "FAILED: worker recreate failed" >&2
  rollback
  exit 1
fi

if [ "$PRIVACY_SCRUBBED" -eq 1 ]; then
  echo "==> Starting a fresh sanitized Litestream generation"
  "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --force-recreate litestream"
  replica_ready=0
  for attempt in $(seq 1 40); do
    if "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml exec -T library-backup sh /scripts/purge-route-db-backups.sh --require-litestream >/dev/null"; then
      replica_ready=1
      break
    fi
    sleep 3
  done
  if [ "$replica_ready" -ne 1 ]; then
    echo "FAILED: sanitized Litestream generation was not visible in R2" >&2
    rollback
    exit 1
  fi
  "${SSH[@]}" "touch '${PRIVACY_MARKER}' && chown 10001:10001 '${PRIVACY_MARKER}'"
elif [ "$LITESTREAM_HASH_BEFORE" != "$LITESTREAM_HASH_AFTER" ]; then
  echo "==> litestream.yml changed -- restarting Litestream"
  if ! "${SSH[@]}" "cd ${REMOTE_DIR} && started=0; for attempt in \$(seq 1 8); do if docker compose -f backend/deploy/docker-compose.yml up -d --force-recreate litestream; then started=1; break; fi; sleep 2; done; test \"\$started\" -eq 1"; then
    echo "FAILED: Litestream restart did not stabilize" >&2
    rollback
    exit 1
  fi
fi

if [ "$LIBRARY_BACKUP_HASH_BEFORE" != "$LIBRARY_BACKUP_HASH_AFTER" ]; then
  echo "==> backup-library.sh changed -- restarting the delayed backup sidecar"
  "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --force-recreate library-backup"
fi

echo "==> Smoke-checking the public API"
smoke_failed=0
for path in /healthz /readyz /skills-catalog; do
  url="https://skills.autoskill.dev${path}"
  status=$(curl -s -o /dev/null -w "%{http_code}" "$url" --max-time 15)
  if [ "$path" = "/skills-catalog" ]; then
    # /skills-catalog requires a bearer token (401 without one is expected and
    # still proves the route exists and the guard is evaluating it correctly;
    # a 403 "read-only public API" would mean the route regressed again).
    if [ "$status" != "401" ] && [ "$status" != "200" ]; then
      echo "FAILED: $url returned $status" >&2
      smoke_failed=1
    fi
  elif [ "$status" != "200" ]; then
    echo "FAILED: $url returned $status" >&2
    smoke_failed=1
  fi
  echo "checked: $url -> $status"
done

if [ "$smoke_failed" -ne 0 ]; then
  rollback
  exit 1
fi

privacy_clean=$(curl -fsS --max-time 15 https://skills.autoskill.dev/readyz \
  | "$PYTHON_BIN" -c 'import json,sys; p=json.load(sys.stdin).get("route_privacy") or {}; print("yes" if p.get("ok") is True and int(p.get("violations") or 0) == 0 else "no")')
if [ "$privacy_clean" != "yes" ]; then
  echo "FAILED: public readiness did not prove zero retained route prompt fields" >&2
  rollback
  exit 1
fi
echo "checked: route privacy -> clean"

echo "==> Deploy complete"
