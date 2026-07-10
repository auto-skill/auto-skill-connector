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

echo "==> Rebuilding containers"
$SSH "cd ${REMOTE_DIR} && docker compose -f backend/deploy/docker-compose.yml up -d --build api mcp worker"

echo "==> Smoke-checking the public API"
for path in /healthz /skills-catalog; do
  url="https://skills.autoskill.dev${path}"
  status=$(curl -s -o /dev/null -w "%{http_code}" "$url" --max-time 15)
  if [ "$status" != "200" ] && [ "$path" != "/skills-catalog" ]; then
    echo "FAILED: $url returned $status" >&2
    exit 1
  fi
  if [ "$path" = "/skills-catalog" ] && [ "$status" != "401" ] && [ "$status" != "200" ]; then
    # /skills-catalog requires a bearer token (401 without one is expected and
    # still proves the route exists and the guard is evaluating it correctly;
    # a 403 "read-only public API" would mean the route regressed again).
    echo "FAILED: $url returned $status" >&2
    exit 1
  fi
  echo "OK: $url -> $status"
done

echo "==> Deploy complete"
