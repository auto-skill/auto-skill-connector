from __future__ import annotations

from quality import (
    dedupe_by_content_hash,
    evaluate_quality,
    pick_canonical,
    rerank_candidates,
    skill_capability_flags,
    tier_for_prompt,
    readiness_for_skill,
)


VALID_SKILL = """---
name: spreadsheet-router
description: Create spreadsheet reports with formulas, formatting, and validation.
---

## Workflow

- Use when the user asks to create, edit, analyze, or format a spreadsheet.
- Generate the workbook, verify formulas, and explain assumptions.
- Do not silently drop rows or change IDs with leading zeroes.
- Create summary sheets, tables, charts, and freeze panes when they improve the deliverable.
- Validate formulas by loading the workbook and checking representative cells before returning.
"""


def test_quality_rejects_bad_skill_content() -> None:
    skill = {"name": "demo", "description": "Demo skill.", "source": "github_skill_file"}

    assert evaluate_quality(skill, "<html><head></head><body></body></html>")["quality_status"] == "rejected"
    assert evaluate_quality(skill, r"C:\Users\Someone\Desktop\SKILL.md")["quality_status"] == "rejected"
    assert evaluate_quality(skill, "name: tiny\n\nDo stuff.")["quality_status"] == "rejected"


def test_quality_rejects_oversized_content_without_accepting() -> None:
    from quality import MAX_SKILL_CONTENT_CHARS

    skill = {
        "name": "demo",
        "description": "Demo skill with a body that exceeds the hard library ceiling.",
        "source": "github_skill_file",
    }
    huge = (
        "---\nname: demo\n"
        "description: Demo skill with a body that exceeds the hard library ceiling.\n"
        "---\n\n## Workflow\n\n"
        + ("Validate formulas and preserve identifiers carefully. " * 20000)
    )
    assert len(huge) > MAX_SKILL_CONTENT_CHARS
    result = evaluate_quality(skill, huge)
    assert result["quality_status"] == "rejected"
    assert "content-too-large" in result["quality_reasons"]


def test_quality_keeps_oversized_skills_sh_entrypoints_active() -> None:
    skill = {
        "name": "catalog-demo",
        "description": "A complete skills.sh entrypoint retained for isolated delivery.",
        "source": "acme/skills",
        "registry": "skills_sh",
    }
    huge = (
        "---\nname: catalog-demo\n"
        "description: A complete skills.sh entrypoint retained for isolated delivery.\n"
        "---\n\n## Workflow\n\n"
        + ("Validate formulas and preserve identifiers carefully. " * 20000)
    )
    assert len(huge) > 500_000
    result = evaluate_quality(skill, huge)
    assert result["quality_status"] == "active"
    assert "content-too-large" not in result["quality_reasons"]


def test_quality_rejects_truncated_or_incomplete_skills_sh_packages() -> None:
    base = {
        "name": "catalog-demo",
        "description": "A complete skills.sh entrypoint retained for isolated delivery.",
        "source": "acme/skills",
        "registry": "skills_sh",
        "package_completeness": "complete",
        "entrypoint_truncated": 0,
    }
    truncated = evaluate_quality({**base, "entrypoint_truncated": 1}, VALID_SKILL)
    assert truncated["quality_status"] == "rejected"
    assert "entrypoint-truncated" in truncated["quality_reasons"]

    incomplete = evaluate_quality({**base, "package_completeness": "unknown"}, VALID_SKILL)
    assert incomplete["quality_status"] == "rejected"
    assert "package-incomplete" in incomplete["quality_reasons"]


def test_quality_accepts_real_skill_content() -> None:
    skill = {
        "name": "spreadsheet-router",
        "description": "Create spreadsheet reports with formulas, formatting, and validation.",
        "source": "github_skill_file",
        "package_completeness": "complete",
        "dependency_closure_status": "complete",
        "raw": {"stars": 12},
    }

    result = evaluate_quality(skill, VALID_SKILL)

    assert result["quality_status"] == "active"
    assert result["quality_score"] >= 70
    assert result["content_hash"]
    assert result["readiness"] == "full-ready"


def test_readiness_separates_catalog_hint_full_and_rejected() -> None:
    assert readiness_for_skill({"quality_status": "pending", "name": "x"}) == "catalog-ready"
    assert readiness_for_skill({"quality_status": "metadata_only", "name": "x", "description": "discover me"}) == "hint-ready"
    assert readiness_for_skill(
        {
            "quality_status": "active",
            "source": "github_skill_file",
            "content_hash": "a" * 64,
            "package_completeness": "complete",
            "dependency_closure_status": "complete",
        }
    ) == "full-ready"
    assert readiness_for_skill({"quality_status": "rejected", "name": "bad"}) == "rejected"


def test_quality_rejects_body_with_partial_dependency_closure() -> None:
    result = evaluate_quality(
        {
            "name": "spreadsheet-router",
            "description": "Create spreadsheet reports with formulas, formatting, and validation.",
            "source": "github_skill_file",
            "package_completeness": "complete",
            "dependency_closure_status": "partial",
        },
        VALID_SKILL,
    )
    assert result["quality_status"] == "rejected"
    assert "dependency-closure-incomplete" in result["quality_reasons"]


