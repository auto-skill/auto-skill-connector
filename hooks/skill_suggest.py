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
import time
import urllib.request
from pathlib import Path
from urllib.parse import quote

AUTOSKILL_URL = os.getenv("AUTOSKILL_URL", "https://skills.avalahome.com").rstrip("/")
ROUTING_LOG_PATH = Path(os.getenv("AUTOSKILL_ROUTING_LOG", "")) if os.getenv("AUTOSKILL_ROUTING_LOG") else Path.home() / ".claude" / "auto-skill-routing.jsonl"
MAX_LOG_LINES = 2000
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


def _log_routing_decision(prompt: str, tier: str, skill: dict | None = None, reason: str = "") -> None:
    """Append one JSONL record so a derailed session can be diagnosed later
    without needing to reproduce the exact prompt. Local-only, never
    transmitted; best-effort and never allowed to break routing itself."""
    try:
        record = {
            "at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tier": tier,
            "prompt_len": len(prompt),
            "prompt_snippet": prompt[:120],
        }
        if reason:
            record["reason"] = reason
        if skill:
            record["skill"] = {
                "name": skill.get("name"),
                "url": skill.get("url"),
                "risk_score": skill.get("risk_score"),
            }
        ROUTING_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with ROUTING_LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
        if ROUTING_LOG_PATH.stat().st_size > 2_000_000:  # ~2MB: cheap trim, rare path
            lines = ROUTING_LOG_PATH.read_text(encoding="utf-8", errors="replace").splitlines()[-MAX_LOG_LINES:]
            ROUTING_LOG_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")
    except Exception:
        pass


def main() -> None:
    payload = json.load(sys.stdin)
    prompt = (payload.get("prompt") or "").strip()
    should_route, _reason = _should_route(prompt)
    if not should_route:
        return

    # No Supabase fallback: it was frozen since 2026-07-05 (storage moved
    # local) and silently serving a stale corpus with no signal to the
    # caller was worse than admitting no route is available this turn. Fails
    # open by design -- an unreachable self-hosted server just means no
    # suggestion, not a stale one.
    found = _selfhosted_matches(prompt)
    if found is None:
        return
    matches, tier = found
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
        _log_routing_decision(prompt, "hint", skill, reason="multiple candidates plausible")
        return

    content = _fetch_content(url)
    if not content:
        _log_routing_decision(prompt, "none", skill, reason="content fetch failed or rejected (HTML/stub)")
        return
    if _is_unconfirmed_action_content(content):
        # Stopgap: this skill's body pairs an action verb (send/post/delete/...)
        # with explicit no-confirmation language. risk_score doesn't catch
        # this, so never silently inject it as active instructions -- surface
        # it as a hint and let a human/Claude decide with eyes open.
        _print_hint("looks like it takes an action without asking for confirmation")
        _log_routing_decision(prompt, "hint", skill, reason="unconfirmed-action content downgrade")
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
    _log_routing_decision(prompt, "full", skill)


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        main()
    except Exception:
        pass
