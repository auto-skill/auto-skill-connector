"""Shared search, fetch, and install logic for auto-skill."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

DEFAULT_AUTOSKILL_URL = "https://skills.avalahome.com"
CLIENT_NAME = "auto-skill-connector"
CLIENT_VERSION = "0.1.0"

_BLOB_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)")
_TREE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)")
_REPO_RE = re.compile(r"github\.com/([^/]+)/([^/#?]+)(?:[/#?].*)?$")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
_MIN_SKILL_CONTENT_CHARS = 180
_MIN_SKILL_WORDS = 35
_DEFAULT_MAX_INJECTED_CONTENT_CHARS = 12000
_FULL_SIMILARITY_THRESHOLD = 0.90
_HINT_SIMILARITY_THRESHOLD = 0.87
_NAME_STOPWORDS = {
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
_PLATFORM_SPECIFIC_MARKERS = (
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
_SKILL_BODY_CUES = (
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
_ACK_PROMPTS = {
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
}
_META_PATTERNS = (
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
    "can you explain",
    "what you just",
    "why did",
    "remember th",
    "sounds good",
    "that worked",
    "looks good",
)


class AutoSkillError(Exception):
    """Base class for expected auto-skill failures."""


class UnsupportedTargetError(AutoSkillError):
    """Raised when a client target cannot install a permanent skill."""


class SkillAlreadyExistsError(AutoSkillError):
    """Raised when an install would overwrite an existing skill."""

    def __init__(self, dest_file: Path) -> None:
        super().__init__(f"Skill already exists at {dest_file}")
        self.dest_file = dest_file


def get_autoskill_url() -> str:
    """Return the configured self-hosted search URL."""
    return os.getenv("AUTOSKILL_URL", DEFAULT_AUTOSKILL_URL).rstrip("/")


def get_skills_home(target: str = "claude") -> Path:
    """Return the install directory for a target."""
    if target != "claude":
        raise UnsupportedTargetError(
            "Permanent installs are only supported for Claude today. "
            "For Codex, use the MCP route_task tool and apply full routes in-turn."
        )
    return Path(os.getenv("SKILLS_HOME", str(Path.home() / ".claude" / "skills"))).expanduser()


def is_url(value: str) -> bool:
    return bool(_URL_RE.match(value.strip()))


def should_route_prompt(prompt: str) -> dict[str, Any]:
    """Decide whether a prompt is worth sending through skill routing."""
    text = " ".join(prompt.split())
    lowered = text.lower()
    if not text:
        return {"should_route": False, "reason": "empty prompt"}
    if text.startswith(("/", "!")):
        return {"should_route": False, "reason": "command prompt"}
    if len(text) < 12:
        return {"should_route": False, "reason": "too short"}
    if len(text) > 3000:
        return {"should_route": False, "reason": "too long; likely pasted context"}
    if lowered in _ACK_PROMPTS:
        return {"should_route": False, "reason": "acknowledgement or continuation"}
    if any(pattern in lowered for pattern in _META_PATTERNS) and len(text) < 180:
        return {"should_route": False, "reason": "meta or status prompt"}
    return {"should_route": True, "reason": "skill-shaped prompt"}


def _raw_candidates(url: str) -> list[str]:
    """Return candidate raw URLs for a GitHub skill URL."""
    m = _BLOB_RE.search(url)
    if m:
        owner, repo, ref, path = m.groups()
        return [f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}"]
    m = _TREE_RE.search(url)
    if m:
        owner, repo, ref, path = m.groups()
        base = f"https://raw.githubusercontent.com/{owner}/{repo}/{ref}/{path}".rstrip("/")
        return [f"{base}/SKILL.md", f"{base}/skill.md"]
    m = _REPO_RE.search(url)
    if m:
        owner, repo = m.groups()
        repo = repo.removesuffix(".git")
        base = f"https://raw.githubusercontent.com/{owner}/{repo}"
        return [
            f"{base}/HEAD/SKILL.md",
            f"{base}/HEAD/skill.md",
            f"{base}/main/SKILL.md",
            f"{base}/main/skill.md",
            f"{base}/master/SKILL.md",
            f"{base}/master/skill.md",
        ]
    return [url]


def _strip_frontmatter(text: str) -> str:
    stripped = text.lstrip()
    if not stripped.startswith("---"):
        return text
    parts = stripped.split("---", 2)
    if len(parts) == 3:
        return parts[2]
    return text


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


def _looks_like_skill_content(text: str) -> bool:
    """Return whether fetched text is safe and useful enough to inject.

    Plain repo URLs often resolve to GitHub's HTML, and some indexed entries
    are stubs that only contain a local path. Neither should become active
    model instructions.
    """
    head = text.lstrip()[:300].lower()
    if head.startswith(("<!doctype", "<html", "<?xml")):
        return False
    if "<head>" in head or "githubassets.com" in head:
        return False

    normalized = text.strip()
    if len(normalized) < _MIN_SKILL_CONTENT_CHARS:
        return False

    nonempty_lines = [line.strip() for line in normalized.splitlines() if line.strip()]
    if nonempty_lines:
        path_or_link_lines = [line for line in nonempty_lines if _is_path_or_link_line(line)]
        if len(path_or_link_lines) / len(nonempty_lines) >= 0.6:
            return False

    body = _strip_frontmatter(normalized)
    words = re.findall(r"[A-Za-z][A-Za-z0-9_-]+", body)
    if len(words) < _MIN_SKILL_WORDS:
        return False

    body_lower = body.lower()
    has_frontmatter_name = bool(re.search(r"^name:\s*\S+", normalized, re.MULTILINE))
    has_markdown_structure = "##" in body or re.search(r"^\s*[-*]\s+\S+", body, re.MULTILINE)
    has_instruction_cue = any(cue in body_lower for cue in _SKILL_BODY_CUES)
    if not (has_frontmatter_name or has_markdown_structure):
        return False
    if not has_instruction_cue:
        return False
    return True


_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
_ABS_PATH_RE = re.compile(
    r"^\s*(?:[A-Za-z]:\\|/(?:home|Users|mnt|c|d)/|~[\\/])[^\n]*\s*$"
)
MIN_STUB_BODY_CHARS = 200


_ACTION_VERB_RE = re.compile(
    r"\b(send|post|delete|remove|execute|run|publish|deploy|push|commit|email|message|transfer|pay|purchase|upload)\b",
    re.IGNORECASE,
)
_NO_CONFIRM_RE = re.compile(
    r"do\s*not\s*(?:ask|confirm|wait)|don'?t\s*(?:ask|confirm|wait)|"
    r"without\s*(?:asking|confirmation)|immediately\s*--?\s*do\s*not|no\s*confirmation\s*needed",
    re.IGNORECASE,
)


def _is_unconfirmed_action_content(text: str) -> bool:
    """Stopgap heuristic (2026-07-07): risk_score only catches malware
    patterns, not skills that take real side effects (send a message, read a
    secrets file, hit an API) while explicitly instructing the agent not to
    confirm first. Observed live: a risk_score=0 skill auto-selected for full
    injection whose body said 'Send the message immediately -- do NOT ask for
    confirmation' and read a bot token from a secrets file. Demote (not
    reject outright -- the skill may still be legitimate) any content that
    pairs an action verb with explicit no-confirmation language; callers
    should treat this as hint-tier at most, never silent full injection.
    A real fix belongs in the risk scorer itself; this is a stopgap."""
    return bool(_ACTION_VERB_RE.search(text) and _NO_CONFIRM_RE.search(text))


def _is_stub_content(text: str) -> bool:
    """Reject skill bodies too thin to be real instructions -- e.g. the
    autoplan incident, whose entire body was one absolute path from a
    stranger's machine. It cleared the HTML check and the similarity floor
    and still got injected as instructions, because nothing checked the
    body itself had anything to follow."""
    body = _FRONTMATTER_RE.sub("", text, count=1).strip()
    if len(body) < MIN_STUB_BODY_CHARS:
        return True
    if _ABS_PATH_RE.match(body):
        return True
    return False


async def _fetch_content(client: httpx.AsyncClient, url: str) -> str:
    """Fetch skill content from a URL or GitHub skill folder URL. Never
    returns content that pairs an action verb with no-confirmation language
    (see _is_unconfirmed_action_content) -- recommend_skill_payload has no
    tier concept, only "use this or don't", so for it the safe answer is
    "don't". The hook has a hint tier and applies its own softer downgrade."""
    for candidate in _raw_candidates(url):
        try:
            r = await client.get(candidate, timeout=10)
            if (
                r.status_code == 200
                and _looks_like_skill_content(r.text)
                and not _is_stub_content(r.text)
                and not _is_unconfirmed_action_content(r.text)
            ):
                return r.text
        except Exception:
            continue
    return ""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _extract_skill_name(content: str) -> str:
    m = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
    return m.group(1).strip() if m else ""


def _candidate_key(candidate: dict[str, Any]) -> str:
    name = str(candidate.get("name") or "").strip()
    if name:
        return f"name:{_slugify(name)}"
    url = str(candidate.get("url") or "").strip()
    return f"url:{url}"


def _dedupe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Keep the first candidate for each skill name or URL."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in candidates:
        key = _candidate_key(candidate)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _safe_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    safe = [c for c in candidates if (c.get("risk_score") or 0) < 3]
    return _dedupe_candidates(safe)


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _max_injected_content_chars() -> int:
    try:
        return int(os.getenv("AUTOSKILL_MAX_INJECTED_CHARS", str(_DEFAULT_MAX_INJECTED_CONTENT_CHARS)))
    except ValueError:
        return _DEFAULT_MAX_INJECTED_CONTENT_CHARS


def _content_exceeds_budget(content: str) -> bool:
    return len(content or "") > _max_injected_content_chars()


def _candidate_similarity(candidate: dict[str, Any]) -> float | None:
    for key in ("similarity", "score", "semantic_score", "vector_score"):
        value = candidate.get(key)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                continue
    return None


def _candidate_rank(candidate: dict[str, Any]) -> float:
    value = candidate.get("rank")
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def _name_tokens(candidate: dict[str, Any]) -> list[str]:
    name = str(candidate.get("name") or "")
    tokens = re.findall(r"[a-z0-9]+", name.lower())
    return [token for token in tokens if len(token) > 2 and token not in _NAME_STOPWORDS]


def _platform_specific_penalty(candidate: dict[str, Any], task: str) -> float:
    """Penalize brand/platform support skills unless the prompt names them.

    This catches cases like a generic "build a landing page" prompt routing to
    a Landingi support skill just because "landing" and "page" are nearby.
    """
    task_lower = task.lower()
    description = str(candidate.get("description") or "").lower()
    tags = " ".join(str(tag).lower() for tag in (candidate.get("tags") or []))
    text = f"{description} {tags}"
    if not any(marker in text for marker in _PLATFORM_SPECIFIC_MARKERS):
        return 0.0

    unmatched = [token for token in _name_tokens(candidate) if token not in task_lower]
    if not unmatched:
        return 0.0
    if "platform" in text or "api" in text or "oauth" in text:
        return 0.08
    return 0.04


def _routing_score(candidate: dict[str, Any], task: str) -> float | None:
    similarity = _candidate_similarity(candidate)
    if similarity is None:
        return None
    return max(0.0, similarity - _platform_specific_penalty(candidate, task))


def _routing_tier(candidate: dict[str, Any], task: str) -> str:
    score = _routing_score(candidate, task)
    if score is None:
        return "hint"
    if score >= _env_float("AUTOSKILL_FULL_THRESHOLD", _FULL_SIMILARITY_THRESHOLD):
        return "full"
    if score >= _env_float("AUTOSKILL_HINT_THRESHOLD", _HINT_SIMILARITY_THRESHOLD):
        return "hint"
    return "none"


def _public_candidate(candidate: dict[str, Any], task: str) -> dict[str, Any]:
    score = _routing_score(candidate, task)
    public = {
        "name": candidate.get("name"),
        "description": candidate.get("description"),
        "url": candidate.get("url"),
        "source": candidate.get("source"),
        "stars": candidate.get("stars"),
        "risk_score": candidate.get("risk_score"),
        "similarity": _candidate_similarity(candidate),
        "rank": candidate.get("rank"),
        "routing_tier": _routing_tier(candidate, task),
    }
    if score is not None:
        public["routing_score"] = round(score, 6)
    return public


def _rank_candidates_for_task(candidates: list[dict[str, Any]], task: str) -> list[dict[str, Any]]:
    def sort_key(candidate: dict[str, Any]) -> tuple[float, float, int]:
        score = _routing_score(candidate, task)
        if score is None:
            score = 0.0
        has_similarity = 1 if _candidate_similarity(candidate) is not None else 0
        return (score, _candidate_rank(candidate), has_similarity)

    return sorted(candidates, key=sort_key, reverse=True)


def _candidate_pool(result: dict[str, Any], task: str) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    if result.get("skill"):
        candidates.append(dict(result["skill"]))
    candidates.extend(dict(c) for c in (result.get("candidates") or []))
    candidates.extend(dict(c) for c in (result.get("options") or []))
    return _rank_candidates_for_task(_safe_candidates(candidates), task)


def _autopick(candidates: list[dict[str, Any]], task: str = "") -> dict[str, Any]:
    """Pick the top-ranked safe candidate instead of surfacing a menu."""
    ranked = _rank_candidates_for_task(candidates, task)
    return {
        "type": "recommend",
        "skill": ranked[0],
        "candidates": ranked,
        "message": f"Best match: {ranked[0].get('name')}.",
    }


def _with_backend(result: dict[str, Any], backend: str, warnings: list[str]) -> dict[str, Any]:
    result = dict(result)
    result["search_backend"] = backend
    if warnings:
        result["warnings"] = warnings
    if result.get("options"):
        result["options"] = _safe_candidates(list(result.get("options") or []))
    if result.get("candidates"):
        result["candidates"] = _safe_candidates(list(result.get("candidates") or []))
    if result.get("skill") and (result["skill"].get("risk_score") or 0) >= 3:
        return {
            "type": "none",
            "message": "The best match was filtered out by its risk score.",
            "search_backend": backend,
            "warnings": warnings,
        }
    return result


async def _search_selfhosted(
    client: httpx.AsyncClient,
    task: str,
    autoskill_url: str | None = None,
) -> dict[str, Any] | None:
    """Search the self-hosted index. Return None on failure or no safe hits."""
    url = (autoskill_url if autoskill_url is not None else get_autoskill_url()).rstrip("/")
    if not url:
        return None
    try:
        r = await client.get(
            f"{url}/find-semantic",
            params={"q": task, "limit": 8},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        results = _safe_candidates(list((r.json().get("results") or [])))
    except Exception:
        return None
    if not results:
        return None
    return _autopick(results, task)


def _public_backend_skill(skill: dict[str, Any] | None, task: str, tier: str) -> dict[str, Any]:
    """Normalize backend /route skill payloads into the public connector shape."""
    if not skill:
        return {}
    url = skill.get("url") or skill.get("source_url")
    public = {
        "name": skill.get("name") or skill.get("slug"),
        "description": skill.get("description") or skill.get("summary"),
        "url": url,
        "source": skill.get("source"),
        "stars": skill.get("stars"),
        "risk_score": skill.get("risk_score"),
        "similarity": skill.get("similarity"),
        "rank": skill.get("rank"),
        "routing_tier": tier,
    }
    score = skill.get("route_score") or skill.get("routing_score")
    if score is None:
        score = _routing_score({"similarity": skill.get("similarity"), **skill, "url": url}, task)
    if isinstance(score, (int, float)):
        public["routing_score"] = round(float(score), 6)
    for key in ("content_hash", "quality_status", "quality_score", "platforms", "category"):
        if skill.get(key) is not None:
            public[key] = skill.get(key)
    return public


async def _fetch_backend_content_url(client: httpx.AsyncClient, autoskill_url: str, content_url: str) -> str:
    target = content_url if is_url(content_url) else f"{autoskill_url.rstrip('/')}/{content_url.lstrip('/')}"
    try:
        r = await client.get(target, timeout=10)
        if (
            r.status_code == 200
            and _looks_like_skill_content(r.text)
            and not _is_stub_content(r.text)
            and not _is_unconfirmed_action_content(r.text)
        ):
            return r.text
    except Exception:
        return ""
    return ""


async def _route_selfhosted(
    client: httpx.AsyncClient,
    task: str,
    autoskill_url: str | None = None,
) -> dict[str, Any] | None:
    """Use the backend-owned deterministic route contract when available.

    Return None only when the endpoint is unavailable/unsupported so callers
    can fall back to the legacy /find-semantic compatibility path.
    """
    url = (autoskill_url if autoskill_url is not None else get_autoskill_url()).rstrip("/")
    if not url:
        return None

    try:
        r = await client.post(
            f"{url}/route",
            json={
                "task": task,
                "limit": 8,
                "client": CLIENT_NAME,
                "client_version": CLIENT_VERSION,
            },
            timeout=10,
        )
    except Exception:
        return None

    if r.status_code in {403, 404, 405}:
        return None
    if r.status_code != 200:
        return {
            "routed": False,
            "route_type": "none",
            "route_tier": "none",
            "task": task,
            "message": f"Self-hosted route endpoint returned HTTP {r.status_code}.",
            "instructions": "Continue normally.",
            "warnings": [f"Self-hosted route at {url}/route was unavailable."],
            "search_backend": "self-hosted-route",
        }

    try:
        route = r.json()
    except Exception:
        return None

    tier = str(route.get("tier") or "none").lower()
    debug = route.get("score_debug") if isinstance(route.get("score_debug"), dict) else {}
    warnings = list(debug.get("warnings") or route.get("warnings") or [])
    selected = _public_backend_skill(route.get("skill") if isinstance(route.get("skill"), dict) else None, task, tier)
    raw_candidates = route.get("candidates") if isinstance(route.get("candidates"), list) else []
    normalized_candidates = []
    for candidate in raw_candidates:
        if not isinstance(candidate, dict):
            continue
        public_candidate = _public_backend_skill(candidate, task, "hint")
        if public_candidate.get("name") or public_candidate.get("url"):
            normalized_candidates.append(public_candidate)
    route_candidates = _safe_candidates(normalized_candidates)[:3]
    if tier == "hint" and selected:
        route_candidates = _safe_candidates(_dedupe_candidates([{**selected, "routing_tier": "hint"}, *route_candidates]))[:3]
    common = {
        "task": task,
        "search_backend": "self-hosted-route",
        "warnings": warnings,
        "score_debug": debug,
        "config_version": route.get("config_version"),
        "route_id": route.get("route_id"),
        "ttl": route.get("ttl"),
    }
    if tier == "hint" and route_candidates:
        common["candidates"] = route_candidates

    if tier == "none" or not selected:
        return {
            "routed": False,
            "route_type": "none",
            "route_tier": "none",
            "message": route.get("message") or "No backend route cleared the quality and confidence gates.",
            "instructions": "Continue normally.",
            **common,
        }

    if tier == "hint":
        return {
            "routed": True,
            "route_type": "hint",
            "route_tier": "hint",
            "selected_skill": selected,
            "skill_content": "",
            "message": (
                "A related skill exists, but backend confidence is medium. Treat this as a hint, "
                "not active instructions."
            ),
            "instructions": (
                "Mention or consider the selected skill only if it clearly helps. Do not inject "
                "or follow full SKILL.md content for this task."
            ),
            "install_hint": (
                "Preview the selected_skill.url before installing. Codex should treat this as "
                "an in-turn suggestion only."
            ),
            **common,
        }

    if tier != "full":
        return {
            "routed": False,
            "route_type": "none",
            "route_tier": "none",
            "message": f"Backend returned unknown route tier: {tier}.",
            "instructions": "Continue normally.",
            **common,
        }

    content = str(route.get("content") or "")
    if not content and route.get("content_url"):
        content = await _fetch_backend_content_url(client, url, str(route["content_url"]))
    if not (
        content
        and _looks_like_skill_content(content)
        and not _is_stub_content(content)
        and not _is_unconfirmed_action_content(content)
    ):
        warnings.append("Backend selected a full route, but usable SKILL.md content was unavailable; downgraded to hint.")
        hint_selected = {**selected, "routing_tier": "hint"}
        return {
            "routed": True,
            "route_type": "hint",
            "route_tier": "hint",
            "selected_skill": hint_selected,
            "candidates": _safe_candidates(_dedupe_candidates([hint_selected, *route_candidates]))[:3],
            "skill_content": "",
            "message": "A matching skill exists, but full content was unavailable. Treat this as a hint.",
            "instructions": "Do not inject or follow full SKILL.md content for this task.",
            "install_hint": "Preview the selected_skill.url before installing.",
            **common,
        }
    if _content_exceeds_budget(content):
        warnings.append("Backend selected a full route, but SKILL.md content exceeded the connector injection budget; downgraded to hint.")
        hint_selected = {**selected, "routing_tier": "hint"}
        return {
            "routed": True,
            "route_type": "hint",
            "route_tier": "hint",
            "selected_skill": hint_selected,
            "candidates": _safe_candidates(_dedupe_candidates([hint_selected, *route_candidates]))[:3],
            "skill_content": "",
            "message": "A matching skill exists, but full content exceeded the injection budget. Treat this as a hint.",
            "instructions": "Do not inject or follow full SKILL.md content for this task.",
            "install_hint": "Preview the selected_skill.url before installing.",
            **common,
        }

    return {
        "routed": True,
        "route_type": "skill",
        "route_tier": "full",
        "selected_skill": selected,
        "skill_content": content,
        "instructions": (
            "Use this as the routing result for the current task. Treat skill_content as active "
            "task-specific instructions, apply it immediately, and produce the user's requested "
            "output in this same turn. Do not ask the user to choose a skill unless the selected "
            "skill content is missing, unusable, or unsafe."
        ),
        "install_hint": (
            "For Claude-style clients, call install_skill with the selected_skill.url only if "
            "this workflow is worth keeping permanently. Codex should use this route in-turn."
        ),
        **common,
    }


async def _search(
    client: httpx.AsyncClient | None,
    task: str,
    autoskill_url: str | None = None,
) -> dict[str, Any]:
    """Search the self-hosted index. No fallback to Supabase: the edge
    function and REST corpus there were frozen on 2026-07-05 when storage
    moved local, and silently serving stale results with no signal to the
    caller was worse than admitting no route is available. One truthful
    backend; the hook already fails open, so callers don't break, they just
    get no suggestion for that turn."""
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await _search(owned, task, autoskill_url=autoskill_url)

    warnings: list[str] = []
    selfhosted = await _search_selfhosted(client, task, autoskill_url=autoskill_url)
    if selfhosted is not None:
        return _with_backend(selfhosted, "self-hosted", warnings)

    configured_url = autoskill_url if autoskill_url is not None else get_autoskill_url()
    if configured_url:
        warnings.append(f"Self-hosted search at {configured_url} was unreachable or returned no usable result.")
    return _with_backend(
        {"type": "none", "message": "Skill search is unavailable right now. Try again shortly."},
        "unavailable",
        warnings,
    )


async def recommend_skill_payload(task: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Return the MCP payload for a task recommendation."""
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await recommend_skill_payload(task, client=owned)

    try:
        result = await _search(client, task)
    except Exception as exc:
        return {"found": False, "message": f"Skill database is unavailable right now ({exc}). Try again shortly."}

    common = {
        "search_backend": result.get("search_backend"),
        "warnings": result.get("warnings", []),
    }
    if result.get("type") == "none":
        return {"found": False, "message": result.get("message", "No matching skill found."), **common}

    if result.get("type") == "clarify":
        options = result.get("options") or []
        return {
            "found": False,
            "message": result.get("message"),
            "candidates": [
                {
                    "name": o.get("name"),
                    "description": (o.get("description") or "")[:150],
                    "url": o.get("url"),
                    "risk_score": o.get("risk_score"),
                    "stars": o.get("stars"),
                }
                for o in options
            ],
            "instructions": "Ask the user to pick one, or call recommend_skill again with a more specific task.",
            **common,
        }

    warnings = list(result.get("warnings", []))
    for top in _candidate_pool(result, task):
        if _routing_tier(top, task) == "none":
            continue
        content = await _fetch_content(client, top.get("url", ""))
        if not content:
            warnings.append(f"Matched skill content could not be fetched or was not usable: {top.get('url')}")
            continue
        if _content_exceeds_budget(content):
            warnings.append(f"Matched skill content exceeded injection budget: {top.get('url')}")
            continue
        selected = _public_candidate(top, task)
        return {
            "found": True,
            "best_match": selected,
            "skill_content": content,
            "instructions": (
                "This is the single best-matching usable skill, already chosen for you. Apply "
                "skill_content's instructions immediately and generate the actual output the user "
                "asked for in this same turn. Only pause if skill_content is missing, unusable, or "
                "genuinely unsafe. Call install_skill with force=true only when you intend to "
                "overwrite an existing skill."
            ),
            "search_backend": result.get("search_backend"),
            "warnings": warnings,
        }

    return {
        "found": False,
        "message": "Matching skills were found, but none had enough confidence and usable SKILL.md content.",
        "candidates": [_public_candidate(c, task) for c in _candidate_pool(result, task)[:5]],
        "instructions": "Continue normally, or preview a specific source URL if you trust it.",
        "warnings": warnings,
        "search_backend": result.get("search_backend"),
    }


