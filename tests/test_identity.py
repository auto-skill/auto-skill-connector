from __future__ import annotations

import json
import uuid

import auto_skill_identity as identity


def test_anonymous_identity_is_opt_in_and_stable(tmp_path, monkeypatch) -> None:
    path = tmp_path / "installation.json"
    monkeypatch.setenv("AUTOSKILL_INSTALLATION_ID_PATH", str(path))
    monkeypatch.delenv("AUTOSKILL_ANONYMOUS_ANALYTICS", raising=False)

    assert identity.get_anonymous_installation_id() is None
    assert not path.exists()

    monkeypatch.setenv("AUTOSKILL_ANONYMOUS_ANALYTICS", "true")
    first = identity.get_anonymous_installation_id()
    second = identity.get_anonymous_installation_id()

    assert first == second
    assert uuid.UUID(first)
    assert json.loads(path.read_text(encoding="utf-8")) == {"anonymous_id": first}


def test_anonymous_identity_can_be_reset(tmp_path, monkeypatch) -> None:
    path = tmp_path / "installation.json"
    monkeypatch.setenv("AUTOSKILL_INSTALLATION_ID_PATH", str(path))
    monkeypatch.setenv("AUTOSKILL_ANONYMOUS_ANALYTICS", "1")

    first = identity.get_anonymous_installation_id()
    identity.clear_anonymous_installation_id()
    second = identity.get_anonymous_installation_id()

    assert first != second

