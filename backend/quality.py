"""Quality gates and deterministic reranking for Auto-Skill.

This module is deliberately dependency-free so scraper, recommender, tests, and
future CLI tools can share the same launch-critical rules.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

CONFIG_VERSION = "quality-routing-v1"
MIN_CONTENT_CHARS = 180
MIN_BODY_WORDS = 35

ACTIVE_STATUSES = {"active", "metadata_only"}
FULL_ROUTE_STATUS = "active"

TRUSTED_METADATA_SOURCES = {
    "mcp_official_registry",
    "smithery_registry",
    "glama_registry",
    "pulsemcp_registry",
    "npm",
}

PLATFORM_ALIASES: dict[str, tuple[str, ...]] = {
    "airtable": ("airtable",),
    "aws": ("aws", "amazon web services"),
    "github": ("github", "git hub"),
    "google-sheets": ("google sheets", "gsheets", "g sheet"),
    "jira": ("jira", "atlassian"),
    "landingi": ("landingi", "landingi.com"),
    "mongodb": ("mongodb", "mongo db"),
    "notion": ("notion",),
    "postgres": ("postgres", "postgresql", "pgvector"),
    "salesforce": ("salesforce", "sfdc", "apex"),
    "shopify": ("shopify", "liquid theme", "liquid"),
    "slack": ("slack",),
    "stripe": ("stripe",),
    "wordpress": ("wordpress", "wp-"),
}

PLATFORM_SPECIFIC_MARKERS = (
    "platform help",
    "api key",
    "oauth",
    "custom domain",
    "webhook",
    "crm",
    "leads",
    "won't publish",
    "wont publish",
    "not syncing",
    "use when your",
)

BODY_CUES = (
    "use when",
    "when the user",
    "you should",
    "must",
    "do not",
    "workflow",
    "steps",
    "instructions",
    "create",
    "generate",
    "analyze",
    "edit",
    "build",
    "write",
    "run",
    "verify",
    "output",
)

NAME_STOPWORDS = {
    "agent",
    "agents",
    "ai",
    "auto",
    "automation",
    "assistant",
    "code",
    "claude",
    "creator",
    "helper",
    "mcp",
    "sales",
    "skill",
    "skills",
    "tool",
    "tools",
    "writer",
}

TOKEN_RE = re.compile(r"[a-z0-9]+")
FRONTMATTER_BLOCK_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
FRONTMATTER_NAME_RE = re.compile(r"^name:\s*\S+", re.MULTILINE)
FRONTMATTER_DESCRIPTION_RE = re.compile(r"^description:\s*(?:\S+|[>|])", re.MULTILINE)

# The connector and Claude hook already avoid these prompts. Keep the backend
# equally conservative because hosted MCP callers can invoke /route directly.
ACK_PROMPTS = {
    "ok",
    "okay",
    "yes",
    "no",
    "thanks",
    "thank you",
    "continue",
    "go on",
    "do it",
    "sounds good",
    "yes please",
    "please do",
    "go ahead",
    "continue please",
}
META_PATTERNS = (
    "what did you",
    "what are you",
    "what is the current state",
    "current state",
    "explain this",
    "summarize",
    "status",
    "whats the",
    "what's the",
    "why is",
    "why did",
    "remember th",
    "sounds good",
    "that worked",
    "looks good",
    "can you explain",
    "what you just",
)


def normalize_content(text: str) -> str:
    """Stable normalization for dedupe hashes."""
    text = text.replace("\r\n", "\n").replace("\r", "\n").strip()
    text = re.sub(r"(?im)^(updated_at|version|date):\s*.+$", "", text)
    return re.sub(r"\s+", " ", text).strip().lower()


def content_hash(text: str) -> str:
    if not text:
        return ""
    return hashlib.sha256(normalize_content(text).encode("utf-8")).hexdigest()


def strip_frontmatter(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    parts = stripped.split("---", 2)
    return parts[2] if len(parts) == 3 else text


def has_valid_skill_frontmatter(text: str) -> bool:
    """True only for a Claude-style skill document with its core metadata."""
    match = FRONTMATTER_BLOCK_RE.match(text or "")
    if not match:
        return False
    block = match.group(1)
    return bool(FRONTMATTER_NAME_RE.search(block) and FRONTMATTER_DESCRIPTION_RE.search(block))


def is_non_task_prompt(prompt: str) -> bool:
    """Cheap backend guard for acknowledgements and conversational meta text."""
    text = " ".join((prompt or "").split())
    lowered = text.lower()
    if not text or text.startswith(("/", "!")) or len(text) > 3000:
        return True
    if lowered in ACK_PROMPTS:
        return True
    return len(text) < 180 and any(pattern in lowered for pattern in META_PATTERNS)


def _contains_alias(text: str, alias: str) -> bool:
    """Match platform aliases as words, not arbitrary substrings.

    `aws` should not match `laws`, and `notion` should not match `notional`.
    The `wp-` alias deliberately remains a prefix because WordPress plugins
    commonly use names such as `wp-cli`.
    """
    escaped = re.escape(alias.lower())
    if alias.endswith("-"):
        return bool(re.search(rf"(?<![a-z0-9]){escaped}", text))
    return bool(re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", text))


def _is_path_or_link_line(line: str) -> bool:
    value = line.strip().strip("`'\"")
    if not value:
        return False
    if re.match(r"^[a-zA-Z]:[\\/]", value):
        return True
    if value.startswith(("http://", "https://", "file://")):
        return True
    if value.startswith(("./", "../", "~/", "/")):
        return True
    slash_count = value.count("/") + value.count("\\")
    return slash_count >= 2 and len(value.split()) <= 3


def skill_content_rejection_reasons(text: str) -> list[str]:
    """Return rejection reasons for fetched skill-like content."""
    reasons: list[str] = []
    head = text.lstrip()[:300].lower()
    if head.startswith(("<!doctype", "<html", "<?xml")) or "<head>" in head or "githubassets.com" in head:
        reasons.append("html-response")

    normalized = text.strip()
    if len(normalized) < MIN_CONTENT_CHARS:
        reasons.append("too-short")

    nonempty_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if nonempty_lines:
        path_lines = [line for line in nonempty_lines if _is_path_or_link_line(line)]
        if len(path_lines) / len(nonempty_lines) >= 0.6:
            reasons.append("path-or-link-only")

    body = strip_frontmatter(normalized)
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", body)
    if len(words) < MIN_BODY_WORDS:
        reasons.append("too-few-body-words")

    body_lower = body.lower()
    has_structure = "##" in body or re.search(r"^\s*[-*]\s+\S+", body, re.MULTILINE)
    has_name = bool(FRONTMATTER_NAME_RE.search(normalized))
    has_cue = any(cue in body_lower for cue in BODY_CUES)
    if not (has_name or has_structure):
        reasons.append("no-skill-structure")
    if not has_cue:
        reasons.append("no-instruction-cues")
    return reasons


def infer_platforms(skill: dict[str, Any]) -> list[str]:
    blob = " ".join(
        [
            str(skill.get("name") or ""),
            str(skill.get("description") or ""),
            " ".join(str(tag) for tag in (skill.get("tags") or [])),
            str((skill.get("raw") or {}).get("github") or "") if isinstance(skill.get("raw"), dict) else "",
        ]
    ).lower()
    found = []
    for platform, aliases in PLATFORM_ALIASES.items():
        if any(_contains_alias(blob, alias) for alias in aliases):
            found.append(platform)
    return sorted(set(found))


def platform_mentions(prompt: str, platforms: list[str]) -> bool:
    text = prompt.lower()
    for platform in platforms:
        aliases = PLATFORM_ALIASES.get(platform, (platform,))
        if any(_contains_alias(text, alias) for alias in aliases):
            return True
    return False


def evaluate_quality(skill: dict[str, Any], content: str = "") -> dict[str, Any]:
    """Return ingest quality metadata for a candidate skill."""
    reasons: list[str] = []
    score = 0
    source = str(skill.get("source") or "")
    description = str(skill.get("description") or "").strip()
    name = str(skill.get("name") or "").strip()
    platforms = infer_platforms(skill)

    if name:
        score += 10
    else:
        reasons.append("missing-name")
    if len(description) >= 40:
        score += 20
    elif description:
        score += 8
    else:
        reasons.append("missing-description")

    valid_frontmatter = has_valid_skill_frontmatter(content) if content else False
    if content:
        chash = content_hash(content)
        content_reasons = skill_content_rejection_reasons(content)
        reasons.extend(content_reasons)
        if not content_reasons:
            score += 45
        elif "html-response" in content_reasons or "path-or-link-only" in content_reasons:
            score -= 20
        if len(content) > 800:
            score += 10
        if "##" in content:
            score += 5
        if not valid_frontmatter:
            # Structured README files can be useful for discovery, but without
            # skill metadata they must never be injected as active instructions.
            reasons.append("missing-skill-frontmatter")
    else:
        chash = ""
        if source in TRUSTED_METADATA_SOURCES and name and len(description) >= 40:
            reasons.append("metadata-only")
            score += 15
        else:
            reasons.append("missing-content")

    raw = skill.get("raw") or {}
    stars = 0
    if isinstance(raw, dict):
        try:
            stars = int(raw.get("stars") or skill.get("stars") or 0)
        except (TypeError, ValueError):
            stars = 0
    if stars > 0:
        score += min(10, len(str(stars)) * 2)

    if content and content_reasons:
        status = "rejected"
    elif content and not valid_frontmatter:
        status = "metadata_only"
    elif not content and "missing-content" in reasons:
        status = "rejected"
    elif "metadata-only" in reasons:
        status = "metadata_only"
    else:
        status = "active"

    score = max(0, min(score, 100))
    return {
        "content_hash": chash,
        "quality_status": status,
        "quality_reasons": sorted(set(reasons)),
        "quality_score": score,
        "platforms": platforms,
        "category": "integration" if platforms else "capability",
    }


def _tokens(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if len(t) > 2 and t not in NAME_STOPWORDS]


def lexical_overlap(prompt: str, candidate: dict[str, Any]) -> int:
    prompt_tokens = set(_tokens(prompt))
    name_tokens = set(_tokens(str(candidate.get("name") or "")))
    desc_tokens = set(_tokens(str(candidate.get("description") or "")))
    return len(prompt_tokens & name_tokens) * 2 + len(prompt_tokens & desc_tokens)


def _platform_specific_penalty(prompt: str, candidate: dict[str, Any]) -> float:
    platforms = candidate.get("platforms") or []
    if isinstance(platforms, str):
        platforms = [platforms]
    platforms = [str(p) for p in platforms if p]
    text = " ".join(
        [
            str(candidate.get("description") or ""),
            " ".join(str(tag) for tag in (candidate.get("tags") or [])),
        ]
    ).lower()
    looks_platform_specific = platforms or any(marker in text for marker in PLATFORM_SPECIFIC_MARKERS)
    if not looks_platform_specific:
        return 0.0
    if platforms and platform_mentions(prompt, platforms):
        return 0.0

    name_tokens = [token for token in _tokens(str(candidate.get("name") or "")) if token not in NAME_STOPWORDS]
    prompt_tokens = set(_tokens(prompt))
    if any(token in prompt_tokens for token in name_tokens):
        return 0.0
    return 0.08


def rerank_candidates(prompt: str, candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Return candidates with deterministic route_score metadata."""
    reranked: list[dict[str, Any]] = []
    for candidate in candidates:
        row = dict(candidate)
        if (row.get("quality_status") or "active") == "rejected":
            continue

        base_rank = float(row.get("rank") or 0.0)
        similarity = row.get("similarity")
        try:
            sim = float(similarity) if similarity is not None else 0.0
        except (TypeError, ValueError):
            sim = 0.0
        overlap = lexical_overlap(prompt, row)
        penalty = _platform_specific_penalty(prompt, row)
        quality = float(row.get("quality_score") or 50) / 100.0
        feedback = row.get("feedback_score")
        feedback = float(feedback) if feedback is not None else 0.5
        route_score = base_rank + (0.02 * overlap) + (0.01 * quality) + (0.01 * (feedback - 0.5)) - penalty

        row["lexical_overlap"] = overlap
        row["platform_mismatch"] = penalty > 0
        row["route_score"] = round(route_score, 6)
        if sim:
            row["similarity"] = sim
        reranked.append(row)
    reranked.sort(key=lambda item: item.get("route_score", item.get("rank", 0)), reverse=True)
    return reranked


