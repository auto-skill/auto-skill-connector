from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import backfill_quality
from embeddings import LibraryContent
import local_store
from quality import content_hash


VALID_SKILL = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas and charts.
---

## Workflow

Use this skill when a task needs a spreadsheet report. Inspect the source data,
create formulas, verify totals, add useful charts, and validate the workbook
before returning it. Preserve leading-zero identifiers and explain assumptions.
Create summary sheets when they help the reader, check worksheet names before
writing formulas, and compare the completed totals against representative
source rows. Confirm chart ranges and output formatting before the final
handoff, then describe any assumptions or limitations clearly.
"""

README_ONLY = """# Spreadsheet project

## Workflow

Use this repository to create spreadsheet reports from source data. Build
formulas, inspect totals, make charts, and validate output before sharing it.
Keep notes about assumptions, preserve leading-zero identifiers, verify sheet
names, and check representative rows before the final handoff.
"""

NESTED_METADATA_SKILL = """---
name: xlsx-creator
description: >
  Create, edit, and analyze Excel spreadsheets. Use when the user wants to
  build a spreadsheet report or a financial model.
metadata:
  tags: [excel, xlsx]
  triggers:
    - create an Excel file
    - build a spreadsheet
---

## Workflow

Use this skill when a task needs a spreadsheet report. Inspect the source data,
create formulas, verify totals, add useful charts, and validate the workbook
before returning it. Preserve leading-zero identifiers and explain assumptions.
Create summary sheets when they help the reader, check worksheet names before
writing formulas, and compare the completed totals against representative
source rows. Confirm chart ranges and output formatting before the final
handoff, then describe any assumptions or limitations clearly.
"""


class BackfillQualityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db_path = Path(self.tmp.name) / "local_skills.db"
        self.library_dir = Path(self.tmp.name) / "skills_library"
        (self.library_dir / "files").mkdir(parents=True)

        self.old_store_db_path = local_store.DB_PATH
        self.old_backfill_db_path = backfill_quality.DB_PATH
        local_store.DB_PATH = self.db_path
        backfill_quality.DB_PATH = self.db_path
        local_store.init_db()

    def tearDown(self) -> None:
        local_store.invalidate_vector_cache()
        local_store.DB_PATH = self.old_store_db_path
        backfill_quality.DB_PATH = self.old_backfill_db_path

    def test_backfill_quarantines_readmes_and_keeps_matching_valid_vectors(self) -> None:
        valid_url = "https://example.com/valid-skill"
        readme_url = "https://example.com/readme-only"
        valid_hash = content_hash(VALID_SKILL)
        readme_hash = content_hash(README_ONLY)
        (self.library_dir / "files" / "valid.md").write_text(VALID_SKILL, encoding="utf-8")
        (self.library_dir / "files" / "readme.md").write_text(README_ONLY, encoding="utf-8")
        (self.library_dir / "index.json").write_text(
            json.dumps(
                {
                    valid_url: {"file": "valid.md", "content_hash": valid_hash},
                    readme_url: {"file": "readme.md", "content_hash": readme_hash},
                }
            ),
            encoding="utf-8",
        )

        embedding = local_store.pack_embedding([1.0] + [0.0] * 383)
        conn = local_store.get_conn()
        try:
            conn.executemany(
                """
                INSERT INTO skills (
                    id, name, description, source, url, tags, raw, content_hash,
                    quality_status, quality_score, embedding, embedding_text_hash, embedded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        "valid",
                        "spreadsheet-reporter",
                        "Build spreadsheet reports with formulas and charts.",
                        "github_skill_file",
                        valid_url,
                        "[]",
                        "{}",
                        valid_hash,
                        "active",
                        90,
                        embedding,
                        "current",
                        "2026-07-01T00:00:00+00:00",
                    ),
                    (
                        "readme",
                        "spreadsheet-project",
                        "A repository README about building spreadsheet reports with formulas and charts.",
                        "github_repo",
                        readme_url,
                        "[]",
                        "{}",
                        readme_hash,
                        "active",
                        90,
                        embedding,
                        "stale",
                        "2026-07-01T00:00:00+00:00",
                    ),
                ],
            )
            conn.commit()
        finally:
            conn.close()

        with patch.object(backfill_quality, "LibraryContent", return_value=LibraryContent(self.library_dir)):
            backfill_quality.main()

        conn = local_store.get_conn()
        try:
            valid = conn.execute(
                "SELECT quality_status, embedding, embedding_text_hash FROM skills WHERE id='valid'"
            ).fetchone()
            readme = conn.execute(
                "SELECT quality_status, embedding, embedding_text_hash FROM skills WHERE id='readme'"
            ).fetchone()
        finally:
            conn.close()

        self.assertEqual(valid["quality_status"], "active")
        self.assertIsNotNone(valid["embedding"])
        self.assertEqual(valid["embedding_text_hash"], "current")
        self.assertEqual(readme["quality_status"], "metadata_only")
        self.assertIsNone(readme["embedding"])
        self.assertIsNone(readme["embedding_text_hash"])

    def test_backfill_recovers_nested_metadata_tags_and_invalidates_embedding(self) -> None:
        url = "https://example.com/xlsx-creator"
        chash = content_hash(NESTED_METADATA_SKILL)
        (self.library_dir / "files" / "xlsx.md").write_text(NESTED_METADATA_SKILL, encoding="utf-8")
        (self.library_dir / "index.json").write_text(
            json.dumps({url: {"file": "xlsx.md", "content_hash": chash}}),
            encoding="utf-8",
        )

        embedding = local_store.pack_embedding([1.0] + [0.0] * 383)
        conn = local_store.get_conn()
        try:
            conn.execute(
                """
                INSERT INTO skills (
                    id, name, description, source, url, tags, raw, content_hash,
                    quality_status, quality_score, embedding, embedding_text_hash, embedded_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "xlsx",
                    "xlsx-creator",
                    "Create, edit, and analyze Excel spreadsheets.",
                    "github_skill_file",
                    url,
                    "[]",  # scraped under the old parser: tags empty, nested metadata dropped
                    "{}",
                    chash,
                    "active",
                    90,
                    embedding,
                    "current",
                    "2026-07-01T00:00:00+00:00",
                ),
            )
            conn.commit()
        finally:
            conn.close()

        with patch.object(backfill_quality, "LibraryContent", return_value=LibraryContent(self.library_dir)):
            backfill_quality.main()

        conn = local_store.get_conn()
        try:
            row = conn.execute(
                "SELECT tags, embedding, embedding_text_hash FROM skills WHERE id='xlsx'"
            ).fetchone()
        finally:
            conn.close()

        recovered_tags = json.loads(row["tags"])
        for expected in ("excel", "xlsx", "create an Excel file", "build a spreadsheet"):
            self.assertIn(expected, recovered_tags)
        # Tags changed -> the stale embedding built without this signal must
        # be invalidated so the worker rebuilds it with the recovered tags.
        self.assertIsNone(row["embedding"])
        self.assertIsNone(row["embedding_text_hash"])


if __name__ == "__main__":
    unittest.main()
