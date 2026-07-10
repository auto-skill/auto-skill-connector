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

REMOTE_DIR="/opt/auto-skill-connector"
SSH="ssh -i ${DEPLOY_SSH_KEY_PATH} -o StrictHostKeyChecking=accept-new ${DEPLOY_USER}@${DEPLOY_HOST}"
SCP="scp -i ${DEPLOY_SSH_KEY_PATH} -o StrictHostKeyChecking=accept-new"

WORKDIR=$(mktemp -d)
trap 'rm -rf "$WORKDIR"' EXIT

echo "==> Archiving $(git rev-parse HEAD)"
git archive --format=tar.gz -o "$WORKDIR/deploy.tar.gz" HEAD

echo "==> Shipping archive to ${DEPLOY_HOST}"
$SCP "$WORKDIR/deploy.tar.gz" "${DEPLOY_USER}@${DEPLOY_HOST}:/tmp/deploy.tar.gz"

echo "==> Extracting and syncing into ${REMOTE_DIR}"
$SSH bash -s <<'REMOTE'
set -euo pipefail
rm -rf /tmp/deploy-extract
mkdir -p /tmp/deploy-extract
tar -xzf /tmp/deploy.tar.gz -C /tmp/deploy-extract
rsync -a --delete \
  --exclude 'backend/deploy/.env' \
  --exclude 'backend/deploy/.env.ci' \
  --exclude 'backend/data/' \
  --exclude 'backend/skills_library/' \
  --exclude 'backend/content_blobs/' \
  --exclude '.git/' \
  /tmp/deploy-extract/ /opt/auto-skill-connector/
rm -rf /tmp/deploy-extract /tmp/deploy.tar.gz
REMOTE

echo "==> Ensuring bind-mounted data dirs are owned by the container's non-root user (uid 10001)"
$SSH "mkdir -p ${REMOTE_DIR}/backend/data ${REMOTE_DIR}/backend/skills_library && chown -R 10001:10001 ${REMOTE_DIR}/backend/data ${REMOTE_DIR}/backend/skills_library"

# Tag whatever is currently running as :previous before rebuilding, so a
# failed smoke check can restore it. `|| true` covers the very first deploy,
# when no :latest image exists yet to tag.
echo "==> Tagging current images as :previous for rollback"
$SSH "for img in deploy-api deploy-mcp deploy-worker; do docker tag \$img:latest \$img:previous 2>/dev/null || true; done"

rollback() {
  echo "==> Rolling back to the previous images" >&2
  $SSH "cd ${REMOTE_DIR} && for img in deploy-api deploy-mcp deploy-worker; do docker tag \$img:previous \$img:latest 2>/dev/null || true; done && docker compose -f backend/deploy/docker-compose.yml up -d --force-recreate api mcp worker"
}

echo "==> Rebuilding containers"
if ! $SSH "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --build api mcp worker"; then
  echo "FAILED: container rebuild/recreate failed" >&2
  rollback
  exit 1
fi

echo "==> Smoke-checking the public API"
smoke_failed=0
for path in /healthz /skills-catalog; do
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

echo "==> Deploy complete"
