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
SNAPSHOT="$DATA_DIR/.skills-sh-mirror.snapshot.${STAMP}.$$"
STAGING="$DATA_DIR/.local_skills.db.seed.${STAMP}.$$"
BACKUP="$DATA_DIR/backups/local_skills.db.preseed.${STAMP}"

cleanup() { rm -f -- "$ARCHIVE" "$SNAPSHOT" "$STAGING"; }
trap cleanup EXIT

mkdir -p "$DATA_DIR" "$DATA_DIR/backups"
ACTUAL_SHA256=$(sha256sum "$ARCHIVE" | awk '{print $1}')
if [[ "$ACTUAL_SHA256" != "$EXPECTED_SHA256" ]]; then
  echo "compressed mirror hash mismatch" >&2
  exit 1
fi
gzip -t -- "$ARCHIVE"
gzip -dc -- "$ARCHIVE" >"$SNAPSHOT"

python3 - "$SNAPSHOT" <<'PY'
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

if [[ ! -f "$TARGET" ]]; then
  echo "refusing to seed without the existing application database: $TARGET" >&2
  exit 1
fi

# Stop readers and Litestream before touching the inode. Checkpoint any old
# WAL first so a rollback/backup represents the complete old database.
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

# Build a complete application database staging copy, then replace only the
# skills.sh mirror tables from the verified snapshot. The compact snapshot is
# not itself the application DB and must never replace it wholesale.
if ! python3 - "$BACKUP" "$SNAPSHOT" "$STAGING" <<'PY'
import os
import sqlite3
import sys

target_path, snapshot_path, staging_path = sys.argv[1:]
if os.path.exists(staging_path):
    os.unlink(staging_path)

target = sqlite3.connect(target_path, timeout=120)
staging = sqlite3.connect(staging_path, timeout=120)
try:
    target.backup(staging)
    staging.execute("PRAGMA foreign_keys=ON")
    staging.execute("ATTACH DATABASE ? AS mirror_src", (snapshot_path,))
    tables = ("skills_sh_mirror", "skills_sh_sources", "skills_sh_ingestion_attempts")

    def table_columns(conn, schema, table):
        return [row[1] for row in conn.execute(f'PRAGMA {schema}.table_info("{table}")')]

    for table in tables:
        source_columns = table_columns(staging, "mirror_src", table)
        target_columns = table_columns(staging, "main", table)
        if target_columns and target_columns != source_columns:
            raise SystemExit(
                f"target schema mismatch for {table}: "
                f"target={target_columns} snapshot={source_columns}"
            )
        if not target_columns:
            create_sql = staging.execute(
                "SELECT sql FROM mirror_src.sqlite_master "
                "WHERE type='table' AND name=?",
                (table,),
            ).fetchone()[0]
            staging.execute(create_sql)
        quoted = ", ".join(f'"{column}"' for column in source_columns)
        staging.execute(f'DELETE FROM main."{table}"')
        staging.execute(
            f'INSERT INTO main."{table}" ({quoted}) '
            f'SELECT {quoted} FROM mirror_src."{table}"'
        )

    expected_fts = table_columns(staging, "mirror_src", "skills_sh_mirror_fts")
    actual_fts = table_columns(staging, "main", "skills_sh_mirror_fts")
    if actual_fts != expected_fts:
        raise SystemExit(
            f"target FTS schema mismatch: target={actual_fts} snapshot={expected_fts}"
        )
    staging.execute(
        "INSERT INTO main.skills_sh_mirror_fts(skills_sh_mirror_fts) VALUES ('rebuild')"
    )
    staging.commit()
    staging.execute("DETACH DATABASE mirror_src")
    staging.commit()
    result = staging.execute("PRAGMA integrity_check").fetchone()[0]
    if result != "ok":
        raise SystemExit(f"merged application database integrity check failed: {result}")
finally:
    staging.close()
    target.close()
PY
then
  echo "mirror merge failed; restoring the previous application database" >&2
  rm -f -- "$STAGING"
  mv -- "$BACKUP" "$TARGET"
  exit 1
fi

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

if ! mv -- "$STAGING" "$TARGET"; then
  echo "atomic replacement failed; restoring the previous application database" >&2
  mv -- "$BACKUP" "$TARGET" || true
  exit 1
fi
if ! chown 10001:10001 "$TARGET" || ! chmod 0640 "$TARGET"; then
  rollback
  exit 1
fi

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
