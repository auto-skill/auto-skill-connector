from __future__ import annotations

import json
import sqlite3

import backfill_packages as packages
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


def test_package_backfill_uses_explicit_local_db_library_and_cas(tmp_path, monkeypatch) -> None:
    db = tmp_path / "skills.db"
    url = "https://github.com/acme/repo/tree/main/skill"
    content = "---\nname: report-skill\ndescription: Create reports.\n---\n\nCreate reports.\n"
    library = _seed_library(tmp_path, url, content)
    package_root = tmp_path / "packages"

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

    stats = packages.run(
        db_path=db,
        library_dir=library,
        package_root=package_root,
        limit=1,
    )

    assert stats["succeeded"] == 1
    conn = sqlite3.connect(db)
    try:
        package_hash = conn.execute("SELECT package_hash FROM skills WHERE id='skill-1'").fetchone()[0]
    finally:
        conn.close()
    assert package_hash
    assert (package_root / "manifests" / f"{package_hash}.json").exists()
