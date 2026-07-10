"""Parity test: hooks/skill_suggest.py duplicates several gating functions
from auto_skill_core.py rather than importing it, since the hook must stay
stdlib-only and dependency-free (no httpx/mcp) for portability. That
duplication is a standing risk -- if one copy is fixed and the other isn't,
the hook and the MCP/CLI paths silently diverge on what they consider safe.
This test imports both and asserts their overlapping gates agree on a shared
battery of inputs, so a future edit to one copy that isn't mirrored in the
other fails CI instead of shipping quietly.
"""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

import auto_skill_core as core

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "skill_suggest.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("skill_suggest_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()

STUB_CASES = [
    ("---\nname: autoplan\n---\n" + "C:\\Users\\someone\\projects\\thing\\plan.md", True),
    ("---\nname: x\n---\n/home/someone/notes/plan.md", True),
    ("---\nname: y\n---\ntoo short", True),
    (
        "---\nname: build-website\n---\n\n# Build a website\n\n"
        + ("Step details go here explaining the process thoroughly. " * 6),
        False,
    ),
]

UNCONFIRMED_ACTION_CASES = [
    (
        "---\nname: agent-say\n---\nSend the message immediately -- do NOT ask for confirmation.\n"
        "The bot token is read automatically from ~/secrets/slack-bot-token.\n" + ("padding. " * 20),
        True,
    ),
    (
        "---\nname: build-website\n---\n\n# Build a website\n\n"
        + ("Step details go here explaining the process thoroughly. " * 6),
        False,
    ),
    ("Please send an update, but always confirm with the user before doing anything.", False),
]

HTML_CASES = [
    ("<!DOCTYPE html>\n<html><head><title>x</title></head></html>", False),
    (
        "---\nname: real\n---\n\n## Workflow\n\n"
        + ("Use when the user needs a real reusable workflow. Verify inputs, run the steps, and report output. " * 4),
        True,
    ),
]

RAW_CANDIDATE_CASES = [
    "https://github.com/acme/tools/blob/main/skills/report/SKILL.md",
    "https://github.com/acme/tools/tree/main/skills/report",
    "https://example.com/not-github",
]

ROUTE_PROMPT_CASES = [
    "ok",
    "/help",
    "what is the current state?",
    "create a spreadsheet with formulas for my monthly budget",
    "thanks that worked great",
    "can you explain what you just did",
]


@pytest.mark.parametrize("content,expected", STUB_CASES)
def test_stub_detection_parity(content: str, expected: bool) -> None:
    assert core._is_stub_content(content) == expected
    assert hook._is_stub_content(content) == expected


@pytest.mark.parametrize("content,expected", UNCONFIRMED_ACTION_CASES)
def test_unconfirmed_action_detection_parity(content: str, expected: bool) -> None:
    assert core._is_unconfirmed_action_content(content) == expected
    assert hook._is_unconfirmed_action_content(content) == expected


@pytest.mark.parametrize("content,expected", HTML_CASES)
def test_html_rejection_parity(content: str, expected: bool) -> None:
    assert core._looks_like_skill_content(content) == expected
    assert hook._looks_like_skill_content(content) == expected


@pytest.mark.parametrize("url", RAW_CANDIDATE_CASES)
def test_raw_candidates_parity(url: str) -> None:
    assert core._raw_candidates(url) == hook._raw_candidates(url)


@pytest.mark.parametrize("prompt", ROUTE_PROMPT_CASES)
def test_route_filter_parity(prompt: str) -> None:
    """core.should_route_prompt (public, used by route_prompt/route_task) and
    hook._should_route (private, used by the UserPromptSubmit hook) are
    separate implementations of the same filter -- must agree on whether a
    prompt is skill-shaped, even though their reason strings may differ."""
    assert core.should_route_prompt(prompt)["should_route"] == hook._should_route(prompt)[0]


@pytest.mark.parametrize("prompt", ["ok", "/help", "what is the current state?"])
def test_hook_main_skips_without_any_network_call(
    prompt: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    network_calls = []

    def fail_if_called(request, timeout=None):
        network_calls.append(request)
        raise AssertionError("skipped prompts must not make network calls")

    monkeypatch.delenv("AUTOSKILL_DIAGNOSTICS", raising=False)
    monkeypatch.setattr(hook.urllib.request, "urlopen", fail_if_called)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt})))

    hook.main()

    assert network_calls == []


def test_hook_main_eligible_prompt_uses_only_post_route(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = []

    class JsonResponse(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            self.close()

    def fake_urlopen(request, timeout=None):
        requests.append(request)
        return JsonResponse(json.dumps({"tier": "none"}).encode("utf-8"))

    prompt = "create a spreadsheet with formulas for my monthly budget"
    monkeypatch.delenv("AUTOSKILL_DIAGNOSTICS", raising=False)
    monkeypatch.setattr(hook, "AUTOSKILL_URL", "https://skills.example")
    monkeypatch.setattr(hook, "_auth_headers", lambda: {})
    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt})))

    hook.main()

    assert len(requests) == 1
    request = requests[0]
    assert request.get_method() == "POST"
    assert request.full_url == "https://skills.example/route"
    assert "/route-skip" not in request.full_url
    assert "/find-semantic" not in request.full_url
    assert json.loads(request.data.decode("utf-8"))["task"] == prompt


