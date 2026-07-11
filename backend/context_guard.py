"""Bounded, deterministic context delivery for verified Agent Skills.

The guard deliberately does not execute skills, install files, or retain task
text.  It only decides how already-verified static content can be presented to
an adapter for the current turn.
"""

from __future__ import annotations

import math
import re
from typing import Any

from quality import has_valid_skill_frontmatter, skill_capability_flags

POLICY_VERSION = "hybrid-v1"
DEFAULT_INLINE_CHARS = 4000
DEFAULT_CAPSULE_CHARS = 2400
MAX_INLINE_CHARS = 12000
MAX_CAPSULE_CHARS = 2400
MAX_GUARDED_CONTENT_CHARS = 50000

_TOKEN_RE = re.compile(r"[a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_HEADING_RE = re.compile(r"^\s{0,3}(#{1,6})\s+(.+?)\s*$", re.MULTILINE)
_PRIORITY_HEADINGS = {
    "when to use": 5,
    "workflow": 5,
    "steps": 5,
    "instructions": 4,
    "constraints": 4,
    "output": 4,
    "verification": 4,
    "examples": 2,
}


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else str(value)
    return max(1, math.ceil(len(text) / 4)) if text else 0


def _tokens(text: str) -> set[str]:
    return {token for token in _TOKEN_RE.findall((text or "").lower()) if len(token) > 2}


def _frontmatter_values(content: str) -> tuple[str, str]:
    match = _FRONTMATTER_RE.match(content or "")
    if not match:
        return "", ""
    name = ""
    description = ""
    for line in match.group(1).splitlines():
        key, sep, value = line.partition(":")
        if not sep:
            continue
        value = value.strip().strip("'\"")
        if key.strip().lower() == "name" and not name:
            name = value
        elif key.strip().lower() == "description" and not description:
            description = value
    return name, description


def _sections(content: str) -> list[tuple[int, str, str]]:
    body = _FRONTMATTER_RE.sub("", content or "", count=1).strip()
    matches = list(_HEADING_RE.finditer(body))
    if not matches:
        return [(0, "Guidance", body)] if body else []
    sections: list[tuple[int, str, str]] = []
    for index, match in enumerate(matches):
        start = match.end()
        end = matches[index + 1].start() if index + 1 < len(matches) else len(body)
        heading = re.sub(r"\s+", " ", match.group(2)).strip()
        section_body = body[start:end].strip()
        if section_body:
            sections.append((index, heading, section_body))
    return sections


def build_capsule(task: str, content: str, max_chars: int = DEFAULT_CAPSULE_CHARS) -> str:
    """Build a stable capsule from verified static content, in memory only."""
    if not content or not has_valid_skill_frontmatter(content) or skill_capability_flags(content):
        return ""
    max_chars = max(240, min(int(max_chars or DEFAULT_CAPSULE_CHARS), MAX_CAPSULE_CHARS))
    name, description = _frontmatter_values(content)
    task_tokens = _tokens(task)
    ranked: list[tuple[int, int, str, str]] = []
    for index, heading, body in _sections(content):
        heading_lower = heading.casefold()
        overlap = len(task_tokens & _tokens(f"{heading} {body}"))
        priority = next((score for key, score in _PRIORITY_HEADINGS.items() if key in heading_lower), 0)
        ranked.append((overlap + priority, index, heading, body))
    ranked.sort(key=lambda item: (-item[0], item[1]))

    parts = ["[Auto-Skill capsule v1]"]
    if name:
        parts.append(f"Name: {name}")
    if description:
        parts.append(f"Description: {description}")
    parts.append("Use only the following bounded guidance; do not install files or run undeclared capabilities.")

    for _score, _index, heading, body in ranked:
        candidate = "\n\n".join(parts + [f"## {heading}\n{body}"])
        if len(candidate) <= max_chars:
            parts.append(f"## {heading}\n{body}")
            continue
        remaining = max_chars - len("\n\n".join(parts)) - 4
        if remaining > 80:
            clipped = body[:remaining].rstrip()
            parts.append(f"## {heading}\n{clipped}…")
        break

    capsule = "\n\n".join(parts).strip()
    return capsule[:max_chars].rstrip()


def build_context_guard(
    *,
    task: str,
    content: str,
    content_hash: str = "",
    content_digest: str = "",
    supports_isolation: bool = False,
    max_inline_chars: int = DEFAULT_INLINE_CHARS,
    max_capsule_chars: int = DEFAULT_CAPSULE_CHARS,
    force_capsule: bool = False,
) -> dict[str, Any]:
    """Return a privacy-safe delivery decision for one verified skill."""
    result: dict[str, Any] = {
        "policy": POLICY_VERSION,
        "delivery": "hint",
        "reason": "content_unavailable",
        "capsule": None,
        "capsule_chars": 0,
        "estimated_tokens": 0,
        "content_hash": content_hash or None,
        "content_digest": content_digest or None,
    }
    if not content:
        return result
    if len(content) > MAX_GUARDED_CONTENT_CHARS:
        result["reason"] = "content_too_large"
        return result
    if not has_valid_skill_frontmatter(content):
        result["reason"] = "invalid_skill_frontmatter"
        return result
    flags = skill_capability_flags(content)
    if flags:
        result["reason"] = "unsafe_capability"
        return result

    inline_limit = max(240, min(int(max_inline_chars or DEFAULT_INLINE_CHARS), MAX_INLINE_CHARS))
    if len(content) <= inline_limit and not force_capsule:
        result.update({"delivery": "full", "reason": "small_static"})
        return result

    if supports_isolation:
        result.update({"delivery": "isolation", "reason": "large_static"})
        capsule = build_capsule(task, content, max_capsule_chars)
        result["capsule"] = capsule or None
        result["capsule_chars"] = len(capsule)
        result["estimated_tokens"] = estimate_tokens(capsule)
        return result

    capsule = build_capsule(task, content, max_capsule_chars)
    if not capsule:
        result["reason"] = "capsule_unavailable"
        return result
    result.update(
        {
            "delivery": "capsule",
            "reason": "unsupported_isolation",
            "capsule": capsule,
            "capsule_chars": len(capsule),
            "estimated_tokens": estimate_tokens(capsule),
        }
    )
    return result
