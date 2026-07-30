"""Whole context delivery for verified, safety-stripped Agent Skill content.

Reconciliation note: this module originally bounded delivery to a
compile_capsule() extract (fixed char budget, "action-shaped lines only",
gated behind a manual capsule-digest allowlist -- see git history). That
tiering model was superseded: quality.tier_for_ranked_candidates (score/
risk/similarity/active-status gates) is the sole full/hint/none decision,
and a "full" result now delivers the whole curated, already safety-stripped
skill (see scraper.curate_skill_content + capsule_compiler.strip_unsafe_content,
run once at ingest time) -- not a bounded procedural digest. strip_unsafe_content
still runs here too, defense-in-depth, in case content reached this path
without going through ingest-time stripping.
"""

from __future__ import annotations

import math
from typing import Any

from capsule_compiler import STRIP_VERSION, strip_unsafe_content
from quality import has_valid_skill_frontmatter


POLICY_VERSION = STRIP_VERSION
# Package ingestion accepts complete entrypoints up to the quality ceiling;
# delivery is bounded by the same ceiling now, not a separate small budget.
MAX_GUARDED_CONTENT_CHARS = 500_000

# Compatibility constants: callers (request-body defaults, tests) still
# reference these names, but delivery is no longer truncated to them -- a
# "full" result is the whole safety-stripped skill, capped only by
# MAX_GUARDED_CONTENT_CHARS above.
DEFAULT_CAPSULE_CHARS = 2400
DEFAULT_INLINE_CHARS = 4000
MAX_INLINE_CHARS = 12_000
MAX_CAPSULE_CHARS = MAX_GUARDED_CONTENT_CHARS
INCOMPLETE_CAPSULE_HINT = "This is not the complete SKILL.md; fetch the full content for the rest."


def estimate_tokens(value: Any) -> int:
    if value is None:
        return 0
    text = value if isinstance(value, str) else str(value)
    return max(1, math.ceil(len(text) / 4)) if text else 0


def build_capsule(task: str, content: str, *_args: Any, **_kwargs: Any) -> str:
    """Compatibility wrapper returning the whole safety-stripped text."""
    del task  # kept for signature compatibility; content is delivered whole, not task-bounded
    return strip_unsafe_content(content).text if content else ""


def build_context_guard(
    *,
    task: str,
    content: str,
    content_hash: str = "",
    content_digest: str = "",
    package_manifest: dict[str, Any] | None = None,
    source_url: str | None = None,
    source_commit_sha: str | None = None,
    package_hash: str | None = None,
    **_compat_kwargs: Any,
) -> dict[str, Any]:
    """Deliver the whole, safety-stripped skill; never raw scraped bytes."""
    del task, package_manifest  # kept for call-site/signature compatibility
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

    stripped = strip_unsafe_content(content)
    if not stripped.text:
        result["reason"] = "capsule_unavailable"
        return result
    result.update(
        {
            "delivery": "capsule",
            "reason": "public-source-distilled",
            "complete": True,
            "capsule": stripped.text,
            "capsule_chars": len(stripped.text),
            "estimated_tokens": estimate_tokens(stripped.text),
            "removed_meta_lines": stripped.removed_meta_lines,
            "removed_credential_lines": stripped.removed_credential_lines,
            "destructive_actions": stripped.destructive_actions,
            "external_actions": stripped.external_actions,
            "source_url": source_url,
            "source_commit_sha": source_commit_sha,
            "package_hash": package_hash,
        }
    )
    return result
