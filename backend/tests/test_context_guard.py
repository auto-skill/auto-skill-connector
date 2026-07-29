import unittest

from context_guard import build_capsule, build_context_guard
from quality import dedupe_by_content_hash, rerank_candidates, tier_for_prompt


VALID = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas, charts, and validation.
---

## Workflow

Inspect the source data, create the workbook, add formulas, verify calculations,
and explain the generated file. Validate sheet names, formulas, chart ranges,
headers, totals, and representative cells before returning output.

## Verification

Check formulas, preserve identifiers with leading zeroes, document assumptions,
and include a short audit note for every generated summary sheet.
"""


class ContextGuardTests(unittest.TestCase):
    def test_capsule_is_deterministic_and_bounded(self):
        content = VALID + ("\n## Reference\n" + ("Keep the output reproducible. " * 400))
        first = build_capsule("create an Excel report with formulas", content, 900)
        second = build_capsule("create an Excel report with formulas", content, 900)
        self.assertEqual(first, second)
        self.assertLessEqual(len(first), 900)
        self.assertIn("spreadsheet-reporter", first)
        self.assertIn("Workflow", first)

    def test_large_safe_content_uses_capsule_without_isolation(self):
        content = VALID + ("\n## Reference\n" + ("Keep the output reproducible. " * 400))
        guard = build_context_guard(
            task="create an Excel report with formulas",
            content=content,
            content_hash="a" * 64,
            content_digest="b" * 64,
            supports_isolation=False,
        )
        self.assertEqual(guard["delivery"], "capsule")
        self.assertFalse(guard["complete"])
        self.assertIn("not the complete SKILL.md", guard["fetch_hint"])
        self.assertLessEqual(guard["capsule_chars"], 2400)
        self.assertEqual(guard["content_hash"], "a" * 64)

    def test_large_safe_content_can_request_isolation(self):
        content = VALID + ("\n## Reference\n" + ("Keep the output reproducible. " * 400))
        guard = build_context_guard(
            task="create an Excel report with formulas",
            content=content,
            supports_isolation=True,
        )
        self.assertEqual(guard["delivery"], "isolation")
        self.assertEqual(guard["reason"], "large_static")
        self.assertFalse(guard["complete"])

    def test_small_safe_content_is_complete_full(self):
        guard = build_context_guard(task="make a report", content=VALID, content_hash="f" * 64)
        self.assertEqual(guard["delivery"], "full")
        self.assertTrue(guard["complete"])
        self.assertIsNone(guard["fetch_hint"])

    def test_capsule_only_never_returns_full(self):
        guard = build_context_guard(task="make a report", content=VALID, force_capsule=True)
        self.assertEqual(guard["delivery"], "capsule")

    def test_capability_content_never_gets_capsule(self):
        content = VALID + "\nUse curl to fetch a remote endpoint and run scripts/install dependencies."
        guard = build_context_guard(task="create an Excel report", content=content)
        self.assertEqual(guard["delivery"], "hint")
        self.assertEqual(guard["reason"], "unsafe_capability")
        self.assertEqual(guard["capsule"], None)

    def test_official_low_star_candidate_can_clear_meaningfulness(self):
        candidate = {
            "name": "official-spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "mcp_official_registry",
            "stars": 1,
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
            "similarity": 0.95,
        }
        ranked = rerank_candidates("create an Excel spreadsheet report with formulas", [candidate])
        self.assertGreaterEqual(ranked[0]["meaningfulness_score"], 0.55)
        self.assertEqual(tier_for_prompt("create an Excel spreadsheet report with formulas", ranked), "full")

    def test_high_star_clone_wins_canonicalization(self):
        rows = [
            {
                "id": "low",
                "name": "report-skill",
                "description": "Build spreadsheet reports with formulas and charts.",
                "content_hash": "low-hash",
                "quality_score": 90,
                "source": "github_repo",
                "stars": 1,
            },
            {
                "id": "high",
                "name": "report-skill",
                "description": "Build spreadsheet reports with formulas and charts.",
                "content_hash": "high-hash",
                "quality_score": 90,
                "source": "github_repo",
                "stars": 100000,
            },
        ]
        self.assertEqual(dedupe_by_content_hash(rows)[0]["id"], "high")

    def test_unrelated_high_star_candidate_abstains(self):
        candidate = {
            "name": "popular-calendar-skill",
            "description": "Manage calendar events and meetings.",
            "source": "github_repo",
            "stars": 100000,
            "quality_status": "active",
            "quality_score": 95,
            "rank": 1.0,
            "similarity": 0.70,
        }
        self.assertEqual(tier_for_prompt("create an Excel spreadsheet report", [candidate]), "none")


if __name__ == "__main__":
    unittest.main()
