import unittest

from recommender import analyze_task
from quality import (
    PLATFORM_ALIASES,
    dedupe_by_content_hash,
    evaluate_quality,
    infer_platforms,
    is_non_task_prompt,
    rerank_candidates,
    retrieval_record_overlap,
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

    def test_data_family_recognizes_modern_tabular_prompts(self):
        prompts = (
            "clean a messy CSV with pandas and remove duplicate rows",
            "query a Parquet dataset and normalize the data frame",
            "design a database schema for a multi-tenant analytics warehouse",
            "build a KPI dashboard from Snowflake tables",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                result = analyze_task(prompt)
                self.assertEqual(result["family"], "data")
                self.assertIn("data-artifact", result["signals"])


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

    def test_normalized_retrieval_record_breaks_metadata_ties(self):
        prompt = "parse JSON logs with a Rust command line tool"
        generic = {
            "name": "code-helper",
            "description": "General coding workflow guidance.",
            "quality_status": "active",
            "quality_score": 90,
            "rank": 0.52,
            "similarity": 0.91,
        }
        procedure_match = {
            "name": "log-toolkit",
            "description": "General coding workflow guidance.",
            "retrieval_text": "technology: rust; operation: extract parse; artifact: command line tool; parse JSON logs with serde",
            "quality_status": "active",
            "quality_score": 90,
            "rank": 0.51,
            "similarity": 0.91,
        }

        ranked = rerank_candidates(prompt, [generic, procedure_match])

        self.assertEqual(ranked[0]["name"], "log-toolkit")
        self.assertGreater(retrieval_record_overlap(prompt, procedure_match), 0)
        self.assertEqual(retrieval_record_overlap(prompt, generic), 0)

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

    def test_authenticated_skills_sh_candidate_without_cosine_can_full_route(self):
        prompt = "create an excel spreadsheet report with formulas"
        candidate = {
            "name": "document-xlsx",
            "description": "Create/edit .xlsx spreadsheets with formulas, charts, and data validation.",
            "retrieval_backend": "skills_sh",
            "quality_status": "active",
            "quality_score": 90,
            "risk_score": 0,
            "audit_status": "pass",
            "content_hash": "a" * 64,
            "source_snapshot_hash": "b" * 64,
            "lexical_overlap": 6,
            "meaningfulness_score": 0.635,
            "provenance_score": 0.45,
            "stars": 0,
            "rank": 0.016,
        }

        self.assertEqual(tier_for_prompt(prompt, [candidate]), "full")

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

    def test_platform_name_token_overlap_does_not_waive_mismatch(self):
        """Regression: shopify-storefront used to escape the trap via 'storefront'."""
        cases = [
            (
                "build an ecommerce storefront for a handmade goods launch",
                {
                    "name": "shopify-storefront",
                    "description": "Shopify platform help for storefronts, liquid themes, and API keys.",
                    "platforms": ["shopify"],
                    "tags": ["shopify"],
                },
            ),
            (
                "publish a blog website with categories and an about page",
                {
                    "name": "wordpress-blog-publisher",
                    "description": "WordPress platform help for blogs, categories, publishing, and API keys.",
                    "platforms": ["wordpress"],
                    "tags": ["wordpress"],
                },
            ),
            (
                "add a payment form to my product page",
                {
                    "name": "stripe-payment-form",
                    "description": "Stripe platform help for payment forms, API keys, and webhooks.",
                    "platforms": ["stripe"],
                    "tags": ["stripe"],
                },
            ),
            (
                "organize meeting notes into a project tracker",
                {
                    "name": "notion-project-tracker",
                    "description": "Notion platform help for notes, project trackers, and sync.",
                    "platforms": ["notion"],
                    "tags": ["notion"],
                },
            ),
        ]
        for prompt, base in cases:
            with self.subTest(name=base["name"]):
                candidate = {
                    **base,
                    "quality_status": "active",
                    "quality_score": 90,
                    "rank": 1.0,
                    "similarity": 0.95,
                }
                ranked = rerank_candidates(prompt, [candidate])
                self.assertTrue(ranked[0]["platform_mismatch"])
                self.assertEqual(tier_for_prompt(prompt, [candidate]), "hint")

    def test_generic_capability_beats_mismatched_platform_skill(self):
        prompt = "build an ecommerce storefront for a handmade goods launch"
        candidates = [
            {
                "name": "shopify-storefront",
                "description": "Shopify platform help for storefronts, liquid themes, and API keys.",
                "platforms": ["shopify"],
                "quality_status": "active",
                "quality_score": 90,
                "rank": 1.0,
                "similarity": 0.95,
                "stars": 50,
                "source": "github_skill_file",
            },
            {
                "name": "ecommerce-storefront-builder",
                "description": "Create ecommerce storefronts for handmade goods launches with catalogs.",
                "platforms": [],
                "quality_status": "active",
                "quality_score": 90,
                "rank": 0.03,
                "similarity": 0.90,
                "stars": 40,
                "source": "github_skill_file",
            },
        ]

        ranked = rerank_candidates(prompt, candidates)

        self.assertEqual(ranked[0]["name"], "ecommerce-storefront-builder")
        self.assertTrue(ranked[1]["platform_mismatch"])
        # Mismatched runner's higher cosine must not ambiguity-cap the winner.
        self.assertEqual(tier_for_prompt(prompt, candidates), "full")

    def test_close_specialists_stay_hint(self):
        prompt = "create a monthly sales report with charts and a summary table"
        candidates = [
            {
                "name": "finance-report",
                "description": "Monthly financial report with revenue charts and a summary table.",
                "quality_status": "active",
                "quality_score": 90,
                "rank": 0.5,
                "similarity": 0.92,
            },
            {
                "name": "sales-summary-helper",
                "description": "Monthly sales report with charts and a summary table.",
                "quality_status": "active",
                "quality_score": 90,
                "rank": 0.49,
                "similarity": 0.915,
            },
        ]

        self.assertEqual(tier_for_prompt(prompt, candidates), "hint")


class TaskContractRoutingTests(unittest.TestCase):
    def test_policy_lane_excludes_ponytail_from_primary(self):
        from recommender import candidate_matches_task_contract, skill_role

        ponytail = {
            "name": "ponytail",
            "description": "Always-on coding policy with a minimal safe decision ladder.",
            "category": "policy",
            "platforms": [],
        }
        specialist = {
            "name": "frontend-design",
            "description": "Create polished frontend UI components and layouts.",
            "platforms": [],
        }

        self.assertEqual(skill_role(ponytail), "policy")
        self.assertFalse(
            candidate_matches_task_contract("refactor this React component to use hooks", ponytail)
        )
        self.assertTrue(
            candidate_matches_task_contract("refactor this React component to use hooks", specialist)
        )

    def test_integration_requires_platform_signal_not_generic_verb(self):
        from recommender import candidate_matches_task_contract

        wordpress = {
            "name": "wordpress-blog-publisher",
            "description": "WordPress platform help for blogs and publishing.",
            "platforms": ["wordpress"],
            "category": "integration",
        }
        slack = {
            "name": "slack-messenger",
            "description": "Send Slack messages from an agent.",
            "platforms": ["slack"],
            "category": "integration",
        }

        self.assertFalse(
            candidate_matches_task_contract(
                "publish a blog website with categories and an about page", wordpress
            )
        )
        self.assertFalse(
            candidate_matches_task_contract(
                "build a customer dashboard for weekly reports", wordpress
            )
        )
        self.assertTrue(
            candidate_matches_task_contract("send slack messages from my agent", slack)
        )
        self.assertTrue(
            candidate_matches_task_contract("fix my WordPress categories plugin", wordpress)
        )


class FailurePackInventoryTests(unittest.TestCase):
    def test_failure_pack_covers_required_categories(self):
        from pathlib import Path

        from eval_search import _load_route_cases

        path = Path(__file__).resolve().parents[1] / "evals" / "failure_pack.jsonl"
        cases = _load_route_cases(path)
        tags = {tag for case in cases for tag in case["tags"]}

        for required in (
            "platform-trap",
            "platform-explicit",
            "generic-vs-platform",
            "policy-lane",
            "integration-gate",
            "ambiguous-hint",
            "negative",
        ):
            self.assertIn(required, tags)
        self.assertGreaterEqual(len(cases), 12)


if __name__ == "__main__":
    unittest.main()
