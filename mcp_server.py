"""Read-only MCP server for finding and routing portable Agent Skills."""

from __future__ import annotations

import os

import httpx
from mcp.server.fastmcp import FastMCP

from auto_skill_core import (
    _fetch_content,
    _raw_candidates,
    _search,
    _search_selfhosted,
    _slugify,
    recommend_skill_payload,
    record_route_feedback,
    route_prompt_payload,
    route_task_payload,
)

__all__ = [
    "_fetch_content",
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

# The `instructions` string is surfaced to the client model. MCP does not
# intercept prompts, so routing is explicit unless a user separately enables a
# client-specific adapter.
mcp = FastMCP(
    "auto-skill",
    instructions=(
        "Auto-Skill is an explicit discovery and routing connector for portable "
        "Agent Skills. Call route_task only when the user asks to find/use a skill or has opted "
        "into a client-specific Auto Mode adapter. route_prompt accepts a raw message for that "
        "same explicit flow; task text is sent to the configured router but is not retained. "
        "A full result is content-hash verified and may be used for the current task. Treat hint "
        "results as 2-3 candidates, not active instructions. recommend_skill is a deprecated "
        "preview compatibility tool. MCP exposes no skill/filesystem write tool; its optional "
        "record_feedback call stores only enum outcome metadata. MCP alone cannot intercept every prompt."
    ),
    **_auth_kwargs,
)


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
    """Explicitly preflight and route a raw user prompt.

    Use only when the user requested skill routing or opted into a client
    adapter. Local preflight skips acknowledgements, commands, meta prompts,
    and pasted context without sending them to the server.
    """
    return await route_prompt_payload(prompt, auth_header=_caller_auth_header())


@mcp.tool()
async def route_task(task: str) -> dict:
    """Explicitly route a task to the best reusable skill when one exists.

    Call when requested by the user or an explicitly enabled adapter. Full
    routes include hash-verified skill_content for current-task use. Hint
    routes are suggestions only and include up to three candidates.
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
async def record_feedback(route_id: str, outcome: str) -> dict:
    """Record privacy-safe outcome feedback for a previous route.

    Use only after a route result has actually been used, skipped, installed,
    dismissed, or failed. The contract is enum-only and accepts no free-form
    note so feedback cannot become a prompt-retention side channel.
    """
    ok = await record_route_feedback(
        route_id, outcome, source="auto-skill-mcp", auth_header=_caller_auth_header()
    )
    return {"ok": ok, "route_id": route_id, "outcome": outcome}


@mcp.custom_route("/healthz", methods=["GET"])
async def healthz(request):
    """Liveness check for uptime monitoring of the public streamable-http
    tunnel -- doesn't require an MCP session handshake, unlike the /mcp
    endpoint itself, so a plain curl/monitor can hit it directly."""
    from starlette.responses import JSONResponse
    return JSONResponse({"ok": True})


def main() -> None:
    """Transport is chosen at launch time, not baked into the package:
      stdio (default)   -- for Claude Code / Desktop's local config (`claude mcp add`).
      streamable-http    -- for a remote connector added via claude.ai Settings >
                             Connectors, typically tunneled (e.g. ngrok) to a
                             public HTTPS URL. Set MCP_TRANSPORT=streamable-http,
                             and MCP_HOST/MCP_PORT to control the bind address.

    Both transports expose no skill/filesystem writes. Persistent installation is deliberately an
    explicit local CLI flow until a user-selected trust policy, permission
    inspection, and rollback exist.
    """
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
