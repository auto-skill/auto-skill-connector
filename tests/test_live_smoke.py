from __future__ import annotations

from scripts.live_smoke import validate_route_payload


FULL_CHECK = {
    "name": "spreadsheet full route",
    "task": "create an excel spreadsheet report with formulas and charts",
    "min_tier": "full",
    "must_include": ("spreadsheet", "formula"),
}


def _full_payload() -> dict:
    return {
        "routed": True,
        "route_tier": "full",
        "selected_skill": {
            "name": "spreadsheet-router",
            "description": "Create spreadsheet reports with formulas.",
            "url": "https://example.com/spreadsheet",
        },
        "skill_content": "Use formulas to create a spreadsheet report.",
        "route_metrics": {
            "latency_ms": 42,
            "skill_find_ms": 30,
            "injected_tokens": 120,
            "response_tokens": 160,
        },
        "route_summary": {
            "decision": "apply_skill_content",
            "selected_name": "spreadsheet-router",
            "metrics": {
                "latency_ms": 42,
                "skill_find_ms": 30,
                "injected_tokens": 120,
                "response_tokens": 160,
            },
        },
    }


def test_validate_route_payload_accepts_summary_and_metrics() -> None:
    ok, failures = validate_route_payload(FULL_CHECK, _full_payload())

    assert ok is True
    assert failures == []


def test_validate_route_payload_requires_summary_decision() -> None:
    payload = _full_payload()
    payload["route_summary"]["decision"] = "consider_hint"

    ok, failures = validate_route_payload(FULL_CHECK, payload)

    assert ok is False
    assert any("route_summary decision" in failure for failure in failures)


def test_validate_route_payload_requires_metrics() -> None:
    payload = _full_payload()
    payload["route_metrics"].pop("skill_find_ms")

    ok, failures = validate_route_payload(FULL_CHECK, payload)

    assert ok is False
    assert "route_metrics missing skill_find_ms" in failures
