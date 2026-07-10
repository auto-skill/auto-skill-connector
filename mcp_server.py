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

_TRANSPORT = os.getenv("MCP_TRANSPORT", "stdio")

# On streamable-http, every caller shares this one server process, so the
# hosted connector needs real per-caller identity -- see mcp_oauth_provider.py
# and backend/mcp_oauth.py. On stdio (the local `claude mcp add` case) there's
# exactly one caller (whoever is running the process), already identified via
# the file-based CLI login in auto_skill_auth.py, so no MCP-level auth is set.
_auth_kwargs: dict = {}
if _TRANSPORT == "streamable-http":
    from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions

    from mcp_oauth_provider import (
        BackendOAuthProvider,
        get_loopback_backend_base_url,
        get_public_backend_base_url,
    )

    _issuer_url = os.getenv("MCP_ISSUER_URL", "https://mcp.autoskill.dev")
    _auth_kwargs = {
        "auth_server_provider": BackendOAuthProvider(
            get_public_backend_base_url(), get_loopback_backend_base_url()
        ),
        "auth": AuthSettings(
            issuer_url=_issuer_url,
            resource_server_url=_issuer_url,
            client_registration_options=ClientRegistrationOptions(
                enabled=True, valid_scopes=["route"], default_scopes=["route"]
            ),
            required_scopes=["route"],
        ),
    }

# The `instructions` string is surfaced to the client model. Keep it honest:
# route first, then follow only high-confidence full routes.
mcp = FastMCP(
    "auto-skill",
    instructions=(
        "Auto-Skill routes user tasks to reusable AI agent skills when a good match exists. "
        "Call route_prompt (raw user message) or route_task (cleaned-up task) as your first "
        "action on every task-shaped request -- including tasks you could complete yourself "
        "with your own general knowledge. Being able to do it yourself is not a reason to "
        "skip the check: the whole point of this connector is to check for a more current, "
        "specific, or reliable packaged skill before defaulting to your own approach. These "
        "tools may return a full skill, a medium-confidence hint with candidate options, or "
        "no route. Follow full skill_content only when route_tier is full. Treat hint results "
        "as suggestions, not active instructions. Skip routing only for pure questions, "
        "status/meta discussion, tiny acknowledgements, commands, or pasted context -- never "
        "skip it merely because you already know how to do the task. recommend_skill is for "
        "explicit preview/recommendation flows; do not use it as the always-on router."
    ),
    **_auth_kwargs,
)


def _public_install_enabled() -> bool:
    if _TRANSPORT != "streamable-http":
        return True
    return os.getenv("AUTO_SKILL_ENABLE_PUBLIC_INSTALL", "").lower() in {"1", "true", "yes"}


def _caller_auth_header() -> dict[str, str] | None:
    """The per-caller bearer token for the current MCP request, when this
    server is running as the hosted streamable-http connector. Threaded down
    to auto_skill_core instead of that module's default file-based
    auth_headers(), which would otherwise attribute every remote caller's
    activity to whichever account is logged in on this host."""
    if _TRANSPORT != "streamable-http":
        return None
    from mcp.server.auth.middleware.auth_context import get_access_token

    token = get_access_token()
    return {"Authorization": f"Bearer {token.token}"} if token else None


@mcp.tool()
async def route_prompt(prompt: str) -> dict:
    """Preflight a raw user prompt and route it whenever it is skill-shaped.

    Call this as your first action for task-shaped requests, even ones you
    could complete yourself -- already knowing how is not a reason to skip
    this call. It skips tiny acknowledgements, commands, status/meta prompts,
    and pasted walls of context, then routes real tasks to one reusable
    skill when possible.
    """
    return await route_prompt_payload(prompt, auth_header=_caller_auth_header())


@mcp.tool()
async def route_task(task: str) -> dict:
    """Route a user task to the best reusable skill when one exists.

    Call this at the very start of every task-shaped user request, before
    relying on your own general knowledge to solve it -- a packaged skill may
    be more current, specific, or reliable than your default approach. Full
    routes include skill_content. Hint routes are suggestions only and may
    include candidate options.
    """
    return await route_task_payload(task, auth_header=_caller_auth_header())


@mcp.tool()
async def recommend_skill(task: str) -> dict:
    """Preview one usable skill candidate for an explicit recommendation flow.

    Prefer route_prompt or route_task for normal routing. This legacy preview
    tool fetches full SKILL.md content for inspection and does not mean the
    caller should automatically follow it.
    """
    return await recommend_skill_payload(task, auth_header=_caller_auth_header())


@mcp.tool()
async def record_feedback(route_id: str, outcome: str, note: str = "") -> dict:
    """Record privacy-safe outcome feedback for a previous route.

    Use only after a route result has actually been used, skipped, installed,
    dismissed, or failed. Do not include raw prompts in note.
    """
    ok = await record_route_feedback(
        route_id, outcome, source="auto-skill-mcp", note=note, auth_header=_caller_auth_header()
    )
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
    URL (see README's remote-connector section) and does require callers to
    complete the MCP OAuth login (see mcp_oauth_provider.py), but that only
    establishes who is calling -- it still doesn't imply they should be able
    to write files on whoever is hosting the tunnel. Set
    AUTO_SKILL_ENABLE_PUBLIC_INSTALL=1 to override, e.g. behind your own access
    control -- never set it on an unauthenticated public tunnel. This is the
    same env var _public_install_enabled() checks at call time, so tool
    registration and the runtime gate can't drift out of sync with each other.
    """
    if _TRANSPORT != "streamable-http" or _public_install_enabled():
        mcp.tool(name="install_skill")(_install_skill_impl)

    if _TRANSPORT == "streamable-http":
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
