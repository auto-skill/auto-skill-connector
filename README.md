# auto-skill-connector

Find, preview, and install reusable AI agent skills before your agent rebuilds
them from scratch.

`auto-skill` searches a large scraped index of agent skills, MCP servers, and
plugins, auto-routes a task to the best matching `SKILL.md`, and lets an agent
use those instructions immediately through MCP or install them safely for
Claude-style skill clients.

## Why This Exists

Agents keep rediscovering the same workflows: spreadsheet generation, browser
testing, document formatting, security review, outreach writing, and hundreds
more. Static skill lists are useful, but they still make you browse, compare,
copy, and install by hand.

`auto-skill` is different:

- Route by task, not by repo name.
- Auto-pick one safe best match instead of making the user choose.
- Preview the actual skill instructions before installing.
- Install with overwrite protection.
- Use through MCP in any compatible agent.
- Keep permanent installs honest: Claude skills install to Claude; Codex uses
  MCP instructions in the current turn.

## Demo

```bash
auto-skill route "create an Excel report with formulas and charts"
auto-skill route-prompt "create an Excel report with formulas and charts" --context-only
auto-skill search "create an Excel report with formulas and charts"
auto-skill preview "create an Excel report with formulas and charts"
auto-skill install "https://github.com/example/skills/tree/main/xlsx" --target claude --dry-run
auto-skill doctor
```

Demo GIF/video: coming before the first public launch.

## Install

### CLI From This Repo

```bash
git clone https://github.com/neelavalareddy/auto-skill-connector
cd auto-skill-connector
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Then run:

```bash
auto-skill doctor
```

### Claude Code MCP

```bash
claude mcp add auto-skill --scope user -- uvx --from git+https://github.com/neelavalareddy/auto-skill-connector auto-skill-mcp
```

### Claude Desktop MCP

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "auto-skill": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/neelavalareddy/auto-skill-connector", "auto-skill-mcp"]
    }
  }
}
```

Restart Claude Desktop after editing the config.

## Commands

```bash
auto-skill route "<task>"
auto-skill route "<task>" --json
auto-skill route-prompt "<raw-user-prompt>"
auto-skill route-prompt "<raw-user-prompt>" --context-only
auto-skill search "<task>"
auto-skill preview "<task-or-url>"
auto-skill install "<task-or-url>" --target claude
auto-skill install "<task-or-url>" --target claude --yes
auto-skill install "<task-or-url>" --target claude --force
auto-skill doctor
```

Install safety defaults:

- Installs preview the destination and source before writing.
- Non-interactive installs require `--yes`.
- Existing skills are never overwritten unless `--force` is passed.
- `SKILLS_HOME` can override the Claude skill install directory.
- `AUTOSKILL_URL` can override or disable the self-hosted search endpoint.

## MCP Tools

The MCP server exposes four tools:

- `route_prompt(prompt)` is the always-on integration path: it skips prompts
  that are too short, meta/status-like, commands, or pasted context, then emits
  injectable skill context for real tasks.
- `route_task(task)` is the universal router contract: call it near the start
  of a user task, and it returns either one selected skill plus `skill_content`
  to apply immediately, or a no-route result.
- `recommend_skill(task)` searches for a matching skill and returns the full
  instructions for the single auto-picked best match.
- `install_skill(url, name?, target?, force?, dry_run?)` fetches and installs a
  Claude-style skill with overwrite protection.

Codex note: Codex can use `route_task` or `recommend_skill` through MCP and
follow the returned instructions in the current turn. This repo does not
pretend Claude `SKILL.md` folders are native Codex skills.

Universal routing note: MCP servers cannot intercept every prompt by
themselves. A client or agent still has to call `route_task`. The optional
Claude Code hook gets closer to always-on routing for Claude Code by injecting
selected skill content before the model answers. Other clients can call
`route_prompt` or `route_task` as their first step for skill-shaped tasks.

## Remote Connector

Claude Code/Desktop's config-file install above runs the server as a local
subprocess over stdio. To add it as a custom connector in claude.ai Settings >
Connectors, reachable from the browser, mobile app, or any device on your
account, it needs to run as an HTTP server with a public HTTPS URL because the
connection is made from Anthropic's servers, not your local machine.

```bash
git clone https://github.com/neelavalareddy/auto-skill-connector
cd auto-skill-connector
pip install -e .
MCP_TRANSPORT=streamable-http MCP_PORT=8765 python mcp_server.py
```

Then expose port 8765 publicly. No router or firewall changes are needed with
either option below.

Temporary with ngrok:

```bash
ngrok http 8765
```

Take the HTTPS URL it prints, append `/mcp`, and paste that into claude.ai >
Settings > Connectors > Add custom connector. On ngrok's free tier, the
hostname changes every time the tunnel restarts unless you claim a static
domain from ngrok's dashboard.

Permanent with Cloudflare Tunnel, if you already have a domain on Cloudflare:

```bash
cloudflared tunnel login
cloudflared tunnel create auto-skill
cloudflared tunnel route dns auto-skill mcp.yourdomain.com
```

Add an ingress rule to `~/.cloudflared/config.yml`:

```yaml
tunnel: <the tunnel ID printed above>
credentials-file: /path/to/<tunnel-id>.json
ingress:
  - hostname: mcp.yourdomain.com
    service: http://localhost:8765
  - service: http_status:404
```

Then run `cloudflared tunnel run auto-skill`, or install it as a service so it
survives reboots. The connector URL is now permanent:
`https://mcp.yourdomain.com/mcp`.

With a stable hostname, set `MCP_ALLOWED_HOSTS=mcp.yourdomain.com` to keep
DNS-rebinding Host-header protection enabled. If no allowed hosts are provided
for streamable HTTP mode, that protection is disabled so temporary tunnels can
work.

Security note: `install_skill` writes files on whichever machine is running
the server. This is fine for a personal connector on your own account, but do
not hand the URL to other people without adding your own access control.

## Hosted Claude Connector

For claude.ai custom connectors, use:

- Name: `Auto-Skill`
- Remote MCP server URL: `https://mcp.avalahome.com/mcp`

## Automatic Suggestions For Claude Code

The optional hook in `hooks/skill_suggest.py` can check each submitted prompt,
skip tiny/meta prompts, route real tasks, fetch the selected `SKILL.md`, and
inject it into Claude's context. This sends prompt snippets to the configured
search service, so read `SECURITY.md` before enabling it.

## How Search Works

Search tries these backends in order:

1. Self-hosted search at `AUTOSKILL_URL` for the freshest corpus.
2. Supabase Edge Function semantic search.
3. Supabase keyword RPC fallback.

If the self-hosted service is unavailable, the CLI and MCP payload include a
warning and continue with the fallback.

## Roadmap

The launch goal is public trust and GitHub stars, not immediate monetization.
See `ROADMAP.md` for the free/open-source direction and possible future paid
features.

## Security

This tool fetches and installs instructions that can shape agent behavior.
Read `SECURITY.md` before using auto suggestions or installing untrusted skills.
