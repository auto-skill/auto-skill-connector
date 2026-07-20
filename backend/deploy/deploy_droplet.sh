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
TRANSPORT_ATTEMPTS="${AUTOSKILL_DEPLOY_TRANSPORT_ATTEMPTS:-4}"
case "$TRANSPORT_ATTEMPTS" in
  ''|*[!0-9]*)
    echo "AUTOSKILL_DEPLOY_TRANSPORT_ATTEMPTS must be a positive integer" >&2
    exit 2
    ;;
esac
if [ "$TRANSPORT_ATTEMPTS" -lt 1 ]; then
  echo "AUTOSKILL_DEPLOY_TRANSPORT_ATTEMPTS must be a positive integer" >&2
  exit 2
fi

REMOTE_SUDO="${AUTOSKILL_DEPLOY_REMOTE_SUDO:-0}"
case "$REMOTE_SUDO" in
  0|1) ;;
  *)
    echo "AUTOSKILL_DEPLOY_REMOTE_SUDO must be 0 or 1" >&2
    exit 2
    ;;
esac

# Retry only the connection and upload stages. They happen before the remote
# archive is extracted, so repeating them cannot replay a partial deploy.
SSH_OPTIONS=(
  -i "$DEPLOY_SSH_KEY_PATH"
  -o StrictHostKeyChecking=accept-new
  -o BatchMode=yes
  -o ConnectTimeout=15
  -o ConnectionAttempts=1
  -o ServerAliveInterval=10
  -o ServerAliveCountMax=3
)
SSH=(ssh "${SSH_OPTIONS[@]}" "${DEPLOY_USER}@${DEPLOY_HOST}")
SCP=(scp "${SSH_OPTIONS[@]}")

remote_sudo_ssh() {
  local -a connection_args=()
  local host=""

  # The deploy uses only -i and -o connection options. Preserve those options,
  # isolate the host, and pass the original remote command as one root-shell
  # payload so shell builtins such as `cd` continue to work.
  while [ "$#" -gt 0 ]; do
    case "$1" in
      -i|-o)
        if [ "$#" -lt 2 ]; then
          echo "missing value for SSH option $1" >&2
          return 2
        fi
        connection_args+=("$1" "$2")
        shift 2
        ;;
      -*)
        connection_args+=("$1")
        shift
        ;;
      *)
        host="$1"
        shift
        break
        ;;
    esac
  done
  if [ -z "$host" ] || [ "$#" -eq 0 ]; then
    echo "remote sudo wrapper requires a host and command" >&2
    return 2
  fi

  local payload
  printf -v payload '%q ' "$@"
  command ssh "${connection_args[@]}" "$host" "sudo -n bash -lc $payload"
}

if [ "$REMOTE_SUDO" = "1" ]; then
  # A restricted operator can use this mode without reading the root-owned
  # production env file. `sudo -n` fails explicitly rather than prompting.
  SSH=(remote_sudo_ssh "${SSH[@]}")
fi

retry_transport() {
  local label="$1"
  shift
  local attempt status=0

  for attempt in $(seq 1 "$TRANSPORT_ATTEMPTS"); do
    echo "==> ${label} (attempt ${attempt}/${TRANSPORT_ATTEMPTS})"
    if "$@"; then
      return 0
    else
      status=$?
    fi
    if [ "$attempt" -lt "$TRANSPORT_ATTEMPTS" ]; then
      sleep "$((attempt * 3))"
    fi
  done

  echo "FAILED: ${label} after ${TRANSPORT_ATTEMPTS} attempts (exit ${status})" >&2
  return "$status"
}

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT
ARCHIVE_NAME="auto-skill-deploy-$(git rev-parse --short HEAD)-$$.tar.gz"
REMOTE_ARCHIVE="/tmp/${ARCHIVE_NAME}"

retry_transport "Checking SSH connectivity to ${DEPLOY_HOST}" "${SSH[@]}" true

echo "==> Archiving $(git rev-parse HEAD)"
git archive --format=tar.gz -o "$WORKDIR/deploy.tar.gz" HEAD

echo "==> Shipping archive to ${DEPLOY_HOST}"
retry_transport "Shipping deployment archive" "${SCP[@]}" \
  "$WORKDIR/deploy.tar.gz" "${DEPLOY_USER}@${DEPLOY_HOST}:${REMOTE_ARCHIVE}"

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

