"""Context delivery for verified, safety-stripped Agent Skill content.

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
The on-demand internet path opts into ``bounded_delivery=True`` and receives
an exact-token capsule from ``capsule_compiler``. Legacy indexed delivery
keeps its existing whole safety-stripped behavior for wire compatibility.
"""

from __future__ import annotations

import math
import os
from typing import Any

from capsule_compiler import STRIP_VERSION, compile_capsule, strip_unsafe_content
from quality import has_valid_skill_frontmatter
from token_budget import TokenCounter, TokenizerUnavailable


POLICY_VERSION = STRIP_VERSION
# Compatibility symbol only. Complete source is retained without a hard
# delivery ceiling; oversized responses use the isolated fetch path below.
MAX_GUARDED_CONTENT_CHARS = 500_000

# Compatibility constants: callers (request-body defaults, tests) still
# reference these names, but delivery is no longer truncated to them.
DEFAULT_CAPSULE_CHARS = 2400
DEFAULT_INLINE_CHARS = 4000
MAX_INLINE_CHARS = 12_000
MAX_CAPSULE_CHARS = MAX_GUARDED_CONTENT_CHARS
INCOMPLETE_CAPSULE_HINT = "This is not the complete SKILL.md; fetch the full content for the rest."
DEFAULT_INLINE_DELIVERY_CHARS = 24_000


def _inline_delivery_budget() -> int:
    try:
        return max(1, int(os.getenv("AUTOSKILL_MAX_INLINE_DELIVERY_CHARS", str(DEFAULT_INLINE_DELIVERY_CHARS))))
    except (TypeError, ValueError):
        return DEFAULT_INLINE_DELIVERY_CHARS


def _bounded_compilation(
    *,
    task: str,
    content: str,
    max_chars: int,
    max_tokens: int,
    token_counter: TokenCounter,
    content_hash: str,
    source_url: str | None,
    source_commit_sha: str | None,
    package_hash: str | None,
):
    """Compile and token-cap a capsule without ever clipping token IDs."""

    # The compiler's output is deterministic but section boundaries can make
    # the token count jump. Try a small descending character budget instead
    # of slicing the already-tokenized result, which could cut a procedure in
    # the middle and invalidate the digest.
    upper = max(400, min(int(max_chars or DEFAULT_CAPSULE_CHARS), DEFAULT_CAPSULE_CHARS))
    lower = 400
    for candidate_chars in range(upper, lower - 1, -100):
        compiled = compile_capsule(
            task=task,
            content=content,
            max_chars=candidate_chars,
            source_url=source_url,
            source_commit_sha=source_commit_sha,
            package_hash=package_hash,
        )
        if compiled is None:
            continue
        try:
            token_count = int(token_counter.count(compiled.text))
        except (TokenizerUnavailable, RuntimeError, ValueError, TypeError):
            return None, None
        if 0 < token_count <= max_tokens:
            return compiled, token_count
    return None, None


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
    bounded_delivery: bool = False,
    token_counter: TokenCounter | None = None,
    max_tokens: int | None = None,
    **_compat_kwargs: Any,
) -> dict[str, Any]:
    """Deliver the whole, safety-stripped skill; never raw scraped bytes."""
    del package_manifest  # kept for call-site/signature compatibility
    try:
        max_capsule_chars = max(400, int(_compat_kwargs.get("max_capsule_chars") or DEFAULT_CAPSULE_CHARS))
    except (TypeError, ValueError):
        max_capsule_chars = DEFAULT_CAPSULE_CHARS
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
    if not has_valid_skill_frontmatter(content):
        result["reason"] = "invalid_skill_frontmatter"
        return result

    if len(content) > MAX_GUARDED_CONTENT_CHARS:
        result["reason"] = "content_too_large"
        return result

    if bounded_delivery:
        if token_counter is None:
            result["reason"] = "tokenizer_unavailable"
            return result
        stripped = strip_unsafe_content(content)
        if stripped.non_portable:
            result["reason"] = "non_portable_project_specific"
            return result
        try:
            token_limit = max(
                1,
                int(
                    max_tokens
                    or os.getenv("AUTOSKILL_ON_DEMAND_TOKEN_BUDGET", "1000")
                ),
            )
        except (TypeError, ValueError):
            token_limit = 1000
        compiled, token_count = _bounded_compilation(
            task=task,
            content=content,
            max_chars=max_capsule_chars,
            max_tokens=token_limit,
            token_counter=token_counter,
            content_hash=content_hash,
            source_url=source_url,
            source_commit_sha=source_commit_sha,
            package_hash=package_hash,
        )
        if compiled is None or token_count is None:
            result["reason"] = "capsule_token_budget_exceeded"
            return result
        result.update(
            {
                "delivery": "capsule",
                "reason": "public-source-distilled",
                "complete": True,
                "capsule": compiled.text,
                "capsule_chars": len(compiled.text),
                "estimated_tokens": token_count,
                "tokenizer_id": getattr(token_counter, "tokenizer_id", "unknown"),
                "capsule_digest": compiled.capsule_digest,
                "removed_meta_lines": compiled.removed_meta_lines,
                "removed_credential_lines": compiled.removed_credential_lines,
                "destructive_actions": compiled.destructive_actions,
                "external_actions": compiled.external_actions,
                "source_url": source_url,
                "source_commit_sha": source_commit_sha,
                "package_hash": package_hash,
            }
        )
        return result

    stripped = strip_unsafe_content(content)
    if not stripped.text:
        result["reason"] = "capsule_unavailable"
        return result

    if stripped.non_portable:
        # Repeatedly references the same made-up project path (e.g.
        # "Calypso/tools/x.py") -- a bespoke automation for one specific
        # repo, not a technique that generalizes to a stranger's machine.
        # Retrieval similarity doesn't catch this; only content does.
        result["reason"] = "non_portable_project_specific"
        return result

    if len(stripped.text) > _inline_delivery_budget():
        result.update(
            {
                "delivery": "isolation",
                "reason": "public-source-isolated-full",
                "complete": True,
                "capsule": None,
                "capsule_chars": 0,
                "estimated_tokens": estimate_tokens(stripped.text),
                "removed_meta_lines": stripped.removed_meta_lines,
                "removed_credential_lines": stripped.removed_credential_lines,
                "destructive_actions": stripped.destructive_actions,
                "external_actions": stripped.external_actions,
                "source_url": source_url,
                "source_commit_sha": source_commit_sha,
                "package_hash": package_hash,
                "fetch_hint": (
                    "Fetch the complete verified entrypoint through the content URL "
                    f"for content hash {content_hash}."
                    if content_hash
                    else "Fetch the complete verified entrypoint through the content URL."
                ),
            }
        )
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
