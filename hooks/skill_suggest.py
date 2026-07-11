"""Claude Code UserPromptSubmit hook for auto-skill routing.

The hook preflights each eligible prompt, calls the configured self-hosted
router, fetches high-confidence SKILL.md content, and prints injectable
context. It fails open: errors and timeouts never block a chat.

Security note: eligible prompts are sent to the configured routing backend.
Local diagnostics are disabled by default and never include prompt content.
Do not enable remote routing for sensitive conversations.
"""

from __future__ import annotations

import json
import hashlib
import os
import re
import sys
import time
import urllib.request
from pathlib import Path

AUTOSKILL_URL = os.getenv("AUTOSKILL_URL", "https://skills.autoskill.dev").rstrip("/")
CLIENT_NAME = "auto-skill-hook"
CLIENT_VERSION = "0.1.0"

# Cloudflare's bot protection on the hosted backend rejects urllib's default
# "Python-urllib/x.y" user agent outright (error 1010), which silently killed
# every call this hook made. Install a real product UA globally so all
# urlopen() call sites -- including the bare-URL content fetches -- send it.
_opener = urllib.request.build_opener()
_opener.addheaders = [("User-Agent", f"{CLIENT_NAME}/{CLIENT_VERSION}")]
urllib.request.install_opener(_opener)
ROUTING_LOG_PATH = Path(os.getenv("AUTOSKILL_ROUTING_LOG", "")) if os.getenv("AUTOSKILL_ROUTING_LOG") else Path.home() / ".claude" / "auto-skill-routing.jsonl"
MAX_LOG_LINES = 2000
TIMEOUT_SECONDS = float(os.getenv("AUTOSKILL_HOOK_TIMEOUT_SECONDS", "1.0"))
MAX_CONTENT_CHARS = int(os.getenv("AUTOSKILL_HOOK_MAX_CHARS", "12000"))
_BLOB_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)")
_TREE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)")
_ACK_PROMPTS = {"ok", "okay", "yes", "no", "thanks", "thank you", "continue", "go on", "do it", "sounds good"}
_META_PATTERNS = ("what did you", "what are you", "what is the current state", "current state", "whats the", "what's the", "why is", "why did", "remember th", "sounds good", "that worked", "looks good", "can you explain", "what you just")
_META_EXACT = {"status", "summarize", "explain this"}
_TRUTHY_VALUES = {"1", "true", "yes", "on"}
_GENERIC_DIAGNOSTIC_REASONS = {
    "empty prompt",
    "command prompt",
    "too short",
    "too long",
    "acknowledgement",
    "meta prompt",
    "route unavailable",
    "backend route none",
    "multiple candidates plausible",
    "content unavailable",
    "unsafe action downgrade",
    "content hash not verified",
    "selected",
}


def _served_content_digest(text: str) -> str:
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest() if text else ""


def _auth_headers() -> dict[str, str]:
    """Read the token `auto-skill login` stored (see auto_skill_auth.py). This
    file can't import that module -- it ships and runs standalone as a Claude
    Code hook -- so it re-reads the same credentials file directly."""
    override = os.getenv("AUTOSKILL_CREDENTIALS_PATH")
    path = Path(override) if override else Path.home() / ".autoskill" / "credentials.json"
    try:
        token = json.loads(path.read_text(encoding="utf-8")).get("token")
    except Exception:
        return {}
    return {"Authorization": f"Bearer {token}"} if token else {}


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
    if lowered in _META_EXACT or (any(pattern in lowered for pattern in _META_PATTERNS) and len(text) < 180):
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


