from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import auto_skill_core as core


VALID_SKILL = """---
name: spreadsheet-router
description: Create spreadsheet reports with formulas, formatting, and validation.
---

## Workflow

- Use this skill when the user asks to create, edit, analyze, or format a spreadsheet.
- Inspect the requested output and choose formulas, tables, charts, and validation rules.
- Generate the workbook, verify formulas, and explain any assumptions in the final answer.
"""


class FakeResponse:
    def __init__(self, status_code: int, payload: object | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class UnreachableClient:
    """Self-hosted search unreachable, and nothing else should be tried --
    there is no fallback backend (dropped 2026-07-07: the old Supabase
    fallback was frozen and could only serve stale results silently)."""

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        del kwargs
        if "find-semantic" in url:
            return FakeResponse(530, {})
        raise AssertionError(f"no fallback should be attempted: {url}")


class SelfHostedClient:
    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        raise AssertionError(f"search text must not be sent in a GET URL: {url} {kwargs}")

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        assert url.endswith("/find-semantic")
        assert kwargs["json"] == {"q": "ambiguous browser task", "limit": 8, "gate": False}
        return FakeResponse(
            200,
            {
                "results": [
                    {"name": "first", "url": "https://example.com/first", "rank": 10, "risk_score": 0},
                    {"name": "second", "url": "https://example.com/second", "rank": 9.9, "risk_score": 0},
                ]
            },
        )

class RouteClient:
    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        raise AssertionError(f"full backend /route payload should not fetch content: {url} {kwargs}")

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        assert url.endswith("/route")
        assert kwargs["json"]["task"] == "make a spreadsheet"
        assert kwargs["json"]["client"] == core.CLIENT_NAME
        assert kwargs["json"]["client_version"] == core.CLIENT_VERSION
        return FakeResponse(
            200,
            {
                "tier": "full",
                "skill": {
                    "name": "spreadsheet-router",
                    "description": "Create spreadsheet reports.",
                    "url": "https://github.com/example/skills/tree/main/spreadsheet",
                    "route_score": 0.92,
                    "similarity": 0.92,
                    "risk_score": 0,
                    "quality_status": "active",
                    "quality_score": 92,
                    "verification": {
                        "content_hash_verified": True,
                        "static_instruction_only": True,
                        "source": "indexed-local-copy",
                        "publisher_verified": False,
                    },
                },
                "content": VALID_SKILL,
                "route_id": "route-123",
                "score_debug": {
                    "tier": "full",
                    "quality_status": "active",
                    "metrics": {
                        "latency_ms": 42,
                        "skill_find_ms": 30,
                        "injected_tokens": 120,
                        "response_tokens": 160,
                    },
                },
                "config_version": "test",
            },
        )


class NoRouteClient:
    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        raise AssertionError(f"backend /route none should be authoritative: {url} {kwargs}")

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        del url, kwargs
        return FakeResponse(200, {"tier": "none", "skill": None, "score_debug": {"tier": "none"}})


def test_raw_candidates_from_github_blob() -> None:
    assert core._raw_candidates("https://github.com/acme/tools/blob/main/skills/report/SKILL.md") == [
        "https://raw.githubusercontent.com/acme/tools/main/skills/report/SKILL.md"
    ]


def test_raw_candidates_from_github_tree() -> None:
    assert core._raw_candidates("https://github.com/acme/tools/tree/main/skills/report") == [
        "https://raw.githubusercontent.com/acme/tools/main/skills/report/SKILL.md",
        "https://raw.githubusercontent.com/acme/tools/main/skills/report/skill.md",
    ]


def test_raw_candidates_from_github_repo_root() -> None:
    assert core._raw_candidates("https://github.com/acme/tools")[:2] == [
        "https://raw.githubusercontent.com/acme/tools/HEAD/SKILL.md",
        "https://raw.githubusercontent.com/acme/tools/HEAD/skill.md",
    ]


def test_skill_content_quality_gate() -> None:
    assert core._looks_like_skill_content("<!doctype html><html><head></head><body></body></html>") is False
    assert core._looks_like_skill_content(r"C:\Users\Someone\Desktop\SKILL.md") is False
    assert core._looks_like_skill_content("name: tiny\n\nDo stuff.") is False
    assert core._looks_like_skill_content(VALID_SKILL) is True


def test_slugify() -> None:
    assert core._slugify("Cold Email Outreach!") == "cold-email-outreach"
    assert core._slugify("!!!") == "skill"


def test_dedupe_candidates_by_name() -> None:
    candidates = [
        {"name": "webapp-testing", "url": "https://one.example"},
        {"name": "webapp-testing", "url": "https://two.example"},
        {"name": "spreadsheet", "url": "https://three.example"},
    ]
    assert [c["url"] for c in core._dedupe_candidates(candidates)] == [
        "https://one.example",
        "https://three.example",
    ]


def test_search_reports_no_route_when_selfhosted_unreachable() -> None:
    """No fallback backend exists (dropped 2026-07-07): an unreachable
    self-hosted server means an honest no-route result, not a silent query
    to a second, possibly stale service."""
    result = asyncio.run(core._search(UnreachableClient(), "make a spreadsheet"))
    assert result["type"] == "none"
    assert result["search_backend"] == "unavailable"
    assert "unreachable" in result["warnings"][0] or "no usable result" in result["warnings"][0]


def test_selfhosted_search_autopicks_top_candidate() -> None:
    result = asyncio.run(core._search(SelfHostedClient(), "ambiguous browser task"))
    assert result["type"] == "recommend"
    assert result["skill"]["name"] == "first"
    assert "options" not in result


def test_should_route_prompt_skips_non_tasks() -> None:
    assert core.should_route_prompt("ok")["should_route"] is False
    assert core.should_route_prompt("/help")["should_route"] is False
    assert core.should_route_prompt("what is the current state?")["should_route"] is False
    assert core.should_route_prompt("create a spreadsheet with formulas")["should_route"] is True


def test_route_task_payload_returns_router_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(client: object, url: str) -> str:
        raise AssertionError(f"backend /route already supplied full content: {client} {url}")

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=RouteClient()))
    assert result["routed"] is True
    assert result["route_type"] == "skill"
    assert result["route_tier"] == "full"
    assert result["selected_skill"]["name"] == "spreadsheet-router"
    assert result["selected_skill"]["quality_status"] == "active"
    assert result["route_id"] == "route-123"
    assert result["route_metrics"]["skill_find_ms"] == 30
    assert result["route_metrics"]["injected_tokens"] == 120
    assert result["route_summary"]["decision"] == "apply_skill_content"
    assert result["route_summary"]["selected_name"] == "spreadsheet-router"
    assert result["route_summary"]["metrics"]["skill_find_ms"] == 30
    assert "Generate the workbook" in result["skill_content"]
    assert "apply it immediately" in result["instructions"]
    assert "task" not in result
    assert "make a spreadsheet" not in json.dumps(result)


