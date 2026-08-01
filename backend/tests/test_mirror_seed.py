from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
import sys
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]
COMPACTOR = BACKEND / "compact_skills_sh_mirror.py"
SEED_SCRIPT = BACKEND / "deploy" / "seed-skills-sh-mirror.sh"


def _canonical_digest(path: Path) -> str:
    conn = sqlite3.connect(path)
    digest = hashlib.sha256()
    try:
        for table in (
            "skills_sh_mirror",
            "skills_sh_sources",
            "skills_sh_ingestion_attempts",
        ):
            columns = [row[1] for row in conn.execute(f"PRAGMA table_info({table})")]
            quoted = ", ".join(f'"{column}"' for column in columns)
            order = ", ".join(f'"{column}"' for column in columns)
            for row in conn.execute(
                f'SELECT {quoted} FROM "{table}" ORDER BY {order}'
            ):
                for value in row:
                    encoded = b"<NULL>" if value is None else str(value).encode()
                    digest.update(len(encoded).to_bytes(8, "big"))
                    digest.update(encoded)
    finally:
        conn.close()
    return digest.hexdigest()


def _seed_fixture(path: Path) -> None:
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE skills_sh_mirror (
            id TEXT PRIMARY KEY,
            name TEXT NOT NULL DEFAULT '',
            description TEXT NOT NULL DEFAULT '',
            retrieval_text TEXT NOT NULL DEFAULT '',
            row_json TEXT NOT NULL,
            snapshot_hash TEXT,
            updated_at REAL NOT NULL,
            expires_at REAL NOT NULL,
            stale_until REAL NOT NULL
        );
        CREATE TABLE skills_sh_sources (
            content_hash TEXT PRIMARY KEY,
            canonical_skill_id TEXT NOT NULL DEFAULT '',
            snapshot_hash TEXT,
            entrypoint_path TEXT,
            content TEXT NOT NULL,
            files_json TEXT NOT NULL DEFAULT '[]',
            byte_count INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            last_seen_at REAL NOT NULL
        );
        CREATE TABLE skills_sh_ingestion_attempts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            skill_id TEXT NOT NULL,
            snapshot_hash TEXT,
            status TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            retryable INTEGER NOT NULL DEFAULT 0,
            content_hash TEXT,
            byte_count INTEGER NOT NULL DEFAULT 0,
            error TEXT,
            attempted_at REAL NOT NULL
        );
        """
    )
    row = {"id": "owner/repo/skill", "quality_status": "active"}
    conn.execute(
        "INSERT INTO skills_sh_mirror VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            row["id"],
            "Skill",
            "Description",
            "full retrieval text",
            json.dumps(row, sort_keys=True),
            "snapshot-1",
            1.0,
            9.0,
            9.0,
        ),
    )
    full_content = "---\nname: Skill\n---\n" + ("preserve-me\n" * 1000)
    conn.execute(
        "INSERT INTO skills_sh_sources VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "content-1",
            row["id"],
            "snapshot-1",
            "SKILL.md",
            full_content,
            json.dumps([{"path": "SKILL.md", "sha256": "file-1"}]),
            len(full_content.encode()),
            1.0,
            1.0,
        ),
    )
    conn.execute(
        "INSERT INTO skills_sh_ingestion_attempts "
        "(skill_id, snapshot_hash, status, reason, retryable, content_hash, byte_count, error, attempted_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (row["id"], "snapshot-1", "active", "active", 0, "content-1", 1, None, 1.0),
    )
    conn.commit()
    conn.close()


def test_compactor_preserves_canonical_rows_and_full_source(tmp_path: Path) -> None:
    source = tmp_path / "source.db"
    output = tmp_path / "compact.db"
    _seed_fixture(source)
    before_digest = _canonical_digest(source)

    subprocess.run(
        [sys.executable, str(COMPACTOR), str(source), str(output)],
        cwd=BACKEND.parent,
        check=True,
        capture_output=True,
        text=True,
    )

    assert _canonical_digest(output) == before_digest
    conn = sqlite3.connect(output)
    try:
        assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        assert conn.execute("SELECT content FROM skills_sh_sources").fetchone()[0].endswith(
            "preserve-me\n" * 1000
        )
        assert conn.execute("SELECT count(*) FROM skills_sh_mirror_fts").fetchone()[0] == 1
    finally:
        conn.close()


def test_seed_script_has_hash_atomic_swap_and_rollback_guards() -> None:
    script = SEED_SCRIPT.read_text(encoding="utf-8")
    assert "sha256sum" in script
    assert "gzip -t" in script
    assert "PRAGMA integrity_check" in script
    assert "mv -- \"$STAGING\" \"$TARGET\"" in script
    assert "BACKUP=\"$DATA_DIR/backups/local_skills.db.preseed" in script
    assert "rollback()" in script
    assert "stop api admin-local mcp litestream" in script
    assert "up -d admin-local mcp litestream" in script
    assert "/app/skills_library" not in script
