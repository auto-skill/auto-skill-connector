from __future__ import annotations

from auto_skill_receipt import (
    format_mcp_route_tool_result,
    format_route_card_markdown,
    format_route_receipt,
)
import mcp_server


def _full_payload() -> dict:
    return {
        "routed": True,
        "route_type": "skill",
        "route_tier": "full",
        "route_id": "route-1",
        "selected_skill": {
            "name": "frontend-design",
            "url": "https://example.com/frontend-design",
            "risk_score": 0,
            "verification": {"content_hash_verified": True, "static_instruction_only": True},
        },
        "skill_plan": {
            "policy_skills": [
                {
                    "name": "ponytail",
                    "url": "https://example.com/ponytail",
                    "risk_score": 0,
                    "capsule": "Prefer existing code.",
                    "verification": {"content_hash_verified": True, "static_instruction_only": True},
                }
            ],
            "primary_skill": {
                "name": "frontend-design",
                "url": "https://example.com/frontend-design",
            },
        },
        "skill_content": "name: frontend-design\n\n## Workflow\n\nBuild the page.\n",
    }


def test_format_route_card_markdown_full_plan() -> None:
    card = format_route_card_markdown(_full_payload())
    assert card.startswith("### AUTO-SKILL")
    assert "**2 skills routed**" in card
    assert "| policy | ponytail |" in card
    assert "| primary | frontend-design |" in card
    assert "`✓ verified`" in card
    assert "`risk_score=0`" in card
    assert "malware" not in card.lower()


def test_format_route_card_markdown_hint_tier() -> None:
    payload = _full_payload()
    payload["route_tier"] = "hint"
    payload["route_type"] = "hint"
    card = format_route_card_markdown(payload)
    assert "`hint only`" in card
    assert "not verified for injection" in card


def test_format_mcp_route_tool_result_is_card_first() -> None:
    text = format_mcp_route_tool_result(_full_payload())
    assert text.startswith("### AUTO-SKILL")
    assert "APPLY —" in text
    assert "```json" in text
    assert '"skill_content"' in text
    assert text.index("### AUTO-SKILL") < text.index("```json")
    # Same helper used by mcp_server
    assert mcp_server.format_mcp_route_visible(_full_payload()) == text


def test_format_mcp_route_tool_result_no_route() -> None:
    text = format_mcp_route_tool_result(
        {"routed": False, "route_tier": "none", "message": "No match."}
    )
    assert text.startswith("### AUTO-SKILL")
    assert "**no route**" in text
    assert "No match." in text
