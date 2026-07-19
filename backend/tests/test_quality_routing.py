import unittest

from quality import (
    PLATFORM_ALIASES,
    dedupe_by_content_hash,
    evaluate_quality,
    infer_platforms,
    is_non_task_prompt,
    rerank_candidates,
    tier_for_ranked_candidates,
    tier_for_prompt,
)


VALID_CONTENT = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas and charts.
---

## Workflow

Use when the user needs an Excel or spreadsheet report with formulas, charts,
tables, and repeatable formatting. Inspect the source data, create a workbook,
add formulas, verify calculations, add charts, and explain the generated file.
Always validate sheet names, formulas, and chart ranges before returning output.
"""


class QualityGateTests(unittest.TestCase):
    def test_accepts_structured_skill_content(self):
        result = evaluate_quality(
            {
                "name": "spreadsheet-reporter",
                "description": "Build spreadsheet reports with formulas and charts.",
                "source": "github_skill_file",
                "tags": [],
                "raw": {"stars": 12},
            },
            VALID_CONTENT,
        )

        self.assertEqual(result["quality_status"], "active")
        self.assertGreaterEqual(result["quality_score"], 40)
        self.assertTrue(result["content_hash"])

    def test_rejects_path_only_stubs(self):
        result = evaluate_quality(
            {
                "name": "stub",
                "description": "A placeholder skill entry.",
                "source": "github_skill_file",
                "tags": [],
                "raw": {},
            },
            "./foo/bar/SKILL.md\n./foo/baz/SKILL.md\nhttps://example.com/a/b\n",
        )

        self.assertEqual(result["quality_status"], "rejected")
        self.assertIn("path-or-link-only", result["quality_reasons"])

    def test_trusted_registry_metadata_is_hint_only(self):
        result = evaluate_quality(
            {
                "name": "slack-mcp-server",
                "description": "Connect an agent to Slack channels, messages, and workspace search.",
                "source": "mcp_official_registry",
                "tags": ["mcp", "slack"],
                "raw": {},
            },
            "",
        )

        self.assertEqual(result["quality_status"], "metadata_only")
        self.assertIn("slack", result["platforms"])

    def test_structured_readme_is_metadata_only_without_skill_frontmatter(self):
        readme = """# Spreadsheet helper

## Workflow

