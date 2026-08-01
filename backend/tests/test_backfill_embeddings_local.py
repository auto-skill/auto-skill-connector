from __future__ import annotations

import sqlite3
import sys

import backfill_embeddings_local as drain
import local_store as store


def test_local_embedding_drain_updates_null_active_rows(tmp_path, monkeypatch) -> None:
    db = tmp_path / "skills.db"
    monkeypatch.setattr(store, "DB_PATH", db)
    store.init_db()
    store.upsert_rows(
        "skills",
        [
            {
                "id": "skill-1",
                "name": "Report skill",
                "description": "Creates reports",
                "source": "github_skill_file",
                "url": "https://github.com/acme/repo/tree/main/skill",
                "quality_status": "active",
            }
        ],
        on_conflict="url",
    )
    monkeypatch.setattr(drain, "embed_texts", lambda texts, batch_size: [[0.1] * 384 for _ in texts])
    monkeypatch.setattr(
        drain,
        "LibraryContent",
        lambda library_dir: type("EmptyLibrary", (), {"get": lambda self, url: ""})(),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["backfill_embeddings_local.py", "--db", str(db), "--library-dir", str(tmp_path / "library")],
    )
    assert drain.main() == 0
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT length(embedding), embedding_text_hash, embedded_at FROM skills WHERE id='skill-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == 384 * 4
    assert row[1]
    assert row[2]