def test_route_task_payload_composes_policy_before_primary_skill() -> None:
    policy_capsule = "Prefer existing code, then the standard library, then native platform capabilities."

    class PlanClient:
        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            assert url.endswith("/route")
            return FakeResponse(
                200,
                {
                    "tier": "full",
                    "skill": {
                        "name": "frontend-design",
                        "description": "Build distinctive frontend interfaces.",
                        "url": "https://github.com/example/frontend-design",
                        "risk_score": 0,
                        "content_hash": "frontend-hash",
                        "verification": {"content_hash_verified": True, "static_instruction_only": True},
                    },
                    "content": VALID_SKILL,
                    "context_guard": {"delivery": "full", "policy": "hybrid-v1"},
                    "task_analysis": {"family": "coding", "action": "implement"},
                    "skill_plan": {
                        "task_family": "coding",
                        "policy_skills": [
                            {
                                "name": "ponytail",
                                "description": "Minimal safe coding policy.",
                                "url": "https://github.com/DietrichGebert/ponytail",
                                "content_hash": "policy-hash",
                                "risk_score": 0,
                                "capsule": policy_capsule,
                                "verification": {
                                    "content_hash_verified": True,
                                    "static_instruction_only": True,
                                },
                            }
                        ],
                        "primary_skill": {
                            "name": "frontend-design",
                            "url": "https://github.com/example/frontend-design",
                            "role": "specialist",
                            "routing_tier": "full",
                        },
                        "selected_roles": ["policy", "specialist"],
                        "precedence": ["user-project-team", "policy", "primary", "supporting"],
                    },
                    "score_debug": {"tier": "full", "metrics": {"injected_tokens": 80}},
                },
            )

    result = asyncio.run(core.route_task_payload("build a React landing page", client=PlanClient()))
    assert result["skill_plan"]["task_family"] == "coding"
    assert result["skill_plan"]["policy_skills"][0]["name"] == "ponytail"
    assert result["skill_plan"]["primary_skill"]["name"] == "frontend-design"
    assert result["route_summary"]["skill_count"] == 2
    receipt = result["route_receipt"]
    assert receipt == (
        "AUTO-SKILL\n"
        "2 skills routed\n"
        "01  policy  ponytail\n"
        "02  primary frontend-design\n"
        "✓ verified | risk_score=0 | content-hash verified | static guidance only | no skill install"
    )
    context = core.build_route_context(result)
    assert context.startswith("### AUTO-SKILL\n")
    assert "| policy | ponytail |" in context
    assert "| primary | frontend-design |" in context
    assert "Task-family policy: ponytail" in context
    assert "Route selected: frontend-design" in context
    assert context.index("### AUTO-SKILL") < context.index("Task-family policy: ponytail")
    assert context.index("Task-family policy: ponytail") < context.index("Route selected: frontend-design")
    assert policy_capsule in context
    assert result["route_card_markdown"].startswith("### AUTO-SKILL")
    assert "| primary | frontend-design |" in result["route_card_markdown"]


