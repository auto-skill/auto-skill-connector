from __future__ import annotations

import asyncio
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

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        del kwargs
        if "find-semantic" in url:
            return FakeResponse(530, {})
        raise AssertionError(f"no fallback should be attempted: {url}")

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        raise AssertionError(f"no fallback should be attempted: {url}")


class SelfHostedClient:
    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        del url, kwargs
        return FakeResponse(
            200,
            {
                "results": [
                    {"name": "first", "url": "https://example.com/first", "rank": 10, "risk_score": 0},
                    {"name": "second", "url": "https://example.com/second", "rank": 9.9, "risk_score": 0},
                ]
            },
        )

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        raise AssertionError(f"fallback should not be called: {url}")


class RouteClient:
    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        del url, kwargs
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

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        raise AssertionError(f"fallback should not be called: {url}")


class NoRouteClient:
    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        del url, kwargs
        return FakeResponse(200, {"results": []})

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        del url, kwargs
        return FakeResponse(200, {"type": "none", "message": "No matching skill found."})


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
        del client
        assert url == "https://github.com/example/skills/tree/main/spreadsheet"
        return VALID_SKILL

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=RouteClient()))
    assert result["routed"] is True
    assert result["route_type"] == "skill"
    assert result["route_tier"] == "full"
    assert result["selected_skill"]["name"] == "spreadsheet-router"
    assert "Generate the workbook" in result["skill_content"]
    assert "apply it immediately" in result["instructions"]


def test_route_task_payload_returns_hint_without_fetch(monkeypatch: pytest.MonkeyPatch) -> None:
    class HintClient:
        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            del url, kwargs
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "name": "spreadsheet-router",
                            "description": "Create spreadsheet reports.",
                            "url": "https://github.com/example/skills/tree/main/spreadsheet",
                            "similarity": 0.88,
                            "risk_score": 0,
                        }
                    ]
                },
            )

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"fallback should not be called: {url}")

    async def fake_fetch(client: object, url: str) -> str:
        raise AssertionError(f"hint routes should not fetch full content: {url}")

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=HintClient()))
    assert result["routed"] is True
    assert result["route_type"] == "hint"
    assert result["route_tier"] == "hint"
    assert result["skill_content"] == ""


def test_route_task_skips_platform_specific_false_positive(monkeypatch: pytest.MonkeyPatch) -> None:
    class LandingClient:
        async def get(self, url: str, **kwargs: object) -> FakeResponse:
            del url, kwargs
            return FakeResponse(
                200,
                {
                    "results": [
                        {
                            "name": "sales-landingi",
                            "description": (
                                "Landingi platform help. Use when your Landingi page will not publish, "
                                "custom domain is stuck, OAuth API key setup is broken, or leads are not syncing."
                            ),
                            "url": "https://github.com/sales-skills/sales/tree/HEAD/skills/sales-landingi",
                            "similarity": 0.905,
                            "risk_score": 0,
                            "tags": ["platform", "landing-pages"],
                        },
                        {
                            "name": "landing-page-architect",
                            "description": "Create, audit, or rewrite product and service landing pages.",
                            "url": "https://github.com/example/skills/tree/main/landing-page-architect",
                            "similarity": 0.906,
                            "risk_score": 0,
                        },
                    ]
                },
            )

        async def post(self, url: str, **kwargs: object) -> FakeResponse:
            raise AssertionError(f"fallback should not be called: {url}")

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


def test_route_prompt_payload_skips_without_network() -> None:
    result = asyncio.run(core.route_prompt_payload("ok", client=RouteClient()))
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


def test_codex_install_target_is_unsupported(tmp_path: Path) -> None:
    with pytest.raises(core.UnsupportedTargetError):
        core.install_skill_from_content(
            "name: demo\n",
            source_url="https://example.com/demo/SKILL.md",
            target="codex",
            skills_home=tmp_path,
        )
