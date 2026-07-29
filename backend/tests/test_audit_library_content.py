"""Tests for the read-only library content audit helper."""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from audit_library_content import audit_library
from quality import content_hash


class AuditLibraryContentTests(unittest.TestCase):
    def test_flags_missing_and_short_and_mismatch(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            db_path = root / "local_skills.db"
            library = root / "skills_library"
            files = library / "files"
            files.mkdir(parents=True)

            good = "---\nname: good\ndescription: A complete skill body with enough instruction text for audits.\n---\n\n## Workflow\n\nUse when building reports. Validate formulas and preserve identifiers.\n"
            short = "too short"
            mismatched = good + "\n## Extra\n\nAnother section.\n"
            (files / "good.md").write_text(good, encoding="utf-8")
            (files / "short.md").write_text(short, encoding="utf-8")
            (files / "mismatch.md").write_text(mismatched, encoding="utf-8")

            (library / "index.json").write_text(
                json.dumps(
                    {
                        "https://example.com/good": {
                            "file": "good.md",
                            "content_hash": content_hash(good),
                        },
                        "https://example.com/short": {
                            "file": "short.md",
                            "content_hash": content_hash(short),
                        },
                        "https://example.com/mismatch": {
                            "file": "mismatch.md",
                            "content_hash": content_hash(good),
                        },
                    }
                ),
                encoding="utf-8",
            )

            conn = sqlite3.connect(db_path)
            conn.execute(
                "CREATE TABLE skills (id TEXT, url TEXT, name TEXT, source TEXT, "
                "quality_status TEXT, content_hash TEXT)"
            )
            rows = [
                ("1", "https://example.com/good", "good", "test", "active", content_hash(good)),
                ("2", "https://example.com/short", "short", "test", "active", content_hash(short)),
                ("3", "https://example.com/mismatch", "mismatch", "test", "active", content_hash(good)),
                ("4", "https://example.com/missing", "missing", "test", "active", "a" * 64),
                ("5", "https://example.com/empty-hash", "empty", "test", "active", ""),
            ]
            conn.executemany(
                "INSERT INTO skills (id, url, name, source, quality_status, content_hash) VALUES (?,?,?,?,?,?)",
                rows,
            )
            conn.commit()
            conn.close()

            report = audit_library(db_path, library)
            self.assertEqual(report["ok"], 1)
            self.assertEqual(len(report["short_content"]), 1)
            self.assertEqual(len(report["hash_mismatch"]), 1)
            self.assertEqual(len(report["missing_index"]), 1)
            self.assertEqual(len(report["empty_hash"]), 1)
            self.assertEqual(report["issue_count"], 4)


if __name__ == "__main__":
    unittest.main()