def test_route_task_payload_downgrades_oversized_backend_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSKILL_MAX_INJECTED_CHARS", "260")

    class LargeRouteClient:
        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"backend /route already supplied content: {url} {kwargs}")

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            assert url.endswith("/route")
            return FakeResponse(
                200,
                {
                    "tier": "full",
                    "skill": {
                        "name": "spreadsheet-router",
                        "description": "Create spreadsheet reports.",
                        "url": "https://github.com/example/skills/tree/main/spreadsheet",
                        "content_hash": "a" * 64,
                        "route_score": 0.92,
                        "similarity": 0.92,
                        "risk_score": 0,
                        "verification": {"content_hash_verified": True, "static_instruction_only": True},
                    },
                    "content": VALID_SKILL + ("\n- Extra detailed workflow step." * 20),
                    "content_url": "/content/" + "a" * 64,
                    "score_debug": {"tier": "full"},
                },
            )

    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=LargeRouteClient()))
    assert result["routed"] is True
    assert result["route_type"] == "isolation"
    assert result["route_tier"] == "full"
    assert result["skill_content"] == ""
    assert result["content_url"] == "/content/" + "a" * 64
    assert "Do not inline or truncate" in result["instructions"]
    assert "inline budget" in result["warnings"][0]


def test_route_task_payload_returns_hint_without_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    class HintClient:
        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"hint routes should not fetch full content: {url} {kwargs}")

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            assert url.endswith("/route")
            assert kwargs["json"]["client"] == core.CLIENT_NAME
            assert kwargs["json"]["client_version"] == core.CLIENT_VERSION
            return FakeResponse(
                200,
                {
                    "tier": "hint",
                    "skill": {
                        "name": "spreadsheet-router",
                        "description": "Create spreadsheet reports.",
                        "url": "https://github.com/example/skills/tree/main/spreadsheet",
                        "route_score": 0.88,
                        "similarity": 0.88,
                        "risk_score": 0,
                    },
                    "candidates": [
                        {
                            "name": "spreadsheet-router",
                            "description": "Create spreadsheet reports.",
                            "url": "https://github.com/example/skills/tree/main/spreadsheet",
                            "route_score": 0.88,
                            "similarity": 0.88,
                            "risk_score": 0,
                        },
                        {
                            "name": "spreadsheet-cleanup",
                            "description": "Clean and normalize spreadsheet data.",
                            "url": "https://github.com/example/skills/tree/main/spreadsheet-cleanup",
                            "route_score": 0.78,
                            "similarity": 0.78,
                            "risk_score": 0,
                        },
                    ],
                    "score_debug": {
                        "tier": "hint",
                        "metrics": {
                            "latency_ms": 45,
                            "skill_find_ms": 30,
                            "injected_tokens": 0,
                            "response_tokens": 180,
                        },
                    },
                },
            )

    async def fake_fetch(client: object, url: str) -> str:
        raise AssertionError(f"hint routes should not fetch full content: {url}")

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=HintClient()))
    assert result["routed"] is True
    assert result["route_type"] == "hint"
    assert result["route_tier"] == "hint"
    assert result["skill_content"] == ""
    assert [c["name"] for c in result["candidates"]] == ["spreadsheet-router", "spreadsheet-cleanup"]
    assert result["route_summary"]["decision"] == "consider_hint"
    assert result["route_summary"]["candidate_count"] == 2
    context = core.build_route_context(result)
    assert "Candidate options:" in context
    assert "spreadsheet-cleanup" in context
    assert "Route metrics:" in context
    assert "skill_find" in context
    assert "Choose a listed candidate yourself only when the fit is obvious" in context
    assert "active instructions" in context


