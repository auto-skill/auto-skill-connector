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


def _hook_ns(config_path: Path, *, target: str = "codex", yes: bool = True) -> argparse.Namespace:
    return argparse.Namespace(target=target, yes=yes, settings_path=str(config_path))


def test_enable_hook_codex_appends_to_existing_config(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5-codex"\n\n[sandbox]\nmode = "workspace-write"\n', encoding="utf-8")

    result = cli._command_enable_hook(_hook_ns(config_path))
    out = capsys.readouterr().out

    assert result == 0
    assert "enabled: wrote hook entry" in out
    text = config_path.read_text(encoding="utf-8")
    assert 'model = "gpt-5-codex"' in text  # untouched pre-existing content
    assert "[[hooks.UserPromptSubmit]]" in text
    assert str(cli.HOOK_SCRIPT_PATH) in text

    tomllib = pytest.importorskip("tomllib")  # stdlib only on Python 3.11+
    parsed = tomllib.loads(text)
    assert parsed["model"] == "gpt-5-codex"
    hook_entry = parsed["hooks"]["UserPromptSubmit"][0]["hooks"][0]
    assert hook_entry["command"] == f'python "{cli.HOOK_SCRIPT_PATH}"'


def test_enable_hook_codex_is_idempotent(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    cli._command_enable_hook(_hook_ns(config_path))
    capsys.readouterr()

    result = cli._command_enable_hook(_hook_ns(config_path))
    out = capsys.readouterr().out

    assert result == 0
    assert "already enabled" in out
    assert config_path.read_text(encoding="utf-8").count("BEGIN auto-skill hook") == 1


def test_disable_hook_codex_removes_block_and_preserves_rest(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    config_path.write_text('model = "gpt-5-codex"\n', encoding="utf-8")
    cli._command_enable_hook(_hook_ns(config_path))
    capsys.readouterr()

    result = cli._command_disable_hook(_hook_ns(config_path))
    out = capsys.readouterr().out

    assert result == 0
    assert "disabled: removed hook entry" in out
    text = config_path.read_text(encoding="utf-8")
    assert "BEGIN auto-skill hook" not in text
    assert 'model = "gpt-5-codex"' in text


def test_disable_hook_codex_reports_not_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    config_path = tmp_path / "config.toml"
    result = cli._command_disable_hook(_hook_ns(config_path))
    out = capsys.readouterr().out

    assert result == 0
    assert "not enabled" in out


def test_enable_hook_claude_registers_stop_outcome_hook_alongside_route_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / "settings.json"

    result = cli._command_enable_hook(_hook_ns(settings_path, target="claude"))
    out = capsys.readouterr().out

    assert result == 0
    assert "enabled: wrote hook entry" in out
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    route_args = settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["args"]
    assert route_args == [str(cli.HOOK_SCRIPT_PATH), "--json-output"]
    outcome_args = settings["hooks"]["Stop"][0]["hooks"][0]["args"]
    assert outcome_args == [str(cli.OUTCOME_HOOK_SCRIPT_PATH)]


def test_enable_hook_claude_is_idempotent_for_stop_hook(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / "settings.json"
    cli._command_enable_hook(_hook_ns(settings_path, target="claude"))
    capsys.readouterr()

    cli._command_enable_hook(_hook_ns(settings_path, target="claude"))

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(settings["hooks"]["Stop"]) == 1


def test_disable_hook_claude_removes_both_route_and_outcome_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / "settings.json"
    cli._command_enable_hook(_hook_ns(settings_path, target="claude"))
    capsys.readouterr()

    result = cli._command_disable_hook(_hook_ns(settings_path, target="claude"))
    out = capsys.readouterr().out

    assert result == 0
    assert "disabled: removed hook entry" in out
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert "hooks" not in settings


def test_enable_hook_claude_upgrades_a_pre_json_output_registration(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A settings.json written before --json-output/the Stop hook existed
    must not be treated as already up to date -- the old check only compared
    args[0], which matched forever and silently skipped the Stop hook and
    --json-output additions on every real machine that had already run
    enable-hook once."""
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "hooks": {
                    "UserPromptSubmit": [
                        {
                            "hooks": [
                                {
                                    "type": "command",
                                    "command": "python",
                                    "args": [str(cli.HOOK_SCRIPT_PATH)],
                                    "timeout": 15,
                                    "statusMessage": "Routing prompt through auto-skill...",
                                }
                            ]
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )

    result = cli._command_enable_hook(_hook_ns(settings_path, target="claude"))
    out = capsys.readouterr().out

    assert result == 0
    assert "already enabled" not in out
    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert settings["hooks"]["UserPromptSubmit"][0]["hooks"][0]["args"] == [str(cli.HOOK_SCRIPT_PATH), "--json-output"]
    assert settings["hooks"]["Stop"][0]["hooks"][0]["args"] == [str(cli.OUTCOME_HOOK_SCRIPT_PATH)]


def test_disable_hook_claude_preserves_unrelated_stop_hooks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    settings_path = tmp_path / "settings.json"
    settings_path.write_text(
        json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python", "args": ["/other/hook.py"]}]}]}}),
        encoding="utf-8",
    )
    cli._command_enable_hook(_hook_ns(settings_path, target="claude"))
    capsys.readouterr()

    cli._command_disable_hook(_hook_ns(settings_path, target="claude"))

    settings = json.loads(settings_path.read_text(encoding="utf-8"))
    assert len(settings["hooks"]["Stop"]) == 1
    assert settings["hooks"]["Stop"][0]["hooks"][0]["args"] == ["/other/hook.py"]


def _log_ns(log_path: Path | None, *, tail: int = 20, as_json: bool = False) -> argparse.Namespace:
    return argparse.Namespace(log_path=str(log_path) if log_path else None, tail=tail, json=as_json)


def test_log_show_reports_when_diagnostics_never_enabled(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    missing = tmp_path / "auto-skill-routing.jsonl"
    result = cli._command_log_show(_log_ns(missing))
    out = capsys.readouterr().out

    assert result == 0
    assert "no local routing log yet" in out
    assert "AUTOSKILL_DIAGNOSTICS=1" in out


def test_log_show_prints_recent_entries_most_recent_last(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log_path = tmp_path / "routing.jsonl"
    log_path.write_text(
        json.dumps({"timestamp": "2026-07-31T10:00:00Z", "tier": "full", "selected_skill": {"name": "spreadsheet-router"}})
        + "\n"
        + json.dumps({"timestamp": "2026-07-31T10:05:00Z", "tier": "hint", "reason": "multiple candidates plausible"})
        + "\n",
        encoding="utf-8",
    )

    result = cli._command_log_show(_log_ns(log_path))
    out = capsys.readouterr().out

    assert result == 0
    assert "last 2 of 2 routing decisions" in out
    assert "tier=full  skill=spreadsheet-router" in out
    assert "tier=hint  (multiple candidates plausible)" in out


def test_log_show_respects_tail_limit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_path = tmp_path / "routing.jsonl"
    log_path.write_text(
        "\n".join(json.dumps({"timestamp": f"t{i}", "tier": "full"}) for i in range(30)) + "\n",
        encoding="utf-8",
    )

    result = cli._command_log_show(_log_ns(log_path, tail=5))
    out = capsys.readouterr().out

    assert result == 0
    assert "last 5 of 30 routing decisions" in out
    assert "t29" in out
    assert "t25" in out
    assert "t24" not in out


def test_log_show_json_mode_prints_raw_entries(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_path = tmp_path / "routing.jsonl"
    log_path.write_text(json.dumps({"timestamp": "t0", "tier": "full"}) + "\n", encoding="utf-8")

    result = cli._command_log_show(_log_ns(log_path, as_json=True))
    out = capsys.readouterr().out

    assert result == 0
    assert json.loads(out) == [{"timestamp": "t0", "tier": "full"}]


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


def test_route_outputs_ordered_plan_receipt(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_route(task: str, client: object | None = None) -> dict:
        del client
        assert task == "build a landing page"
        return {
            "routed": True,
            "route_type": "skill",
            "route_tier": "full",
            "search_backend": "test",
            "warnings": [],
            "selected_skill": {
                "name": "frontend-design",
                "description": "Build distinctive frontend interfaces.",
                "url": "https://example.com/frontend-design",
                "risk_score": 0,
                "verification": {"content_hash_verified": True, "static_instruction_only": True},
            },
            "skill_plan": {
                "task_family": "coding",
                "policy_skills": [
                    {
                        "name": "ponytail",
                        "url": "https://example.com/ponytail",
                        "risk_score": 0,
                        "verification": {"content_hash_verified": True, "static_instruction_only": True},
                    }
                ],
                "primary_skill": {"name": "frontend-design", "url": "https://example.com/frontend-design"},
            },
            "route_receipt": (
                "AUTO-SKILL\n"
                "2 skills routed\n"
                "01  policy  ponytail\n"
                "02  primary frontend-design\n"
                "✓ verified | risk_score=0 | content-hash verified | static guidance only | no skill install"
            ),
            "skill_content": "name: frontend-design\n",
            "route_summary": {
                "decision": "apply_skill_content",
                "selected_name": "frontend-design",
                "skill_count": 2,
                "reason": "High-confidence route.",
            },
        }

    monkeypatch.setattr(cli, "route_task_payload", fake_route)
    result = cli.main(["route", "build", "a", "landing", "page"])
    out = capsys.readouterr().out
    assert result == 0
    assert "AUTO-SKILL" in out
    assert "2 skills routed" in out
    assert "01  policy  ponytail" in out
    assert "02  primary frontend-design" in out
    assert "risk_score=0" in out
    assert "no skill install" in out
    assert out.index("AUTO-SKILL") < out.index("compatibility selection:")


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


def test_survey_command_records_response(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_survey(response: str) -> bool:
        assert response == "helpful"
        return True

    monkeypatch.setattr(cli, "record_route_survey_response", fake_survey)
    result = cli.main(["survey", "helpful"])
    out = capsys.readouterr().out
    assert result == 0
    assert "recorded survey response: helpful" in out


def test_survey_command_reports_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_survey(response: str) -> bool:
        del response
        return False

    monkeypatch.setattr(cli, "record_route_survey_response", fake_survey)
    result = cli.main(["survey", "skip"])
    out = capsys.readouterr().out
    assert result == 1
    assert "not recorded" in out


def test_impact_report_command_prints_summary(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_report(days: int) -> dict:
        assert days == 30
        return {
            "window_days": 30,
            "activity": {
                "substantial_tasks_routed": 12,
                "tiers": {"full": 8, "hint": 3, "none": 1},
                "declined_uncertain_count": 1,
                "top_task_families": [{"task_family": "coding", "count": 9}],
                "distinct_specialists_delivered": 4,
                "specialists_discovered_without_install": 3,
            },
            "context_efficiency": {
                "delivered_capsule_tokens": 2140,
                "eligible_raw_tokens": 17600,
                "compression_ratio": 0.88,
                "median_injected_tokens": 300,
                "p95_injected_tokens": 900,
                "token_budget": 1000,
                "budget_compliance_rate": 0.95,
            },
            "measured_lift": {"available": False, "reason": "measurement_mode_not_enabled"},
        }

    monkeypatch.setattr(cli, "get_impact_report", fake_report)
    result = cli.main(["impact-report"])
    out = capsys.readouterr().out
    assert result == 0
    assert "substantial tasks routed: 12" in out
    assert "88% reduction in reference context" in out
    assert "opt in with `auto-skill measurement-mode enable`" in out


def test_impact_report_command_requires_login(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_report(days: int) -> dict:
        del days
        raise cli.NotLoggedInError("Not logged in. Run `auto-skill login` first.")

    monkeypatch.setattr(cli, "get_impact_report", fake_report)
    result = cli.main(["impact-report"])
    err = capsys.readouterr().err
    assert result == 1
    assert "Not logged in" in err


def test_measurement_mode_status_and_enable_and_disable(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    async def fake_get() -> dict:
        return {"enabled": False, "holdout_rate": 0.05}

    monkeypatch.setattr(cli, "get_measurement_mode", fake_get)
    result = cli.main(["measurement-mode", "status"])
    out = capsys.readouterr().out
    assert result == 0
    assert "disabled" in out

    async def fake_set(enabled: bool, holdout_rate: float | None) -> dict:
        assert enabled is True
        assert holdout_rate == 0.1
        return {"enabled": True, "holdout_rate": 0.1}

    monkeypatch.setattr(cli, "set_measurement_mode", fake_set)
    result = cli.main(["measurement-mode", "enable", "--holdout-rate", "0.1"])
    out = capsys.readouterr().out
    assert result == 0
    assert "measurement mode enabled" in out
    assert "may not receive automated guidance" in out

    async def fake_disable(enabled: bool, holdout_rate: float | None = None) -> dict:
        assert enabled is False
        assert holdout_rate is None
        return {"enabled": False, "holdout_rate": 0.1}

    monkeypatch.setattr(cli, "set_measurement_mode", fake_disable)
    result = cli.main(["measurement-mode", "disable"])
    out = capsys.readouterr().out
    assert result == 0
    assert "measurement mode disabled" in out


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


VALID_LOCAL_SKILL = """---
name: acme-review-standards
description: Apply Acme's code review rubric. Use when reviewing pull requests or writing new endpoints.
---

## When reviewing a PR

- You should verify every endpoint has an auth check and a rate-limit bucket.
- Flag any new SQL that concatenates user input; parameterized queries are required.
- Must confirm new config values appear in .env.example with a comment.
"""


def test_validate_command_accepts_valid_skill(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text(VALID_LOCAL_SKILL, encoding="utf-8")
    result = cli.main(["validate", str(path)])
    out = capsys.readouterr().out
    assert result == 0
    assert "ok: acme-review-standards" in out


def test_validate_command_rejects_invalid_skill(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "SKILL.md"
    path.write_text("---\nname: bad\n---\nshort", encoding="utf-8")
    result = cli.main(["validate", str(path)])
    err = capsys.readouterr().err
    assert result == 1
    assert "description" in err
    assert "invalid: fix the errors above" in err


def test_validate_command_missing_file(capsys: pytest.CaptureFixture[str]) -> None:
    result = cli.main(["validate", "no-such-file.md"])
    assert result == 1
    assert "is not a file" in capsys.readouterr().err


def test_install_from_local_path_writes_validated_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "draft.md"
    source.write_text(VALID_LOCAL_SKILL, encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("SKILLS_HOME", str(home))
    result = cli.main(["install", str(source), "--yes"])
    assert result == 0
    installed = home / "acme-review-standards" / "SKILL.md"
    assert installed.read_text(encoding="utf-8") == VALID_LOCAL_SKILL


def test_install_from_local_path_rejects_invalid_skill(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    source = tmp_path / "draft.md"
    source.write_text("---\nname: bad\n---\nshort", encoding="utf-8")
    home = tmp_path / "home"
    monkeypatch.setenv("SKILLS_HOME", str(home))
    result = cli.main(["install", str(source), "--yes"])
    assert result == 1
    assert "failed validation" in capsys.readouterr().err
    assert not home.exists()


def _isolate_mining(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSKILL_MINED_SKILLS_PATH", str(tmp_path / "mined_skills"))


def test_mine_list_sessions_reports_none_found(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = cli.main(["mine", "list-sessions"])
    out = capsys.readouterr().out
    assert result == 0
    assert "no local sessions found" in out


def test_mine_save_then_list_then_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_mining(tmp_path, monkeypatch)
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_LOCAL_SKILL, encoding="utf-8")

    result = cli.main(["mine", "save", str(draft), "--source-session", "sess-1"])
    out = capsys.readouterr().out
    assert result == 0
    assert "saved: acme-review-standards" in out
    assert "private --" in out

    result = cli.main(["mine", "list"])
    out = capsys.readouterr().out
    assert result == 0
    assert "acme-review-standards (draft)" in out

    result = cli.main(["mine", "remove", "acme-review-standards"])
    out = capsys.readouterr().out
    assert result == 0
    assert "removed: acme-review-standards" in out


def test_mine_save_rejects_invalid_draft(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_mining(tmp_path, monkeypatch)
    draft = tmp_path / "draft.md"
    draft.write_text("---\nname: bad\n---\nshort", encoding="utf-8")

    result = cli.main(["mine", "save", str(draft)])
    err = capsys.readouterr().err
    assert result == 1
    assert "failed validation" in err


def test_mine_publish_requires_yes_when_noninteractive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_mining(tmp_path, monkeypatch)
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_LOCAL_SKILL, encoding="utf-8")
    cli.main(["mine", "save", str(draft)])
    capsys.readouterr()

    result = cli.main(["mine", "publish", "acme-review-standards"])
    err = capsys.readouterr().err
    assert result == 1
    assert "refusing non-interactive publish without --yes" in err


def test_mine_publish_delegates_when_confirmed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_mining(tmp_path, monkeypatch)
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_LOCAL_SKILL, encoding="utf-8")
    cli.main(["mine", "save", str(draft)])
    capsys.readouterr()

    async def fake_publish(slug: str) -> dict:
        assert slug == "acme-review-standards"
        return {"slug": slug, "published_skill_id": "priv_123"}

    monkeypatch.setattr(cli, "publish_mined_skill", fake_publish)

    result = cli.main(["mine", "publish", "acme-review-standards", "--yes"])
    out = capsys.readouterr().out
    assert result == 0
    assert "published: acme-review-standards -> private skill priv_123" in out


def test_mine_publish_surfaces_not_logged_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _isolate_mining(tmp_path, monkeypatch)
    draft = tmp_path / "draft.md"
    draft.write_text(VALID_LOCAL_SKILL, encoding="utf-8")
    cli.main(["mine", "save", str(draft)])
    capsys.readouterr()

    async def fake_publish(slug: str) -> dict:
        raise cli.NotLoggedInError("Run `auto-skill login` first.")

    monkeypatch.setattr(cli, "publish_mined_skill", fake_publish)

    result = cli.main(["mine", "publish", "acme-review-standards", "--yes"])
    err = capsys.readouterr().err
    assert result == 1
    assert "login" in err


def test_weights_show_and_reset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOSKILL_WEIGHTS_PATH", str(tmp_path / "weights.json"))
    monkeypatch.setenv("AUTOSKILL_ROUTE_HISTORY_PATH", str(tmp_path / "route_history.json"))

    result = cli.main(["weights", "show"])
    out = capsys.readouterr().out
    assert result == 0
    assert "no learned weights yet" in out

    import auto_skill_personalize as personalize

    for i in range(6):
        personalize.record_route(f"route-{i}", "some-skill", tags=[])
        personalize.record_outcome(f"route-{i}", "used")

    result = cli.main(["weights", "show"])
    out = capsys.readouterr().out
    assert result == 0
    assert "skill:some-skill" in out
    assert "learned" in out

    result = cli.main(["weights", "reset"])
    out = capsys.readouterr().out
    assert result == 0
    assert "weights reset" in out
    assert not personalize.get_weights_path().exists()
