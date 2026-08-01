#!/usr/bin/env bash
# Seed the shared production skills.sh mirror without touching skills_library.
# The input is a verified compact SQLite snapshot. It is compressed for
# transport, hash-checked on both ends, and atomically swapped after integrity
# validation. The previous database is retained for rollback.
set -euo pipefail

MIRROR_DB_PATH="${1:-${MIRROR_DB_PATH:-}}"
if [[ -z "$MIRROR_DB_PATH" ]]; then
  echo "usage: $0 <compact-mirror.db>" >&2
  echo "required env: DEPLOY_HOST DEPLOY_USER DEPLOY_SSH_KEY_PATH" >&2
  exit 2
fi
: "${DEPLOY_HOST:?DEPLOY_HOST is required}"
: "${DEPLOY_USER:?DEPLOY_USER is required}"
: "${DEPLOY_SSH_KEY_PATH:?DEPLOY_SSH_KEY_PATH is required}"

REMOTE_DIR="${REMOTE_DIR:-/opt/auto-skill-connector}"
REMOTE_ARCHIVE="/tmp/autoskill-skills-sh-mirror-$$.db.gz"
ARCHIVE_PATH="${MIRROR_ARCHIVE_PATH:-${MIRROR_DB_PATH}.gz}"

if [[ ! -f "$MIRROR_DB_PATH" ]]; then
  echo "mirror snapshot not found: $MIRROR_DB_PATH" >&2
  exit 1
fi
if [[ ! -f "$ARCHIVE_PATH" ]]; then
  echo "==> Compressing mirror snapshot for transport"
  gzip -n -9 -c -- "$MIRROR_DB_PATH" >"$ARCHIVE_PATH"
fi

if command -v sha256sum >/dev/null 2>&1; then
  LOCAL_SHA256=$(sha256sum "$ARCHIVE_PATH" | awk '{print $1}')
else
  LOCAL_SHA256=$(shasum -a 256 "$ARCHIVE_PATH" | awk '{print $1}')
fi

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

echo "==> Uploading compressed mirror ($(du -h "$ARCHIVE_PATH" | awk '{print $1}'))"
"${SCP[@]}" "$ARCHIVE_PATH" "${DEPLOY_USER}@${DEPLOY_HOST}:${REMOTE_ARCHIVE}"

echo "==> Verifying and atomically installing the remote mirror"
"${SSH[@]}" "sudo bash -s -- '$REMOTE_ARCHIVE' '$LOCAL_SHA256' '$REMOTE_DIR'" <<'REMOTE'
set -euo pipefail
ARCHIVE="$1"
EXPECTED_SHA256="$2"
REMOTE_DIR="$3"
DATA_DIR="$REMOTE_DIR/backend/data"
TARGET="$DATA_DIR/local_skills.db"
COMPOSE=(docker compose -f "$REMOTE_DIR/backend/deploy/docker-compose.yml")
STAMP=$(date -u +%Y%m%dT%H%M%SZ)
STAGING="$DATA_DIR/.local_skills.db.seed.${STAMP}.$$"
BACKUP="$DATA_DIR/backups/local_skills.db.preseed.${STAMP}"

cleanup() { rm -f -- "$ARCHIVE" "$STAGING"; }
trap cleanup EXIT

mkdir -p "$DATA_DIR" "$DATA_DIR/backups"
ACTUAL_SHA256=$(sha256sum "$ARCHIVE" | awk '{print $1}')
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "compressed mirror hash mismatch" >&2
  exit 1
fi
gzip -t -- "$ARCHIVE"
gzip -dc -- "$ARCHIVE" >"$STAGING"

python3 - "$STAGING" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1], timeout=60)
try:
    result = conn.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"SQLite integrity check failed: {result}")
finally:
    conn.close()
PY

chown 10001:10001 "$STAGING"
chmod 0640 "$STAGING"

# Stop readers before replacing the inode. Checkpoint any old WAL first so a
# rollback/backup represents the complete old database, not just its main file.
if ! "${COMPOSE[@]}" stop api admin-local mcp litestream >/dev/null; then
  echo "could not stop database readers; refusing to replace the mirror" >&2
  exit 1
fi
if [[ -f "$TARGET" ]]; then
  python3 - "$TARGET" <<'PY'
import sqlite3
import sys

conn = sqlite3.connect(sys.argv[1], timeout=60)
try:
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.commit()
finally:
    conn.close()
PY
  mv -- "$TARGET" "$BACKUP"
  rm -f -- "${TARGET}-wal" "${TARGET}-shm"
fi
mv -- "$STAGING" "$TARGET"
chown 10001:10001 "$TARGET"
chmod 0640 "$TARGET"

rollback() {
  echo "seed verification failed; rolling back" >&2
  "${COMPOSE[@]}" stop api admin-local mcp litestream >/dev/null 2>&1 || true
  rm -f -- "$TARGET" "${TARGET}-wal" "${TARGET}-shm"
  if [[ -f "$BACKUP" ]]; then
    mv -- "$BACKUP" "$TARGET"
    chown 10001:10001 "$TARGET"
    chmod 0640 "$TARGET"
  fi
  "${COMPOSE[@]}" up -d api admin-local mcp litestream >/dev/null 2>&1 || true
}

if ! "${COMPOSE[@]}" up -d api >/dev/null; then
  rollback
  exit 1
fi
ready=0
for _ in $(seq 1 60); do
  if curl -fsS --max-time 5 http://127.0.0.1:8000/readyz >/dev/null; then
    ready=1
    break
  fi
  sleep 3
done
if [[ "$ready" -ne 1 ]]; then
  rollback
  exit 1
fi
if ! "${COMPOSE[@]}" up -d admin-local mcp litestream >/dev/null; then
  rollback
  exit 1
fi

echo "seeded mirror: $TARGET"
echo "rollback backup: $BACKUP"
REMOTE

echo "==> Mirror seed installed and API readiness passed"
