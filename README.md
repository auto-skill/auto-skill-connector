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
- Auto-pick one safe best match, with full/hint/no-route confidence tiers.
- Preview the actual skill instructions before installing.
- Install with overwrite protection.
- Use through MCP in any compatible agent.
- Keep permanent installs honest: Claude skills install to Claude; Codex uses
  MCP instructions in the current turn.

## Demo

Three real tasks, run against the live index (2026-07-07) -- not placeholder
output:

```bash
$ auto-skill search "create an excel report with formulas and charts"
backend: self-hosted
best match:
1. xlsx-creator
   Create, edit, and analyze Excel spreadsheets (.xlsx, .xlsm, .csv, .tsv files)...
   stars=4, risk=0
   https://github.com/jignesh-ponamwar/skills-mcp/tree/HEAD/skill_mcp/skills_data/xlsx-creator

$ auto-skill search "extract text and tables from a pdf"
backend: self-hosted
best match:
1. pdf-text-extract
   Extract text and simple table-like rows from a PDF for downstream AI without OCR binaries...
   stars=0, risk=0
   https://github.com/baronguyen001/ai-automation-skills/tree/HEAD/skills/pdf-text-extract

$ auto-skill search "build a react landing page with tailwind"
backend: self-hosted
best match:
1. frontend-design
   Guidance for distinctive, intentional visual design when building new UI or reshaping an existing one...
   stars=11503, risk=0
   https://github.com/xiaomimimo/mimo-code/tree/HEAD/packages/opencode/src/skill/builtin/.bundle/frontend-design
```

Once a match looks right, apply it in-turn (`route`), read it first
(`preview`), or install it permanently (`install`):

```bash
auto-skill route "create an excel report with formulas and charts" --show-content
auto-skill preview "extract text and tables from a pdf"
auto-skill install "build a react landing page with tailwind" --target claude --dry-run
auto-skill doctor
```

Demo GIF/video: not yet recorded -- contributions welcome.

## Prerequisites

- Python 3.10+
- `git`, for cloning this repo or the `uvx --from git+...` install path below
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) if you use
  the `claude mcp add ... uvx --from ...` install command -- it's what
  resolves and runs the package without a manual clone. Not needed for the
  "CLI From This Repo" path, which uses a plain venv instead.

## Install

### CLI From This Repo

```bash
git clone https://github.com/auto-skill/auto-skill-connector
cd auto-skill-connector
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Then run:

```bash
auto-skill doctor
auto-skill enable-hook
auto-skill doctor
```

`doctor` checks that `python` resolves on PATH, that the self-hosted search
backend is reachable (with round-trip latency), and whether the routing hook
is registered. `enable-hook` writes (or repoints) the `UserPromptSubmit` hook
entry in Claude Code's `settings.json`, after showing a privacy note --
prompt snippets are sent to the configured search backend, so read
`SECURITY.md` first if that matters for your conversations. Run
`auto-skill disable-hook` to remove it again. Once enabled, just type a task
in Claude Code -- no need to mention this connector by name.

### Claude Code MCP

```bash
claude mcp add auto-skill --scope user -- uvx --from git+https://github.com/auto-skill/auto-skill-connector auto-skill-mcp
```

### Claude Desktop MCP

Add this to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "auto-skill": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/auto-skill/auto-skill-connector", "auto-skill-mcp"]
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
auto-skill enable-hook
auto-skill enable-hook --yes
auto-skill disable-hook
```

Install safety defaults:

- Installs preview the destination and source before writing.
- Non-interactive installs require `--yes`.
- Existing skills are never overwritten unless `--force` is passed.
- `SKILLS_HOME` can override the Claude skill install directory.
- `AUTOSKILL_URL` can override or disable the self-hosted route/search endpoint.

