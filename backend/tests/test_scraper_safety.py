from __future__ import annotations

import asyncio
import unittest

from scraper import RunBudget, scan_skill


VALID_CONTENT = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas and charts.
---

## Workflow

Use this skill when the user needs a spreadsheet report. Inspect the data,
create formulas, verify calculations, add charts, and validate the workbook
before returning it. Explain important assumptions to the user.
Preserve leading-zero identifiers, confirm worksheet names, and check totals
against representative source rows. Document calculation choices, inspect
chart ranges, and ensure the finished workbook opens without formula errors.
"""


class ScraperSafetyTests(unittest.TestCase):
    def test_unauthenticated_budget_is_a_small_incremental_crawl(self) -> None:
        budget = RunBudget(authenticated=False)

        self.assertTrue(all(budget.take("core") for _ in range(12)))
        self.assertFalse(budget.take("core"))
        self.assertTrue(all(budget.take("search") for _ in range(8)))
        self.assertFalse(budget.take("search"))
        self.assertFalse(budget.take("code_search"))

    def test_rescan_invalidates_embedding_when_content_changes(self) -> None:
        skill = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://github.com/example/repo/blob/main/SKILL.md",
            "content_hash": "old-content-hash",
            "embedding": [1.0] * 384,
            "embedding_text_hash": "old-embedding-hash",
            "embedded_at": "2026-07-01T00:00:00+00:00",
            "_content": VALID_CONTENT,
        }

        asyncio.run(scan_skill(None, skill))

        self.assertEqual(skill["quality_status"], "active")
        self.assertIsNone(skill["embedding"])
        self.assertIsNone(skill["embedding_text_hash"])
        self.assertIsNone(skill["embedded_at"])


if __name__ == "__main__":
    unittest.main()
