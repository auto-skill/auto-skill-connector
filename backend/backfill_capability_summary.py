"""Backfill capability summaries, triggers, and embeddings from cached content.

This repair only reads already-served library files. It does not re-fetch,
re-curate, or re-run safety stripping, so content hashes and CAS bytes remain
unchanged. The database, library, and Ollama roots are explicit so the same
bounded repair works against production containers or the off-host local
staging database.

Usage:
    python backfill_capability_summary.py --db path/to/local_skills.db \
        --library-dir path/to/skills_library --limit 20 --dry-run
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import embeddings as embedding_lib
from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts, generate_capability_summary
import local_store as store


DEFAULT_LIBRARY_DIR = Path(__file__).parent / "skills_library"
PAGE_SIZE = 40
_WS_RE = re.compile(r"\s+")


def _decode_json(value: object, default: object) -> object:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except Exception:
        return default


def _deterministic_fallback(row: dict, content: str) -> dict[str, object]:
    """Produce a conservative metadata-only index signal when Ollama is absent."""
    description = _WS_RE.sub(" ", str(row.get("description") or "")).strip()
    name = _WS_RE.sub(" ", re.sub(r"[-_]+", " ", str(row.get("name") or ""))).strip()
    summary = description
    if not summary and content:
        for line in content.splitlines():
            candidate = line.strip().lstrip("#*- ").strip()
            if candidate:
                summary = _WS_RE.sub(" ", candidate)
                break
    if not summary:
        summary = f"Provides the {name or 'skill'} workflow."
    summary = summary[: embedding_lib.SUMMARY_MAX_CHARS].strip()
    if summary and summary[-1] not in ".!?":
        summary += "."
    raw_tags = _decode_json(row.get("tags"), [])
    tags = raw_tags if isinstance(raw_tags, list) else []
    triggers = []
    for tag in tags:
        phrase = _WS_RE.sub(" ", str(tag or "")).strip()[: embedding_lib.MAX_TRIGGER_CHARS]
        if phrase and phrase not in triggers:
            triggers.append(phrase)
        if len(triggers) >= embedding_lib.MAX_TRIGGERS:
            break
    if not triggers and name:
        triggers = [f"{name} tasks"[: embedding_lib.MAX_TRIGGER_CHARS]]
    return {"summary": summary, "triggers": triggers}


async def _process_row(
    client: httpx.AsyncClient,
    library: LibraryContent,
    row: dict,
    *,
    deterministic_fallback: bool = False,
) -> dict | None:
    content = library.get(row["url"] or "")
    if not content and not deterministic_fallback:
        return None
    if content:
        understanding = await generate_capability_summary(
            client,
            row.get("name") or "",
            row.get("description") or "",
            content,
        )
    else:
        understanding = {"summary": "", "triggers": []}
    summary = understanding.get("summary") or ""
    triggers = understanding.get("triggers") or []
    used_fallback = False
    if deterministic_fallback and not summary and not triggers:
        understanding = _deterministic_fallback(row, content)
        summary = understanding["summary"]
        triggers = understanding["triggers"]
        used_fallback = True
    if not summary and not triggers:
        return None
    embed_row = dict(row)
    embed_row["capability_summary"] = summary
    embed_row["triggers"] = triggers
    return {
        "id": row["id"],
        "capability_summary": summary,
        "triggers": triggers,
        "embed_text": build_embed_text(embed_row, content),
        "fallback": used_fallback,
    }


async def run(
    *,
    db_path: Path,
    library_dir: Path,
    limit: int = 0,
    dry_run: bool = False,
    concurrency: int = 2,
    page_size: int = PAGE_SIZE,
    ollama_url: str | None = None,
    deterministic_fallback: bool = False,
) -> dict[str, int]:
    """Fill capability metadata and re-embed each repaired page safely."""
    store.DB_PATH = Path(db_path)
    if ollama_url:
        embedding_lib.OLLAMA_URL = ollama_url.rstrip("/")
    store.init_db()
    library = LibraryContent(Path(library_dir))
    concurrency = max(1, min(int(concurrency), 4))
    page_size = max(1, int(page_size))
    conn = sqlite3.connect(store.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        query = (
            "SELECT id,url,name,description,tags,triggers,tools_hash,retrieval_text "
            "FROM skills WHERE quality_status='active' AND url IS NOT NULL "
            "AND (capability_summary IS NULL OR capability_summary='' "
            "OR triggers IS NULL OR triggers='[]') ORDER BY id"
        )
        if limit:
            query += f" LIMIT {int(limit)}"
        rows = conn.execute(query).fetchall()
        print(f"backfill-capability-summary: {len(rows)} candidate rows", flush=True)

        semaphore = asyncio.Semaphore(concurrency)
        skipped_no_content = 0
        skipped_empty_summary = 0
        errors = 0
        succeeded = 0
        embedded = 0
        fallback_count = 0
        started = time.monotonic()

        async with httpx.AsyncClient(timeout=90) as client:
            for start in range(0, len(rows), page_size):
                batch = rows[start:start + page_size]
                page_results: list[dict] = []

                async def bounded(row: sqlite3.Row) -> None:
                    nonlocal skipped_no_content, skipped_empty_summary, errors, fallback_count
                    skill = dict(row)
                    skill["tags"] = _decode_json(skill.get("tags"), [])
                    skill["triggers"] = _decode_json(skill.get("triggers"), [])
                    try:
                        async with semaphore:
                            result = await _process_row(
                                client,
                                library,
                                skill,
                                deterministic_fallback=deterministic_fallback,
                            )
                        if result is None:
                            if not library.get(skill["url"] or ""):
                                skipped_no_content += 1
                            else:
                                skipped_empty_summary += 1
                            return
                        page_results.append(result)
                        if result["fallback"]:
                            fallback_count += 1
                        print(
                            f"  row {result['id']}: summary={result['capability_summary'][:80]!r} "
                            f"triggers={result['triggers']}",
                            flush=True,
                        )
                    except Exception as e:
                        errors += 1
                        print(f"  row {skill['id']} ({skill['url']}): ERROR {e}", flush=True)

                await asyncio.gather(*(bounded(row) for row in batch))
                if page_results and not dry_run:
                    try:
                        texts = [result["embed_text"] for result in page_results]
                        vectors = await asyncio.to_thread(embed_texts, texts, min(32, len(texts)))
                        now = datetime.now(timezone.utc).isoformat()
                        conn.executemany(
                            "UPDATE skills SET capability_summary=?, triggers=?, embedding=?, "
                            "embedding_text_hash=?, embedded_at=? WHERE id=?",
                            [
                                (
                                    result["capability_summary"],
                                    json.dumps(result["triggers"]),
                                    store.pack_embedding(vector),
                                    embed_text_hash(result["embed_text"]),
                                    now,
                                    result["id"],
                                )
                                for result, vector in zip(page_results, vectors)
                            ],
                        )
                        conn.commit()
                        embedded += len(page_results)
                    except Exception as e:
                        conn.rollback()
                        errors += len(page_results)
                        print(f"  embedding page ERROR: {e}", flush=True)
                succeeded += len(page_results)
                elapsed = time.monotonic() - started
                done = min(start + page_size, len(rows))
                print(
                    f"backfill-capability-summary: {done}/{len(rows)} rows attempted, "
                    f"{succeeded} summaries, {embedded} embedded, {fallback_count} fallback, "
                    f"{skipped_no_content} no-content, "
                    f"{skipped_empty_summary} empty-summary, {errors} errors, {elapsed:.0f}s elapsed",
                    flush=True,
                )

        print(
            f"backfill-capability-summary: done. {succeeded} summaries, {embedded} embedded, "
            f"{fallback_count} fallback, "
            f"{skipped_no_content} skipped (no cached content), "
            f"{skipped_empty_summary} skipped (LLM returned empty summary+triggers), {errors} errors",
            flush=True,
        )
        return {
            "candidates": len(rows),
            "succeeded": succeeded,
            "embedded": embedded,
            "fallback": fallback_count,
            "skipped_no_content": skipped_no_content,
            "skipped_empty_summary": skipped_empty_summary,
            "errors": errors,
        }
    finally:
        conn.close()


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=store.DB_PATH)
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR)
    parser.add_argument("--limit", type=int, default=0, help="Process at most N rows (0 = all).")
    parser.add_argument("--dry-run", action="store_true", help="Print results, do not write to the DB.")
    parser.add_argument("--concurrency", type=int, default=2, help="Bounded concurrent LLM requests (1-4).")
    parser.add_argument("--page-size", type=int, default=PAGE_SIZE)
    parser.add_argument("--ollama-url", default=None, help="Override the local Ollama base URL.")
    parser.add_argument(
        "--deterministic-fallback",
        action="store_true",
        help="use description/tags when Ollama is unavailable; leaves full-route package gates unchanged",
    )
    args = parser.parse_args()
    if not 1 <= args.concurrency <= 4:
        parser.error("--concurrency must be between 1 and 4")
    stats = await run(
        db_path=args.db,
        library_dir=args.library_dir,
        limit=args.limit,
        dry_run=args.dry_run,
        concurrency=args.concurrency,
        page_size=args.page_size,
        ollama_url=args.ollama_url,
        deterministic_fallback=args.deterministic_fallback,
    )
    return 1 if stats["errors"] else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
