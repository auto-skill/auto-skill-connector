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
from local_store import DB_PATH, init_db, upsert_skill_package
from package_store import ImmutablePackageStore, PackageFileInput, build_package_manifest, git_blob_sha
from quality import content_hash as compute_content_hash

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

PACKAGE_ROOT = Path(__file__).parent / "skills_library" / "packages"
ENTRYPOINT = "SKILL.md"
PAGE_SIZE = 500


def _build_and_store(row: sqlite3.Row, content: str) -> dict:
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
    ImmutablePackageStore(PACKAGE_ROOT).put(manifest, objects)
    upsert_skill_package(manifest, None, skill_id=row["id"])
    return manifest


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Process at most N rows (0 = all).")
    parser.add_argument("--dry-run", action="store_true", help="Build manifests, do not write to disk/DB.")
    args = parser.parse_args()

    init_db()
    library = LibraryContent()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT id,url,name,source,content_hash FROM skills "
            "WHERE quality_status='active' AND package_hash IS NULL ORDER BY id"
        )
        if args.limit:
            query += f" LIMIT {args.limit}"
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
            if args.dry_run:
                succeeded += 1
            else:
                try:
                    manifest = _build_and_store(row, content)
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
    finally:
        conn.close()


if __name__ == "__main__":
    main()
