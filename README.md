# auto-skill-connector

An MCP connector that gives Claude Code / Claude Desktop access to a database
of ~200k scraped Claude skills, MCP servers, and plugins. Instead of building
a capability from scratch, Claude can search this database mid-conversation,
read a matching skill's instructions, and follow them immediately — or
install it permanently as a real `/skill`.

It ships two tools:

- **`recommend_skill(task)`** — hybrid full-text + semantic search over the
  skill database. Returns the best match's full `SKILL.md` content (or a
  short list to pick from, if a few skills fit equally well).
- **`install_skill(url, name?)`** — downloads a skill's `SKILL.md` and saves
  it to `~/.claude/skills/<name>/SKILL.md`, so Claude Code can invoke it as a
  normal skill from then on, in any project.

Query embedding happens server-side (Supabase Edge Function), so this
connector only depends on `mcp` + `httpx` — no local ML runtime to install.

## Install

### Claude Code

```
claude mcp add auto-skill --scope user -- uvx --from git+https://github.com/neelavalareddy/auto-skill-connector auto-skill-mcp
```

### Claude Desktop

Add this to your `claude_desktop_config.json` (Settings → Developer, or find
it directly — on Windows it's usually under
`%APPDATA%\Claude\claude_desktop_config.json`):

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

Both require [`uv`](https://docs.astral.sh/uv/) installed (`uvx` ships with
it) — no cloning or manual `pip install` needed.

## Automatic skill suggestions (optional, Claude Code)

Want every chat message checked against the database automatically? Add the
included `hooks/skill_suggest.py` as a `UserPromptSubmit` hook: it runs on
each prompt you send, and when a skill matches, Claude is told to fetch and
apply it via `recommend_skill`. It fails open — errors and timeouts never
block or slow your chat.

1. Download [`hooks/skill_suggest.py`](hooks/skill_suggest.py) somewhere
   permanent (e.g. `~/.claude/hooks/skill_suggest.py`).
2. Merge this into `~/.claude/settings.json` (use an absolute path on
   Windows, e.g. `C:\\Users\\you\\.claude\\hooks\\skill_suggest.py`):

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python",
            "args": ["~/.claude/hooks/skill_suggest.py"],
            "timeout": 10
          }
        ]
      }
    ]
  }
}
```

## Remote connector (claude.ai Settings → Connectors)

Claude Code/Desktop's config-file install above runs the server as a local
subprocess (stdio). To add it as a **custom connector** in claude.ai's
Settings — reachable from the browser, mobile app, or any device on your
account — it needs to run as an HTTP server with a public HTTPS URL, since
that connection is made from Anthropic's servers, not your local machine.

```
git clone https://github.com/neelavalareddy/auto-skill-connector
cd auto-skill-connector
pip install -e .
MCP_TRANSPORT=streamable-http MCP_PORT=8765 python mcp_server.py
```

Then expose port 8765 publicly — no router/firewall changes needed either way:

**Quick and temporary — [ngrok](https://ngrok.com/):**

```
ngrok http 8765
```

Take the HTTPS URL it prints, append `/mcp`, and paste that into claude.ai →
Settings → Connectors → Add custom connector. On ngrok's free tier the
hostname changes every time the tunnel restarts, so you'll need to update the
connector URL each time (or claim a static free domain from ngrok's
dashboard).

**Permanent — [Cloudflare Tunnel](https://developers.cloudflare.com/cloudflare-one/connections/connect-networks/) (if you already have a domain on Cloudflare):**

```
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

Then `cloudflared tunnel run auto-skill` (or install it as a service so it
survives reboots). The connector URL is now permanent:
`https://mcp.yourdomain.com/mcp` — no updates needed as the tunnel restarts,
and DNS-rebinding Host-header protection can stay on since the hostname never
changes: set `MCP_ALLOWED_HOSTS=mcp.yourdomain.com`.

If `skills.avalahome.com` or another `*.avalahome.com` hostname shows up
anywhere in this repo, that's the maintainer's own instance set up exactly
this way — a live example of the pattern above, not a shared/public endpoint
you should rely on.

**Either way, one security note:** `install_skill` writes files on whichever
machine is running the server — fine for a personal connector on your own
account, but don't hand this URL to other people without adding your own
access control first (there's none built in).

## Running it directly

```
git clone https://github.com/neelavalareddy/auto-skill-connector
cd auto-skill-connector
pip install -e .
python mcp_server.py
```

## How it works

The skill database lives in Supabase (Postgres + pgvector), populated by a
separate scraper that continuously crawls GitHub, npm, and the MCP registry
for Claude skills. This repo only contains the read-only connector — search
queries hit a read-only anon key (RLS grants `SELECT` only; no writes are
possible with it).
