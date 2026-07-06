"""MCP server for finding, previewing, and installing AI agent skills."""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

from auto_skill_core import (
    AutoSkillError,
    SkillAlreadyExistsError,
    UnsupportedTargetError,
    _fetch_content,
    _raw_candidates,
    _search,
    _search_selfhosted,
    _slugify,
    install_skill_from_url,
    route_prompt_payload,
    recommend_skill_payload,
    route_task_payload,
)

__all__ = [
    "_fetch_content",
    "_raw_candidates",
    "_search",
    "_search_selfhosted",
    "_slugify",
    "install_skill",
    "main",
    "recommend_skill",
    "route_prompt",
    "route_task",
]

mcp = FastMCP("auto-skill")


@mcp.tool()
async def route_prompt(prompt: str) -> dict:
    """Preflight a raw user prompt and route it only when it is skill-shaped.

    Use this for always-on integrations. It skips tiny acknowledgements,
    commands, status/meta prompts, and pasted walls of context, then routes
    real tasks to one reusable skill when possible.
    """
    return await route_prompt_payload(prompt)


@mcp.tool()
async def route_task(task: str) -> dict:
    """Route a user task to the best reusable skill when one exists.

    Call this near the start of a user request when a packaged workflow might
    help. The response auto-picks one safe skill and returns instructions for
    applying it immediately in the current turn.
    """
    return await route_task_payload(task)


@mcp.tool()
async def recommend_skill(task: str) -> dict:
    """Search for the single best reusable skill for a task.

    The connector auto-picks the top-ranked safe match, fetches its full
    SKILL.md content, and returns instructions for applying it immediately.
    """
    return await recommend_skill_payload(task)


@mcp.tool()
async def install_skill(
    url: str,
    name: str = "",
    target: str = "claude",
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Install a skill from a URL.

    Permanent installs are currently supported for Claude-style SKILL.md
    folders only. For Codex, call recommend_skill and apply the returned
    instructions in the current turn.
    """
    async with httpx.AsyncClient() as client:
        try:
            result = await install_skill_from_url(
                client,
                url=url,
                name=name,
                target=target,
                force=force,
                dry_run=dry_run,
            )
        except SkillAlreadyExistsError as exc:
            return f"Refusing to overwrite existing skill at {exc.dest_file}. Call install_skill with force=true to replace it."
        except UnsupportedTargetError as exc:
            return str(exc)
        except AutoSkillError as exc:
            return str(exc)

    action = "Would install" if dry_run else "Installed"
    overwrite = " replacing an existing skill" if result["would_overwrite"] else ""
    return f"{action} '{result['slug']}'{overwrite} from {result['source_url']} to {result['dest_file']}."


def main() -> None:
    """Transport is chosen at launch time, not baked into the package:
      stdio (default)   -- for Claude Code / Desktop's local config (`claude mcp add`).
      streamable-http    -- for a remote connector added via claude.ai Settings >
                             Connectors, typically tunneled (e.g. ngrok) to a
                             public HTTPS URL. Set MCP_TRANSPORT=streamable-http,
                             and MCP_HOST/MCP_PORT to control the bind address.
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport == "streamable-http":
        mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8765"))
        # Behind a tunnel (e.g. ngrok) the public hostname changes on every
        # restart on the free tier, so DNS-rebinding Host-header checks would
        # need updating each time too. Set MCP_ALLOWED_HOSTS (comma-separated)
        # once you have a stable domain to re-enable that protection.
        allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if allowed:
            from mcp.server.transport_security import TransportSecuritySettings
            mcp.settings.transport_security = TransportSecuritySettings(allowed_hosts=allowed, allowed_origins=allowed)
        else:
            from mcp.server.transport_security import TransportSecuritySettings
            mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