def test_route_task_skips_platform_specific_false_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    class LandingClient:
        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"backend /route should own platform reranking: {url} {kwargs}")

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            assert url.endswith("/route")
            assert kwargs["json"]["client"] == core.CLIENT_NAME
            assert kwargs["json"]["client_version"] == core.CLIENT_VERSION
            return FakeResponse(
                200,
                {
                    "tier": "full",
                    "skill": {
                        "name": "landing-page-architect",
                        "description": "Create, audit, or rewrite product and service landing pages.",
                        "url": "https://github.com/example/skills/tree/main/landing-page-architect",
                        "route_score": 0.906,
                        "similarity": 0.906,
                        "risk_score": 0,
                        "verification": {"content_hash_verified": True, "static_instruction_only": True},
                    },
                    "content": """---
name: landing-page-architect
description: Create landing pages with clear positioning and conversion structure.
---

## Workflow

- Use when the user asks to create, audit, or rewrite landing-page copy or structure.
- Build sections for hero, proof, offer, objections, CTA, FAQ, and decision details.
- Generate concrete page copy and verify that the page matches the target audience.
""",
                    "score_debug": {"tier": "full", "platform_mismatch": False},
                },
            )

    async def fake_fetch(client: object, url: str) -> str:
        del client
        assert "landing-page-architect" in url
        return """---
name: landing-page-architect
description: Create landing pages with clear positioning and conversion structure.
---

## Workflow

- Use when the user asks to create, audit, or rewrite landing-page copy or structure.
- Build sections for hero, proof, offer, objections, CTA, FAQ, and decision details.
- Generate concrete page copy and verify that the page matches the target audience.
"""

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(
        core.route_task_payload("build a professional landing page for an AI automation agency", client=LandingClient())
    )
    assert result["routed"] is True
    assert result["route_type"] == "skill"
    assert result["selected_skill"]["name"] == "landing-page-architect"


def test_route_task_payload_handles_no_route() -> None:
    result = asyncio.run(core.route_task_payload("too obscure", client=NoRouteClient()))
    assert result["routed"] is False
    assert result["route_type"] == "none"
    assert result["route_summary"]["decision"] == "continue_normally"


