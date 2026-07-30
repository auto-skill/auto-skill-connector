from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

from scraper import RunBudget, scan_skill, scrape_skills_sh


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

    def test_unpinned_github_content_is_quarantined_and_embedding_invalidated(self) -> None:
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

        self.assertEqual(skill["quality_status"], "pending_package")
        self.assertEqual(skill["package_completeness"], "missing")
        self.assertIsNone(skill["embedding"])
        self.assertIsNone(skill["embedding_text_hash"])
        self.assertIsNone(skill["embedded_at"])

    def test_skills_sh_curated_capture_keeps_full_package_separate_from_record(self) -> None:
        class Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        class Client:
            async def get(self, url, **_kwargs):
                if url.endswith("/curated"):
                    return Response(
                        {
                            "data": [
                                {
                                    "skills": [
                                        {
                                            "id": "acme/skills/report",
                                            "name": "report",
                                            "sourceType": "github",
                                            "installUrl": "https://github.com/acme/skills",
                                            "url": "https://skills.sh/acme/skills/report",
                                        }
                                    ]
                                }
                            ]
                        }
                    )
                return Response(
                    {
                        "id": "acme/skills/report",
                        "slug": "report",
                        "hash": "registry-snapshot",
                        "files": [
                            {"path": "SKILL.md", "contents": VALID_CONTENT},
                            {"path": "references/checks.md", "contents": "Verify every total."},
                        ],
                    }
                )

        skills = []
        with patch("scraper.SKILLS_SH_OIDC_TOKEN", "test-oidc"), patch(
            "scraper.ImmutablePackageStore.put", return_value=None
        ):
            asyncio.run(scrape_skills_sh(Client(), skills))

        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill["source"], "skills_sh")
        self.assertEqual(skill["_package_manifest"]["stored_files"], 2)
        self.assertNotIn("Verify every total", skill["retrieval_text"])
        self.assertEqual(skill["_package_manifest"]["source"]["registry_snapshot_hash"], "registry-snapshot")


if __name__ == "__main__":
    unittest.main()
