"""Re-embed active skills directly against the shared SQLite database.

This is the safe operator path after package hydration. It runs while API
readers are stopped, avoiding a second ONNX model inside the serving process;
the API is restarted by the companion deploy script so its vector cache is
fresh when the new embeddings become routable.
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path

import local_store as store
from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts


def _decode_json_list(value: object) -> list:
    """Match delta export's canonical representation for JSON list fields."""
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return []
    return value if isinstance(value, list) else []


def run(
    *,
    db_path: Path,
    library_dir: Path,
    limit: int = 0,
    batch_size: int = 8,
    refresh_stale: bool = False,
    dry_run: bool = False,
) -> dict[str, int]:
    """Embed missing rows, or refresh rows whose canonical text changed."""
    store.DB_PATH = Path(db_path)
    store.init_db()
    library = LibraryContent(Path(library_dir))
    batch_size = max(1, int(batch_size))
    scanned = 0
    updated = 0
    last_id = ""
    while True:
        conn = store.get_conn()
        try:
            page_size = batch_size if not limit else min(batch_size, limit - scanned)
            if page_size <= 0:
                break
            if refresh_stale:
                sql = (
                    "SELECT id,url,name,source,description,tags,triggers,capability_summary,retrieval_text, "
                    "embedding_text_hash,embedding "
                    "FROM skills WHERE url IS NOT NULL AND quality_status='active' "
                )
                params: tuple[object, ...] = ()
                if last_id:
                    sql += "AND id > ? "
                    params = (last_id,)
                sql += "ORDER BY id LIMIT ?"
                params += (page_size,)
            else:
                sql = (
                    "SELECT id,url,name,source,description,tags,triggers,capability_summary,retrieval_text, "
                    "embedding_text_hash,embedding "
                    "FROM skills WHERE embedding IS NULL AND url IS NOT NULL AND quality_status='active' "
                    "ORDER BY id LIMIT ?"
                )
                params = (page_size,)
            rows = [dict(row) for row in conn.execute(sql, params).fetchall()]
        finally:
            conn.close()
        if not rows:
            break
        last_id = str(rows[-1]["id"])
        scanned += len(rows)
        candidates: list[tuple[dict, str]] = []
        for row in rows:
            row["tags"] = _decode_json_list(row.get("tags"))
            row["triggers"] = _decode_json_list(row.get("triggers"))
            text = build_embed_text(row, library.get(row.get("url") or ""))
            if refresh_stale and row.get("embedding") is not None and row.get("embedding_text_hash") == embed_text_hash(text):
                continue
            candidates.append((row, text))
        if not candidates:
            continue
        if dry_run:
            updated += len(candidates)
            print(f"would embed {updated} skills", flush=True)
            continue
        texts = [text for _, text in candidates]
        vectors = embed_texts(texts, min(batch_size, len(texts)))
        now = datetime.now(timezone.utc).isoformat()
        conn = store.get_conn()
        try:
            conn.executemany(
                "UPDATE skills SET embedding=?, embedding_text_hash=?, embedded_at=? WHERE id=?",
                [
                    (store.pack_embedding(vector), embed_text_hash(text), now, row["id"])
                    for (row, text), vector in zip(candidates, vectors)
                ],
            )
            conn.commit()
        finally:
            conn.close()
        store.invalidate_vector_cache()
        updated += len(candidates)
        print(f"embedded {updated} skills", flush=True)
    print(f"done - {updated} skills embedded; scanned {scanned}", flush=True)
    return {"scanned": scanned, "updated": updated}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=store.DB_PATH)
    parser.add_argument("--library-dir", type=Path, default=Path(__file__).parent / "skills_library")
    parser.add_argument("--limit", type=int, default=0, help="maximum rows; zero means all")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument(
        "--refresh-stale",
        action="store_true",
        help="scan active rows and re-embed only when embedding_text_hash is stale",
    )
    parser.add_argument("--dry-run", action="store_true", help="report candidates without writing embeddings")
    args = parser.parse_args()
    run(
        db_path=args.db,
        library_dir=args.library_dir,
        limit=args.limit,
        batch_size=args.batch_size,
        refresh_stale=args.refresh_stale,
        dry_run=args.dry_run,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