def test_record_route_feedback_posts_privacy_safe_payload(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSKILL_URL", "http://localhost:8000")

    class FeedbackClient:
        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            assert url == "http://localhost:8000/route-feedback"
            assert kwargs["json"] == {
                "route_id": "route-123",
                "outcome": "used",
                "source": core.CLIENT_NAME,
            }
            return FakeResponse(200, {"ok": True})

    assert asyncio.run(core.record_route_feedback("route-123", "used", client=FeedbackClient())) is True


def test_record_route_feedback_fails_closed_for_public_or_invalid(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSKILL_URL", "http://localhost:8000")

    class FeedbackClient:
        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            del url, kwargs
            return FakeResponse(403, {"error": "read-only public API"})

    assert asyncio.run(core.record_route_feedback("route-123", "used", client=FeedbackClient())) is False
    assert asyncio.run(core.record_route_feedback("route-123", "raw prompt text", client=FeedbackClient())) is False


def test_route_task_does_not_fall_back_to_query_string_search() -> None:
    class LegacyClient:
        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            del url, kwargs
            return FakeResponse(403, {"error": "read-only public API"})

        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"automatic routing must not use GET fallback: {url} {kwargs}")

    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=LegacyClient()))
    assert result["routed"] is False
    assert result["search_backend"] is None


def test_recommend_skill_payload_is_explicit_preview_only(monkeypatch: pytest.MonkeyPatch) -> None:
    class PreviewClient:
        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"search text must not be sent in a GET URL: {url} {kwargs}")

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            assert url.endswith("/find-semantic")
            assert kwargs["json"]["q"] == "make a spreadsheet"
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "name": "spreadsheet-router",
                            "description": "Create spreadsheet reports.",
                            "url": "https://github.com/example/skills/tree/main/spreadsheet",
                            "rank": 10,
                            "similarity": 0.92,
                            "risk_score": 0,
                        }
                    ]
                },
            )

    async def fake_fetch(client: object, url: str) -> str:
        del client
        assert url == "https://github.com/example/skills/tree/main/spreadsheet"
        return VALID_SKILL

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.recommend_skill_payload("make a spreadsheet", client=PreviewClient()))
    assert result["found"] is True
    assert result["route_tier"] == "preview"
    assert result["legacy_preview"] is True
    instructions = result["instructions"].lower()
    assert "single best" not in instructions
    assert "immediately" not in instructions
    assert "retrieved reference material" in instructions
    assert "prefer route_task" in instructions


def test_full_route_without_verified_hash_is_downgraded() -> None:
    class UnverifiedClient:
        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"unverified content must not be fetched: {url} {kwargs}")

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            assert url.endswith("/route")
            return FakeResponse(
                200,
                {
                    "tier": "full",
                    "skill": {
                        "name": "spreadsheet-router",
                        "description": "Create spreadsheet reports.",
                        "url": "https://github.com/example/skills/tree/main/spreadsheet",
                        "route_score": 0.92,
                        "similarity": 0.92,
                        "risk_score": 0,
                    },
                    "content": VALID_SKILL,
                    "score_debug": {"tier": "full"},
                },
            )

    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=UnverifiedClient()))
    assert result["routed"] is True
    assert result["route_type"] == "hint"
    assert result["skill_content"] == ""
    assert "verified static content" in result["warnings"][0]


@pytest.mark.parametrize(
    "prompt",
    ["ok", "/help", "what is the current state?", "PRIVATE PASTED CONTEXT " * 200],
)
def test_route_prompt_payload_skips_without_network(prompt: str) -> None:
    result = asyncio.run(core.route_prompt_payload(prompt, client=RouteClient()))
    assert result["should_route"] is False
    assert result["route"] is None


def test_route_prompt_payload_returns_injectable_context(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(client: object, url: str) -> str:
        del client, url
        return VALID_SKILL

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.route_prompt_payload("make a spreadsheet", client=RouteClient()))
    assert result["should_route"] is True
    assert result["routed"] is True
    assert "<auto_skill_content>" in result["context"]
    assert "Generate the workbook" in result["context"]


def test_install_refuses_overwrite_without_force(tmp_path: Path) -> None:
    content = "name: demo-skill\n\nUse care."
    first = core.install_skill_from_content(
        content,
        source_url="https://example.com/demo/SKILL.md",
        skills_home=tmp_path,
    )
    assert first["dest_file"].exists()

    with pytest.raises(core.SkillAlreadyExistsError):
        core.install_skill_from_content(
            content,
            source_url="https://example.com/demo/SKILL.md",
            skills_home=tmp_path,
        )


