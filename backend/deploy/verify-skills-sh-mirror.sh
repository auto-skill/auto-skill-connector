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

path = os.environ.get("SKILLS_SH_MIRROR_DB_PATH", "/data/local_skills.db")
conn = sqlite3.connect(path)
try:
    table = conn.execute(
        "select count(*) from sqlite_master where type='table' and name='skills_sh_mirror'"
    ).fetchone()[0]
    rows = conn.execute("select count(*) from skills_sh_mirror").fetchone()[0] if table else 0
finally:
    conn.close()

result = {"mirror_path": path, "table_present": bool(table), "rows": rows}
print(json.dumps(result, sort_keys=True))
if not table or rows < 1:
    raise SystemExit("skills.sh mirror is absent or empty")
PY
