"""Read-only audit of skills_library completeness vs DB content_hash.

Phase 2A helper: find active/routable rows whose on-disk SKILL.md is missing,
shorter than expected, or whose bytes no longer match the stored content_hash.

Does not modify production data. Safe local usage:

    python audit_library_content.py
    python audit_library_content.py --db data/local_skills.db --library skills_library
    python audit_library_content.py --json report.json

Optional backfill notes (manual; not executed by this script):
  1. Re-run ``/rescan-all`` or the worker scrape so fetch paths rewrite full bodies.
  2. For rows still missing after rescan, clear quality_status/content_hash and
     re-queue the source URL rather than inventing truncated stubs.
  3. Re-pack content_blobs only after the library files are complete.
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

from quality import MIN_CONTENT_CHARS, canonicalize_skill_content, content_hash


def _load_index(library_dir: Path) -> dict:
    index_path = library_dir / "index.json"
    if not index_path.is_file():
        return {}
    raw = json.loads(index_path.read_text(encoding="utf-8-sig", errors="replace"))
    return raw if isinstance(raw, dict) else {}


def audit_library(
    db_path: Path,
    library_dir: Path,
    *,
    short_threshold: int = MIN_CONTENT_CHARS,
) -> dict:
    files_dir = library_dir / "files"
    index = _load_index(library_dir)
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT id, url, name, source, quality_status, content_hash "
            "FROM skills WHERE quality_status='active' ORDER BY url"
        ).fetchall()
    finally:
        conn.close()

    report = {
        "db": str(db_path),
        "library": str(library_dir),
        "active_rows": len(rows),
        "missing_file": [],
        "missing_index": [],
        "hash_mismatch": [],
        "short_content": [],
        "empty_hash": [],
        "ok": 0,
    }

    for row in rows:
        url = row["url"] or ""
        expected_hash = row["content_hash"] or ""
        entry = index.get(url) if url else None
        if not expected_hash:
            report["empty_hash"].append({"id": row["id"], "url": url, "name": row["name"]})
            continue
        if not isinstance(entry, dict) or not entry.get("file"):
            report["missing_index"].append(
                {"id": row["id"], "url": url, "name": row["name"], "content_hash": expected_hash}
            )
            continue
        path = files_dir / entry["file"]
        if not path.is_file():
            report["missing_file"].append(
                {
                    "id": row["id"],
                    "url": url,
                    "name": row["name"],
                    "content_hash": expected_hash,
                    "file": entry["file"],
                }
            )
            continue
        text = canonicalize_skill_content(path.read_text(encoding="utf-8", errors="replace"))
        if not text or len(text) < short_threshold:
            report["short_content"].append(
                {
                    "id": row["id"],
                    "url": url,
                    "name": row["name"],
                    "content_hash": expected_hash,
                    "chars": len(text),
                    "file": entry["file"],
                }
            )
            continue
        actual = content_hash(text)
        if actual != expected_hash:
            report["hash_mismatch"].append(
                {
                    "id": row["id"],
                    "url": url,
                    "name": row["name"],
                    "content_hash": expected_hash,
                    "actual_hash": actual,
                    "chars": len(text),
                    "file": entry["file"],
                }
            )
            continue
        report["ok"] += 1

    report["issue_count"] = (
        len(report["missing_file"])
        + len(report["missing_index"])
        + len(report["hash_mismatch"])
        + len(report["short_content"])
        + len(report["empty_hash"])
    )
    return report


def main(argv: list[str] | None = None) -> int:
    root = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(description="Audit skills_library completeness (read-only).")
    parser.add_argument("--db", type=Path, default=root / "data" / "local_skills.db")
    parser.add_argument("--library", type=Path, default=root / "skills_library")
    parser.add_argument("--short-threshold", type=int, default=MIN_CONTENT_CHARS)
    parser.add_argument("--json", type=Path, help="optional path to write the full report JSON")
    parser.add_argument("--limit", type=int, default=20, help="max sample rows to print per bucket")
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"database not found: {args.db}", file=sys.stderr)
        return 2

    report = audit_library(args.db, args.library, short_threshold=args.short_threshold)
    if args.json:
        args.json.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(
        f"active={report['active_rows']} ok={report['ok']} issues={report['issue_count']} "
        f"(missing_index={len(report['missing_index'])} missing_file={len(report['missing_file'])} "
        f"short={len(report['short_content'])} hash_mismatch={len(report['hash_mismatch'])} "
        f"empty_hash={len(report['empty_hash'])})"
    )
    for bucket in ("missing_index", "missing_file", "short_content", "hash_mismatch", "empty_hash"):
        rows = report[bucket][: args.limit]
        if not rows:
            continue
        print(f"\n[{bucket}] showing {len(rows)}/{len(report[bucket])}")
        for row in rows:
            print(f"  - {row.get('url') or row.get('id')} chars={row.get('chars', '-')}")
    return 1 if report["issue_count"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
