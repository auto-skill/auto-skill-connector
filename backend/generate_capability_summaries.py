"""Backfill/redo capability_summary for every active skill using a local
Ollama model, then re-embed with the summary folded in (see
embeddings.build_embed_text).

Resumable: selects rows with capability_summary IS NULL, so an interrupted
run picks up exactly where it left off, and a full re-run only needs
capability_summary cleared first (see --redo-all).

Writes go through the same localhost REST surface as the embed loop
(reindex.py's pattern), so this is safe to run while the scraper/API is up.

Run:  python generate_capability_summaries.py [--limit N] [--concurrency 8]
"""
import argparse
import asyncio
import json
import os
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx

from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts

BASE = Path(__file__).parent
DB_PATH = Path(os.getenv("LOCAL_DB_PATH", str(BASE / "local_skills.db")))
REST = "http://127.0.0.1:8000/rest/v1/skills?on_conflict=url"
HEADERS = {"Content-Type": "application/json", "Prefer": "resolution=merge-duplicates"}

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
SUMMARY_MODEL = os.getenv("AUTOSKILL_SUMMARY_MODEL", "llama3.2:3b")

SCAN_PAGE = 2000
EMBED_BATCH = 128
UPSERT_CHUNK = 50
MAX_CONTENT_CHARS = 4000
MAX_SUMMARY_CHARS = 2000

SUMMARY_SCHEMA = {
    "type": "object",
    "properties": {"summary": {"type": "string"}},
    "required": ["summary"],
}

SUMMARY_SYSTEM = (
    "You analyze developer tool/skill listings for a search index. Given a name, "
    "short description, and (if available) file content, write a concise capability "
    "summary as JSON: {\"summary\": \"...\"}. "
    "summary: 1-3 plain factual sentences stating what the skill/tool actually does, "
    "what task or workflow it helps with, and any concrete capabilities, triggers, "
    "integrations, or platforms it mentions. Third person, no marketing language, no "
    "meta-commentary ('this skill', 'in summary'), no preamble. If the material is too "
    "sparse to say anything concrete, summarize plainly what little is known instead of "
    "inventing detail."
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def _upsert(client: httpx.AsyncClient, rows: list[dict]) -> None:
    for i in range(0, len(rows), UPSERT_CHUNK):
        chunk = rows[i:i + UPSERT_CHUNK]
        for attempt in range(4):
            try:
                r = await client.post(REST, json=chunk, headers=HEADERS, timeout=120)
                if r.status_code in (200, 201, 204):
                    break
            except httpx.HTTPError:
                pass
            await asyncio.sleep(2 ** attempt)
        else:
            raise RuntimeError("upsert failed after retries; re-run to resume")


def pending_rows(limit: int | None) -> list[dict]:
    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    rows: list[dict] = []
    try:
        offset = 0
        while True:
            page = conn.execute(
                "SELECT id,url,name,source,description,tags FROM skills "
                "WHERE url IS NOT NULL AND quality_status='active' "
                "  AND capability_summary IS NULL "
                "ORDER BY id LIMIT ? OFFSET ?",
                (SCAN_PAGE, offset),
            ).fetchall()
            if not page:
                break
            for r in page:
                d = dict(r)
                try:
                    d["tags"] = json.loads(d.get("tags") or "[]")
                except Exception:
                    d["tags"] = []
                rows.append(d)
                if limit and len(rows) >= limit:
                    return rows
            offset += SCAN_PAGE
    finally:
        conn.close()
    return rows


async def summarize_one(client: httpx.AsyncClient, sem: asyncio.Semaphore, row: dict, content: str) -> str:
    description = (row.get("description") or "").strip()
    body = content[:MAX_CONTENT_CHARS].strip()
    if not description and not body:
        return ""
    user = (
        f"Name: {row.get('name') or ''}\n"
        f"Description: {description or '(none)'}\n"
        f"Content:\n{body or '(no file content available)'}"
    )
    async with sem:
        for attempt in range(2):
            try:
                r = await client.post(
                    f"{OLLAMA_URL}/api/chat",
                    json={
                        "model": SUMMARY_MODEL,
                        "messages": [
                            {"role": "system", "content": SUMMARY_SYSTEM},
                            {"role": "user", "content": user},
                        ],
                        "stream": False,
                        "format": SUMMARY_SCHEMA,
                        "options": {"temperature": 0, "num_predict": 200},
                    },
                    timeout=60,
                )
                if r.status_code != 200:
                    continue
                content_json = r.json().get("message", {}).get("content", "")
                parsed = json.loads(content_json)
                summary = str(parsed.get("summary") or "").strip()
                return summary[:MAX_SUMMARY_CHARS]
            except (json.JSONDecodeError, httpx.HTTPError):
                continue
    return ""


async def process_batch(
    client: httpx.AsyncClient, sem: asyncio.Semaphore, library: LibraryContent, batch: list[dict]
) -> int:
    contents = [library.get(row["url"]) for row in batch]
    summaries = await asyncio.gather(
        *(summarize_one(client, sem, row, content) for row, content in zip(batch, contents))
    )
    for row, summary in zip(batch, summaries):
        row["capability_summary"] = summary
    texts = [build_embed_text(row, content) for row, content in zip(batch, contents)]
    vectors = await asyncio.to_thread(embed_texts, texts, EMBED_BATCH)
    now = _now()
    payload = [
        {
            "url": row["url"],
            "name": row.get("name") or "",
            "source": row.get("source") or "",
            "capability_summary": row["capability_summary"],
            "embedding": vec,
            "embedding_text_hash": embed_text_hash(text),
            "embedded_at": now,
        }
        for row, text, vec in zip(batch, texts, vectors)
    ]
    await _upsert(client, payload)
    return len(batch)


async def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="process at most N rows (testing)")
    parser.add_argument("--concurrency", type=int, default=8, help="concurrent Ollama requests")
    parser.add_argument("--batch-size", type=int, default=200, help="rows per embed+upsert batch")
    args = parser.parse_args()

    library = LibraryContent()
    sem = asyncio.Semaphore(args.concurrency)
    rows = pending_rows(args.limit)
    total = len(rows)
    print(f"[capability-summary] {total} active skill(s) pending capability_summary", flush=True)
    if not total:
        return

    start = time.monotonic()
    done = 0
    async with httpx.AsyncClient() as client:
        for i in range(0, total, args.batch_size):
            batch = rows[i:i + args.batch_size]
            done += await process_batch(client, sem, library, batch)
            elapsed = time.monotonic() - start
            rate = done / elapsed if elapsed else 0
            eta_min = ((total - done) / rate / 60) if rate else float("inf")
            print(
                f"[capability-summary] {done}/{total} "
                f"({rate:.2f}/s, eta {eta_min:.0f}m)",
                flush=True,
            )
    print(f"[capability-summary] done - {done} row(s) summarized and re-embedded", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
