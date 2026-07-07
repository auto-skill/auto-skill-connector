"""Shared search, fetch, and install logic for auto-skill."""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import httpx

DEFAULT_AUTOSKILL_URL = "https://skills.avalahome.com"
SUPABASE_URL = "https://kgkuoxdizynkcrbasamu.supabase.co"
SUPABASE_ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imtna3VveGRpenlua2NyYmFzYW11Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI4NzE4NzUsImV4cCI6MjA5ODQ0Nzg3NX0."
    "6rqfcqdVShb9fo3x5z9E6mf6f-0iUbJn9Q7hUFqZ-jw"
)
HEADERS = {
    "apikey": SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type": "application/json",
}

_BLOB_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)")
_TREE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)")
_URL_RE = re.compile(r"^https?://", re.IGNORECASE)
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
            "For Codex, use the MCP recommend_skill tool and apply the returned instructions in-turn."
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
    return [url]


def _looks_like_skill_content(text: str) -> bool:
    """Reject fetches that returned a web page instead of a skill document.
    Plain repo URLs resolve to GitHub's HTML, which must never be injected
    into a model's context as instructions."""
    head = text.lstrip()[:300].lower()
    if head.startswith(("<!doctype", "<html", "<?xml")):
        return False
    if "<head>" in head or "githubassets.com" in head:
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


def _autopick(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    """Pick the top-ranked safe candidate instead of surfacing a menu."""
    return {
        "type": "recommend",
        "skill": candidates[0],
        "message": f"Best match: {candidates[0].get('name')}.",
    }


def _with_backend(result: dict[str, Any], backend: str, warnings: list[str]) -> dict[str, Any]:
    result = dict(result)
    result["search_backend"] = backend
    if warnings:
        result["warnings"] = warnings
    if result.get("options"):
        result["options"] = _safe_candidates(list(result.get("options") or []))
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
    return _autopick(results)


async def _search(
    client: httpx.AsyncClient | None,
    task: str,
    autoskill_url: str | None = None,
) -> dict[str, Any]:
    """Search self-hosted first, then Supabase semantic, then Supabase keyword."""
    if client is None:
        async with httpx.AsyncClient() as owned:
            return await _search(owned, task, autoskill_url=autoskill_url)

    warnings: list[str] = []
    selfhosted = await _search_selfhosted(client, task, autoskill_url=autoskill_url)
    if selfhosted is not None:
        return _with_backend(selfhosted, "self-hosted", warnings)

    configured_url = autoskill_url if autoskill_url is not None else get_autoskill_url()
    if configured_url:
        warnings.append(f"Self-hosted search did not return a usable result from {configured_url}; used fallback search.")

    try:
        r = await client.post(
            f"{SUPABASE_URL}/functions/v1/recommend-skill",
            json={"messages": [{"role": "user", "content": task}]},
            headers=HEADERS,
            timeout=20,
        )
        if r.status_code == 200:
            result = r.json()
            if result.get("type") == "clarify" and result.get("options"):
                safe_options = _safe_candidates(list(result["options"]))
                if not safe_options:
                    result = {"type": "none", "message": "No safe matching skill found in the database."}
                else:
                    result = _autopick(safe_options)
            return _with_backend(result, "supabase-edge", warnings)
    except Exception as exc:
        warnings.append(f"Supabase semantic fallback failed: {exc}")

    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/search_skills",
        json={"query": task, "max_results": 8},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    candidates = _safe_candidates(list(r.json()))
    if not candidates:
        return _with_backend(
            {"type": "none", "message": "No matching skill found in the database."},
            "supabase-keyword",
            warnings,
        )
    return _with_backend(_autopick(candidates), "supabase-keyword", warnings)


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

    top = result.get("skill") or {}
    content = await _fetch_content(client, top.get("url", ""))
    warnings = list(result.get("warnings", []))
    if not content:
        warnings.append("Matched skill content could not be fetched. Open the source URL directly.")
    return {
        "found": True,
        "best_match": {
            "name": top.get("name"),
            "description": top.get("description"),
            "url": top.get("url"),
            "source": top.get("source"),
            "stars": top.get("stars"),
            "risk_score": top.get("risk_score"),
        },
        "skill_content": content or "(content unavailable; fetch the URL directly)",
        "instructions": (
            "This is the single best-matching skill, already chosen for you. Apply skill_content's "
            "instructions immediately and generate the actual output the user asked for in this same turn. "
            "Only pause if skill_content is missing, unusable, or genuinely unsafe. "
            "Call install_skill with force=true only when you intend to overwrite an existing skill."
        ),
        "search_backend": result.get("search_backend"),
        "warnings": warnings,
    }


async def route_task_payload(task: str, client: httpx.AsyncClient | None = None) -> dict[str, Any]:
    """Return a universal routing decision for an agent task."""
    payload = await recommend_skill_payload(task, client=client)
    common = {
        "task": task,
        "search_backend": payload.get("search_backend"),
        "warnings": payload.get("warnings", []),
    }
    if not payload.get("found"):
        return {
            "routed": False,
            "route_type": "none",
            "message": payload.get("message", "No matching skill found."),
            "instructions": (
                "No reusable skill was selected. Continue normally, or call route_task again "
                "with a more specific task description if a reusable workflow likely exists."
            ),
            **common,
        }

    selected = payload.get("best_match") or {}
    return {
        "routed": True,
        "route_type": "skill",
        "selected_skill": selected,
        "skill_content": payload.get("skill_content", ""),
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


def build_route_context(route_payload: dict[str, Any]) -> str:
    """Create compact context that a prompt hook can inject for an agent."""
    if not route_payload.get("routed"):
        return (
            "[auto-skill] No reusable skill was selected for this prompt. "
            "Answer normally unless a more specific skill-shaped subtask appears."
        )

    selected = route_payload.get("selected_skill") or {}
    content = route_payload.get("skill_content") or ""
    name = selected.get("name") or "unknown"
    url = selected.get("url") or ""
    risk = selected.get("risk_score")
    risk_text = f", risk={risk}" if risk is not None else ""
    return (
        f"[auto-skill] Route selected: {name}{risk_text}. Source: {url}\n\n"
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
            "Use the MCP recommend_skill tool to apply a skill in the current Codex turn."
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
