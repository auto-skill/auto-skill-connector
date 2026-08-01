"""Read-only integrity audit for immutable GitHub/SkillsMP packages.

The legacy ``skills`` rows can remain useful discovery metadata, but an active
GitHub-backed instruction body is only eligible for routing when its complete
package manifest and every content-addressed object are present.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
from pathlib import Path


PACKAGE_SOURCES = {"github", "github_skill_file", "skillsmp", "awesome_list"}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def audit(db_path: Path, package_root: Path) -> dict:
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        required_tables = {"skills", "skill_packages", "skill_package_files"}
        report = {
            "db": str(db_path),
            "package_root": str(package_root),
            "package_tables_present": required_tables.issubset(tables),
            "active_package_sources": {},
            "active_missing_package": 0,
            "active_incomplete_package": 0,
            "active_missing_object": 0,
            "active_hash_mismatch": 0,
            "package_rows": 0,
            "package_files": 0,
            "ok": False,
        }
        if not report["package_tables_present"]:
            return report
        manifests: dict[str, dict] = {}
        for row in conn.execute("SELECT package_hash,manifest_json FROM skill_packages"):
            try:
                manifests[str(row[0])] = json.loads(row[1])
            except (TypeError, json.JSONDecodeError):
                manifests[str(row[0])] = {}
        report["package_rows"] = len(manifests)
        report["package_files"] = int(conn.execute("SELECT COUNT(*) FROM skill_package_files").fetchone()[0])
        for row in conn.execute(
            "SELECT source,package_hash,package_completeness,entrypoint_truncated FROM skills "
            "WHERE quality_status='active' AND source IN ('github','github_skill_file','skillsmp','awesome_list')"
        ):
            source = str(row[0])
            bucket = report["active_package_sources"].setdefault(
                source, {"active": 0, "missing": 0, "incomplete": 0, "missing_object": 0, "hash_mismatch": 0}
            )
            bucket["active"] += 1
            package_hash = str(row[1] or "")
            manifest = manifests.get(package_hash)
            if not manifest:
                bucket["missing"] += 1
                report["active_missing_package"] += 1
                continue
            if manifest.get("completeness_status") != "complete" or bool(row[3]) or bool(manifest.get("entrypoint_truncated")):
                bucket["incomplete"] += 1
                report["active_incomplete_package"] += 1
            for file_info in manifest.get("files") or []:
                digest = str(file_info.get("raw_sha256") or "")
                object_path = package_root / "objects" / digest[:2] / digest
                if not object_path.is_file():
                    bucket["missing_object"] += 1
                    report["active_missing_object"] += 1
                elif _sha256(object_path) != digest:
                    bucket["hash_mismatch"] += 1
                    report["active_hash_mismatch"] += 1
        report["ok"] = all(
            report[key] == 0
            for key in ("active_missing_package", "active_incomplete_package", "active_missing_object", "active_hash_mismatch")
        )
        return report
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--package-root", type=Path, required=True)
    args = parser.parse_args()
    result = audit(args.db, args.package_root)
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