"${SSH[@]}" "bash -s -- '${REMOTE_ARCHIVE}'" <<'REMOTE'
set -euo pipefail
REMOTE_ARCHIVE="$1"
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

echo "==> Verifying bind-mounted runtime ownership for the container user (uid 10001)"
# Backup and Litestream history can be intentionally immutable. Recursively
# chowning the whole mounts makes a healthy deployment fail after walking those
# files, so assert ownership only for the paths writable by the running app.
if ! "${SSH[@]}" "bash -s" <<'REMOTE'
set -euo pipefail
for path in \
  /opt/auto-skill-connector/backend/data \
  /opt/auto-skill-connector/backend/data/local_skills.db \
  /opt/auto-skill-connector/backend/skills_library \
  /opt/auto-skill-connector/backend/skills_library/index.json
do
  owner=$(stat -c '%u:%g' "$path")
  if [ "$owner" != "10001:10001" ]; then
    echo "unexpected runtime ownership: $path is $owner, expected 10001:10001" >&2
    exit 1
  fi
done
REMOTE
then
  echo "FAILED: runtime bind mounts are not owned by the container user" >&2
  exit 1
fi

echo "==> Running remote production preflight before touching live services"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && python3 backend/deploy/compose_preflight.py --env-file backend/deploy/.env --skip-seed-checks --skip-docker"; then
  echo "FAILED: remote production preflight did not pass" >&2
  exit 1
fi

# Tag whatever is currently running as :previous before rebuilding, so a
# failed smoke check can restore it. `|| true` covers the very first deploy,
# when no :latest image exists yet to tag.
echo "==> Tagging current images as :previous for rollback"
"${SSH[@]}" "for img in deploy-api deploy-admin-local deploy-mcp; do docker tag \$img:latest \$img:previous 2>/dev/null || true; done"

PRIVACY_SCRUBBED=0
PRIVACY_MARKER="$REMOTE_DIR/backend/data/.route-privacy-scrub-v1.complete"
rollback() {
  if [ "$PRIVACY_SCRUBBED" -eq 1 ] || "${SSH[@]}" "test -f '$PRIVACY_MARKER'"; then
    echo "==> Privacy scrub is complete; refusing to restore pre-privacy images" >&2
    "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --no-build api admin-local mcp litestream" || true
    return
  fi
  echo "==> Rolling back to the previous images" >&2
  "${SSH[@]}" "cd ${REMOTE_DIR} && for img in deploy-api deploy-admin-local deploy-mcp; do docker tag \$img:previous \$img:latest 2>/dev/null || true; done && docker compose -f backend/deploy/docker-compose.yml rm -sf api admin-local mcp && docker compose -f backend/deploy/docker-compose.yml up -d --no-build api admin-local mcp"
}

echo "==> Building replacement images before touching live services"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml build api admin-local mcp"; then
  echo "FAILED: image build failed" >&2
  rollback
  exit 1
fi

# Continuous discovery no longer belongs on the production origin. Remove any
# legacy worker and mark its SQLite lease stale before rebuilding the serving
# stack. Collection is an explicit one-shot profile on a trusted off-host box.
echo "==> Retiring legacy production worker and scrape lease"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && removed=0; for attempt in \$(seq 1 15); do docker compose -f backend/deploy/docker-compose.yml rm -sf worker >/dev/null 2>&1 || true; if test -z \"\$(docker compose -f backend/deploy/docker-compose.yml ps -q worker)\"; then removed=1; break; fi; sleep 2; done; test \"\$removed\" -eq 1 && docker compose -f backend/deploy/docker-compose.yml run --rm --no-deps api python cleanup_scrape_runs.py --apply --retire-all"; then
  echo "FAILED: could not drain the worker lease" >&2
  rollback
  exit 1
fi

