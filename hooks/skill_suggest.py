"""Claude Code UserPromptSubmit hook for auto-skill routing.

The hook preflights each eligible prompt, auto-picks the best safe skill, fetches
its SKILL.md content, and injects that content into Claude's context. It fails
open: errors and timeouts never block a chat.

Security note: eligible prompt snippets are sent to the configured search
backend. Do not enable this hook for sensitive conversations.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.request
from urllib.parse import quote

AUTOSKILL_URL = os.getenv("AUTOSKILL_URL", "https://skills.avalahome.com").rstrip("/")
SUPABASE_URL = "https://kgkuoxdizynkcrbasamu.supabase.co"
ANON_KEY = (
    "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
    "eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imtna3VveGRpenlua2NyYmFzYW11Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI4NzE4NzUsImV4cCI6MjA5ODQ0Nzg3NX0."
    "6rqfcqdVShb9fo3x5z9E6mf6f-0iUbJn9Q7hUFqZ-jw"
)
TIMEOUT_SECONDS = 3.0
MAX_CONTENT_CHARS = int(os.getenv("AUTOSKILL_HOOK_MAX_CHARS", "12000"))
_BLOB_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)")
_TREE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)")
_ACK_PROMPTS = {"ok", "okay", "yes", "no", "thanks", "thank you", "continue", "go on", "do it", "sounds good"}
_META_PATTERNS = ("what did you", "what are you", "what is the current state", "current state", "explain this", "summarize", "status", "whats the", "what's the", "why is", "why did", "remember th", "sounds good", "that worked", "looks good", "can you explain", "what you just")


def _should_route(prompt: str) -> tuple[bool, str]:
    text = " ".join(prompt.split())
    lowered = text.lower()
    if not text:
        return False, "empty prompt"
    if text.startswith(("/", "!")):
        return False, "command prompt"
    if len(text) < 12:
        return False, "too short"
    if len(text) > 3000:
        return False, "too long"
    if lowered in _ACK_PROMPTS:
        return False, "acknowledgement"
    if any(pattern in lowered for pattern in _META_PATTERNS) and len(text) < 180:
        return False, "meta prompt"
    return True, "skill-shaped prompt"


def _safe_dedupe(skills: list[dict]) -> list[dict]:
    seen: set[str] = set()
    result: list[dict] = []
    for skill in skills:
        if (skill.get("risk_score") or 0) >= 3:
            continue
        key = (skill.get("name") or skill.get("url") or "").lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(skill)
    return result


def _selfhosted_matches(prompt: str) -> tuple[list[dict], str] | None:
    """Returns (matches, tier) where tier is "full" (inject the whole skill),
    "hint" (name/url only -- several candidates are plausible), or "none"."""
    if not AUTOSKILL_URL:
        return None
    try:
        with urllib.request.urlopen(
            f"{AUTOSKILL_URL}/find-semantic?q={quote(prompt[:500])}&limit=8",
            timeout=TIMEOUT_SECONDS,
        ) as r:
            body = json.load(r)
            return _safe_dedupe(body.get("results") or []), body.get("tier", "none")
    except Exception:
        return None


def _edge_matches(prompt: str) -> tuple[list[dict], str]:
    """Same (matches, tier) contract as _selfhosted_matches: the edge
    function's own recommend/clarify split already maps onto full/hint."""
    req = urllib.request.Request(
        f"{SUPABASE_URL}/functions/v1/recommend-skill",
        data=json.dumps({"messages": [{"role": "user", "content": prompt[:500]}]}).encode(),
        headers={
            "apikey": ANON_KEY,
            "Authorization": f"Bearer {ANON_KEY}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as r:
        result = json.load(r)
    kind = result.get("type")
    if kind == "recommend" and result.get("skill"):
        return _safe_dedupe([result["skill"]]), "full"
    if kind == "clarify" and result.get("options"):
        return _safe_dedupe(result["options"]), "hint"
    return [], "none"


def _raw_candidates(url: str) -> list[str]:
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
    """Reject fetches that returned a web page (e.g. GitHub's HTML for a plain
    repo URL) instead of a skill document — HTML must never be injected into
    the model's context as instructions."""
    head = text.lstrip()[:300].lower()
    if head.startswith(("<!doctype", "<html", "<?xml")):
        return False
    if "<head>" in head or "githubassets.com" in head:
        return False
    return True


_FRONTMATTER_RE = re.compile(r"\A---\n.*?\n---\n", re.S)
_ABS_PATH_RE = re.compile(r"^\s*(?:[A-Za-z]:\\|/(?:home|Users|mnt|c|d)/|~[\\/])[^\n]*\s*$")
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
    """Stopgap (2026-07-07): risk_score only catches malware patterns, not
    skills that take real side effects while explicitly telling the agent
    not to confirm first. Observed live: a risk_score=0 skill auto-selected
    for full injection whose body said 'Send the message immediately -- do
    NOT ask for confirmation' and read a bot token from a secrets file. A
    match here downgrades a would-be full injection to a hint instead --
    never silent full injection of unconfirmed-side-effect instructions."""
    return bool(_ACTION_VERB_RE.search(text) and _NO_CONFIRM_RE.search(text))


def _is_stub_content(text: str) -> bool:
    """Reject skill bodies too thin to be real instructions -- e.g. a body
    that is just one absolute path from a stranger's machine. That kind of
    stub clears the HTML check and the similarity floor and still has
    nothing worth following."""
    body = _FRONTMATTER_RE.sub("", text, count=1).strip()
    if len(body) < MIN_STUB_BODY_CHARS:
        return True
    if _ABS_PATH_RE.match(body):
        return True
    return False


def _fetch_content(url: str) -> str:
    for candidate in _raw_candidates(url):
        try:
            with urllib.request.urlopen(candidate, timeout=TIMEOUT_SECONDS) as r:
                if getattr(r, "status", 200) == 200:
                    text = r.read().decode("utf-8", errors="replace")
                    if _looks_like_skill_content(text) and not _is_stub_content(text):
                        return text
        except Exception:
            continue
    return ""


def main() -> None:
    payload = json.load(sys.stdin)
    prompt = (payload.get("prompt") or "").strip()
    should_route, _reason = _should_route(prompt)
    if not should_route:
        return

    found = _selfhosted_matches(prompt)
    matches, tier = found if found is not None else _edge_matches(prompt)
    if not matches or tier == "none":
        return

    skill = matches[0]
    name = skill.get("name") or "unknown"
    url = skill.get("url") or ""
    risk = skill.get("risk_score")
    risk_text = f", risk={risk}" if risk is not None else ""

    def _print_hint(reason: str) -> None:
        desc = (skill.get("description") or "").replace("\n", " ")[:160]
        print(
            f"[auto-skill] Possible match (not injected -- {reason}): "
            f"\"{name}\"{risk_text} — {desc} ({url}). "
            "If this fits the user's task, call the auto-skill MCP tool recommend_skill "
            "with a short task description to fetch and apply its full content."
        )

    if tier == "hint":
        # Several candidates are plausible -- name the option instead of
        # committing to one skill's content, which would bias toward
        # whichever happened to rank first among near-ties.
        _print_hint("multiple candidates plausible")
        return

    content = _fetch_content(url)
    if not content:
        return
    if _is_unconfirmed_action_content(content):
        # Stopgap: this skill's body pairs an action verb (send/post/delete/...)
        # with explicit no-confirmation language. risk_score doesn't catch
        # this, so never silently inject it as active instructions -- surface
        # it as a hint and let a human/Claude decide with eyes open.
        _print_hint("looks like it takes an action without asking for confirmation")
        return
    if len(content) > MAX_CONTENT_CHARS:
        content = f"{content[:MAX_CONTENT_CHARS]}\n\n[auto-skill: truncated]"

    print(
        f"[auto-skill] Route selected: {name}{risk_text}. Source: {url}\n\n"
        "Use the following SKILL.md content as active task-specific instructions for this turn. "
        "Apply it immediately unless it is missing, unusable, or unsafe.\n\n"
        "<auto_skill_content>\n"
        f"{content}\n"
        "</auto_skill_content>"
    )


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        main()
    except Exception:
        pass
