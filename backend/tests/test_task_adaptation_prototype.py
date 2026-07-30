import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from backend.bench import task_adaptation_prototype as adapter


def test_truncate_tokens_enforces_measured_budget() -> None:
    source = "alpha beta gamma delta " * 200

    truncated = adapter._truncate_tokens(source, 37)

    assert adapter._token_count(truncated) <= 37
    assert truncated


def test_resize_uses_recorded_deterministic_fallback(monkeypatch, tmp_path: Path) -> None:
    overlong = {"usable": True, "capsule": "step one then validate " * 200, "rationale": "test"}

    def ignore_repair(*_args, **_kwargs):
        return dict(overlong), {"mock_repair": True}

    monkeypatch.setattr(adapter, "_run_codex", ignore_repair)

    value, measured, metadata = adapter._resize_to_budget(
        tmp_path / "codex",
        dict(overlong),
        80,
        "planner",
        tmp_path,
    )

    assert measured <= 80
    assert adapter._token_count(value["capsule"]) == measured
    assert len(metadata) == 4
    assert metadata[-1] == {
        "deterministic_truncation": True,
        "target_tokens": 80,
        "measured_tokens": measured,
    }


def test_codex_cache_reuse_requires_matching_prompt_and_schema(monkeypatch, tmp_path: Path) -> None:
    schema = {"type": "object"}
    schema_text = json.dumps(schema, indent=2) + "\n"
    message_path = tmp_path / "case.message.json"
    request_path = tmp_path / "case.request.json"
    message_path.write_text('{"value":"stale"}\n', encoding="utf-8")
    request_path.write_text(
        json.dumps(
            {
                "model": adapter.MODEL,
                "reasoning_effort": adapter.REASONING_EFFORT,
                "prompt_sha256": hashlib.sha256(b"old prompt").hexdigest(),
                "schema_sha256": hashlib.sha256(schema_text.encode()).hexdigest(),
            }
        ),
        encoding="utf-8",
    )

    def fake_run(command, **_kwargs):
        output = Path(command[command.index("--output-last-message") + 1])
        output.write_text('{"value":"fresh"}\n', encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(adapter.subprocess, "run", fake_run)

    result, metadata = adapter._run_codex(
        tmp_path / "codex",
        "new prompt",
        schema,
        tmp_path,
        "case",
    )

    assert result == {"value": "fresh"}
    assert metadata.get("reused_completed_message") is not True
    saved = json.loads(request_path.read_text(encoding="utf-8"))
    assert saved["prompt_sha256"] == hashlib.sha256(b"new prompt").hexdigest()
