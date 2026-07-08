from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

import pytest

import auto_skill_auth as auth_store
import auto_skill_cli as cli
import auto_skill_core as core


class FakeResponse:
    def __init__(self, status_code: int, payload: object | None = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class RecordingClient:
    """Captures every call made through it so tests can assert on headers/body."""

    def __init__(self, response: FakeResponse) -> None:
        self.response = response
        self.calls: list[tuple[str, str, dict]] = []

    async def get(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("GET", url, kwargs))
        return self.response

    async def post(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("POST", url, kwargs))
        return self.response

    async def delete(self, url: str, **kwargs: object) -> FakeResponse:
        self.calls.append(("DELETE", url, kwargs))
        return self.response


# --- auto_skill_auth.py: local credentials file -----------------------------


def test_credentials_round_trip(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    assert auth_store.load_credentials() == {}
    assert auth_store.auth_headers() == {}

    auth_store.save_credentials({"token": "tok-123"})
    assert auth_store.get_token() == "tok-123"
    assert auth_store.auth_headers() == {"Authorization": "Bearer tok-123"}

    auth_store.clear_credentials()
    assert auth_store.load_credentials() == {}
    assert auth_store.auth_headers() == {}


# --- auto_skill_core.py: account API client functions -----------------------


def test_account_functions_require_login(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    client = RecordingClient(FakeResponse(200, {}))

    with pytest.raises(core.NotLoggedInError):
        asyncio.run(core.list_favorites(client=client))
    with pytest.raises(core.NotLoggedInError):
        asyncio.run(core.add_favorite("s1", client=client))
    with pytest.raises(core.NotLoggedInError):
        asyncio.run(core.list_private_skills(client=client))
    assert client.calls == []  # local check happens before any network call


def test_favorites_send_bearer_token_and_parse_response(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    auth_store.save_credentials({"token": "tok-abc"})

    client = RecordingClient(FakeResponse(200, {"favorites": [{"id": "s1", "name": "Demo"}]}))
    favorites = asyncio.run(core.list_favorites(client=client))
    assert favorites == [{"id": "s1", "name": "Demo"}]
    method, url, kwargs = client.calls[0]
    assert method == "GET" and url.endswith("/favorites")
    assert kwargs["headers"] == {"Authorization": "Bearer tok-abc"}

    client = RecordingClient(FakeResponse(200, {"ok": True}))
    asyncio.run(core.add_favorite("s1", client=client))
    method, url, kwargs = client.calls[0]
    assert method == "POST" and url.endswith("/favorites")
    assert kwargs["json"] == {"skill_id": "s1"}
    assert kwargs["headers"] == {"Authorization": "Bearer tok-abc"}


def test_expired_token_raises_not_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    auth_store.save_credentials({"token": "stale-token"})
    client = RecordingClient(FakeResponse(401, {"detail": "missing or invalid bearer token"}))
    with pytest.raises(core.NotLoggedInError):
        asyncio.run(core.list_favorites(client=client))


def test_report_install_is_silent_when_logged_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    client = RecordingClient(FakeResponse(200, {}))
    asyncio.run(core.report_install("s1", "https://example.com/s1", "claude", client=client))
    assert client.calls == []  # nothing sent, and no exception raised


def test_report_install_posts_when_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    auth_store.save_credentials({"token": "tok-abc"})
    client = RecordingClient(FakeResponse(200, {}))
    asyncio.run(core.report_install("s1", "https://example.com/s1", "claude", client=client))
    method, url, kwargs = client.calls[0]
    assert method == "POST" and url.endswith("/installs")
    assert kwargs["json"] == {"skill_id": "s1", "skill_url": "https://example.com/s1", "target": "claude"}


def test_whoami_returns_none_when_logged_out(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    client = RecordingClient(FakeResponse(200, {"email": "a@example.com"}))
    assert asyncio.run(core.whoami(client=client)) is None
    assert client.calls == []


def test_whoami_returns_profile_when_logged_in(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    auth_store.save_credentials({"token": "tok-abc"})
    client = RecordingClient(FakeResponse(200, {"email": "a@example.com", "name": "A"}))
    profile = asyncio.run(core.whoami(client=client))
    assert profile == {"email": "a@example.com", "name": "A"}


# --- auto_skill_cli.py: account subcommands ---------------------------------


def test_whoami_command_reports_logged_out(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_whoami(client: object | None = None) -> None:
        return None

    monkeypatch.setattr(cli, "whoami", fake_whoami)
    result = cli.main(["whoami"])
    assert result == 1
    assert "not logged in" in capsys.readouterr().out


def test_favorite_command_reports_not_logged_in(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_add_favorite(skill_id: str, client: object | None = None) -> None:
        raise core.NotLoggedInError("Not logged in. Run `auto-skill login` first.")

    monkeypatch.setattr(cli, "add_favorite", fake_add_favorite)
    result = cli.main(["favorite", "s1"])
    assert result == 1
    assert "Not logged in" in capsys.readouterr().err


def test_favorites_command_lists_results(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def fake_list_favorites(client: object | None = None) -> list[dict]:
        return [{"name": "Demo Skill", "url": "https://example.com/demo"}]

    monkeypatch.setattr(cli, "list_favorites", fake_list_favorites)
    result = cli.main(["favorites"])
    out = capsys.readouterr().out
    assert result == 0
    assert "Demo Skill" in out
    assert "https://example.com/demo" in out


def test_my_skills_add_reads_inline_content(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    captured = {}

    async def fake_submit(name: str, description: str, content: str, client: object | None = None) -> dict:
        captured["name"] = name
        captured["description"] = description
        captured["content"] = content
        return {"id": "ps1", "name": name}

    monkeypatch.setattr(cli, "submit_private_skill", fake_submit)
    result = cli.main(["my-skills", "add", "deploy-to-fly", "FLY", "DEPLOY", "STEPS", "--description", "deploy to fly.io"])
    assert result == 0
    assert captured == {"name": "deploy-to-fly", "description": "deploy to fly.io", "content": "FLY DEPLOY STEPS"}
    assert "added private skill: deploy-to-fly" in capsys.readouterr().out


def test_my_skills_add_reads_content_from_file(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    skill_file = tmp_path / "SKILL.md"
    skill_file.write_text("file body", encoding="utf-8")
    captured = {}

    async def fake_submit(name: str, description: str, content: str, client: object | None = None) -> dict:
        captured["content"] = content
        return {"id": "ps1", "name": name}

    monkeypatch.setattr(cli, "submit_private_skill", fake_submit)
    result = cli.main(["my-skills", "add", "deploy-to-fly", str(skill_file)])
    assert result == 0
    assert captured["content"] == "file body"


def test_logout_command_clears_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setenv("AUTOSKILL_CREDENTIALS_PATH", str(tmp_path / "credentials.json"))
    auth_store.save_credentials({"token": "tok-abc"})

    async def fake_logout_backend(client: object | None = None) -> bool:
        return True

    monkeypatch.setattr(cli, "logout_backend", fake_logout_backend)
    result = cli.main(["logout"])
    assert result == 0
    assert "logged out" in capsys.readouterr().out
    assert auth_store.load_credentials() == {}


def test_login_callback_handler_captures_token() -> None:
    import threading
    import time
    import urllib.request
    from http.server import HTTPServer

    server = HTTPServer(("127.0.0.1", 0), cli._LoginCallbackHandler)
    server.received_token = None
    server.timeout = 5
    port = server.server_address[1]

    thread = threading.Thread(target=server.handle_request, daemon=True)
    thread.start()
    time.sleep(0.2)

    resp = urllib.request.urlopen(f"http://127.0.0.1:{port}/callback?token=abc123", timeout=5)
    assert resp.status == 200
    thread.join(timeout=5)
    assert server.received_token == "abc123"
    server.server_close()
