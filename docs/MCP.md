# MCP Setup

Auto-Skill exposes an MCP server for clients that don't support the Auto Mode
prompt hook (Cursor, GitHub Copilot) and as an alternative to it for clients
that do (Claude Code, Codex).

## In-chat route card

When routing runs, clients show a homepage-shaped **AUTO-SKILL** markdown
card inside the agent transcript (not a separate floating window). The card
lists the ordered 2-slot plan (policy, then primary) and truthful
verification chips. MCP tools return that card **first**, then APPLY text,
then a compact JSON block the model uses for `skill_content` / capsules.

| Client | How the card appears |
| --- | --- |
| **Cursor** (first) | Connect Auto-Skill MCP. `route_task` tool results render the card in the agent sidebar. This repo ships `.cursor/rules/auto-skill-route-card.mdc` so the agent calls once and leaves the card visible. |
| **Claude Code** | Same MCP card, plus Auto Mode: `auto-skill enable-hook` injects the markdown card into the turn context. |
| **Codex CLI** | Same MCP card, plus `auto-skill enable-hook --target codex` (same hook script as Claude). |
| GitHub Copilot | MCP card only (no prompt-inject hook yet). |

This is text UI inside the chat/tool panel. Cursor and Claude do not allow
third-party HTML widgets inside the agent chrome.

## Claude Code local MCP

```bash
# From a local checkout with repository access:
python -m pip install -e .
claude mcp add auto-skill --scope user -- auto-skill-mcp
```

## Claude Desktop local MCP

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "auto-skill": {
      "command": "auto-skill-mcp",
      "args": []
    }
  }
}
```

Restart Claude Desktop after editing the configuration.

## GitHub Copilot CLI local MCP

Add this to `~/.copilot/mcp-config.json` (or run
`copilot mcp add auto-skill -- auto-skill-mcp`):

```json
{
  "mcpServers": {
    "auto-skill": {
      "type": "local",
      "command": "auto-skill-mcp",
      "args": [],
      "tools": ["*"]
    }
  }
}
```

The stdio server identifies the caller through the file-based CLI login, so
run `auto-skill login` first. Copilot CLI's support for the hosted OAuth
connector is untested; use the local stdio server with Copilot for now.

## Launch-facing tools

- `route_task(task)` classifies a cleaned-up, privacy-minimized task and
  returns an ordered `skill_plan` plus `full`, `hint`, or no-route delivery.
  Today's full coding plan is a verified policy plus a separate primary
  specialist (supporting slot deferred). Large instructions are reduced to
  bounded context capsules.
- `route_prompt(prompt)` locally preflights a raw prompt, then routes only
  when it is task-shaped.
- `record_feedback(route_id, outcome)` records an enum-only privacy-safe
  outcome; it accepts no free-form notes.

`recommend_skill` is a deprecated compatibility preview surface. New clients
should use `route_task`. MCP exposes no skill/filesystem write tool over
either local stdio or hosted streamable HTTP; enum-only route feedback is
optional.

Adding an MCP server opts into model-directed task-summary routing. Capable
clients are instructed to call `route_task` once for substantial tasks, but
MCP cannot force or invisibly intercept calls.

## Hosted read-only connector

The hosted streamable-HTTP connector is available at:

```text
https://mcp.autoskill.dev/mcp
```

It requires MCP OAuth and exposes routing/preview behavior only. It cannot
write skills onto a caller's computer.

## Self-hosting the connector

```bash
git clone https://github.com/auto-skill/auto-skill-connector
cd auto-skill-connector
pip install -e .
MCP_TRANSPORT=streamable-http MCP_PORT=8765 python mcp_server.py
```

Only expose it publicly after configuring authentication, HTTPS, and
`MCP_ALLOWED_HOSTS`. A temporary tunnel is useful for testing:

```bash
ngrok http 8765
```

For a stable Cloudflare Tunnel:

```bash
cloudflared tunnel login
cloudflared tunnel create auto-skill
cloudflared tunnel route dns auto-skill mcp.yourdomain.com
```

Example ingress configuration:

```yaml
tunnel: <tunnel-id>
credentials-file: /path/to/<tunnel-id>.json
ingress:
  - hostname: mcp.yourdomain.com
    service: http://localhost:8765
  - service: http_status:404
```

If the API and client run on the same LAN behind a Cloudflare Tunnel and
local DNS resolves the public hostname to a private address, point the
client at the loopback service instead:

```bash
AUTOSKILL_URL=http://localhost:<port>
```