PRIVACY_MARKER="${REMOTE_DIR}/backend/data/.route-privacy-scrub-v1.complete"
if ! "${SSH[@]}" "test -f '${PRIVACY_MARKER}'"; then
  echo "==> Stopping the optional read-only database inspector"
  "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml --profile db-inspector stop db-inspector" || true
  echo "==> Stopping database users for the one-time route privacy scrub"
  if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml stop api admin-local mcp litestream"; then
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
  if ! "${SSH[@]}" "rm -rf '${REMOTE_DIR}/backend/data/local_skills.db-litestream' '${REMOTE_DIR}/backend/data/.local_skills.db-litestream' && for img in deploy-api deploy-admin-local deploy-mcp; do docker image rm \$img:previous 2>/dev/null || true; done"; then
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
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && removed=0; for attempt in \$(seq 1 15); do docker compose -f backend/deploy/docker-compose.yml rm -sf api admin-local mcp >/dev/null 2>&1 || true; docker rm -f deploy-api-1 deploy-admin-local-1 deploy-mcp-1 >/dev/null 2>&1 || true; if test -z \"\$(docker ps -aq --filter name=^deploy-api-1$)\" && test -z \"\$(docker ps -aq --filter name=^deploy-admin-local-1$)\" && test -z \"\$(docker ps -aq --filter name=^deploy-mcp-1$)\"; then removed=1; break; fi; sleep 2; done; test \"\$removed\" -eq 1 && docker compose -f backend/deploy/docker-compose.yml up -d --no-build api"; then
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

echo "==> Starting SSH-only admin service after API readiness"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --no-build admin-local"; then
  echo "FAILED: SSH-only admin service failed to start" >&2
  rollback
  exit 1
fi
admin_ready=0
for attempt in $(seq 1 20); do
  if "${SSH[@]}" "curl -fsS --max-time 5 http://127.0.0.1:8002/admin -o /dev/null"; then
    admin_ready=1
    break
  fi
  sleep 2
done
if [ "$admin_ready" -ne 1 ]; then
  echo "FAILED: SSH-only admin service did not become ready on loopback" >&2
  rollback
  exit 1
fi
if ! "${SSH[@]}" "ss -lnt | grep -Eq '127\\.0\\.0\\.1:8002[[:space:]]' && ! ss -lnt | grep -Eq '(^|[[:space:]])0\\.0\\.0\\.0:8002[[:space:]]'"; then
  echo "FAILED: admin port 8002 is not exclusively loopback-bound" >&2
  rollback
  exit 1
fi

echo "==> Starting MCP after API readiness"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && started=0; for attempt in \$(seq 1 8); do if docker compose -f backend/deploy/docker-compose.yml up -d --no-build mcp; then started=1; break; fi; sleep 2; done; test \"\$started\" -eq 1"; then
  echo "FAILED: MCP recreate failed" >&2
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

# `docker compose up` is idempotent when service configuration is unchanged.
# Reconcile these support services on every deploy so digest pins and resource
# limits in docker-compose.yml actually reach the host, without forcing the
# expensive library backup container to restart during ordinary code deploys.
echo "==> Reconciling pinned tunnel and backup services"
if ! "${SSH[@]}" "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --no-build cloudflared litestream library-backup"; then
  echo "FAILED: tunnel/backup service reconciliation failed" >&2
  rollback
  exit 1
fi

# A freshly recreated connector can be locally running before Cloudflare has
# propagated its new connections. HTTP 530 during that short registration
# window is expected; wait here so the real smoke checks still fail on a
# sustained outage rather than racing tunnel startup.
echo "==> Waiting for public tunnel registration"
tunnel_ready=0
for attempt in $(seq 1 40); do
  status=$(curl -s -o /dev/null -w "%{http_code}" \
    "https://skills.autoskill.dev/healthz" --max-time 15)
  if [ "$status" = "200" ]; then
    tunnel_ready=1
    break
  fi
  sleep 3
done
if [ "$tunnel_ready" -ne 1 ]; then
  echo "FAILED: public tunnel did not register within 120 seconds" >&2
  rollback
  exit 1
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

admin_public_status=$(curl -s -o /dev/null -w "%{http_code}" \
  "https://skills.autoskill.dev/admin" --max-time 15)
if [ "$admin_public_status" != "401" ] && [ "$admin_public_status" != "403" ] && [ "$admin_public_status" != "404" ]; then
  echo "FAILED: public admin path returned unexpected status $admin_public_status" >&2
  smoke_failed=1
fi
echo "checked: public /admin -> $admin_public_status (denied)"

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
