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
    recommend_skill_payload,
    record_route_feedback,
    route_prompt_payload,
    route_task_payload,
)

__all__ = [
    "_fetch_content",
    "_install_skill_impl",
    "_raw_candidates",
    "_search",
    "_search_selfhosted",
    "_slugify",
    "main",
    "recommend_skill",
    "record_feedback",
    "route_prompt",
    "route_task",
]

# The `instructions` string is surfaced to the client model. Keep it honest:
# route first, then follow only high-confidence full routes.
mcp = FastMCP(
    "auto-skill",
    instructions=(
        "Auto-Skill routes user tasks to reusable AI agent skills when a good match exists. "
        "Use route_prompt for raw user messages and route_task for a cleaned-up task. These "
        "tools may return a full skill, a medium-confidence hint with candidate options, or "
        "no route. Follow full skill_content only when route_tier is full. Treat hint results "
        "as suggestions, not active instructions. Skip routing for pure questions, status/meta "
        "discussion, tiny acknowledgements, commands, or pasted context. recommend_skill is "
        "for explicit preview/recommendation flows; do not use it as the always-on router."
    ),
)


def _public_install_enabled() -> bool:
    if os.getenv("MCP_TRANSPORT", "stdio") != "streamable-http":
        return True
    return os.getenv("AUTO_SKILL_ENABLE_PUBLIC_INSTALL", "").lower() in {"1", "true", "yes"}


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
    help. Full routes include skill_content. Hint routes are suggestions only
    and may include candidate options.
    """
    return await route_task_payload(task)


@mcp.tool()
async def recommend_skill(task: str) -> dict:
    """Preview one usable skill candidate for an explicit recommendation flow.

    Prefer route_prompt or route_task for normal routing. This legacy preview
    tool fetches full SKILL.md content for inspection and does not mean the
    caller should automatically follow it.
    """
    return await recommend_skill_payload(task)


@mcp.tool()
async def record_feedback(route_id: str, outcome: str, note: str = "") -> dict:
    """Record privacy-safe outcome feedback for a previous route.

    Use only after a route result has actually been used, skipped, installed,
    dismissed, or failed. Do not include raw prompts in note.
    """
    ok = await record_route_feedback(route_id, outcome, source="auto-skill-mcp", note=note)
    return {"ok": ok, "route_id": route_id, "outcome": outcome}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    """Liveness check for uptime monitoring of the public streamable-http
    tunnel -- doesn't require an MCP session handshake, unlike the /mcp
    endpoint itself, so a plain curl/monitor can hit it directly."""
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True})


async def _install_skill_impl(
    url: str,
    name: str = "",
    target: str = "claude",
    force: bool = False,
    dry_run: bool = False,
) -> str:
    """Install a skill from a URL.

    Permanent installs are currently supported for Claude-style SKILL.md
    folders only. For Codex, call route_task and apply full routes in the
    current turn.
    """
    if not _public_install_enabled():
        return (
            "install_skill is disabled for streamable-http by default because it writes files "
            "on the server host. Run this MCP server over local stdio, or set "
            "AUTO_SKILL_ENABLE_PUBLIC_INSTALL=1 only behind your own access control."
        )

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

    install_skill writes files to whatever machine runs this process, so it is
    registered as a tool ONLY on stdio -- the transport used by a user's own
    local `claude mcp add`, where they are installing skills onto their own
    machine. The streamable-http transport is meant to be tunneled to a public
    URL (see README's remote-connector section), and MCP has no per-caller
    auth here, so publishing install_skill on it would let any caller with the
    URL write arbitrary skill files onto whoever is hosting the tunnel. Set
    AUTOSKILL_ALLOW_REMOTE_INSTALL=1 to override, e.g. behind your own auth
    proxy -- never set it on an unauthenticated public tunnel.
    """
    transport = os.getenv("MCP_TRANSPORT", "stdio")
    if transport != "streamable-http" or os.getenv("AUTOSKILL_ALLOW_REMOTE_INSTALL") == "1":
        mcp.tool(name="install_skill")(_install_skill_impl)

    if transport == "streamable-http":
        mcp.settings.host = os.getenv("MCP_HOST", "127.0.0.1")
        mcp.settings.port = int(os.getenv("MCP_PORT", "8765"))
        allowed = [h.strip() for h in os.getenv("MCP_ALLOWED_HOSTS", "").split(",") if h.strip()]
        if allowed:
            from mcp.server.transport_security import TransportSecuritySettings

            mcp.settings.transport_security = TransportSecuritySettings(
                allowed_hosts=allowed,
                allowed_origins=allowed,
            )
        else:
            from mcp.server.transport_security import TransportSecuritySettings

            mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
        mcp.run(transport="streamable-http")
    else:
        mcp.run()


if __name__ == "__main__":
    main()
