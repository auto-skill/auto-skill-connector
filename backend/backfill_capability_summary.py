"""Backfill capability_summary/triggers/embedding for active skills that
predate the generate_capability_summary() pipeline (see scan_skill()).

Deliberately narrower than a full rescan: only fills the missing summary/
triggers/embedding fields on already-served, already-curated content. Does
not re-fetch, re-curate, or re-run safety-stripping, so content_hash never
changes and nothing already serving correctly is disturbed -- safety
stripping already applies defense-in-depth at serve time regardless
(see context_guard.build_context_guard).

Usage:
    python backfill_capability_summary.py --limit 20 --dry-run   # pilot
    python backfill_capability_summary.py                        # full run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sqlite3
import sys
import time
from datetime import datetime, timezone

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts, generate_capability_summary
from local_store import DB_PATH, init_db, pack_embedding

PAGE_SIZE = 40


def _decode_json(value, default):
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(value)
    except Exception:
        return default


async def _process_row(client: httpx.AsyncClient, library: LibraryContent, row: dict) -> dict | None:
    content = library.get(row["url"] or "")
    if not content:
        return None
    understanding = await generate_capability_summary(client, row.get("name") or "", row.get("description") or "", content)
    summary = understanding.get("summary") or ""
    triggers = understanding.get("triggers") or []
    if not summary and not triggers:
        return None
    embed_row = dict(row)
    embed_row["capability_summary"] = summary
    embed_row["triggers"] = triggers
    embed_text = build_embed_text(embed_row, content)
    vectors = await asyncio.to_thread(embed_texts, [embed_text])
    return {
        "id": row["id"],
        "capability_summary": summary,
        "triggers": triggers,
        "embedding": vectors[0],
        "embedding_text_hash": embed_text_hash(embed_text),
        "embedded_at": datetime.now(timezone.utc).isoformat(),
    }


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="Process at most N rows (0 = all).")
    parser.add_argument("--dry-run", action="store_true", help="Print results, do not write to the DB.")
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()

    init_db()
    library = LibraryContent()
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT id,url,name,description,tags,triggers,tools_hash,retrieval_text "
            "FROM skills WHERE quality_status='active' "
            "AND (capability_summary IS NULL OR capability_summary='') ORDER BY id"
        )
        if args.limit:
            query += f" LIMIT {args.limit}"
        rows = conn.execute(query).fetchall()
        print(f"backfill-capability-summary: {len(rows)} candidate rows", flush=True)

        semaphore = asyncio.Semaphore(args.concurrency)
        results: list[dict] = []
        skipped_no_content = 0
        skipped_empty_summary = 0
        started = time.monotonic()

        async def bounded(row: sqlite3.Row) -> None:
            nonlocal skipped_no_content, skipped_empty_summary
            skill = dict(row)
            skill["tags"] = _decode_json(skill.get("tags"), [])
            try:
                async with semaphore:
                    result = await _process_row(client, library, skill)
                if result is None:
                    if not library.get(skill["url"] or ""):
                        skipped_no_content += 1
                    else:
                        skipped_empty_summary += 1
                    return
                results.append(result)
                print(
                    f"  row {result['id']}: summary={result['capability_summary'][:80]!r} "
                    f"triggers={result['triggers']}",
                    flush=True,
                )
            except Exception as e:
                print(f"  row {skill['id']} ({skill['url']}): ERROR {e}", flush=True)

        async with httpx.AsyncClient(timeout=90) as client:
            for start in range(0, len(rows), PAGE_SIZE):
                batch = rows[start:start + PAGE_SIZE]
                await asyncio.gather(*(bounded(row) for row in batch))
                elapsed = time.monotonic() - started
                done = min(start + PAGE_SIZE, len(rows))
                print(
                    f"backfill-capability-summary: {done}/{len(rows)} rows attempted, "
                    f"{len(results)} succeeded, {skipped_no_content} no-content, "
                    f"{skipped_empty_summary} empty-summary, {elapsed:.0f}s elapsed",
                    flush=True,
                )
                if not args.dry_run and results:
                    conn.executemany(
                        "UPDATE skills SET capability_summary=?, triggers=?, embedding=?, "
                        "embedding_text_hash=?, embedded_at=? WHERE id=?",
                        [
                            (
                                r["capability_summary"],
                                json.dumps(r["triggers"]),
                                pack_embedding(r["embedding"]),
                                r["embedding_text_hash"],
                                r["embedded_at"],
                                r["id"],
                            )
                            for r in results
                        ],
                    )
                    conn.commit()
                    results.clear()

        print(
            f"backfill-capability-summary: done. {skipped_no_content} skipped (no cached content), "
            f"{skipped_empty_summary} skipped (LLM returned empty summary+triggers)",
            flush=True,
        )
    finally:
        conn.close()


if __name__ == "__main__":
    asyncio.run(main())