def _selfhosted_route(prompt: str) -> dict | None:
    """Call the sole backend routing contract. Fail open on any error."""
    if not AUTOSKILL_URL:
        return None
    try:
        body = json.dumps(
            {
                "task": prompt[:500],
                "limit": 8,
                "client": CLIENT_NAME,
                "client_version": CLIENT_VERSION,
                "guard_mode": "hybrid",
                "supports_isolation": False,
                "max_inline_chars": 4000,
                "max_capsule_chars": 2400,
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{AUTOSKILL_URL}/route",
            data=body,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS) as r:
            if getattr(r, "status", 200) in {404, 405}:
                return None
            return json.load(r)
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


def _fetch_backend_content(content_url: str) -> str:
    target = content_url if content_url.startswith(("http://", "https://")) else f"{AUTOSKILL_URL}/{content_url.lstrip('/')}"
    try:
        with urllib.request.urlopen(target, timeout=TIMEOUT_SECONDS) as r:
            if getattr(r, "status", 200) == 200:
                text = r.read().decode("utf-8", errors="replace")
                if _looks_like_skill_content(text) and not _is_stub_content(text):
                    return text
    except Exception:
        return ""
    return ""


def _report_outcome(route: dict | None, outcome: str) -> None:
    """Attach this hook's local application decision (was a match actually
    shown/applied, downgraded, or rejected) to the route_events row /route
    already created for this call -- reuses the existing route-feedback
    contract instead of writing a second, duplicate row. Best-effort,
    never allowed to affect routing itself."""
    route_id = (route or {}).get("route_id")
    if not route_id or not AUTOSKILL_URL:
        return
    try:
        body = json.dumps(
            {"route_id": route_id, "outcome": outcome, "source": CLIENT_NAME}
        ).encode("utf-8")
        request = urllib.request.Request(
            f"{AUTOSKILL_URL}/route-feedback",
            data=body,
            headers={"Content-Type": "application/json", **_auth_headers()},
            method="POST",
        )
        urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS)
    except Exception:
        pass


def _diagnostics_enabled() -> bool:
    return os.getenv("AUTOSKILL_DIAGNOSTICS", "").strip().lower() in _TRUTHY_VALUES


def _scrub_legacy_diagnostics() -> None:
    """Best-effort removal of prompt fields written by pre-privacy hooks."""
    try:
        if not ROUTING_LOG_PATH.is_file():
            return
        raw = ROUTING_LOG_PATH.read_text(encoding="utf-8", errors="replace")
        forbidden = ("prompt_snippet", "prompt_text", "query_hash", "prompt")
        if not any(f'"{key}"' in raw for key in forbidden):
            return
        cleaned: list[str] = []
        for line in raw.splitlines()[-MAX_LOG_LINES:]:
            try:
                record = json.loads(line)
            except (TypeError, ValueError):
                continue
            if not isinstance(record, dict):
                continue
            for key in forbidden:
                record.pop(key, None)
            cleaned.append(json.dumps(record, ensure_ascii=False))
        temp_path = ROUTING_LOG_PATH.with_suffix(ROUTING_LOG_PATH.suffix + ".tmp")
        temp_path.write_text("\n".join(cleaned) + ("\n" if cleaned else ""), encoding="utf-8")
        temp_path.replace(ROUTING_LOG_PATH)
    except Exception:
        pass


