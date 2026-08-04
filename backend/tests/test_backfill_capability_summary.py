from __future__ import annotations

import asyncio
import json
import sqlite3

import backfill_capability_summary as summaries
import local_store as store
from quality import content_hash


def _seed_library(tmp_path, url: str, content: str):
    library = tmp_path / "library"
    files = library / "files"
    files.mkdir(parents=True)
    (files / "skill.md").write_text(content, encoding="utf-8")
    (library / "index.json").write_text(
        json.dumps({url: {"file": "skill.md", "content_hash": content_hash(content)}}),
        encoding="utf-8",
    )
    return library


def test_deterministic_fallback_uses_metadata_without_inventing_content() -> None:
    result = summaries._deterministic_fallback(
        {
            "name": "report-writer",
            "description": "Creates validated reports",
            "tags": '["reporting", "analytics"]',
        },
        "",
    )
    assert result["summary"] == "Creates validated reports."
    assert result["triggers"] == ["reporting", "analytics"]


def test_capability_backfill_batches_embedding_and_uses_explicit_paths(tmp_path, monkeypatch) -> None:
    db = tmp_path / "skills.db"
    url = "https://github.com/acme/repo/tree/main/skill"
    content = "---\nname: report-skill\ndescription: Create reports.\n---\n\nCreate reports.\n"
    library = _seed_library(tmp_path, url, content)

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
                "url": url,
                "content_hash": content_hash(content),
                "quality_status": "active",
            }
        ],
        on_conflict="url",
    )

    async def fake_summary(client, name, description, body):
        assert name == "Report skill"
        assert body == content
        return {"summary": "Creates report files.", "triggers": ["creating reports"]}

    monkeypatch.setattr(summaries, "generate_capability_summary", fake_summary)
    monkeypatch.setattr(
        summaries,
        "embed_texts",
        lambda texts, batch_size: [[0.1] * 384 for _ in texts],
    )

    stats = asyncio.run(
        summaries.run(
            db_path=db,
            library_dir=library,
            limit=1,
            concurrency=1,
            page_size=1,
        )
    )

    assert stats["succeeded"] == 1
    assert stats["embedded"] == 1
    conn = sqlite3.connect(db)
    try:
        row = conn.execute(
            "SELECT capability_summary, triggers, length(embedding), embedding_text_hash "
            "FROM skills WHERE id='skill-1'"
        ).fetchone()
    finally:
        conn.close()
    assert row[0] == "Creates report files."
    assert json.loads(row[1]) == ["creating reports"]
    assert row[2] == 384 * 4
    assert row[3]
