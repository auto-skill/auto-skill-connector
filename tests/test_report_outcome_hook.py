"""Tests for hooks/report_outcome.py, the Stop hook that reports session-level
Measurement Mode outcome metrics against the route_ids hooks/skill_suggest.py
journaled during the session (see _track_measurement_route there)."""
from __future__ import annotations

import importlib.util
import io
import json
import sys
from pathlib import Path

import pytest

_HOOK_PATH = Path(__file__).resolve().parent.parent / "hooks" / "report_outcome.py"


def _load_hook():
    spec = importlib.util.spec_from_file_location("report_outcome_hook", _HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


hook = _load_hook()


class _JsonResponse(io.BytesIO):
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()


def _write_transcript(path: Path, lines: list[dict]) -> None:
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")


def test_main_does_nothing_without_a_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    calls = []
    monkeypatch.setattr(hook, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(hook.urllib.request, "urlopen", lambda *a, **kw: calls.append(a) or _JsonResponse(b"{}"))
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"session_id": "sess-1", "transcript_path": str(tmp_path / "t.jsonl")}))
    )

    hook.main()

    assert calls == []


def test_main_reports_metrics_for_each_journaled_route_and_clears_journal(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    journal = tmp_path / "sess-1.jsonl"
    journal.write_text(
        json.dumps({"route_id": "route-a", "ts": 1.0}) + "\n" + json.dumps({"route_id": "route-b", "ts": 2.0}) + "\n",
        encoding="utf-8",
    )
    transcript = tmp_path / "transcript.jsonl"
    _write_transcript(
        transcript,
        [
            {"type": "user", "timestamp": "2026-07-31T10:00:00Z", "message": {"role": "user", "content": "hi"}},
            {
                "type": "assistant",
                "timestamp": "2026-07-31T10:00:05Z",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "tool_use", "name": "Bash", "input": {}}],
                    "usage": {"input_tokens": 100, "output_tokens": 50},
                },
            },
            {"type": "user", "timestamp": "2026-07-31T10:02:00Z", "message": {"role": "user", "content": "thanks"}, "toolUseResult": {}},
            {"type": "user", "timestamp": "2026-07-31T10:03:00Z", "message": {"role": "user", "content": "one more thing"}},
        ],
    )

    requests = []

    def fake_urlopen(request, timeout=None):
        requests.append(json.loads(request.data.decode("utf-8")))
        return _JsonResponse(b"{}")

    monkeypatch.setattr(hook, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(hook, "AUTOSKILL_URL", "https://skills.example")
    monkeypatch.setattr(hook, "_auth_headers", lambda: {})
    monkeypatch.setattr(hook.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        sys, "stdin", io.StringIO(json.dumps({"session_id": "sess-1", "transcript_path": str(transcript)}))
    )

    hook.main()

    assert len(requests) == 2
    route_ids = {r["route_id"] for r in requests}
    assert route_ids == {"route-a", "route-b"}
    for r in requests:
        assert r["turns"] == 2  # the toolUseResult-tagged "user" line must not count as a turn
        assert r["tool_calls"] == 1
        assert r["total_tokens"] == 150
        assert r["elapsed_seconds"] == 180
    assert not journal.exists()


def test_main_reports_no_metrics_key_when_transcript_missing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    journal = tmp_path / "sess-1.jsonl"
    journal.write_text(json.dumps({"route_id": "route-a", "ts": 1.0}) + "\n", encoding="utf-8")

    calls = []
    monkeypatch.setattr(hook, "SESSIONS_DIR", tmp_path)
    monkeypatch.setattr(hook.urllib.request, "urlopen", lambda *a, **kw: calls.append(a) or _JsonResponse(b"{}"))
    monkeypatch.setattr(
        sys,
        "stdin",
        io.StringIO(json.dumps({"session_id": "sess-1", "transcript_path": str(tmp_path / "missing.jsonl")})),
    )

    hook.main()

    # No usable metrics -- nothing worth reporting, but the journal is still cleared.
    assert calls == []
    assert not journal.exists()


def test_session_outcome_metrics_handles_malformed_lines(tmp_path: Path) -> None:
    transcript = tmp_path / "t.jsonl"
    transcript.write_text("not json\n" + json.dumps({"type": "user", "timestamp": "2026-07-31T10:00:00Z"}) + "\n", encoding="utf-8")

    metrics = hook._session_outcome_metrics(str(transcript))

    assert metrics.get("turns") == 1
    assert "elapsed_seconds" not in metrics  # only one timestamp -- nothing to subtract
