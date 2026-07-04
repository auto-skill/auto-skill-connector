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
import re
from pathlib import Path

import httpx
from mcp.server.fastmcp import FastMCP

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


async def _search(client: httpx.AsyncClient, task: str) -> dict:
    """Primary path: the deployed edge function (embeds + hybrid search +
    recommend/clarify/none decision). Falls back to a plain keyword RPC
    (no embedding, lower recall but still useful) if the edge function is
    unavailable."""
    try:
        r = await client.post(
            f"{SUPABASE_URL}/functions/v1/recommend-skill",
            json={"messages": [{"role": "user", "content": task}]},
            headers=HEADERS,
            timeout=20,
        )
        if r.status_code == 200:
            return r.json()
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
    if len(candidates) == 1:
        return {"type": "recommend", "skill": candidates[0], "message": f"Best match: {candidates[0].get('name')}."}
    return {
        "type": "clarify",
        "message": "A few skills fit that about equally well — which is closest to what you're doing?",
        "options": candidates[:3],
    }


@mcp.tool()
async def recommend_skill(task: str) -> dict:
    """Search the auto-skill database (~200k scraped Claude skills, MCP
    servers, and plugins) for the one that best matches a task, and return
    its full SKILL.md content so it can be read and followed immediately.

    Call this whenever the user's request might already be covered by an
    existing packaged skill/MCP server, before building something from
    scratch. Pass a short, keyword-rich description of the task.
    """
    async with httpx.AsyncClient() as client:
        try:
            result = await _search(client, task)
        except Exception as e:
            return {"found": False, "message": f"Skill database is unavailable right now ({e}). Try again shortly."}

        if result.get("type") == "none":
            return {"found": False, "message": result.get("message", "No matching skill found.")}

        if result.get("type") == "clarify":
            options = result.get("options") or []
            return {
                "found": False,
                "message": result.get("message"),
                "candidates": [
                    {"name": o.get("name"), "description": (o.get("description") or "")[:150], "url": o.get("url")}
                    for o in options
                ],
                "instructions": "Ask the user to pick one of these, or call recommend_skill again with a more specific task.",
            }

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
                "Follow skill_content as if it were the active skill's instructions. "
                "If it genuinely fits, you can also call install_skill to save it permanently."
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
