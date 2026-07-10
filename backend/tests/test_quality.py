from __future__ import annotations

from quality import dedupe_by_content_hash, evaluate_quality, pick_canonical, rerank_candidates, tier_for_prompt


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


def test_quality_accepts_real_skill_content() -> None:
    skill = {
        "name": "spreadsheet-router",
        "description": "Create spreadsheet reports with formulas, formatting, and validation.",
        "source": "github_skill_file",
        "raw": {"stars": 12},
    }

    result = evaluate_quality(skill, VALID_SKILL)

    assert result["quality_status"] == "active"
    assert result["quality_score"] >= 70
    assert result["content_hash"]


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
