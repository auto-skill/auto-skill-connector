"""Backfill skill_packages for active skills that predate immutable package
capture (package_hash IS NULL) -- see package_store.py / local_store.py's
upsert_skill_package.

This does NOT re-fetch each skill's source repo/tree over the network (86k+
repos would be prohibitively slow and rate-limit-hostile). It builds a
single-file package from the already-cached, already-served curated content
(the same body LibraryContent serves today), honestly marked
tree_complete=False -- a real, content-addressed, immutable record, just not
a verified multi-file git-tree snapshot. Rows later re-scanned through the
normal tree-crawl path (scan_skill's _snapshot_github_tree_url_package) get
upgraded to a tree_complete=True package automatically; this backfill never
overwrites an existing package_hash.

Usage:
    python backfill_packages.py --limit 20 --dry-run   # pilot
    python backfill_packages.py                         # full run
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path

from embeddings import LibraryContent
import local_store as store
from package_store import ImmutablePackageStore, PackageFileInput, build_package_manifest, git_blob_sha
from quality import content_hash as compute_content_hash

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

DEFAULT_LIBRARY_DIR = Path(__file__).parent / "skills_library"
DEFAULT_PACKAGE_ROOT = DEFAULT_LIBRARY_DIR / "packages"
ENTRYPOINT = "SKILL.md"
PAGE_SIZE = 500


def _build_and_store(row: sqlite3.Row, content: str, package_root: Path) -> dict:
    content_bytes = content.encode("utf-8")
    file_input = PackageFileInput(
        path=ENTRYPOINT,
        content=content_bytes,
        git_blob_sha=git_blob_sha(content_bytes),
        expected_size=len(content_bytes),
    )
    manifest, objects = build_package_manifest(
        source={"provider": str(row["source"] or "unknown"), "root_path": ""},
        source_url=str(row["url"] or ""),
        entrypoint=ENTRYPOINT,
        files=[file_input],
        tree_complete=False,
        provenance={
            "collector": "backfill_packages (cache-only, single-file capture)",
            "immutable_ref": None,
        },
    )
    ImmutablePackageStore(package_root).put(manifest, objects)
    store.upsert_skill_package(manifest, None, skill_id=row["id"])
    return manifest


def run(
    *,
    db_path: Path,
    library_dir: Path,
    package_root: Path,
    limit: int = 0,
    dry_run: bool = False,
) -> dict[str, int]:
    """Backfill immutable single-file packages from an existing local library.

    The caller owns the database/CAS roots explicitly. This keeps the same
    repair code usable for production containers and the off-host local
    staging database without accidentally writing to backend/local_skills.db.
    """
    store.DB_PATH = Path(db_path)
    store.init_db()
    library = LibraryContent(Path(library_dir))
    package_root = Path(package_root)
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT id,url,name,source,content_hash FROM skills "
            "WHERE quality_status='active' AND package_hash IS NULL ORDER BY id"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
        print(f"backfill-packages: {len(rows)} candidate rows", flush=True)

        succeeded = 0
        skipped_no_content = 0
        skipped_hash_mismatch = 0
        errors = 0
        started = time.monotonic()

        for i, row in enumerate(rows, start=1):
            content = library.get(row["url"] or "")
            if not content:
                skipped_no_content += 1
                continue
            if row["content_hash"] and compute_content_hash(content) != row["content_hash"]:
                skipped_hash_mismatch += 1
                print(f"  row {row['id']} ({row['url']}): content_hash mismatch, skipping", flush=True)
                continue
            if dry_run:
                succeeded += 1
            else:
                try:
                    manifest = _build_and_store(row, content, package_root)
                    succeeded += 1
                    if succeeded <= 5 or succeeded % 500 == 0:
                        print(f"  row {row['id']}: package_hash={manifest['package_hash']}", flush=True)
                except Exception as e:
                    errors += 1
                    print(f"  row {row['id']} ({row['url']}): ERROR {e}", flush=True)

            if i % PAGE_SIZE == 0 or i == len(rows):
                elapsed = time.monotonic() - started
                print(
                    f"backfill-packages: {i}/{len(rows)} attempted, {succeeded} succeeded, "
                    f"{skipped_no_content} no-content, {skipped_hash_mismatch} hash-mismatch, "
                    f"{errors} errors, {elapsed:.0f}s elapsed",
                    flush=True,
                )

        print(
            f"backfill-packages: done. {succeeded} succeeded, {skipped_no_content} skipped (no cached content), "
            f"{skipped_hash_mismatch} skipped (hash mismatch), {errors} errors",
            flush=True,
        )
        return {
            "candidates": len(rows),
            "succeeded": succeeded,
            "skipped_no_content": skipped_no_content,
            "skipped_hash_mismatch": skipped_hash_mismatch,
            "errors": errors,
        }
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=store.DB_PATH)
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR)
    parser.add_argument("--package-root", type=Path, default=DEFAULT_PACKAGE_ROOT)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N rows (0 = all).")
    parser.add_argument("--dry-run", action="store_true", help="Build manifests, do not write to disk/DB.")
    args = parser.parse_args()
    run(
        db_path=args.db,
        library_dir=args.library_dir,
        package_root=args.package_root,
        limit=args.limit,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
