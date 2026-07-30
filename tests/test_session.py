from __future__ import annotations

import json

import auto_skill_session as session
import auto_skill_core as core


def test_session_manifest_is_bounded_metadata_only(tmp_path, monkeypatch) -> None:
    path = tmp_path / "session.json"
    monkeypatch.setenv("AUTOSKILL_SESSION_STATE_PATH", str(path))
    session.record_session_activation(
        "session-1",
        {
            "mode": "skills_sh_use",
            "source": "https://github.com/acme/skills",
            "skill": "Reporting",
            "agent": "codex",
            "snapshot_hash": "abc123",
            "prompt": "must never be persisted",
        },
    )
    assert session.get_session_activations("session-1")[0]["skill"] == "Reporting"
    raw = json.loads(path.read_text(encoding="utf-8"))
    serialized = json.dumps(raw)
    assert "must never be persisted" not in serialized
    assert "prompt" not in serialized
    assert raw["version"] == 1
    assert raw["sessions"]["session-1"]["activations"][0]["command"] == [
        "npx", "skills", "use", "https://github.com/acme/skills",
        "--skill", "Reporting", "--agent", "codex",
    ]


def test_session_manifest_replaces_duplicate_snapshot(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOSKILL_SESSION_STATE_PATH", str(tmp_path / "session.json"))
    activation = {"source": "https://github.com/acme/skills", "skill": "Reporting"}
    session.record_session_activation("session-1", activation)
    session.record_session_activation("session-1", activation)
    assert len(session.get_session_activations("session-1")) == 1
    session.clear_session_activations("session-1")
    assert session.get_session_activations("session-1") == []


def test_skills_use_command_is_bounded_and_not_executed() -> None:
    assert session.skills_use_command(
        {"source": "https://github.com/acme/skills", "skill": "Reporting", "agent": "codex"}
    ) == [
        "npx", "skills", "use", "https://github.com/acme/skills",
        "--skill", "Reporting", "--agent", "codex",
    ]
    assert session.skills_use_command({"source": "https://github.com/acme/skills", "skill": "a; rm -rf /"}) is None


def test_live_public_skill_contains_session_use_plan() -> None:
    public = core._public_backend_skill(
        {
            "id": "acme/skills/reporting",
            "name": "Reporting",
            "description": "Build spreadsheet reports.",
            "source": "skills_sh",
            "url": "https://skills.sh/acme/skills/reporting",
            "install_url": "https://github.com/acme/skills",
            "skills_sh_id": "acme/skills/reporting",
            "source_snapshot_hash": "snapshot-123",
            "retrieval_backend": "skills_sh",
            "quality_status": "active",
            "risk_score": 0,
            "similarity": None,
        },
        "create a spreadsheet report",
        "hint",
    )
    assert public["session_activation"]["scope"] == "session"
    assert public["session_activation"]["source"] == "https://github.com/acme/skills"
    assert public["session_activation"]["snapshot_hash"] == "snapshot-123"


def test_route_records_live_activation_for_explicit_session(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AUTOSKILL_SESSION_STATE_PATH", str(tmp_path / "session.json"))

    class Response:
        status_code = 200

        def json(self):
            return {
                "tier": "hint",
                "skill": {
                    "name": "Reporting",
                    "description": "Build spreadsheet reports.",
                    "source": "skills_sh",
                    "retrieval_backend": "skills_sh",
                    "url": "https://skills.sh/acme/skills/reporting",
                    "install_url": "https://github.com/acme/skills",
                    "source_snapshot_hash": "snapshot-123",
                },
                "candidates": [],
                "score_debug": {},
                "route_id": "route-1",
            }

    class Client:
        async def post(self, url, **kwargs):
            assert kwargs["json"]["session_id"] == "session-42"
            return Response()

    result = __import__("asyncio").run(
        core.route_task_payload("create a spreadsheet report", client=Client(), session_id="session-42")
    )
    assert result["selected_skill"]["session_activation"]["scope"] == "session"
    assert session.get_session_activations("session-42")[0]["skill"] == "Reporting"
