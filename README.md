# Auto-Skill

Trusted, compatible skill discovery and lifecycle foundations for Claude Code,
Codex, Cursor, and GitHub Copilot.

Auto-Skill finds portable Agent Skills, checks their structure and provenance,
routes an explicit task to a compatible `SKILL.md`, and supports careful local
installation. The launch product is deliberately not a claim to have the
largest scraped catalog. Existing registries already compete on breadth;
Auto-Skill is focused on integrity, compatibility, predictable routing, and a
safe path toward update, rollback, and team policy.

## Launch Scope

What ships now:

- Deterministic task ranking with quality and platform gates.
- Source provenance and normalized content hashes.
- Full in-turn use only for high-confidence, risk-0 public content whose
  canonical hash and returned raw digest verify; ambiguous matches return hints.
- Explicit search, route, preview, and manual persistent install commands.
- Native `SKILL.md` install targets for Claude Code, Codex, Cursor, and
  GitHub Copilot.
- An optional Claude Code prompt adapter for users who deliberately enable Auto
  Mode.
- A hosted MCP connector with no skill/filesystem writes; optional enum-only
  route feedback.
- Health, readiness, eval, smoke-test, backup, and deploy checks.
- No server-side retention of raw prompts or prompt snippets.

For an indexed public skill, `content-hash verified` means the local indexed
snapshot matches its canonical normalized hash and the served bytes carry a
separate raw SHA-256 digest. It does not query the current upstream revision,
prove publisher identity, guarantee safety, or replace review of an unfamiliar
skill.

Not shipped yet:

- Automatic persistent installation.
- A one-time publisher/permission trust policy.
- Publisher identity verification or signed releases.
- Managed updates, uninstall, or rollback.
- Auto Mode adapters for Codex or Cursor.
- Complete installation of skill bundles that require scripts, dependencies,
  references, or assets.

## Client Compatibility

Claude Code, Codex, Cursor, and GitHub Copilot all support Agent Skills built
around `SKILL.md`. Their native discovery locations differ:

| Client | Auto-Skill default user location | Launch behavior |
| --- | --- | --- |
| Claude Code | `~/.claude/skills` | Manual install; optional opt-in prompt adapter |
| Codex | `~/.agents/skills` | Manual install and explicit MCP/CLI routing |
| Cursor | `~/.agents/skills` | Manual install and explicit MCP/CLI routing |
| GitHub Copilot | `~/.agents/skills` | Manual install and explicit MCP routing |

Cursor also recognizes `~/.cursor/skills`, and Copilot also recognizes
`~/.copilot/skills`. Auto-Skill uses the portable `~/.agents/skills` location
by default for Codex, Cursor, and Copilot, so one installed copy serves all
three. `SKILLS_HOME` can override the selected destination.

Vendor references:

- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Codex skills](https://developers.openai.com/codex/skills)
- [Cursor Agent Skills](https://cursor.com/docs/skills)
- [Copilot agent skills](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills)

## Routing Modes

### On-demand mode (default)

Nothing intercepts prompts. A user or agent explicitly calls `search`,
`route`, `preview`, `route_prompt`, or the corresponding MCP tool. This is the
default for every client.

### Auto Mode (opt-in adapter)

Auto Mode is a client integration, not a property of MCP. The launch build
only includes a Claude Code `UserPromptSubmit` adapter. Enabling it allows the
adapter to inspect eligible prompts, call the route service, and add verified
task-specific context for the current turn. Tiny acknowledgements, commands,
meta/status prompts, and pasted context are filtered locally without a server
call.

Auto Mode never means automatic persistent installation. It can use
high-confidence, content-hash-verified, risk-0, static instructions in the current task
without a modal; ambiguous matches stay as candidate hints, and unverifiable
content is not injected. Normal client permissions still govern every tool,
script, network call, and side effect mentioned by those instructions.

MCP servers cannot invisibly intercept every prompt. Codex and Cursor can use
the explicit router today, but true Auto Mode for those clients requires a
separate, tested adapter and is P1 work.

## Repo Layout

This repository holds both halves of Auto-Skill:

- **Client** (repo root): `auto_skill_cli.py`, `mcp_server.py`, and `hooks/`.
  This is the package end users install; it talks to the hosted backend over
  HTTP.
- **Backend** (`backend/`): the FastAPI/SQLite service behind
  `skills.autoskill.dev`, including ingestion, embeddings, deterministic
  routing, content storage, and deploy scripts. It has its own run story and CI
  and is not part of the pip package.

The backend subtree tracks the standalone backend repository at
`https://github.com/auto-skill/auto-skill.git`. After syncing it, update
`.autoskill-backend-subtree.json` and run:

```bash
python scripts/check_backend_subtree.py --check-remote
```

The standalone backend workflow is intentionally kept at the connector
repository's top-level `.github/workflows/backend-ci.yml`, not under
`backend/.github/`.

## Demo

These example searches were run against the live index on 2026-07-07:

```text
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

Search results are candidates, not endorsements. A `risk=0` result only means
the current pattern scanner found no flagged indicator.

## Prerequisites

- Python 3.10+
- `git`, for cloning this repository or using the `uvx --from git+...` path
- [`uv`](https://docs.astral.sh/uv/getting-started/installation/) for the
  `uvx` MCP setup below

## Install the CLI

```bash
git clone https://github.com/auto-skill/auto-skill-connector
cd auto-skill-connector
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
```

Start in explicit mode:

```bash
auto-skill doctor
auto-skill search "create an excel report with formulas and charts"
auto-skill route "create an excel report with formulas and charts" --show-content
auto-skill preview "extract text and tables from a pdf"
```

No prompt hook is enabled by installation or by `doctor`.

## Commands

```bash
auto-skill search "<task>"
auto-skill route "<task>"
auto-skill route "<task>" --json --show-content
auto-skill route-prompt "<raw-user-prompt>"
auto-skill preview "<task-or-url>"
auto-skill install "<task-or-url>" --target claude --dry-run
auto-skill install "<task-or-url>" --target codex --dry-run
auto-skill install "<task-or-url>" --target cursor --dry-run
auto-skill feedback "<route-id>" used
auto-skill metrics --base-url http://127.0.0.1:8000
auto-skill doctor
auto-skill enable-hook
auto-skill disable-hook
```

`route-prompt` accepts raw prompt text and sends an eligible prompt to the
configured router. It is intended for an explicitly enabled adapter or manual
testing, not as a required first action for every task.

When the backend provides route metrics, `route` prints latency, skill-find
time, injected-token estimates, and response-token estimates. The aggregate
`/route-metrics` endpoint is host-local and is intended for operational smoke
checks, not a consumer analytics product.

## Manual Persistent Install

Persistent installation is a separate action from using a skill in the
current task:

```bash
auto-skill install "<task-or-url>" --target claude --dry-run
auto-skill install "<task-or-url>" --target codex --dry-run
auto-skill install "<task-or-url>" --target cursor --dry-run
```

After reviewing the source, destination, and content, repeat without
`--dry-run`. Installation safety defaults are:

- Interactive confirmation unless the user explicitly passes `--yes`.
- No overwrite unless the user explicitly passes `--force`.
- A visible source URL and destination before writing.
- No automatic install from Auto Mode or hosted MCP.

The launch installer is for static, instruction-only `SKILL.md` content. It
does not promise to fetch a complete bundle of scripts, references, assets, or
dependencies. Do not install an unfamiliar skill that relies on those
capabilities until they can be reviewed as a bundle. Managed update and
rollback are P1; back up an existing skill before using `--force`.

## MCP

### Claude Code local MCP

```bash
# From a local checkout with repository access:
python -m pip install -e .
claude mcp add auto-skill --scope user -- auto-skill-mcp
```

### Claude Desktop local MCP

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

### Launch-facing tools

- `route_task(task)` explicitly routes a cleaned-up task and returns `full`,
  `hint`, or no route. A full route may be delivered as a bounded context
  capsule when the selected skill is too large for the current client.
- `route_prompt(prompt)` locally preflights a raw prompt, then routes only when
  it is task-shaped.
- `record_feedback(route_id, outcome)` records an enum-only privacy-safe
  outcome; it accepts no free-form notes.

`recommend_skill` is a deprecated compatibility preview surface. New clients
should use `route_task`. The supported persistent-install flow is the explicit
local CLI. MCP exposes no skill/filesystem write tool over either local stdio or
hosted streamable HTTP; enum-only route feedback is optional.

Adding an MCP server makes these tools available. It does not cause a client to
call them on every prompt.

## Hosted Read-only Connector

The hosted streamable-HTTP connector is available at:

```text
https://mcp.autoskill.dev/mcp
```

It requires MCP OAuth and exposes routing/preview behavior only. It cannot
write skills onto a caller's computer.

For a self-hosted connector:

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

## Optional Claude Code Auto Mode Adapter

Enable the adapter only after reading `SECURITY.md`:

```bash
auto-skill enable-hook
```

The command shows a privacy notice and asks for confirmation. Once enabled,
eligible prompt text is sent to the configured route service so it can select
task-specific skill context. Neither the hosted backend nor the local routing
log retains raw prompt text or snippets. Operational events retain only
privacy-safe metadata such as prompt length, route tier, selected skill,
latency, client/version, and outcome.

Local diagnostics are off by default. Set `AUTOSKILL_DIAGNOSTICS=1` only when
you need a metadata-only routing log for troubleshooting. The current hook
removes legacy prompt fields from an older routing log on its next run; if you
have not upgraded the hook, delete `~/.claude/auto-skill-routing.jsonl`.

Anonymous usage analytics are also off by default. If you explicitly opt in by
setting `AUTOSKILL_ANONYMOUS_ANALYTICS=1`, the local adapter creates one random
installation UUID in `~/.autoskill/installation.json` and sends it with routes.
The backend stores only a one-way hash of that UUID, never the UUID, prompt,
IP address, or a machine fingerprint. Remove the file (or unset the variable)
to reset/stop anonymous tracking. Account authentication always takes priority
over the anonymous ID.

Disable the adapter at any time:

```bash
auto-skill disable-hook
```

There is no invisible prompt interceptor for Codex or Cursor. Use explicit
CLI/MCP routing on those clients until a tested client-specific adapter exists.
When a client cannot isolate a large skill, Auto-Skill falls back to a
deterministic capsule rather than silently injecting the full document.

## How Routing Works

Routing uses the service configured by `AUTOSKILL_URL`, defaulting to
`https://skills.autoskill.dev`. The backend performs local embedding retrieval
and deterministic reranking with lexical overlap, quality, platform mismatch,
provenance, evaluation/feedback evidence, and a log-scaled popularity prior.
Popularity is only a soft signal; relevance, verified static content, and
meaningfulness gates decide whether a route is full, a hint, or no route.
Routing does not install skills or write them to disk.

The response tiers are:

- `full`: a high-confidence, risk-0 public match whose local snapshot matches
  its canonical hash and served digest, and passes the static capability
  screen. Content is
  eligible for current-task use; normal client tool and permission controls
  still apply.
- `hint`: an ambiguous, unverified, risky, incomplete, platform-specific, or
  capability-bearing match. The response contains metadata and up to three
  candidates, not active instructions.
- `none`: no candidate cleared the routing floor.

If the service is unavailable, the client reports no route instead of silently
querying a stale fallback.

To disable hosted routing entirely:

```bash
AUTOSKILL_URL=
```

If the API and client run on the same LAN behind a Cloudflare Tunnel and local
DNS resolves the public hostname to a private address, point the client at the
loopback service instead:

```bash
AUTOSKILL_URL=http://localhost:<port>
```

## Launch Verification

Before a public launch or backend deploy, run:

```bash
python scripts/live_smoke.py
```

The smoke test checks remote MCP health, prompt preflight, a verified full
route, and a generic platform-trap case that must not misroute.

Backend operators should also use the readiness, eval, backup, and deployment
checks documented in `backend/README.md` and `backend/RUNBOOK.md`.

## Intentionally Deferred

The Ollama `/chat` recommender UI, broad web crawling as a product moat,
consumer run analytics, mandatory accounts for public discovery, public remote
installs, and `recommend_skill` are not launch priorities. Compatibility code
may remain temporarily, but these surfaces are not the product contract.

## Security

Skills are instructions that can shape agent behavior. Content-hash matching
detects drift; it does not establish trust. Read `SECURITY.md` before enabling
Auto Mode or installing an unfamiliar skill.
