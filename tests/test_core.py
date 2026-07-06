from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

import auto_skill_core as core


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


class FakeClient:
    def __init__(self) -> None:
        self.posts: list[str] = []

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        del kwargs
        if "find-semantic" in url:
            return FakeResponse(530, {})
        return FakeResponse(404, {})

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        del kwargs
        self.posts.append(url)
        return FakeResponse(
            200,
            {
                "type": "recommend",
                "skill": {
                    "name": "spreadsheet-writer",
                    "description": "Create spreadsheets.",
                    "url": "https://github.com/example/skills/tree/main/spreadsheet",
                    "risk_score": 0,
                },
            },
        )


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


class ClarifyClient:
    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        del url, kwargs
        return FakeResponse(530, {})

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        del kwargs
        if "functions/v1/recommend-skill" in url:
            return FakeResponse(
                200,
                {
                    "type": "clarify",
                    "options": [
                        {"name": "first", "url": "https://example.com/first", "risk_score": 0},
                        {"name": "second", "url": "https://example.com/second", "risk_score": 0},
                    ],
                },
            )
        raise AssertionError("keyword fallback should not be called")


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


def test_search_falls_back_when_selfhosted_fails() -> None:
    fake = FakeClient()
    result = asyncio.run(core._search(fake, "make a spreadsheet"))
    assert result["type"] == "recommend"
    assert result["search_backend"] == "supabase-edge"
    assert "Self-hosted search did not return" in result["warnings"][0]
    assert fake.posts


def test_selfhosted_search_autopicks_top_candidate() -> None:
    result = asyncio.run(core._search(SelfHostedClient(), "ambiguous browser task"))
    assert result["type"] == "recommend"
    assert result["skill"]["name"] == "first"
    assert "options" not in result


def test_supabase_clarify_response_autopicks_top_safe_candidate() -> None:
    result = asyncio.run(core._search(ClarifyClient(), "ambiguous browser task"))
    assert result["type"] == "recommend"
    assert result["skill"]["name"] == "first"
    assert result["search_backend"] == "supabase-edge"


def test_should_route_prompt_skips_non_tasks() -> None:
    assert core.should_route_prompt("ok")["should_route"] is False
    assert core.should_route_prompt("/help")["should_route"] is False
    assert core.should_route_prompt("what is the current state?")["should_route"] is False
    assert core.should_route_prompt("create a spreadsheet with formulas")["should_route"] is True


def test_route_task_payload_returns_router_decision(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_fetch(client: object, url: str) -> str:
        del client
        assert url == "https://github.com/example/skills/tree/main/spreadsheet"
        return "name: spreadsheet-router\n\nDo spreadsheet work."

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.route_task_payload("make a spreadsheet", client=RouteClient()))
    assert result["routed"] is True
    assert result["route_type"] == "skill"
    assert result["selected_skill"]["name"] == "spreadsheet-router"
    assert "Do spreadsheet work" in result["skill_content"]
    assert "apply it immediately" in result["instructions"]


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
        return "name: spreadsheet-router\n\nDo spreadsheet work."

    monkeypatch.setattr(core, "_fetch_content", fake_fetch)
    result = asyncio.run(core.route_prompt_payload("make a spreadsheet", client=RouteClient()))
    assert result["should_route"] is True
    assert result["routed"] is True
    assert "<auto_skill_content>" in result["context"]
    assert "Do spreadsheet work" in result["context"]


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
