"""Backfill quality metadata for existing local skills rows.

This is intentionally non-destructive: it marks quality_status/reasons,
platforms, category, and same-run content-hash duplicates. Search and routing
then decide how conservative to be from those fields.

Run while the API is stopped or quiet:
    python backfill_quality.py
"""

from __future__ import annotations

import json
import os
import sqlite3
import tempfile

from embeddings import LibraryContent
from local_store import DB_PATH, init_db
from quality import evaluate_quality, pick_canonical

PAGE_SIZE = 1000


def _decode_json(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


def main() -> None:
    init_db()
    library = LibraryContent()
    index_path = library.library_dir / "index.json"
    try:
        raw_index = json.loads(index_path.read_text(encoding="utf-8"))
    except Exception:
        raw_index = {}
    index_dirty = False
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    # Two passes instead of one: canonical-duplicate selection needs to see
    # every row sharing a content_hash before deciding a winner (highest
    # quality_score/stars/most-recent -- see quality.pick_canonical), which
    # a single streaming "first id wins" pass can't do. That old rule is why
    # duplicates scraped across separate runs kept surfacing as active: the
    # earliest-id fork always won regardless of which fork was actually best.
    records: list[dict] = []
    missing_content_github_skill_file = 0
    try:
        offset = 0
        while True:
            rows = conn.execute(
                "SELECT id,url,name,source,description,tags,raw,discovered_at,scanned_at "
                "FROM skills ORDER BY id LIMIT ? OFFSET ?",
                (PAGE_SIZE, offset),
            ).fetchall()
            if not rows:
                break

            for row in rows:
                skill = dict(row)
                skill["tags"] = _decode_json(skill.get("tags"), [])
                skill["raw"] = _decode_json(skill.get("raw"), {})
                url = skill.get("url") or ""
                content = library.get(url)
                if not content and skill.get("source") == "github_skill_file":
                    missing_content_github_skill_file += 1
                quality = evaluate_quality(skill, content)
                chash = quality.get("content_hash")
                entry = raw_index.get(url)
                if content and chash and isinstance(entry, dict) and entry.get("content_hash") != chash:
                    entry["content_hash"] = chash
                    index_dirty = True

                records.append({
                    "id": skill["id"],
                    "url": url,
                    "raw": skill.get("raw"),
                    "discovered_at": skill.get("discovered_at"),
                    "scanned_at": skill.get("scanned_at"),
                    "content_hash": chash,
                    "canonical_id": None,
                    "quality_status": quality.get("quality_status"),
                    "quality_reasons": quality.get("quality_reasons") or [],
                    "quality_score": quality.get("quality_score") or 0,
                    "platforms": quality.get("platforms") or [],
                    "category": quality.get("category"),
                })

            offset += PAGE_SIZE
            print(f"quality-backfill: pass 1, {len(records)} rows evaluated", flush=True)

        if missing_content_github_skill_file:
            print(
                f"quality-backfill: {missing_content_github_skill_file} github_skill_file rows have no "
                "locally saved content (LibraryContent is local-file-only, no network re-fetch here -- "
                "these need a scraper.py re-scan to recover a content_hash).",
                flush=True,
            )

        by_hash: dict[str, list[dict]] = {}
        for record in records:
            if record["quality_status"] == "active" and record["content_hash"]:
                by_hash.setdefault(record["content_hash"], []).append(record)

        duplicates = 0
        for group in by_hash.values():
            if len(group) < 2:
                continue
            canonical = pick_canonical(group)
            for record in group:
                if record is canonical:
                    continue
                record["quality_status"] = "duplicate"
                reasons = set(record["quality_reasons"])
                reasons.add("duplicate-content")
                record["quality_reasons"] = sorted(reasons)
                record["canonical_id"] = canonical["id"]
                duplicates += 1

        conn.executemany(
            "UPDATE skills SET content_hash=?, canonical_id=?, quality_status=?, "
            "quality_reasons=?, quality_score=?, platforms=?, category=? WHERE id=?",
            [
                (
                    record["content_hash"] or None,
                    record["canonical_id"],
                    record["quality_status"],
                    json.dumps(record["quality_reasons"]),
                    record["quality_score"],
                    json.dumps(record["platforms"]),
                    record["category"],
                    record["id"],
                )
                for record in records
            ],
        )
        conn.commit()
        print(f"quality-backfill: {len(records)} rows, {duplicates} duplicates", flush=True)

        if index_dirty:
            fd, tmp = tempfile.mkstemp(dir=str(index_path.parent), suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(raw_index, f, indent=2)
                os.replace(tmp, index_path)
            except Exception:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
                raise
    finally:
        conn.close()


if __name__ == "__main__":
    main()
