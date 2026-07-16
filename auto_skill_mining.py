"""Local session-transcript discovery and mined-skill storage.

Session mining mirrors skills/skill-creator/SKILL.md's pattern: the user's
own agent does the semantic extraction (reading a transcript, drafting a
SKILL.md), while this module provides the deterministic scaffolding around
it -- discovering candidate transcripts, validating drafts, checking for
near-duplicates against skills already mined on this machine, and storing
the result locally and privately until the user explicitly publishes it.

Nothing here reads message content out of a transcript; only the agent
invoking the session-miner skill does that, with its own filesystem access.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from auto_skill_core import AutoSkillError, submit_private_skill, validate_skill_content

_NEAR_DUPLICATE_OVERLAP = 0.80


def get_mined_skills_dir() -> Path:
    override = os.getenv("AUTOSKILL_MINED_SKILLS_PATH")
    return Path(override) if override else Path.home() / ".autoskill" / "mined_skills"


def _get_index_path() -> Path:
    return get_mined_skills_dir() / "index.json"


def _load_index() -> dict[str, Any]:
    path = _get_index_path()
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"skills": {}}
    if not isinstance(value, dict) or not isinstance(value.get("skills"), dict):
        return {"skills": {}}
    return value


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(text, encoding="utf-8")
    try:
        os.chmod(temp, stat.S_IRUSR | stat.S_IWUSR)  # best effort on Windows
    except OSError:
        pass
    temp.replace(path)


def _save_index(index: dict[str, Any]) -> None:
    _atomic_write_text(_get_index_path(), json.dumps(index, indent=2) + "\n")


# ---------------------------------------------------------------------------
# Session discovery (deterministic filesystem scan; never reads message
# content -- the agent invoking the session-miner skill does that itself).
# ---------------------------------------------------------------------------

def _claude_sessions() -> list[dict[str, Any]]:
    # Claude Code transcripts: ~/.claude/projects/<project-slug>/<session-uuid>.jsonl
    root = Path.home() / ".claude" / "projects"
    if not root.is_dir():
        return []
    sessions = []
    for path in root.glob("*/*.jsonl"):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        sessions.append(
            {
                "session_id": path.stem,
                "client": "claude",
                "project": path.parent.name,
                "path": str(path),
                "mtime": stat_result.st_mtime,
                "size_bytes": stat_result.st_size,
            }
        )
    return sessions


_CODEX_ROLLOUT_RE = re.compile(r"^rollout-(.+)\.jsonl$")


def _codex_sessions() -> list[dict[str, Any]]:
    # Codex CLI transcripts: ~/.codex/sessions/YYYY/MM/DD/rollout-<ts>-<id>.jsonl
    root = Path.home() / ".codex" / "sessions"
    if not root.is_dir():
        return []
    sessions = []
    for path in root.glob("*/*/*/rollout-*.jsonl"):
        try:
            stat_result = path.stat()
        except OSError:
            continue
        match = _CODEX_ROLLOUT_RE.match(path.name)
        session_id = match.group(1) if match else path.stem
        sessions.append(
            {
                "session_id": session_id,
                "client": "codex",
                "project": "",
                "path": str(path),
                "mtime": stat_result.st_mtime,
                "size_bytes": stat_result.st_size,
            }
        )
    return sessions


def list_local_sessions(client: str = "all", since_days: float | None = None) -> list[dict[str, Any]]:
    """Deterministic discovery of local session transcript files. Returns
    metadata only (id, client, project, path, mtime, size) -- never opens or
    parses file content; that happens only inside the agent-driven
    session-miner skill, which has its own filesystem access."""
    client = (client or "all").strip().lower()
    sessions: list[dict[str, Any]] = []
    if client in {"all", "claude"}:
        sessions.extend(_claude_sessions())
    if client in {"all", "codex"}:
        sessions.extend(_codex_sessions())

    if since_days is not None:
        cutoff = time.time() - since_days * 86400
        sessions = [s for s in sessions if s["mtime"] >= cutoff]

    sessions.sort(key=lambda s: s["mtime"], reverse=True)
    for session in sessions:
        session["mtime_iso"] = datetime.fromtimestamp(session["mtime"], tz=timezone.utc).isoformat()
    return sessions


# ---------------------------------------------------------------------------
# Local mined-skill store: private by default, one explicit publish step.
# ---------------------------------------------------------------------------

def _content_hash(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").strip().lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _tokens(text: str) -> set[str]:
    return {token for token in re.findall(r"[a-z0-9]+", (text or "").lower()) if len(token) > 2}


def _near_duplicate(a_name: str, a_description: str, b_name: str, b_description: str) -> bool:
    a_tokens = _tokens(a_name) | _tokens(a_description)
    b_tokens = _tokens(b_name) | _tokens(b_description)
    if not a_tokens or not b_tokens:
        return False
    overlap = len(a_tokens & b_tokens) / max(1, len(a_tokens | b_tokens))
    return overlap >= _NEAR_DUPLICATE_OVERLAP


def save_mined_skill(content: str, source_session_id: str = "", force: bool = False) -> dict[str, Any]:
    """Validate, dedup against skills already mined on this machine, and
    store a candidate skill privately. Raises AutoSkillError on validation
    failure or an un-forced duplicate.

    Catalog-level dedup happens separately at publish time, through the
    same ingest pipeline every other private skill submission goes
    through -- this local check only guards against re-mining the same
    session insight twice on one machine.
    """
    result = validate_skill_content(content)
    if not result["ok"]:
        raise AutoSkillError("draft failed validation: " + "; ".join(result["errors"]))

    name = result["name"]
    description = result["description"]
    slug = result["slug"]
    chash = _content_hash(content)

    index = _load_index()
    existing = index["skills"]

    if not force:
        for existing_slug, entry in existing.items():
            if entry.get("content_hash") == chash:
                raise AutoSkillError(
                    f"identical to already-mined skill {existing_slug!r}; use --force to save anyway"
                )
            if existing_slug != slug and _near_duplicate(
                name, description, entry.get("name", ""), entry.get("description", "")
            ):
                raise AutoSkillError(
                    f"looks like a near-duplicate of already-mined skill {existing_slug!r}; "
                    "use --force to save anyway"
                )

    dest = get_mined_skills_dir() / f"{slug}.md"
    _atomic_write_text(dest, content)

    entry = {
        "slug": slug,
        "name": name,
        "description": description,
        "source_session_id": source_session_id,
        "content_hash": chash,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "published": False,
        "published_skill_id": None,
        "warnings": result["warnings"],
    }
    existing[slug] = entry
    _save_index(index)
    return entry


def list_mined_skills() -> list[dict[str, Any]]:
    index = _load_index()
    return sorted(index["skills"].values(), key=lambda e: e.get("created_at", ""), reverse=True)


def get_mined_skill(slug: str) -> dict[str, Any] | None:
    return _load_index()["skills"].get(slug)


def remove_mined_skill(slug: str) -> bool:
    index = _load_index()
    entry = index["skills"].pop(slug, None)
    if entry is None:
        return False
    _save_index(index)
    try:
        (get_mined_skills_dir() / f"{slug}.md").unlink()
    except FileNotFoundError:
        pass
    return True


async def publish_mined_skill(slug: str) -> dict[str, Any]:
    """Explicit opt-in step: submit a locally mined skill to the existing
    account-gated private-skill catalog via submit_private_skill (the same
    call skill-creator already tells users to run as `my-skills add`).
    Raises NotLoggedInError (via submit_private_skill) if the user hasn't
    logged in, AutoSkillError if the slug is unknown or re-validation
    fails."""
    index = _load_index()
    entry = index["skills"].get(slug)
    if entry is None:
        raise AutoSkillError(f"no mined skill named {slug!r}; run `auto-skill mine list`")

    dest = get_mined_skills_dir() / f"{slug}.md"
    content = dest.read_text(encoding="utf-8")
    result = validate_skill_content(content)
    if not result["ok"]:
        raise AutoSkillError("mined skill no longer validates: " + "; ".join(result["errors"]))

    published = await submit_private_skill(entry["name"], entry["description"], content)
    entry["published"] = True
    entry["published_skill_id"] = published.get("id")
    entry["published_at"] = datetime.now(timezone.utc).isoformat()
    index["skills"][slug] = entry
    _save_index(index)
    return entry