Use this repository to create spreadsheet reports. Inspect the input data,
build formulas, verify the calculations, and create charts for the reader.
Document assumptions and validate representative cells before shipping.
Check workbook formats, preserve identifiers with leading zeroes, and keep a
short audit note of the formulas used for every generated summary sheet.
Review chart ranges, headers, and totals before returning the deliverable.
"""
        result = evaluate_quality(
            {
                "name": "spreadsheet-helper",
                "description": "A repository README about building spreadsheet reports with formulas and charts.",
                "source": "github_repo",
            },
            readme,
        )

        self.assertEqual(result["quality_status"], "metadata_only")
        self.assertIn("missing-skill-frontmatter", result["quality_reasons"])

    def test_platform_aliases_require_word_boundaries(self):
        for name, platform in (
            ("laws-reviewer", "aws"),
            ("notional-planning", "notion"),
            ("liquidity-analysis", "shopify"),
        ):
            with self.subTest(name=name):
                platforms = infer_platforms({"name": name, "description": "Analyze project work.", "tags": []})
                self.assertNotIn(platform, platforms)

    def test_non_task_guard_keeps_short_real_tasks(self):
        self.assertFalse(is_non_task_prompt("fix css"))
        self.assertTrue(is_non_task_prompt("thanks that worked great"))


class RoutingTierTests(unittest.TestCase):
    def test_exact_metadata_duplicates_collapse_even_when_content_hashes_differ(self):
        candidates = [
            {
                "id": "fork-low-stars",
                "name": "finance-report",
                "description": "Create a monthly financial report with charts.",
                "content_hash": "one",
                "quality_score": 90,
                "stars": 1,
            },
            {
                "id": "fork-high-stars",
                "name": "finance-report",
                "description": "Create a monthly financial report with charts.",
                "content_hash": "two",
                "quality_score": 90,
                "stars": 100,
            },
            {
                "id": "xlsx-creator",
                "name": "xlsx-creator",
                "description": "Create Excel spreadsheets with formulas and charts.",
                "content_hash": "three",
                "quality_score": 90,
            },
        ]

        deduped = dedupe_by_content_hash(candidates)

        self.assertEqual([row["id"] for row in deduped], ["fork-high-stars", "xlsx-creator"])

    def test_explicit_spreadsheet_semantics_beat_generic_report_words(self):
        prompt = "create a monthly Excel sales report with formulas, charts, and a summary dashboard"
        candidates = [
            {
                "name": "finance-report",
                "description": "Monthly financial report with revenue charts and a summary table.",
                "quality_status": "active",
                "quality_score": 100,
                "rank": 0.06575,
                "similarity": 0.8599,
            },
            {
                "name": "xlsx-creator",
                "description": "Create Excel spreadsheets with formulas, professional formatting, and charts.",
                "quality_status": "active",
                "quality_score": 92,
                "rank": 0.06903,
                "similarity": 0.8701,
            },
        ]

        ranked = rerank_candidates(prompt, candidates)

        self.assertEqual(ranked[0]["name"], "xlsx-creator")

    def test_platform_trap_caps_landingi_to_hint(self):
        prompt = "build a landing page for an AI automation agency"
        candidate = {
            "name": "sales-landingi",
            "description": "Landingi platform help for landing pages, leads, CRM sync, API keys, and publishing.",
            "tags": ["landingi", "landing-page"],
            "platforms": ["landingi"],
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
            "similarity": 0.95,
        }

        ranked = rerank_candidates(prompt, [candidate])

        self.assertTrue(ranked[0]["platform_mismatch"])
        self.assertEqual(tier_for_prompt(prompt, [candidate]), "hint")

    def test_platform_explicit_can_full_route(self):
        prompt = "build a landing page on Landingi for an AI automation agency"
        candidate = {
            "name": "sales-landingi",
            "description": "Landingi platform help for landing pages, leads, CRM sync, API keys, and publishing.",
            "tags": ["landingi", "landing-page"],
            "platforms": ["landingi"],
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
            "similarity": 0.95,
        }

        self.assertEqual(tier_for_prompt(prompt, [candidate]), "full")

    def test_ranked_tier_matches_prompt_tier(self):
        prompt = "build a landing page on Landingi for an AI automation agency"
        candidate = {
            "name": "sales-landingi",
            "description": "Landingi platform help for landing pages, leads, CRM sync, API keys, and publishing.",
            "tags": ["landingi", "landing-page"],
            "platforms": ["landingi"],
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
            "similarity": 0.95,
        }

        ranked = rerank_candidates(prompt, [candidate])

        self.assertEqual(tier_for_ranked_candidates(ranked), tier_for_prompt(prompt, [candidate]))

    def test_platform_traps_cap_all_known_platforms_to_hint(self):
        generic_prompt = "build a customer dashboard and publish it"
        for platform in sorted(PLATFORM_ALIASES):
            with self.subTest(platform=platform):
                candidate = {
                    "name": f"{platform}-workflow",
                    "description": (
                        f"{platform} platform help for customer dashboards, publishing, "
                        "API keys, webhooks, and sync issues."
                    ),
                    "tags": [platform, "dashboard"],
                    "platforms": [platform],
                    "quality_status": "active",
                    "quality_score": 90,
                    "rank": 1.0,
                    "similarity": 0.95,
                }

                ranked = rerank_candidates(generic_prompt, [candidate])

                self.assertTrue(ranked[0]["platform_mismatch"])
                self.assertEqual(tier_for_prompt(generic_prompt, [candidate]), "hint")

    def test_platform_explicit_prompts_allow_known_platforms(self):
        for platform, aliases in sorted(PLATFORM_ALIASES.items()):
            with self.subTest(platform=platform):
                alias = aliases[0]
                prompt = f"build a {alias} customer dashboard and publish it"
                candidate = {
                    "name": f"{platform}-workflow",
                    "description": (
                        f"{alias} platform help for customer dashboards, publishing, "
                        "API keys, webhooks, and sync issues."
                    ),
                    "tags": [platform, "dashboard"],
                    "platforms": [platform],
                    "quality_status": "active",
                    "quality_score": 90,
                    "rank": 1.0,
                    "similarity": 0.95,
                }

                ranked = rerank_candidates(prompt, [candidate])

                self.assertFalse(ranked[0]["platform_mismatch"])
                self.assertEqual(tier_for_prompt(prompt, [candidate]), "full")

    def test_metadata_only_never_full_routes(self):
        prompt = "send slack messages from my agent"
        candidate = {
            "name": "slack-mcp-server",
            "description": "Connect an agent to Slack channels, messages, and workspace search.",
            "tags": ["slack"],
            "platforms": ["slack"],
            "quality_status": "metadata_only",
            "quality_score": 55,
            "rank": 1.0,
            "similarity": 0.96,
        }

        self.assertEqual(tier_for_prompt(prompt, [candidate]), "hint")

    def test_no_similarity_never_full_routes(self):
        prompt = "create an excel report with formulas"
        candidate = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "tags": ["spreadsheet", "excel"],
            "platforms": [],
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
        }

        self.assertEqual(tier_for_prompt(prompt, [candidate]), "hint")

    def test_selected_candidate_must_own_the_similarity_score(self):
        prompt = "create an excel spreadsheet report with formulas"
        lexical_but_no_vector = {
            "name": "excel-spreadsheet-report-formulas",
            "description": "Create an Excel spreadsheet report with formulas.",
            "quality_status": "active",
            "quality_score": 90,
            "rank": 1.0,
        }
        lower_vector_hit = {
            "name": "generic-workbook-helper",
            "description": "A helper for workbooks.",
            "quality_status": "active",
            "quality_score": 90,
            "rank": 0.1,
            "similarity": 0.96,
        }

        ranked = rerank_candidates(prompt, [lexical_but_no_vector, lower_vector_hit])

        self.assertEqual(ranked[0]["name"], "excel-spreadsheet-report-formulas")
        self.assertEqual(tier_for_prompt(prompt, [lexical_but_no_vector, lower_vector_hit]), "hint")

    def test_risk_flagged_skill_never_full_routes(self):
        candidate = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "quality_status": "active",
            "quality_score": 90,
            "risk_score": 1,
            "rank": 1.0,
            "similarity": 0.95,
        }

        self.assertEqual(tier_for_prompt("create an excel spreadsheet report with formulas", [candidate]), "hint")


if __name__ == "__main__":
    unittest.main()