def test_install_force_overwrites(tmp_path: Path) -> None:
    content = "name: demo-skill\n\nUse care."
    core.install_skill_from_content(content, source_url="https://example.com/demo/SKILL.md", skills_home=tmp_path)
    result = core.install_skill_from_content(
        "name: demo-skill\n\nUpdated.",
        source_url="https://example.com/demo/SKILL.md",
        skills_home=tmp_path,
        force=True,
    )
    assert result["would_overwrite"] is True
    assert result["dest_file"].read_text(encoding="utf-8").endswith("Updated.")


def test_dry_run_reports_existing_skill_without_force(tmp_path: Path) -> None:
    content = "name: demo-skill\n\nUse care."
    core.install_skill_from_content(content, source_url="https://example.com/demo/SKILL.md", skills_home=tmp_path)
    result = core.install_skill_from_content(
        content,
        source_url="https://example.com/demo/SKILL.md",
        skills_home=tmp_path,
        dry_run=True,
    )
    assert result["would_overwrite"] is True
    assert result["dry_run"] is True


@pytest.mark.parametrize("target", ["claude", "codex", "cursor", "copilot"])
def test_portable_install_targets_write_skill_md(tmp_path: Path, target: str) -> None:
    result = core.install_skill_from_content(
        "name: demo\n\nUse care.",
        source_url="https://example.com/demo/SKILL.md",
        target=target,
        skills_home=tmp_path / target,
    )
    assert result["target"] == target
    assert result["dest_file"] == tmp_path / target / "demo" / "SKILL.md"
    assert result["dest_file"].exists()


def test_unknown_install_target_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(core.UnsupportedTargetError):
        core.install_skill_from_content(
            "name: demo\n\nUse care.",
            source_url="https://example.com/demo/SKILL.md",
            target="unknown",
            skills_home=tmp_path,
        )


def test_validate_skill_content_accepts_valid_skill() -> None:
    result = core.validate_skill_content(VALID_SKILL)
    assert result["ok"] is True
    assert result["errors"] == []
    assert result["name"] == "spreadsheet-router"
    assert result["slug"] == "spreadsheet-router"


def test_validate_skill_content_requires_frontmatter_fields() -> None:
    body = "## Workflow\n\n- " + "You should verify every rule in the checklist. " * 10
    result = core.validate_skill_content(body)
    assert result["ok"] is False
    assert any("frontmatter" in e for e in result["errors"])
    assert any("name:" in e for e in result["errors"])
    assert any("description:" in e for e in result["errors"])


def test_validate_skill_content_rejects_stub_body() -> None:
    result = core.validate_skill_content(
        "---\nname: stub\ndescription: Use when testing stub rejection behavior here.\n---\n\nToo short."
    )
    assert result["ok"] is False
    assert any("too thin" in e for e in result["errors"])


def test_validate_skill_content_rejects_oversized_description() -> None:
    content = VALID_SKILL.replace(
        "description: Create spreadsheet reports with formulas, formatting, and validation.",
        "description: " + "x" * 1100,
    )
    result = core.validate_skill_content(content)
    assert result["ok"] is False
    assert any("1024" in e for e in result["errors"])


def test_validate_skill_content_warns_on_no_confirmation_language() -> None:
    content = VALID_SKILL + "\n- Send the report immediately -- do NOT ask for confirmation.\n"
    result = core.validate_skill_content(content)
    assert result["ok"] is True
    assert any("no-confirmation" in w for w in result["warnings"])


def test_validate_skill_content_warns_on_missing_trigger_phrase() -> None:
    result = core.validate_skill_content(VALID_SKILL)
    assert any("trigger" in w for w in result["warnings"])


def test_validate_skill_content_empty() -> None:
    result = core.validate_skill_content("")
    assert result["ok"] is False
    assert result["errors"] == ["content is empty"]
