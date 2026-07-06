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

Then expose port 8765 publicly — a tunnel (e.g. `ngrok http 8765`) is the
simplest way, with no router/firewall changes needed. Take the HTTPS URL it
gives you, append `/mcp`, and paste that into claude.ai → Settings →
Connectors → Add custom connector.

Notes:
- On ngrok's free tier the hostname changes on every restart, so you'll need
  to update the connector URL each time unless you claim a static domain.
- DNS-rebinding Host-header protection is disabled by default in this mode
  for that reason. Once you have a stable hostname, set `MCP_ALLOWED_HOSTS`
  (comma-separated) to re-enable it.
- `install_skill` writes files on whichever machine is running the server —
  fine for a personal connector on your own account, but don't expose this
  publicly to other people without adding your own access control first.

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
