"""Privacy-safe anonymous installation identity for local connectors.

The identifier is a random UUID kept locally, never derived from a prompt,
machine fingerprint, IP address, or account credential. It is only created
when the caller explicitly opts into anonymous analytics with
``AUTOSKILL_ANONYMOUS_ANALYTICS=1``.
"""

from __future__ import annotations

import json
import os
import stat
import uuid
from pathlib import Path
from typing import Any

_TRUTHY_VALUES = {"1", "true", "yes", "on"}


def anonymous_analytics_enabled() -> bool:
    return os.getenv("AUTOSKILL_ANONYMOUS_ANALYTICS", "").strip().lower() in _TRUTHY_VALUES


def get_installation_id_path() -> Path:
    override = os.getenv("AUTOSKILL_INSTALLATION_ID_PATH")
    if override:
        return Path(override)
    return Path.home() / ".autoskill" / "installation.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _valid_uuid(value: object) -> str | None:
    try:
        return str(uuid.UUID(str(value)))
    except (ValueError, AttributeError, TypeError):
        return None


def get_anonymous_installation_id() -> str | None:
    """Return/create the local anonymous ID, or ``None`` when opted out."""
    if not anonymous_analytics_enabled():
        return None
    path = get_installation_id_path()
    existing = _valid_uuid(_load(path).get("anonymous_id")) if path.exists() else None
    if existing:
        return existing
    value = str(uuid.uuid4())
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temp = path.with_suffix(path.suffix + ".tmp")
        temp.write_text(json.dumps({"anonymous_id": value}) + "\n", encoding="utf-8")
        try:
            os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)  # best effort on Windows
        except OSError:
            pass
        temp.replace(path)
    except OSError:
        # A read-only profile must never block routing; the caller simply
        # gets a fresh ID next time instead of a persistent one.
        pass
    return value


def clear_anonymous_installation_id() -> None:
    path = get_installation_id_path()
    try:
        path.unlink()
    except FileNotFoundError:
        pass
