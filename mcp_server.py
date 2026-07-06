"""MCP connector for the auto-skill recommender.

Exposes a Supabase-backed database of ~200k scraped Claude skills, MCP
servers, and plugins as MCP tools, so Claude Code / Claude Desktop can look
up and use a matching skill mid-conversation instead of building one from
scratch.

Query embedding happens server-side in a Supabase Edge Function (gte-small
via Supabase.ai.Session), so this connector only needs `mcp` + `httpx` --
no local model/runtime required.

Tools:
  recommend_skill(task)      -> ranked candidates + the top match's full
                                 SKILL.md content (read it and follow it).
  install_skill(url, name?)  -> writes the skill's SKILL.md into
                                 ~/.claude/skills/<name>/SKILL.md so it
                                 becomes a real, permanently invocable
                                 Claude Code skill going forward.
"""
import json
import os
import re
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

# Self-hosted skill server (freshest corpus: historical + newly scraped skills,
# full hybrid search). Set AUTOSKILL_URL to override; when unreachable, searches
# fall back to the Supabase snapshot below automatically.
AUTOSKILL_URL = os.getenv("AUTOSKILL_URL", "https://skills.avalahome.com").rstrip("/")

SUPABASE_URL = "https://kgkuoxdizynkcrbasamu.supabase.co"
# Read-only anon key -- safe to ship publicly. RLS on this project grants
# anon SELECT only; all writes require a service_role key that is never
# shipped with this connector.
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6Imtna3VveGRpenlua2NyYmFzYW11Iiwicm9sZSI6ImFub24iLCJpYXQiOjE3ODI4NzE4NzUsImV4cCI6MjA5ODQ0Nzg3NX0.6rqfcqdVShb9fo3x5z9E6mf6f-0iUbJn9Q7hUFqZ-jw"
HEADERS = {
    "apikey": SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
    "Content-Type": "application/json",
}

SKILLS_HOME = Path.home() / ".claude" / "skills"
_BLOB_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/blob/([^/]+)/(.*)")
_TREE_RE = re.compile(r"github\.com/([^/]+)/([^/]+)/tree/([^/]+)/(.*)")

mcp = FastMCP("auto-skill")


def _raw_candidates(url: str) -> list[str]:
    """Candidate raw.githubusercontent.com URLs for a github.com url. A
    "blob" url points at an exact file; a "tree" url points at a directory
    (the skill folder), so SKILL.md is assumed to live directly inside it."""
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


async def _fetch_content(client: httpx.AsyncClient, url: str) -> str:
    for candidate in _raw_candidates(url):
        try:
            r = await client.get(candidate, timeout=10)
            if r.status_code == 200:
                return r.text
        except Exception:
            continue
    return ""


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "skill"


def _autopick(candidates: list[dict]) -> dict:
    """This connector always decides for itself instead of surfacing a menu --
    the caller (Claude) has the full task context, so it picks the top-ranked
    candidate and proceeds. `rank` is assumed to already reflect relevance;
    ties just take the first (highest-ranked) entry."""
    return {"type": "recommend", "skill": candidates[0], "message": f"Best match: {candidates[0].get('name')}."}


async def _search_selfhosted(client: httpx.AsyncClient, task: str) -> dict | None:
    """Hybrid search on the self-hosted server (freshest data). Returns None on
    any failure so callers fall through to the Supabase snapshot."""
    if not AUTOSKILL_URL:
        return None
    try:
        r = await client.get(
            f"{AUTOSKILL_URL}/find-semantic",
            params={"q": task, "limit": 8},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        results = [c for c in (r.json().get("results") or []) if (c.get("risk_score") or 0) < 3]
    except Exception:
        return None
    if not results:
        return None
    return _autopick(results)


async def _search(client: httpx.AsyncClient, task: str) -> dict:
    """Self-hosted server first (full, fresh corpus), then the deployed edge
    function (embeds + hybrid search + recommend/clarify/none decision), then
    a plain keyword RPC (no embedding, lower recall but still useful). Always
    resolves to a single pick -- see _autopick."""
    selfhosted = await _search_selfhosted(client, task)
    if selfhosted is not None:
        return selfhosted
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
                return _autopick(result["options"])
            return result
    except Exception:
        pass

    r = await client.post(
        f"{SUPABASE_URL}/rest/v1/rpc/search_skills",
        json={"query": task, "max_results": 8},
        headers=HEADERS,
        timeout=15,
    )
    r.raise_for_status()
    candidates = [c for c in r.json() if (c.get("risk_score") or 0) < 3]
    if not candidates:
        return {"type": "none", "message": "No matching skill found in the database."}
    return _autopick(candidates)


@mcp.tool()
async def recommend_skill(task: str) -> dict:
    """Search the auto-skill database (~200k scraped Claude skills, MCP
    servers, and plugins), automatically decide the single best match for a
    task, and return its full SKILL.md content so it can be applied right
    away -- no menu, no follow-up question.

    Call this whenever the user's request might already be covered by an
    existing packaged skill/MCP server, before building something from
    scratch. Pass a short, keyword-rich description of the task. Then follow
    skill_content immediately and produce the actual output the user asked
    for -- do not stop to ask the user to confirm or choose between options;
    this tool has already made that decision.
    """
    async with httpx.AsyncClient() as client:
        try:
            result = await _search(client, task)
        except Exception as e:
            return {"found": False, "message": f"Skill database is unavailable right now ({e}). Try again shortly."}

        if result.get("type") == "none":
            return {"found": False, "message": result.get("message", "No matching skill found.")}

        top = result.get("skill") or {}
        content = await _fetch_content(client, top.get("url", ""))
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
            "skill_content": content or "(content unavailable — fetch the url directly)",
            "instructions": (
                "This is the single best-matching skill, already chosen for you. Apply skill_content's "
                "instructions immediately and generate the actual output the user asked for in this same "
                "turn -- do not ask the user to pick a different one or confirm first. Only pause instead of "
                "applying it if skill_content itself is missing/unusable, or if it asks you to do something "
                "genuinely unsafe -- otherwise, use it. You can also call install_skill to save it permanently."
            ),
        }


@mcp.tool()
async def install_skill(url: str, name: str = "") -> str:
    """Download a skill's SKILL.md (by url, as returned from recommend_skill)
    and install it into ~/.claude/skills/<name>/SKILL.md so Claude Code can
    invoke it as a normal /skill from now on, in any project."""
    async with httpx.AsyncClient() as client:
        content = await _fetch_content(client, url)
    if not content:
        return f"Could not fetch content for {url}"

    m = re.search(r"^name:\s*(.+)$", content, re.MULTILINE)
    slug = _slugify(name or (m.group(1).strip() if m else url.rstrip("/").split("/")[-1]))

    dest_dir = SKILLS_HOME / slug
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_file = dest_dir / "SKILL.md"
    dest_file.write_text(content, encoding="utf-8")

    return f"Installed as '{slug}' at {dest_file}. Invoke it with the Skill tool (skill: \"{slug}\")."


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
