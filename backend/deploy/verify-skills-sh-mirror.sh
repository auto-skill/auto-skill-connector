#!/usr/bin/env bash
set -euo pipefail

# Read-only deployment smoke test. It verifies that the running Compose image
# can see the shared mirror and that the index is non-empty before routing is
# enabled for real users.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
COMPOSE_FILE="$ROOT_DIR/backend/deploy/docker-compose.yml"
cd "$ROOT_DIR"
exec docker compose -f "$COMPOSE_FILE" run --rm --no-deps api \
  python - <<'PY'
import json
import os
import sqlite3
from skills_sh_catalog import _PersistentMirror

path = os.environ.get("SKILLS_SH_MIRROR_DB_PATH", "/data/local_skills.db")
conn = sqlite3.connect(path)
try:
    table = conn.execute(
        "select count(*) from sqlite_master where type='table' and name='skills_sh_mirror'"
    ).fetchone()[0]
    rows = conn.execute("select count(*) from skills_sh_mirror").fetchone()[0] if table else 0
    active = conn.execute(
        "select count(*) from skills_sh_mirror where json_extract(row_json, '$.quality_status')='active'"
    ).fetchone()[0] if table else 0
    metadata_only = conn.execute(
        "select count(*) from skills_sh_mirror where json_extract(row_json, '$.quality_status')='metadata_only'"
    ).fetchone()[0] if table else 0
    rejected = conn.execute(
        "select count(*) from skills_sh_mirror where json_extract(row_json, '$.quality_status')='rejected'"
    ).fetchone()[0] if table else 0
    sources = conn.execute("select count(*) from skills_sh_sources").fetchone()[0] if table else 0
    source_bytes = conn.execute("select coalesce(sum(byte_count), 0) from skills_sh_sources").fetchone()[0] if table else 0
    source_byte_mismatches = conn.execute(
        "select count(*) from skills_sh_sources "
        "where byte_count != length(cast(content as blob))"
    ).fetchone()[0] if table else 0
    active_truncated = conn.execute(
        "select count(*) from skills_sh_mirror "
        "where json_extract(row_json, '$.quality_status')='active' "
        "and coalesce(json_extract(row_json, '$.entrypoint_truncated'), 0) != 0"
    ).fetchone()[0] if table else 0
    active_incomplete_declared = conn.execute(
        "select count(*) from skills_sh_mirror "
        "where json_extract(row_json, '$.quality_status')='active' "
        "and coalesce(json_extract(row_json, '$.package_completeness'), '') != 'complete'"
    ).fetchone()[0] if table else 0
    # The declared package flag is not sufficient for legacy rows: some old
    # snapshots retained a multi-file manifest but only the entrypoint body.
    # Reconcile the manifest against the immutable source table and use this
    # strict count as the production gate.
    active_incomplete = 0
    if table:
        for raw_row in conn.execute(
            "select row_json from skills_sh_mirror "
            "where json_extract(row_json, '$.quality_status')='active'"
        ):
            try:
                value = json.loads(raw_row[0])
            except (TypeError, json.JSONDecodeError):
                active_incomplete += 1
                continue
            content_hash = str(value.get("content_hash") or "")
            source_row = conn.execute(
                "select content,files_json from skills_sh_sources where content_hash=?",
                (content_hash,),
            ).fetchone() if content_hash else None
            try:
                files = json.loads(source_row[1] or "[]") if source_row else []
            except (TypeError, json.JSONDecodeError):
                files = []
            content = str(source_row[0] or "") if source_row else ""
            if not source_row or not _PersistentMirror._source_files_complete(value, files, content):
                active_incomplete += 1
    attempts = conn.execute("select count(*) from skills_sh_ingestion_attempts").fetchone()[0] if table else 0
finally:
    conn.close()

result = {
    "mirror_path": path,
    "table_present": bool(table),
    "rows": rows,
    "active": active,
    "metadata_only": metadata_only,
    "rejected": rejected,
    "source_blobs": sources,
    "source_bytes": source_bytes,
    "source_byte_mismatches": source_byte_mismatches,
        "active_truncated": active_truncated,
        "active_incomplete_declared": active_incomplete_declared,
        "active_incomplete": active_incomplete,
    "ingestion_attempts": attempts,
}
print(json.dumps(result, sort_keys=True))
if not table or rows < 1:
    raise SystemExit("skills.sh mirror is absent or empty")
if source_byte_mismatches or active_truncated or active_incomplete:
    raise SystemExit("skills.sh mirror contains incomplete or truncated active packages")
PY
