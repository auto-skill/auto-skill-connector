"""Session-scoped, metadata-only skill activation state.

The skills.sh CLI owns the actual package materialization. Auto-Skill only
keeps a small local manifest describing which immutable package snapshot was
selected for the current session. This lets an adapter reuse a selected skill
without reinstalling it or persisting prompts/source bodies.
"""

from __future__ import annotations

import json
import os
import stat
import time
from pathlib import Path
from typing import Any

_TRUTHY = {"1", "true", "yes", "on"}
_MAX_SESSIONS = 32
_MAX_ACTIVATIONS_PER_SESSION = 12
_SESSION_TTL_SECONDS = 24 * 60 * 60


def session_state_enabled() -> bool:
    return os.getenv("AUTOSKILL_SESSION_STATE", "1").strip().lower() in _TRUTHY


def get_session_state_path() -> Path:
    override = os.getenv("AUTOSKILL_SESSION_STATE_PATH")
    if override:
        return Path(override)
    return Path.home() / ".autoskill" / "session_skills.json"


def _load(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def _write(path: Path, payload: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        try:
            os.chmod(temporary, stat.S_IRUSR | stat.S_IWUSR)
        except OSError:
            pass
        temporary.replace(path)
    except OSError:
        # Session state is an optimization. A read-only profile must not
        # prevent routing or turn a safe hint into an error.
        pass


def _prune(data: dict[str, Any], now: float | None = None) -> dict[str, Any]:
    now = time.time() if now is None else now
    sessions = data.get("sessions") if isinstance(data.get("sessions"), dict) else {}
    fresh: dict[str, Any] = {}
    for session_id, raw in sessions.items():
        if not isinstance(raw, dict):
            continue
        updated = float(raw.get("updated_at") or 0)
        if updated and now - updated > _SESSION_TTL_SECONDS:
            continue
        activations = raw.get("activations")
        if not isinstance(activations, list):
            continue
        clean = [item for item in activations if isinstance(item, dict)][-_MAX_ACTIVATIONS_PER_SESSION:]
        if clean:
            fresh[str(session_id)[:160]] = {
                "updated_at": updated or now,
                "activations": clean,
            }
    if len(fresh) > _MAX_SESSIONS:
        ordered = sorted(fresh.items(), key=lambda item: float(item[1].get("updated_at") or 0), reverse=True)
        fresh = dict(ordered[:_MAX_SESSIONS])
    return {"version": 1, "sessions": fresh}


def record_session_activation(session_id: str, activation: dict[str, Any]) -> None:
    """Persist a bounded activation plan, never raw task text or skill bodies."""
    if not session_state_enabled() or not session_id or not isinstance(activation, dict):
        return
    source = str(activation.get("source") or "").strip()
    skill = str(activation.get("skill") or "").strip()
    if not source or not skill:
        return
    safe = {
        "mode": str(activation.get("mode") or "skills_sh_use")[:40],
        "scope": "session",
        "source": source[:500],
        "skill": skill[:200],
        "agent": str(activation.get("agent") or "codex")[:40],
        "snapshot_hash": str(activation.get("snapshot_hash") or "")[:128],
        "activated_at": time.time(),
    }
    path = get_session_state_path()
    data = _prune(_load(path))
    sessions = data.setdefault("sessions", {})
    key = str(session_id)[:160]
    record = sessions.setdefault(key, {"updated_at": 0, "activations": []})
    activations = record.setdefault("activations", [])
    # Replace the same immutable skill snapshot rather than accumulating
    # duplicate route decisions in a long-lived session.
    activations[:] = [
        item for item in activations
        if not (item.get("source") == safe["source"] and item.get("skill") == safe["skill"])
    ]
    activations.append(safe)
    record["updated_at"] = safe["activated_at"]
    _write(path, _prune(data))


def get_session_activations(session_id: str) -> list[dict[str, Any]]:
    if not session_id:
        return []
    data = _prune(_load(get_session_state_path()))
    record = data.get("sessions", {}).get(str(session_id)[:160], {})
    activations = (record.get("activations") or []) if isinstance(record, dict) else []
    return [dict(item) for item in activations if isinstance(item, dict)]


def clear_session_activations(session_id: str) -> None:
    if not session_id:
        return
    path = get_session_state_path()
    data = _prune(_load(path))
    data.get("sessions", {}).pop(str(session_id)[:160], None)
    _write(path, data)
