from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import pytest

import auto_skill_cli as cli


def test_doctor_outputs_setup(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setenv("AUTOSKILL_URL", "")  # skip the live reachability check
    # Isolate from the real ~/.claude/settings.json and ~/.autoskill/credentials.json
    # -- doctor only reads them, but a test shouldn't depend on (or be affected
    # by) the developer's actual local hook registration or login session.
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    settings_path = tmp_path / "settings.json"
    result = asyncio.run(cli._command_doctor(argparse.Namespace(settings_path=str(settings_path))))
    out = capsys.readouterr().out
    assert result == 0
    assert "auto-skill doctor" in out
    assert "skill install targets:" in out
    assert "codex:" in out
    assert ".agents" in out


def test_route_outputs_selected_skill(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_route(task: str, client: object | None = None) -> dict:
        del client
        assert task == "make a spreadsheet"
        return {
            "routed": True,
            "route_type": "skill",
            "search_backend": "test",
            "warnings": [],
            "selected_skill": {
                "name": "spreadsheet-router",
                "description": "Create spreadsheet reports.",
                "url": "https://example.com/spreadsheet",
                "risk_score": 0,
            },
            "skill_content": "name: spreadsheet-router\n",
            "route_summary": {
                "decision": "apply_skill_content",
                "selected_name": "spreadsheet-router",
                "reason": "High-confidence route. Apply skill_content in this turn.",
            },
            "route_metrics": {
                "latency_ms": 42,
                "skill_find_ms": 30,
                "injected_tokens": 120,
                "response_tokens": 160,
            },
        }

    monkeypatch.setattr(cli, "route_task_payload", fake_route)
    result = cli.main(["route", "make", "a", "spreadsheet"])
    out = capsys.readouterr().out
    assert result == 0
    assert "route: skill" in out
    assert "summary: decision=apply_skill_content; selected=spreadsheet-router" in out
    assert "metrics: latency=42ms, skill_find=30ms, injected_tokens=120, response_tokens=160" in out
    assert "spreadsheet-router" in out


def test_route_outputs_hint_candidates(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_route(task: str, client: object | None = None) -> dict:
        del client
        assert task == "make a spreadsheet"
        return {
            "routed": True,
            "route_type": "hint",
            "route_tier": "hint",
            "search_backend": "test",
            "warnings": [],
            "selected_skill": {
                "name": "spreadsheet-router",
                "description": "Create spreadsheet reports.",
                "url": "https://example.com/spreadsheet",
                "risk_score": 0,
            },
            "candidates": [
                {
                    "name": "spreadsheet-router",
                    "description": "Create spreadsheet reports.",
                    "url": "https://example.com/spreadsheet",
                    "risk_score": 0,
                },
                {
                    "name": "spreadsheet-cleanup",
                    "description": "Clean spreadsheet data.",
                    "url": "https://example.com/spreadsheet-cleanup",
                    "risk_score": 0,
                },
            ],
            "skill_content": "",
        }

    monkeypatch.setattr(cli, "route_task_payload", fake_route)
    result = cli.main(["route", "make", "a", "spreadsheet"])
    out = capsys.readouterr().out
    assert result == 0
    assert "route: hint" in out
    assert "candidate options:" in out
    assert "spreadsheet-cleanup" in out
    assert "do not inject full skill content" in out


def test_route_json_outputs_payload(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_route(task: str, client: object | None = None) -> dict:
        del task, client
        return {"routed": False, "route_type": "none", "message": "No matching skill found."}

    monkeypatch.setattr(cli, "route_task_payload", fake_route)
    result = cli.main(["route", "unknown", "--json"])
    out = capsys.readouterr().out
    assert result == 1
    assert '"route_type": "none"' in out


def test_route_prompt_context_only(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_route_prompt(prompt: str, client: object | None = None) -> dict:
        del client
        assert prompt == "make a spreadsheet"
        return {
            "should_route": True,
            "reason": "skill-shaped prompt",
            "routed": True,
            "route": {
                "selected_skill": {"name": "spreadsheet-router", "url": "https://example.com/spreadsheet"},
                "warnings": [],
            },
            "context": "[auto-skill] Route selected: spreadsheet-router\n<auto_skill_content>\n...",
        }

    monkeypatch.setattr(cli, "route_prompt_payload", fake_route_prompt)
    result = cli.main(["route-prompt", "make", "a", "spreadsheet", "--context-only"])
    out = capsys.readouterr().out
    assert result == 0
    assert "Route selected: spreadsheet-router" in out


def test_route_prompt_skips_non_task(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_route_prompt(prompt: str, client: object | None = None) -> dict:
        del prompt, client
        return {"should_route": False, "reason": "too short", "routed": False, "route": None, "context": ""}

    monkeypatch.setattr(cli, "route_prompt_payload", fake_route_prompt)
    result = cli.main(["route-prompt", "ok"])
    out = capsys.readouterr().out
    assert result == 1
    assert "skip: too short" in out


def test_feedback_command_records_outcome(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_feedback(route_id: str, outcome: str, *, source: str, note: str, client: object | None = None) -> bool:
        del client
        assert route_id == "route-123"
        assert outcome == "used"
        assert source == "auto-skill-cli"
        assert note == "worked"
        return True

    monkeypatch.setattr(cli, "record_route_feedback", fake_feedback)
    result = cli.main(["feedback", "route-123", "used", "--note", "worked"])
    out = capsys.readouterr().out
    assert result == 0
    assert "recorded feedback" in out


def test_feedback_command_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_feedback(route_id: str, outcome: str, *, source: str, note: str, client: object | None = None) -> bool:
        del route_id, outcome, source, note, client
        return False

    monkeypatch.setattr(cli, "record_route_feedback", fake_feedback)
    result = cli.main(["feedback", "route-123", "used"])
    out = capsys.readouterr().out
    assert result == 1
    assert "not recorded" in out


class FakeMetricsResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload or {}
        self.text = text or json.dumps(self._payload)

    def json(self) -> dict:
        return self._payload


def test_metrics_command_outputs_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class MetricsClient:
        async def __aenter__(self) -> "MetricsClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, **kwargs: object) -> FakeMetricsResponse:
            assert url == "http://127.0.0.1:8000/route-metrics"
            assert kwargs["params"] == {"hours": "12"}
            return FakeMetricsResponse(
                200,
                {
                    "ok": True,
                    "window_hours": 12,
                    "total": 3,
                    "tiers": {"full": 2, "hint": 1},
                    "outcomes": {"used": 1, "pending": 2},
                    "p95_latency_ms": 80,
                    "p95_skill_find_ms": 50,
                    "p95_injected_tokens": 700,
                    "p95_response_tokens": 900,
                    "budget_breaches": {"any": 0},
                    "top_skills": [
                        {
                            "skill_name": "spreadsheet-router",
                            "count": 2,
                            "positive_count": 1,
                            "avg_skill_find_ms": 30,
                            "avg_injected_tokens": 500,
                        }
                    ],
                },
            )

    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda: MetricsClient())
    result = cli.main(["metrics", "--base-url", "http://127.0.0.1:8000", "--hours", "12"])
    out = capsys.readouterr().out
    assert result == 0
    assert "routes: 3" in out
    assert "budget_breaches: any=0" in out
    assert "spreadsheet-router" in out


def test_metrics_command_fails_on_budget_breach(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class MetricsClient:
        async def __aenter__(self) -> "MetricsClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, **kwargs: object) -> FakeMetricsResponse:
            del url, kwargs
            return FakeMetricsResponse(200, {"ok": True, "total": 1, "budget_breaches": {"any": 1}})

    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda: MetricsClient())
    result = cli.main(["metrics", "--base-url", "http://127.0.0.1:8000"])
    out = capsys.readouterr().out
    assert result == 1
    assert "budget_breaches: any=1" in out


def test_metrics_command_reports_public_guard(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    class MetricsClient:
        async def __aenter__(self) -> "MetricsClient":
            return self

        async def __aexit__(self, *args: object) -> None:
            pass

        async def get(self, url: str, **kwargs: object) -> FakeMetricsResponse:
            del url, kwargs
            return FakeMetricsResponse(403, {"error": "read-only public API"})

    monkeypatch.setattr(cli.httpx, "AsyncClient", lambda: MetricsClient())
    result = cli.main(["metrics", "--base-url", "https://skills.example.com"])
    captured = capsys.readouterr()
    assert result == 2
    assert "local-only" in captured.err


def test_install_dry_run_does_not_write(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_resolve(source: str) -> dict:
        assert source == "https://example.com/demo/SKILL.md"
        return {
            "content": "name: demo\n\nInstructions.",
            "source_url": source,
            "metadata": {"url": source},
            "warnings": [],
        }

    monkeypatch.setenv("SKILLS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_resolve_cli_skill", fake_resolve)

    result = cli.main(["install", "https://example.com/demo/SKILL.md", "--dry-run"])
    out = capsys.readouterr().out
    assert result == 0
    assert "dry run: no files written" in out
    assert "publisher identity: unverified" in out
    assert "content sha256:" in out
    assert not (tmp_path / "demo" / "SKILL.md").exists()


def test_install_capability_warnings_cover_non_static_skills() -> None:
    content = """---
name: connected-skill
description: Run a connected workflow with bundled tools.
allowed-tools: Bash
---

Run scripts/deploy.py, then pip install a dependency and call https://example.com.
"""
    warnings = cli._install_capability_warnings(content)
    assert "declares agent tool permissions" in warnings
    assert "references bundled scripts" in warnings
    assert "mentions network access" in warnings
    assert "installs external dependencies" in warnings


def test_install_requires_yes_when_noninteractive(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_resolve(source: str) -> dict:
        return {
            "content": "name: demo\n\nInstructions.",
            "source_url": source,
            "metadata": {"url": source},
            "warnings": [],
        }

    monkeypatch.setenv("SKILLS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_resolve_cli_skill", fake_resolve)

    result = cli.main(["install", "https://example.com/demo/SKILL.md"])
    captured = capsys.readouterr()
    assert result == 1
    assert "refusing non-interactive install without --yes" in captured.err
    assert not (tmp_path / "demo" / "SKILL.md").exists()


def test_install_with_yes_writes_file(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    async def fake_resolve(source: str) -> dict:
        return {
            "content": "name: demo\n\nInstructions.",
            "source_url": source,
            "metadata": {"url": source},
            "warnings": [],
        }

    monkeypatch.setenv("SKILLS_HOME", str(tmp_path))
    monkeypatch.setattr(cli, "_resolve_cli_skill", fake_resolve)

    result = cli.main(["install", "https://example.com/demo/SKILL.md", "--yes"])
    assert result == 0
    assert (tmp_path / "demo" / "SKILL.md").read_text(encoding="utf-8") == "name: demo\n\nInstructions."


def test_codex_install_uses_native_agent_skills_path(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_resolve(source: str) -> dict:
        return {
            "content": "name: demo\n\nInstructions.",
            "source_url": source,
            "metadata": {"url": source},
            "warnings": [],
        }

    monkeypatch.setattr(cli, "_resolve_cli_skill", fake_resolve)
    monkeypatch.setenv("AUTOSKILL_CODEX_SKILLS_HOME", str(tmp_path / ".agents" / "skills"))
    result = cli.main(["install", "https://example.com/demo/SKILL.md", "--target", "codex", "--dry-run"])
    captured = capsys.readouterr()
    assert result == 0
    assert str(tmp_path / ".agents" / "skills" / "demo" / "SKILL.md") in captured.out
    assert "explicit local install" in captured.out
