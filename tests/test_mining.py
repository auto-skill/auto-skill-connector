from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

import pytest

import auto_skill_mining as mining
from auto_skill_core import AutoSkillError

VALID_SKILL = (
    "---\nname: build-website\ndescription: Use when the user asks to build a static "
    "marketing website with a clear deploy step.\n---\n\n"
    "# Build a website\n\n"
    + ("Step details go here explaining the process thoroughly. " * 6)
)

NEAR_DUPLICATE_SKILL = (
    "---\nname: build-a-website\ndescription: Use when the user asks to build a static "
    "marketing site with a clear deploy step.\n---\n\n"
    "# Build a website variant\n\n"
    + ("Step details go here explaining the process thoroughly. " * 6)
)

UNRELATED_SKILL = (
    "---\nname: rust-error-handling\ndescription: Use when writing Rust error handling "
    "code that needs consistent Result propagation.\n---\n\n"
    "# Rust error handling\n\n"
    + ("You must prefer thiserror for library errors and anyhow for applications. " * 6)
)


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSKILL_MINED_SKILLS_PATH", str(tmp_path / "mined_skills"))


def test_save_rejects_invalid_content(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with pytest.raises(AutoSkillError):
        mining.save_mined_skill("not a skill at all", source_session_id="sess-1")


def test_save_stores_content_and_index_entry(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    entry = mining.save_mined_skill(VALID_SKILL, source_session_id="sess-1")

    assert entry["slug"] == "build-website"
    assert entry["source_session_id"] == "sess-1"
    assert entry["published"] is False

    dest = mining.get_mined_skills_dir() / "build-website.md"
    assert dest.read_text(encoding="utf-8") == VALID_SKILL

    listed = mining.list_mined_skills()
    assert len(listed) == 1
    assert listed[0]["slug"] == "build-website"


def test_save_rejects_exact_duplicate_without_force(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mining.save_mined_skill(VALID_SKILL, source_session_id="sess-1")
    with pytest.raises(AutoSkillError, match="identical"):
        mining.save_mined_skill(VALID_SKILL, source_session_id="sess-2")


def test_save_rejects_near_duplicate_without_force(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mining.save_mined_skill(VALID_SKILL, source_session_id="sess-1")
    with pytest.raises(AutoSkillError, match="near-duplicate"):
        mining.save_mined_skill(NEAR_DUPLICATE_SKILL, source_session_id="sess-2")


def test_save_force_allows_near_duplicate(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mining.save_mined_skill(VALID_SKILL, source_session_id="sess-1")
    entry = mining.save_mined_skill(NEAR_DUPLICATE_SKILL, source_session_id="sess-2", force=True)
    assert entry["slug"] == "build-a-website"
    assert len(mining.list_mined_skills()) == 2


def test_save_allows_unrelated_skill(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    mining.save_mined_skill(VALID_SKILL, source_session_id="sess-1")
    mining.save_mined_skill(UNRELATED_SKILL, source_session_id="sess-2")
    assert len(mining.list_mined_skills()) == 2


def test_remove_deletes_content_and_index_entry(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    entry = mining.save_mined_skill(VALID_SKILL, source_session_id="sess-1")
    dest = mining.get_mined_skills_dir() / f"{entry['slug']}.md"
    assert dest.exists()

    assert mining.remove_mined_skill(entry["slug"]) is True
    assert not dest.exists()
    assert mining.list_mined_skills() == []
    assert mining.remove_mined_skill(entry["slug"]) is False


def test_publish_delegates_to_submit_private_skill(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    entry = mining.save_mined_skill(VALID_SKILL, source_session_id="sess-1")

    async def fake_submit(name, description, content):
        assert name == entry["name"]
        assert content == VALID_SKILL
        return {"id": "priv_123", "name": name}

    monkeypatch.setattr(mining, "submit_private_skill", fake_submit)

    published = asyncio.run(mining.publish_mined_skill(entry["slug"]))
    assert published["published"] is True
    assert published["published_skill_id"] == "priv_123"

    reloaded = mining.get_mined_skill(entry["slug"])
    assert reloaded["published"] is True


def test_publish_unknown_slug_raises(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    with pytest.raises(AutoSkillError):
        asyncio.run(mining.publish_mined_skill("does-not-exist"))


def test_list_local_sessions_discovers_claude_and_codex(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    claude_dir = tmp_path / ".claude" / "projects" / "my-project"
    claude_dir.mkdir(parents=True)
    (claude_dir / "11111111-1111-1111-1111-111111111111.jsonl").write_text("{}", encoding="utf-8")

    codex_dir = tmp_path / ".codex" / "sessions" / "2026" / "07" / "15"
    codex_dir.mkdir(parents=True)
    (codex_dir / "rollout-2026-07-15T10-00-00-abc123.jsonl").write_text("{}", encoding="utf-8")

    sessions = mining.list_local_sessions("all")
    clients = {s["client"] for s in sessions}
    assert clients == {"claude", "codex"}

    claude_session = next(s for s in sessions if s["client"] == "claude")
    assert claude_session["project"] == "my-project"
    assert claude_session["session_id"] == "11111111-1111-1111-1111-111111111111"

    codex_session = next(s for s in sessions if s["client"] == "codex")
    assert codex_session["session_id"] == "2026-07-15T10-00-00-abc123"


def test_list_local_sessions_filters_by_client_and_recency(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    claude_dir = tmp_path / ".claude" / "projects" / "p"
    claude_dir.mkdir(parents=True)
    session_file = claude_dir / "session.jsonl"
    session_file.write_text("{}", encoding="utf-8")

    ten_days_ago = time.time() - 10 * 86400
    os.utime(session_file, (ten_days_ago, ten_days_ago))

    assert mining.list_local_sessions("codex") == []
    assert len(mining.list_local_sessions("claude")) == 1
    assert len(mining.list_local_sessions("all", since_days=1)) == 0
    assert len(mining.list_local_sessions("all", since_days=30)) == 1