def _row_stars(row: dict[str, Any]) -> int:
    stars = row.get("stars")
    if stars is not None:
        try:
            return int(stars)
        except (TypeError, ValueError):
            return 0
    raw = row.get("raw")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            raw = {}
    if isinstance(raw, dict):
        try:
            return int(raw.get("stars") or 0)
        except (TypeError, ValueError):
            return 0
    return 0


def pick_canonical(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Given candidates that share the same content_hash, return the one that
    should survive as canonical: highest quality_score, then highest stars,
    then most recently scanned/discovered, then a stable id/url tiebreak."""

    def sort_key(row: dict[str, Any]):
        recency = str(row.get("scanned_at") or row.get("discovered_at") or "")
        return (
            int(row.get("quality_score") or 0),
            _row_stars(row),
            recency,
            str(row.get("id") or row.get("url") or ""),
        )

    return max(rows, key=sort_key)


def dedupe_by_content_hash(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Group rows sharing a non-empty content_hash, keep only pick_canonical()
    per group (in original relative order); rows with no hash pass through
    unchanged. Only hashes that actually recur are grouped/decided -- a
    unique-hash row is never replaced by itself through pick_canonical."""
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        chash = row.get("content_hash")
        if chash:
            groups.setdefault(chash, []).append(row)
    winners = {chash: pick_canonical(group) for chash, group in groups.items() if len(group) > 1}

    result: list[dict[str, Any]] = []
    emitted: set[str] = set()
    for row in rows:
        chash = row.get("content_hash")
        if not chash or chash not in winners:
            result.append(row)
            continue
        if chash in emitted:
            continue
        emitted.add(chash)
        result.append(winners[chash])
    return result


def tier_for_prompt(prompt: str, candidates: list[dict[str, Any]], recommend_gap: float = 1.6) -> str:
    """Conservative full/hint/none decision after reranking."""
    if not candidates:
        return "none"
    ranked = rerank_candidates(prompt, candidates)
    if not ranked:
        return "none"
    top = ranked[0]
    if top.get("platform_mismatch"):
        return "hint"
    if (top.get("quality_status") or "active") != FULL_ROUTE_STATUS:
        return "hint"
    if int(top.get("quality_score") or 0) < 40:
        return "hint"
    if int(top.get("risk_score") or 0) > 0:
        return "hint"

    top_similarity = top.get("similarity")
    if top_similarity is None:
        return "hint"
    try:
        if float(top_similarity) < 0.87:
            return "none"
    except (TypeError, ValueError):
        return "none"

    if top.get("lexical_overlap", 0) < 2:
        return "hint"
    if len(ranked) == 1:
        return "full"
    runner = ranked[1]
    top_score = float(top.get("route_score") or 0.0)
    runner_score = float(runner.get("route_score") or 0.0)
    if runner_score <= 0 or top_score >= runner_score * recommend_gap:
        return "full"
    return "hint"