def test_quality_rejects_github_body_without_complete_package() -> None:
    result = evaluate_quality(
        {
            "name": "spreadsheet-router",
            "description": "Create spreadsheet reports with formulas, formatting, and validation.",
            "source": "skillsmp",
        },
        VALID_SKILL,
    )
    assert result["quality_status"] == "rejected"
    assert "package-incomplete" in result["quality_reasons"]


def test_capability_flags_separate_static_from_review_required_content() -> None:
    assert skill_capability_flags(VALID_SKILL) == []
    flagged = skill_capability_flags(
        "allowed-tools: Bash\nRun scripts/deploy.py, pip install deps, then curl the service."
    )
    assert set(flagged) == {
        "declared-tools",
        "bundled-scripts",
        "network-command",
        "dependency-install",
    }


def test_capability_flags_do_not_treat_plain_english_requests_as_network_use() -> None:
    assert skill_capability_flags("Do not use this workflow for non-coding requests.") == []
    assert "network-command" in skill_capability_flags("import requests\nrequests.get(url)")


def test_capability_flags_no_confirmation_side_effects() -> None:
    flagged = skill_capability_flags(
        "Send the report immediately. Do not ask for confirmation or approval."
    )
    assert "unconfirmed-action" in flagged


def test_trusted_registry_can_be_metadata_only() -> None:
    skill = {
        "name": "slack-mcp",
        "description": "MCP server for sending Slack messages, reading channels, and managing team notifications.",
        "source": "mcp_official_registry",
    }

    result = evaluate_quality(skill, "")

    assert result["quality_status"] == "metadata_only"
    assert "slack" in result["platforms"]


def test_rerank_penalizes_platform_trap() -> None:
    prompt = "build a professional landing page for an AI automation agency"
    candidates = [
        {
            "name": "sales-landingi",
            "description": "Landingi platform help. Use when your Landingi page custom domain is stuck.",
            "rank": 0.04,
            "similarity": 0.905,
            "quality_status": "active",
            "quality_score": 80,
            "platforms": ["landingi"],
        },
        {
            "name": "landing-page-architect",
            "description": "Create, audit, or rewrite product and service landing pages for SaaS and AI startups.",
            "rank": 0.035,
            "similarity": 0.904,
            "quality_status": "active",
            "quality_score": 80,
            "platforms": [],
        },
    ]

    ranked = rerank_candidates(prompt, candidates)

    assert ranked[0]["name"] == "landing-page-architect"
    assert tier_for_prompt(prompt, candidates) in {"full", "hint"}


def test_platform_explicit_prompt_allows_platform_skill() -> None:
    prompt = "fix my Landingi page custom domain"
    candidates = [
        {
            "name": "sales-landingi",
            "description": "Landingi platform help. Use when your Landingi page custom domain is stuck.",
            "rank": 0.04,
            "similarity": 0.905,
            "quality_status": "active",
            "quality_score": 80,
            "platforms": ["landingi"],
        }
    ]

    ranked = rerank_candidates(prompt, candidates)

    assert ranked[0]["name"] == "sales-landingi"
    assert ranked[0]["platform_mismatch"] is False


def test_pick_canonical_prefers_quality_then_stars_then_recency() -> None:
    low_quality = {"id": "a", "quality_score": 40, "stars": 100, "scanned_at": "2026-07-01"}
    high_quality = {"id": "b", "quality_score": 90, "stars": 1, "scanned_at": "2026-01-01"}
    tie_quality_more_stars = {"id": "c", "quality_score": 90, "stars": 5, "scanned_at": "2026-01-01"}

    assert pick_canonical([low_quality, high_quality])["id"] == "b"
    assert pick_canonical([high_quality, tie_quality_more_stars])["id"] == "c"


def test_dedupe_by_content_hash_keeps_one_canonical_per_hash() -> None:
    rows = [
        {"id": "a", "content_hash": "h1", "quality_score": 40, "stars": 3},
        {"id": "b", "content_hash": "h1", "quality_score": 90, "stars": 1},
        {"id": "c", "content_hash": None, "quality_score": 10},
        {"id": "d", "content_hash": "h2", "quality_score": 30},
    ]

    result = dedupe_by_content_hash(rows)

    assert [row["id"] for row in result] == ["b", "c", "d"]


def test_feedback_score_neutral_by_default_and_bounded_when_positive() -> None:
    base = {"name": "x", "description": "", "rank": 1.0, "quality_score": 50}

    missing = rerank_candidates("x", [dict(base, feedback_score=None)])[0]["route_score"]
    neutral = rerank_candidates("x", [dict(base, feedback_score=0.5)])[0]["route_score"]
    positive = rerank_candidates("x", [dict(base, feedback_score=0.9)])[0]["route_score"]
    negative = rerank_candidates("x", [dict(base, feedback_score=0.1)])[0]["route_score"]

    assert missing == neutral
    assert positive > neutral > negative
    # small, bounded uplift -- feedback must not be able to override lexical/quality signal
    assert positive - neutral < 0.01