def _log_routing_decision(prompt: str, tier: str, skill: dict | None = None, reason: str = "") -> None:
    """Append a metadata-only local diagnostic record when explicitly enabled.

    Prompt content and prompt-derived hashes are never written. Logging is
    best-effort and must never affect routing.
    """
    if not _diagnostics_enabled():
        return
    try:
        record = {
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "tier": tier,
            "prompt_len": len(prompt),
            "reason": reason if reason in _GENERIC_DIAGNOSTIC_REASONS else "routing decision",
        }
        if skill:
            record["selected_skill"] = {
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
    _scrub_legacy_diagnostics()
    should_route, gate_reason = _should_route(prompt)
    if not should_route:
        _log_routing_decision(prompt, "skipped", reason=gate_reason)
        return

    # No Supabase fallback: it was frozen since 2026-07-05 (storage moved
    # local) and silently serving a stale corpus with no signal to the
    # caller was worse than admitting no route is available this turn. Fails
    # open by design -- an unreachable self-hosted server just means no
    # suggestion, not a stale one.
    route = _selfhosted_route(prompt)
    if route is None:
        _log_routing_decision(prompt, "none", reason="route unavailable")
        return

    tier = str(route.get("tier") or "none").lower()
    skill = route.get("skill") or {}
    candidates = route.get("candidates") if isinstance(route.get("candidates"), list) else []
    matches = _safe_dedupe(([skill] if skill else []) + [c for c in candidates if isinstance(c, dict)])

    if not matches or tier == "none":
        _log_routing_decision(prompt, "none", None, reason="backend route none")
        return

    name = skill.get("name") or "unknown"
    url = skill.get("url") or skill.get("source_url") or ""
    risk = skill.get("risk_score")
    risk_text = f", risk={risk}" if risk is not None else ""
    metrics = route.get("score_debug", {}).get("metrics", {}) if isinstance(route, dict) else {}
    metric_parts = []
    for key, label in (
        ("latency_ms", "latency"),
        ("skill_find_ms", "skill_find"),
        ("injected_tokens", "injected_tokens"),
        ("response_tokens", "response_tokens"),
    ):
        value = metrics.get(key)
        if isinstance(value, (int, float)):
            suffix = "ms" if key.endswith("_ms") else ""
            metric_parts.append(f"{label}={int(value)}{suffix}")
    metrics_text = f" Route metrics: {', '.join(metric_parts)}." if metric_parts else ""

    def _print_hint(reason: str) -> None:
        desc = (skill.get("description") or "").replace("\n", " ")[:160]
        option_lines = []
        for index, candidate in enumerate(matches[:3], start=1):
            candidate_name = candidate.get("name") or "unknown"
            candidate_url = candidate.get("url") or candidate.get("source_url") or ""
            candidate_description = (candidate.get("description") or "").replace("\n", " ")[:120]
            option_lines.append(f"{index}. {candidate_name}: {candidate_description} ({candidate_url})")
        options_text = "\nCandidate options:\n" + "\n".join(option_lines) if option_lines else ""
        print(
            f"[auto-skill] Possible match (not injected -- {reason}): "
            f"\"{name}\"{risk_text} — {desc} ({url}). "
            "Choose a candidate only if the fit is obvious; otherwise continue normally. "
            "Apply content only when route_tier is full."
            f"{metrics_text}"
            f"{options_text}"
        )

    verification = skill.get("verification") if isinstance(skill.get("verification"), dict) else {}
    context_guard = route.get("context_guard") if isinstance(route.get("context_guard"), dict) else {}
    delivery = str(context_guard.get("delivery") or "full").lower()
    if tier == "full" and (
        verification.get("content_hash_verified") is not True
        or verification.get("static_instruction_only") is not True
    ):
        _print_hint("content was not verified as static and hash-pinned")
        _log_routing_decision(prompt, "hint", skill, reason="content hash not verified")
        _report_outcome(route, "shown")
        return

    if tier == "full" and delivery in {"capsule", "isolation"}:
        capsule = str(context_guard.get("capsule") or "")
        if not capsule or len(capsule) > 2400:
            _print_hint("bounded context capsule unavailable")
            _log_routing_decision(prompt, "hint", skill, reason="content unavailable")
            _report_outcome(route, "shown")
            return
        mode = "isolated fallback capsule" if delivery == "isolation" else "bounded capsule"
        print(
            f"[auto-skill] Route selected: {name}{risk_text}. Source: {url}\n\n"
            f"Use this {mode} as task-specific guidance for this turn. Do not install files or execute undeclared capabilities.\n\n"
            "<auto_skill_capsule>\n"
            f"{capsule}\n"
            "</auto_skill_capsule>"
            f"{metrics_text}"
        )
        _log_routing_decision(prompt, "full", skill, reason="selected")
        _report_outcome(route, "injected")
        return

    if tier == "hint":
        # Several candidates are plausible -- name the option instead of
        # committing to one skill's content, which would bias toward
        # whichever happened to rank first among near-ties.
        _print_hint("multiple candidates plausible")
        _log_routing_decision(prompt, "hint", skill, reason="multiple candidates plausible")
        _report_outcome(route, "shown")
        return

    content = route.get("content") or ""
    if not content and route.get("content_url"):
        content = _fetch_backend_content(str(route.get("content_url")))
    if not content:
        _log_routing_decision(prompt, "none", skill, reason="content unavailable")
        _report_outcome(route, "failed")
        return
    expected_digest = verification.get("content_digest")
    if expected_digest and _served_content_digest(content) != expected_digest:
        _print_hint("served content digest mismatch")
        _log_routing_decision(prompt, "hint", skill, reason="content digest mismatch")
        _report_outcome(route, "shown")
        return
    if _is_unconfirmed_action_content(content):
        # Stopgap: this skill's body pairs an action verb (send/post/delete/...)
        # with explicit no-confirmation language. risk_score doesn't catch
        # this, so never silently inject it as active instructions -- surface
        # it as a hint and let a human/Claude decide with eyes open.
        _print_hint("looks like it takes an action without asking for confirmation")
        _log_routing_decision(prompt, "hint", skill, reason="unsafe action downgrade")
        _report_outcome(route, "shown")
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
    _log_routing_decision(prompt, "full", skill, reason="selected")
    _report_outcome(route, "injected")


if __name__ == "__main__":
    try:
        if hasattr(sys.stdout, "reconfigure"):
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        main()
    except Exception:
        pass