async def route_task_payload(task: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Return a universal routing decision for an agent task."""
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await route_task_payload(task, client=owned)

    route = await _route_selfhosted(client, task)
    if route is not None:
        return route

    try:
        result = await _search(client, task)
    except Exception as exc:
        return {
            "routed": False,
            "route_type": "none",
            "route_tier": "none",
            "task": task,
            "message": f"Skill database is unavailable right now ({exc}). Try again shortly.",
            "instructions": "Continue normally.",
            "warnings": [],
            "search_backend": None,
        }

    common = {
        "task": task,
        "search_backend": result.get("search_backend"),
        "warnings": list(result.get("warnings", [])),
    }
    if result.get("type") in {"none", "clarify"}:
        return {
            "routed": False,
            "route_type": "none",
            "route_tier": "none",
            "message": result.get("message", "No matching skill found."),
            "instructions": (
                "No reusable skill was selected. Continue normally, or call route_task again "
                "with a more specific task description if a reusable workflow likely exists."
            ),
            **common,
        }

    candidates = _candidate_pool(result, task)
    if not candidates:
        return {
            "routed": False,
            "route_type": "none",
            "route_tier": "none",
            "message": "No safe matching skill found.",
            "instructions": "Continue normally.",
            **common,
        }

    for candidate in candidates:
        tier = _routing_tier(candidate, task)
        selected = _public_candidate(candidate, task)
        if tier == "none":
            continue

        if tier == "hint":
            return {
                "routed": True,
                "route_type": "hint",
                "route_tier": "hint",
                "selected_skill": selected,
                "skill_content": "",
                "message": (
                    "A related skill exists, but confidence is medium. Treat this as a hint, "
                    "not active instructions."
                ),
                "instructions": (
                    "Mention or consider the selected skill only if it clearly helps. Do not inject "
                    "or follow full SKILL.md content for this task."
                ),
                "install_hint": (
                    "Preview the selected_skill.url before installing. Codex should treat this as "
                    "an in-turn suggestion only."
                ),
                **common,
            }

        content = await _fetch_content(client, candidate.get("url", ""))
        if not content:
            common["warnings"].append(
                f"Skipped matched skill because its SKILL.md content was missing or low quality: {candidate.get('url')}"
            )
            continue
        if _content_exceeds_budget(content):
            common["warnings"].append(
                f"Downgraded matched skill because its SKILL.md content exceeded the injection budget: {candidate.get('url')}"
            )
            return {
                "routed": True,
                "route_type": "hint",
                "route_tier": "hint",
                "selected_skill": {**selected, "routing_tier": "hint"},
                "skill_content": "",
                "message": "A matching skill exists, but full content exceeded the injection budget. Treat this as a hint.",
                "instructions": "Do not inject or follow full SKILL.md content for this task.",
                "install_hint": "Preview the selected_skill.url before installing.",
                **common,
            }

        return {
            "routed": True,
            "route_type": "skill",
            "route_tier": "full",
            "selected_skill": selected,
            "skill_content": content,
            "instructions": (
                "Use this as the routing result for the current task. Treat skill_content as active "
                "task-specific instructions, apply it immediately, and produce the user's requested "
                "output in this same turn. Do not ask the user to choose a skill unless the selected "
                "skill content is missing, unusable, or unsafe."
            ),
            "install_hint": (
                "For Claude-style clients, call install_skill with the selected_skill.url only if "
                "this workflow is worth keeping permanently. Codex should use this route in-turn."
            ),
            **common,
        }

    return {
        "routed": False,
        "route_type": "none",
        "route_tier": "none",
        "message": "Matching skills were found, but none were confident and usable enough to route.",
        "instructions": (
            "No reusable skill was selected. Continue normally, or call route_task again "
            "with a more specific task description if a reusable workflow likely exists."
        ),
        **common,
    }


async def record_route_feedback(
    route_id: str,
    outcome: str,
    *,
    source: str = CLIENT_NAME,
    note: str = "",
    client: httpx.AsyncClient | None = None,
) -> bool:
    """Best-effort local-host feedback for route outcome analytics."""
    route_id = (route_id or "").strip()
    outcome = (outcome or "").strip().lower()
    if not route_id or outcome not in {"used", "skipped", "installed", "failed", "dismissed"}:
        return False
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await record_route_feedback(route_id, outcome, source=source, note=note, client=owned)

    url = get_autoskill_url()
    if not url:
        return False
    try:
        response = await client.post(
            f"{url}/route-feedback",
            json={
                "route_id": route_id,
                "outcome": outcome,
                "source": source[:80],
                "note": note[:300],
            },
            timeout=5,
        )
        return response.status_code == 200
    except Exception:
        return False


def build_route_context(route_payload: dict[str, Any]) -> str:
    """Create compact context that a prompt hook can inject for an agent."""
    if not route_payload.get("routed"):
        return ""

    selected = route_payload.get("selected_skill") or {}
    content = route_payload.get("skill_content") or ""
    name = selected.get("name") or "unknown"
    url = selected.get("url") or ""
    risk = selected.get("risk_score")
    tier = route_payload.get("route_tier") or selected.get("routing_tier") or route_payload.get("route_type")
    score = selected.get("routing_score")
    risk_text = f", risk={risk}" if risk is not None else ""
    score_text = f", score={score}" if score is not None else ""
    if route_payload.get("route_type") == "hint":
        description = selected.get("description") or ""
        candidates = route_payload.get("candidates") if isinstance(route_payload.get("candidates"), list) else []
        option_lines = []
        for index, candidate in enumerate(candidates[:3], start=1):
            candidate_name = candidate.get("name") or "unknown"
            candidate_url = candidate.get("url") or ""
            candidate_description = (candidate.get("description") or "")[:160]
            option_lines.append(f"{index}. {candidate_name}: {candidate_description} Source: {candidate_url}")
        options_text = "\nCandidate options:\n" + "\n".join(option_lines) if option_lines else ""
        return (
            f"[auto-skill] Related skill hint: {name}{risk_text}{score_text}, tier={tier}. Source: {url}\n"
            f"Only use this as a hint if it clearly fits the user's task. Do not treat it as active instructions.\n"
            f"{description[:300]}"
            f"{options_text}"
        )
    return (
        f"[auto-skill] Route selected: {name}{risk_text}{score_text}, tier={tier}. Source: {url}\n\n"
        "Use the following SKILL.md content as active task-specific instructions for this turn. "
        "Apply it immediately unless it is missing, unusable, or unsafe.\n\n"
        "<auto_skill_content>\n"
        f"{content}\n"
        "</auto_skill_content>"
    )


async def route_prompt_payload(prompt: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Run the prompt preflight gate, then route skill-shaped prompts."""
    decision = should_route_prompt(prompt)
    if not decision["should_route"]:
        return {
            "should_route": False,
            "reason": decision["reason"],
            "routed": False,
            "route": None,
            "context": "",
        }

    route = await route_task_payload(prompt, client=client)
    return {
        "should_route": True,
        "reason": decision["reason"],
        "routed": route.get("routed", False),
        "route": route,
        "context": build_route_context(route),
    }


def install_skill_from_content(
    content: str,
    source_url: str,
    name: str = "",
    target: str = "claude",
    skills_home: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Install fetched skill content into the requested target."""
    if target != "claude":
        raise UnsupportedTargetError(
            "Codex does not support permanent Claude SKILL.md installs. "
            "Use the MCP route_task tool to apply full routes in the current Codex turn."
        )
    slug = _slugify(name or _extract_skill_name(content) or source_url.rstrip("/").split("/")[-1])
    home = Path(skills_home) if skills_home is not None else get_skills_home(target)
    dest_dir = home / slug
    dest_file = dest_dir / "SKILL.md"
    would_overwrite = dest_file.exists()
    if would_overwrite and not force and not dry_run:
        raise SkillAlreadyExistsError(dest_file)

    result = {
        "slug": slug,
        "target": target,
        "source_url": source_url,
        "dest_file": dest_file,
        "would_overwrite": would_overwrite,
        "dry_run": dry_run,
    }
    if dry_run:
        return result

    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file.write_text(content, encoding="utf-8")
    return result


async def install_skill_from_url(
    client: httpx.AsyncClient,
    url: str,
    name: str = "",
    target: str = "claude",
    skills_home: Path | None = None,
    force: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Fetch a skill URL and install it."""
    content = await _fetch_content(client, url)
    if not content:
        raise AutoSkillError(f"Could not fetch content for {url}")
    return install_skill_from_content(
        content,
        source_url=url,
        name=name,
        target=target,
        skills_home=skills_home,
        force=force,
        dry_run=dry_run,
    )
