#!/usr/bin/env bash
set -euo pipefail

# Fail-closed audit for active GitHub/SkillsMP/curated-list instruction rows.
# This is intentionally separate from the skills.sh mirror verifier: the
# sources have different provenance and storage invariants.
ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
DB_PATH="${LOCAL_DB_PATH:-$ROOT_DIR/backend/data/local_skills.db}"
PACKAGE_ROOT="${PACKAGE_ROOT:-$ROOT_DIR/backend/skills_library/packages}"
cd "$ROOT_DIR"
PYTHON_BIN="${PYTHON_BIN:-python3}"
command -v "$PYTHON_BIN" >/dev/null 2>&1 || PYTHON_BIN=python
exec "$PYTHON_BIN" backend/audit_package_integrity.py --db "$DB_PATH" --package-root "$PACKAGE_ROOT"