**Self-hosting on the same LAN as your Cloudflare Tunnel:** if you run the
skills server and the connector on the same machine that hosts the tunnel,
your own router/DNS may resolve the public hostname (e.g.
`skills.avalahome.com`) to a private LAN address instead of Cloudflare's edge
-- a router-level DNS override or split-horizon DNS setup, common on home
routers, will do this even though the hostname resolves correctly for
everyone else. If requests to your own public URL fail only from that
machine, set `AUTOSKILL_URL=http://localhost:<port>` (or whatever loopback
address the skills server binds to) so the connector talks to it directly
instead of round-tripping through DNS and the tunnel.

## MCP Tools

- `route_prompt(prompt)` is the always-on integration path: it skips prompts
  that are too short, meta/status-like, commands, or pasted context, then emits
  full skill context only for high-confidence real tasks. Medium-confidence
  matches return a short hint instead of full instructions.
- `route_task(task)` is the universal router contract: call it near the start
  of a user task, and it returns one of three results: `route_tier=full` with
  `skill_content`, `route_tier=hint` with a source suggestion, or no route.
- `recommend_skill(task)` searches for a matching skill and returns the full
  instructions for the single best usable match. Use this for explicit preview
  or recommendation flows, not as the always-on router.
- `record_feedback(route_id, outcome, note?)` records privacy-safe route
  outcome feedback after a route is used, skipped, installed, dismissed, or
  fails. Do not include raw prompts in notes.
- `install_skill(url, name?, target?, force?, dry_run?)` fetches and installs a
  Claude-style skill with overwrite protection. **Only registered on stdio**
  (`claude mcp add` / Claude Desktop's local subprocess config) -- it writes
  files to whatever machine runs the server, and the streamable-http transport
  has no per-caller auth, so it's omitted there by default. See the Remote
  Connector section below.

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
git clone https://github.com/auto-skill/auto-skill-connector
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
the server, so it is **not** registered as a tool over streamable-http by
default -- a caller with your tunnel URL gets `route_prompt`/`route_task`/
`recommend_skill` only. Set `AUTOSKILL_ALLOW_REMOTE_INSTALL=1` to re-enable it
remotely, but only if you've added your own auth in front of the tunnel;
never on an unauthenticated public URL.

## Hosted Claude Connector

For claude.ai custom connectors, use:

- Name: `Auto-Skill`
- Remote MCP server URL: `https://mcp.avalahome.com/mcp`

## Automatic Suggestions For Claude Code

The optional hook in `hooks/skill_suggest.py` can check each submitted prompt,
skip tiny/meta prompts, call the backend `/route` contract for real tasks, and
inject selected `SKILL.md` content only for full routes. Run
`auto-skill enable-hook` to turn it on (see [Commands](#commands) above) -- it
prints a privacy note before doing anything, since eligible prompt snippets
are sent to the configured route service. Read `SECURITY.md` before enabling it
for sensitive conversations.
Every routing decision it makes is logged locally to
`~/.claude/auto-skill-routing.jsonl` so you can review what got suggested and
why.

## How Routing Works

Routing uses the self-hosted server at `AUTOSKILL_URL` only. `route_task` and
the Claude Code hook call `/route` first so the backend owns quality gates,
platform-trap handling, and full/hint/no-route tiers. `/find-semantic` remains
as a compatibility fallback for older self-hosted backends.

Before a public launch or backend deploy, run the live smoke test from a source
checkout:

```bash
python scripts/live_smoke.py
```

It verifies prompt preflight, a full spreadsheet route with content, and the
generic landing-page trap that must not route to Landingi.

There is no Supabase fallback: an earlier version of this connector fell back
to a Supabase-hosted corpus that was frozen once storage moved local, silently
serving stale results with no signal that they weren't fresh. If the
self-hosted service is unavailable, the CLI and MCP payload report no route
found instead -- one truthful backend beats two that can silently disagree.

## Roadmap

The launch goal is public trust and GitHub stars, not immediate monetization.
See `ROADMAP.md` for the free/open-source direction and possible future paid
features.

## Security

This tool fetches and installs instructions that can shape agent behavior.
Read `SECURITY.md` before using auto suggestions or installing untrusted skills.
