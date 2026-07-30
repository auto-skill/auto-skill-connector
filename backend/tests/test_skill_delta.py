from __future__ import annotations

import gzip
import hashlib
import io
import json
import sqlite3
import struct
import tempfile
import unittest
import zipfile
from pathlib import Path

from embeddings import build_embed_text, embed_text_hash
from quality import content_hash
from skill_delta import (
    LIBRARY_MEMBER,
    MANIFEST_MEMBER,
    SKILLS_MEMBER,
    SkillDeltaError,
    apply_import,
    export_package,
    load_package,
    plan_import,
)


SCHEMA = """
CREATE TABLE skills (
    id TEXT PRIMARY KEY, name TEXT NOT NULL, description TEXT, source TEXT NOT NULL,
    url TEXT UNIQUE, tags TEXT DEFAULT '[]', raw TEXT DEFAULT '{}', discovered_at TEXT,
    risk_score INTEGER DEFAULT 0, risk_flags TEXT DEFAULT '[]', scanned_at TEXT,
    content_hash TEXT, canonical_id TEXT, quality_status TEXT DEFAULT 'pending',
    quality_reasons TEXT DEFAULT '[]', quality_score INTEGER DEFAULT 0,
    prominence_score REAL DEFAULT 0, provenance_score REAL DEFAULT 0.25,
    meaningfulness_score REAL DEFAULT 0, platforms TEXT DEFAULT '[]', category TEXT,
    embedding BLOB, embedding_text_hash TEXT, embedded_at TEXT, feedback_score REAL,
    capability_summary TEXT, triggers TEXT DEFAULT '[]'
);
CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, plan TEXT);
CREATE TABLE cli_tokens (id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, user_id TEXT);
CREATE TABLE route_events (id TEXT PRIMARY KEY, tier TEXT);
CREATE TABLE skill_versions (
    id TEXT PRIMARY KEY, skill_id TEXT NOT NULL, content_hash TEXT NOT NULL, seen_at TEXT,
    UNIQUE(skill_id, content_hash)
);
CREATE TABLE admin_audit_log (
    id TEXT PRIMARY KEY, actor_user_id TEXT NOT NULL, actor_email TEXT NOT NULL,
    target_user_id TEXT, target_email TEXT, action TEXT NOT NULL, old_value TEXT,
    new_value TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER admin_audit_log_no_update BEFORE UPDATE ON admin_audit_log BEGIN
    SELECT RAISE(ABORT, 'admin audit log is append-only');
END;
CREATE TRIGGER admin_audit_log_no_delete BEFORE DELETE ON admin_audit_log BEGIN
    SELECT RAISE(ABORT, 'admin audit log is append-only');
END;
"""


def gzip_bytes(lines: list[bytes]) -> bytes:
    output = io.BytesIO()
    with gzip.GzipFile(fileobj=output, mode="wb", mtime=0) as gz:
        for line in lines:
            gz.write(line)
    return output.getvalue()


class SkillDeltaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.collector_db = self.root / "collector.db"
        self.production_db = self.root / "production.db"
        self.collector_library = self.root / "collector-library"
        self.production_library = self.root / "production-library"
        self.package = self.root / "delta.zip"
        for path in (self.collector_db, self.production_db):
            conn = sqlite3.connect(path)
            conn.executescript(SCHEMA)
            conn.commit()
            conn.close()
        self._seed_collector()
        self._seed_production_secrets()

    def tearDown(self) -> None:
        self.temp.cleanup()

    def _seed_collector(self) -> None:
        content = (
            "---\nname: report-builder\n"
            "description: Build verified reports and charts.\n---\n\n"
            "## Workflow\n\nInspect the source data and confirm the requested reporting period. "
            "Build formulas, summary tables, and charts from explicit ranges. "
            "Verify totals against representative source rows and preserve "
            "leading-zero identifiers. Document assumptions, check worksheet "
            "names, validate chart labels, and ensure the finished workbook "
            "opens without formula errors. Return a polished report together "
            "with a concise explanation of important calculation choices.\n"
        )
        files = self.collector_library / "files"
        files.mkdir(parents=True)
        (files / "report.md").write_text(content, encoding="utf-8")
        url = "https://github.com/example/report-skill/blob/main/SKILL.md"
        (self.collector_library / "index.json").write_text(
            json.dumps({url: {"file": "report.md"}}), encoding="utf-8"
        )
        conn = sqlite3.connect(self.collector_db)
        values = {
            "id": "collector-id",
            "name": "report-builder",
            "description": "Build verified reports and charts.",
            "source": "github_skill_file",
            "url": url,
            "tags": json.dumps(["reports"]),
            "discovered_at": "2026-07-15T00:00:00+00:00",
            "risk_score": 0,
            "risk_flags": "[]",
            "scanned_at": "2026-07-15T00:00:00+00:00",
            "content_hash": content_hash(content),
            "canonical_id": url,
            "quality_status": "active",
            "quality_reasons": "[]",
            "quality_score": 91,
            "prominence_score": 0.3,
            "provenance_score": 0.8,
            "meaningfulness_score": 0.9,
            "platforms": json.dumps(["codex"]),
            "category": "documents",
            "embedding": struct.pack("384f", *([0.05] * 384)),
            "embedding_text_hash": embed_text_hash(build_embed_text({
                "name": "report-builder",
                "description": "Build verified reports and charts.",
                "source": "github_skill_file",
                "url": url,
                "tags": ["reports"],
            }, content)),
            "embedded_at": "2026-07-15T00:00:00+00:00",
        }
        columns = ",".join(values)
        conn.execute(
            f"INSERT INTO skills ({columns}) VALUES ({','.join('?' for _ in values)})",
            list(values.values()),
        )
        # Pending and unembedded rows must never leave a collector machine.
        conn.execute(
            "INSERT INTO skills (id,name,source,url,quality_status) VALUES (?,?,?,?,?)",
            ("pending", "unreviewed", "web", "https://example.com/unreviewed", "pending"),
        )
        conn.commit()
        conn.close()

    def _seed_production_secrets(self) -> None:
        conn = sqlite3.connect(self.production_db)
        conn.execute("INSERT INTO users VALUES (?,?,?)", ("user-1", "founder@example.com", "pro"))
        conn.execute("INSERT INTO cli_tokens VALUES (?,?,?)", ("token-1", "super-secret-hash", "user-1"))
        conn.execute("INSERT INTO route_events VALUES (?,?)", ("event-1", "full"))
        conn.commit()
        conn.close()

    def _export(self):
        return export_package(self.collector_db, self.collector_library, self.package)

    def _rewrite_skills(self, mutate) -> None:
        with zipfile.ZipFile(self.package, "r") as archive:
            manifest = json.loads(archive.read(MANIFEST_MEMBER))
            skills = gzip.decompress(archive.read(SKILLS_MEMBER))
            library = archive.read(LIBRARY_MEMBER)
        records = [json.loads(line) for line in skills.splitlines() if line]
        mutate(records)
        skills = gzip_bytes([
            json.dumps(record, sort_keys=True, separators=(",", ":")).encode() + b"\n"
            for record in records
        ])
        manifest["files"][SKILLS_MEMBER] = {
            "sha256": hashlib.sha256(skills).hexdigest(),
            "bytes": len(skills),
        }
        with zipfile.ZipFile(self.package, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(MANIFEST_MEMBER, json.dumps(manifest))
            archive.writestr(SKILLS_MEMBER, skills)
            archive.writestr(LIBRARY_MEMBER, library)

    def test_export_contains_only_active_embedded_public_skill_data(self) -> None:
        manifest = self._export()
        package = load_package(self.package)

        self.assertEqual(manifest["skill_count"], 1)
        self.assertEqual(len(package.skills), 1)
        self.assertEqual(package.skills[0]["name"], "report-builder")
        self.assertNotIn("raw", package.skills[0])
        package_text = self.package.read_bytes()
        self.assertNotIn(b"founder@example.com", package_text)
        self.assertNotIn(b"super-secret-hash", package_text)

    def test_apply_preserves_all_operational_tables_and_writes_audit(self) -> None:
        self._export()
        package = load_package(self.package)
        plan = plan_import(self.production_db, package)
        self.assertEqual(plan["inserted"], 1)

        result = apply_import(
            self.production_db,
            self.production_library,
            package,
            backup_root=self.root / "backups",
            actor_email="founder@example.com",
            reason="Monthly reviewed collector import",
        )

        conn = sqlite3.connect(self.production_db)
        self.assertEqual(conn.execute("SELECT email,plan FROM users").fetchone(), ("founder@example.com", "pro"))
        self.assertEqual(conn.execute("SELECT token_hash FROM cli_tokens").fetchone()[0], "super-secret-hash")
        self.assertEqual(conn.execute("SELECT tier FROM route_events").fetchone()[0], "full")
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM skills").fetchone()[0], 1)
        audit = conn.execute("SELECT actor_email,action,reason,new_value FROM admin_audit_log").fetchone()
        conn.close()
        self.assertEqual(audit[:3], ("founder@example.com", "skill_delta_import", "Monthly reviewed collector import"))
        self.assertEqual(json.loads(audit[3])["inserted"], 1)
        self.assertTrue(Path(result["backup_dir"], "local_skills.db").is_file())
        self.assertTrue((self.production_library / "index.json").is_file())

    def test_forbidden_field_is_rejected_even_with_valid_checksum(self) -> None:
        self._export()
        self._rewrite_skills(lambda records: records[0].__setitem__("token_hash", "attack"))

        with self.assertRaisesRegex(SkillDeltaError, "forbidden fields"):
            load_package(self.package)

    def test_tampered_member_is_rejected(self) -> None:
        self._export()
        with zipfile.ZipFile(self.package, "r") as archive:
            manifest = archive.read(MANIFEST_MEMBER)
            skills = archive.read(SKILLS_MEMBER) + b"tamper"
            library = archive.read(LIBRARY_MEMBER)
        with zipfile.ZipFile(self.package, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(MANIFEST_MEMBER, manifest)
            archive.writestr(SKILLS_MEMBER, skills)
            archive.writestr(LIBRARY_MEMBER, library)

        with self.assertRaisesRegex(SkillDeltaError, "checksum"):
            load_package(self.package)

    def test_extra_archive_member_is_rejected(self) -> None:
        self._export()
        with zipfile.ZipFile(self.package, "a") as archive:
            archive.writestr("../users.sql", "DELETE FROM users;")

        with self.assertRaisesRegex(SkillDeltaError, "forbidden members"):
            load_package(self.package)


if __name__ == "__main__":
    unittest.main()
