"""Re-embed active skills directly against the shared SQLite database.

This is the safe operator path after package hydration. It runs while API
readers are stopped, avoiding a second ONNX model inside the serving process;
the API is restarted by the companion deploy script so its vector cache is
fresh when the new embeddings become routable.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
from pathlib import Path

import local_store as store
from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=store.DB_PATH)
    parser.add_argument("--library-dir", type=Path, default=Path(__file__).parent / "skills_library")
    parser.add_argument("--limit", type=int, default=0, help="maximum rows; zero means all")
    parser.add_argument("--batch-size", type=int, default=8)
    args = parser.parse_args()
    store.DB_PATH = args.db
    store.init_db()
    library = LibraryContent(args.library_dir)
    total = 0
    while True:
        conn = store.get_conn()
        try:
            sql = (
                "SELECT id,url,name,source,description,tags,capability_summary,retrieval_text "
                "FROM skills WHERE embedding IS NULL AND url IS NOT NULL AND quality_status='active' "
                "ORDER BY id LIMIT ?"
            )
            page_size = args.batch_size if not args.limit else min(args.batch_size, args.limit - total)
            if page_size <= 0:
                break
            rows = [dict(row) for row in conn.execute(sql, (page_size,)).fetchall()]
        finally:
            conn.close()
        if not rows:
            break
        texts = [build_embed_text(row, library.get(row.get("url") or "")) for row in rows]
        vectors = embed_texts(texts, args.batch_size)
        now = datetime.now(timezone.utc).isoformat()
        conn = store.get_conn()
        try:
            conn.executemany(
                "UPDATE skills SET embedding=?, embedding_text_hash=?, embedded_at=? WHERE id=?",
                [
                    (store.pack_embedding(vector), embed_text_hash(text), now, row["id"])
                    for row, text, vector in zip(rows, texts, vectors)
                ],
            )
            conn.commit()
        finally:
            conn.close()
        store.invalidate_vector_cache()
        total += len(rows)
        print(f"embedded {total} skills", flush=True)
    print(f"done - {total} skills embedded", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