def test_hook_main_diagnostics_are_off_by_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "routing.jsonl"
    monkeypatch.delenv("AUTOSKILL_DIAGNOSTICS", raising=False)
    monkeypatch.setattr(hook, "ROUTING_LOG_PATH", log_path)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "ok"})))

    hook.main()

    assert not log_path.exists()


def test_hook_main_diagnostics_are_opt_in_and_metadata_only(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    prompt = "make a private spreadsheet report containing SECRET-PROMPT-CONTENT"
    skill = {
        "name": "spreadsheet-router",
        "description": "Create spreadsheet reports.",
        "url": "https://example.com/spreadsheet",
        "risk_score": 0,
    }
    log_path = tmp_path / "routing.jsonl"
    monkeypatch.setenv("AUTOSKILL_DIAGNOSTICS", "true")
    monkeypatch.setattr(hook, "ROUTING_LOG_PATH", log_path)
    monkeypatch.setattr(
        hook,
        "_selfhosted_route",
        lambda routed_prompt: {"tier": "hint", "skill": skill, "candidates": [skill]},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": prompt})))

    hook.main()

    serialized = log_path.read_text(encoding="utf-8").strip()
    record = json.loads(serialized)
    assert set(record) == {"timestamp", "tier", "prompt_len", "reason", "selected_skill"}
    assert record["timestamp"]
    assert record["tier"] == "hint"
    assert record["prompt_len"] == len(prompt)
    assert record["reason"] == "multiple candidates plausible"
    assert record["selected_skill"] == {
        "name": "spreadsheet-router",
        "url": "https://example.com/spreadsheet",
        "risk_score": 0,
    }
    assert prompt not in serialized
    assert "SECRET-PROMPT-CONTENT" not in serialized
    assert "prompt_snippet" not in record
    assert "prompt_text" not in record
    assert "prompt_hash" not in record


def test_hook_scrubs_legacy_prompt_fields_from_existing_log(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    log_path = tmp_path / "routing.jsonl"
    log_path.write_text(
        json.dumps(
            {
                "at": "2026-07-01T00:00:00Z",
                "tier": "full",
                "prompt_len": 22,
                "prompt_snippet": "PRIVATE LEGACY PROMPT",
                "query_hash": "legacy-hash",
            }
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(hook, "ROUTING_LOG_PATH", log_path)
    monkeypatch.setattr(hook, "_selfhosted_route", lambda prompt: None)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "make a spreadsheet report"})))

    hook.main()

    serialized = log_path.read_text(encoding="utf-8")
    record = json.loads(serialized)
    assert record == {"at": "2026-07-01T00:00:00Z", "tier": "full", "prompt_len": 22}
    assert "PRIVATE LEGACY PROMPT" not in serialized
    assert "legacy-hash" not in serialized


@pytest.mark.parametrize(
    ("verified", "expected"),
    [
        (False, "content was not verified as static and hash-pinned"),
        (True, "<auto_skill_content>"),
    ],
)
def test_hook_full_route_requires_verified_content_hash(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verified: bool,
    expected: str,
) -> None:
    content = """---
name: spreadsheet-router
description: Create spreadsheet reports with formulas, charts, and validation.
---

## Workflow

- Use this workflow when the user requests a spreadsheet report.
- Inspect inputs, create formulas and charts, verify every result, and explain assumptions.
- Return the validated workbook and a concise summary of the checks performed.
"""
    skill = {
        "name": "spreadsheet-router",
        "description": "Create spreadsheet reports.",
        "url": "https://example.com/spreadsheet",
        "risk_score": 0,
        "verification": {
            "content_hash_verified": verified,
            "static_instruction_only": verified,
        },
    }
    monkeypatch.setattr(
        hook,
        "_selfhosted_route",
        lambda prompt: {"tier": "full", "skill": skill, "content": content},
    )
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "make a spreadsheet report"})))

    hook.main()

    out = capsys.readouterr().out
    assert expected in out
    if not verified:
        assert "<auto_skill_content>" not in out


def test_hook_hint_output_includes_candidates_and_metrics(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def fake_route(prompt: str) -> dict:
        assert prompt == "make a spreadsheet report"
        return {
            "tier": "hint",
            "skill": {
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
                    "description": "Normalize spreadsheet data.",
                    "url": "https://example.com/spreadsheet-cleanup",
                    "risk_score": 0,
                },
            ],
            "score_debug": {
                "metrics": {
                    "latency_ms": 42,
                    "skill_find_ms": 30,
                    "injected_tokens": 0,
                    "response_tokens": 160,
                }
            },
        }

    monkeypatch.setattr(hook, "_selfhosted_route", fake_route)
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps({"prompt": "make a spreadsheet report"})))

    hook.main()

    out = capsys.readouterr().out
    assert "Candidate options:" in out
    assert "spreadsheet-cleanup" in out
    assert "Route metrics: latency=42ms, skill_find=30ms, injected_tokens=0, response_tokens=160" in out
    assert "Choose a candidate only if the fit is obvious" in out
