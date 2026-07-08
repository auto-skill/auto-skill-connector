"""Local CLI credentials for auto-skill accounts. Stores an opaque bearer
token issued by the backend after a Google/GitHub OAuth login (see
`auto-skill login`) -- nothing else lives here, and nothing is stored beyond
this one file.
"""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path
from typing import Any


def get_credentials_path() -> Path:
    override = os.getenv("AUTOSKILL_CREDENTIALS_PATH")
    if override:
        return Path(override)
    return Path.home() / ".autoskill" / "credentials.json"


def load_credentials() -> dict[str, Any]:
    path = get_credentials_path()
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_credentials(data: dict[str, Any]) -> None:
    path = get_credentials_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    try:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)  # 0600: owner read/write only
    except OSError:
        pass  # best-effort on platforms without POSIX permission bits (e.g. some Windows setups)


def clear_credentials() -> None:
    path = get_credentials_path()
    if path.exists():
        path.unlink()


def get_token() -> str | None:
    return load_credentials().get("token")


def auth_headers() -> dict[str, str]:
    token = get_token()
    return {"Authorization": f"Bearer {token}"} if token else {}
