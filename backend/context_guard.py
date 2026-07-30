"""Bounded context delivery for verified, distilled Agent Skill capsules."""

from __future__ import annotations

import math
from typing import Any

from capsule_compiler import CAPSULE_VERSION, compile_capsule
from quality import has_valid_skill_frontmatter


POLICY_VERSION = CAPSULE_VERSION
DEFAULT_INLINE_CHARS = 4000  # retained for API compatibility; raw public content is never inline
DEFAULT_CAPSULE_CHARS = 2400
MAX_INLINE_CHARS = 12000
MAX_CAPSULE_CHARS = 2400
# Package ingestion accepts complete entrypoints up to the quality ceiling.
# Capsule delivery remains bounded independently of this inspection limit.
MAX_GUARDED_CONTENT_CHARS = 500_000
INCOMPLETE_CAPSULE_HINT = (
    "This capsule is not the complete SKILL.md; it is a bounded verified extract. "
    "Use the source provenance for human review rather than treating it as complete instructions."
)


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else str(value)
    return max(1, math.ceil(len(text) / 4)) if text else 0


def build_capsule(
    task: str,
    content: str,
    max_chars: int = DEFAULT_CAPSULE_CHARS,
    *,
    package_manifest: dict[str, Any] | None = None,
    source_url: str | None = None,
    source_commit_sha: str | None = None,
    package_hash: str | None = None,
) -> str:
    """Compatibility wrapper returning only safe distilled capsule text."""
    compiled = compile_capsule(
        task=task,
        content=content,
        max_chars=max_chars,
        package_manifest=package_manifest,
        source_url=source_url,
        source_commit_sha=source_commit_sha,
        package_hash=package_hash,
    )
    return compiled.text if compiled else ""


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
    package_manifest: dict[str, Any] | None = None,
    source_url: str | None = None,
    source_commit_sha: str | None = None,
    package_hash: str | None = None,
) -> dict[str, Any]:
    """Distill public content; never return raw scraped instructions."""
    del supports_isolation, max_inline_chars, force_capsule
    result: dict[str, Any] = {
        "policy": POLICY_VERSION,
        "delivery": "hint",
        "reason": "content_unavailable",
        "complete": False,
        "capsule": None,
        "capsule_chars": 0,
        "estimated_tokens": 0,
        "content_hash": content_hash or None,
        "content_digest": content_digest or None,
        "capsule_digest": None,
        "confidence": None,
        "unresolved_references": [],
        "destructive_actions": False,
        "external_actions": False,
        "fetch_hint": None,
    }
    if not content:
        return result
    if len(content) > MAX_GUARDED_CONTENT_CHARS:
        result["reason"] = "content_too_large"
        return result
    if not has_valid_skill_frontmatter(content):
        result["reason"] = "invalid_skill_frontmatter"
        return result

    compiled = compile_capsule(
        task=task,
        content=content,
        max_chars=max_capsule_chars,
        package_manifest=package_manifest,
        source_url=source_url,
        source_commit_sha=source_commit_sha,
        package_hash=package_hash,
    )
    if not compiled:
        result["reason"] = "capsule_unavailable"
        return result
    result.update(
        {
            "delivery": "capsule",
            "reason": "public-source-distilled",
            "capsule": compiled.text,
            "capsule_chars": len(compiled.text),
            "estimated_tokens": estimate_tokens(compiled.text),
            "capsule_digest": compiled.capsule_digest,
            "confidence": compiled.confidence,
            "unresolved_references": list(compiled.unresolved_references),
            "removed_meta_lines": compiled.removed_meta_lines,
            "removed_credential_lines": compiled.removed_credential_lines,
            "destructive_actions": compiled.destructive_actions,
            "external_actions": compiled.external_actions,
            "source_url": compiled.source_url,
            "source_commit_sha": compiled.source_commit_sha,
            "package_hash": compiled.package_hash,
            "fetch_hint": INCOMPLETE_CAPSULE_HINT,
        }
    )
    return result
