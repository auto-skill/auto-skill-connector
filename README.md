# Auto-Skill

Just-in-time skill orchestration for Claude Code, Codex, Cursor, and GitHub
Copilot. No skill installation required.

Auto-Skill classifies a privacy-minimized task, chooses an ordered skill plan,
fetches verified instructions from its hosted catalog, and supplies only the
bounded context needed for that turn. A plan can combine an always-on policy
with a task specialist—for example, Ponytail for minimal safe code plus
frontend-design for a React interface—without making either skill compete for
the same ranking slot.

Auto-Skill is not another skill directory. Its product is the universal
router: deciding which guidance matters now, composing compatible skills, and
delivering them safely at runtime.

## Launch Scope

What ships now:

- Deterministic task-family classification, role-aware ranking, and platform
  gates.
- Ordered multi-skill plans: task-family policies first, one primary
  specialist, then bounded supporting skills as the plan contract expands.
- A curated Ponytail coding-policy lane. Policy skills cannot self-promote into
  this lane through semantic similarity alone.
- Integration skills require an explicit integration, service, or platform
  signal; a generic integration listing cannot displace a coding specialist.
- Source provenance and normalized content hashes.
- Full in-turn use only for high-confidence, risk-0 public content whose
  canonical hash and returned raw digest verify; ambiguous matches return hints.
- Explicit search, route, and preview commands for inspection and testing.
- An optional Claude Code prompt adapter for users who deliberately enable Auto
  Mode.
- A hosted MCP connector that asks capable clients to preflight substantial
  tasks proactively using concise summaries, with no skill/filesystem writes;
  optional enum-only route feedback.
- Health, readiness, eval, smoke-test, backup, and deploy checks.
- No server-side retention of raw prompts or prompt snippets.

For an indexed public skill, `content-hash verified` means the local indexed
snapshot matches its canonical normalized hash and the served bytes carry a
separate raw SHA-256 digest. It does not query the current upstream revision,
prove publisher identity, guarantee safety, or replace review of an unfamiliar
skill.

Not shipped yet:

- Supporting-skill selection beyond the current policy + primary-specialist
  slice.
- A one-time publisher/permission trust policy.
- Publisher identity verification or signed releases.
- Managed local skill installation, updates, uninstall, or rollback. These are
  unnecessary for normal just-in-time routing.
- Auto Mode adapters for Codex or Cursor.
- Complete installation of skill bundles that require scripts, dependencies,
  references, or assets.

## Client Compatibility

Auto-Skill routes to clients through a connector or adapter; it does not copy
catalog skills into each client's native skill directory.

| Client | Current delivery path | Skill install required? |
| --- | --- | --- |
| Claude Code | Hosted/local MCP; optional prompt adapter | No |
| Codex | Hosted/local MCP proactive task-summary routing | No |
| Cursor | Hosted/local MCP proactive task-summary routing | No |
| GitHub Copilot | Local MCP proactive task-summary routing | No |

Repository-owned skills and always-on instruction files still take precedence.
That lets teams keep their non-negotiable standards local while Auto-Skill
adds verified policies and specialists just in time.

Vendor references:

- [Claude Code skills](https://code.claude.com/docs/en/skills)
- [Codex skills](https://developers.openai.com/codex/skills)
- [Cursor Agent Skills](https://cursor.com/docs/skills)
- [Copilot agent skills](https://docs.github.com/en/copilot/concepts/agents/about-agent-skills)

## Routing Modes

### CLI on-demand mode

Nothing intercepts prompts. A user or agent explicitly calls `search`,
`route`, `preview`, or `route_prompt`.

### Connected MCP proactive routing

Adding the MCP connector deliberately makes its routing tools available. Its
server instructions ask capable client models to call the read-only
`route_task` once near the start of substantial work, without waiting for the
user to ask for a skill. The model must send a concise task summary and omit
secrets, personal data, pasted content, and irrelevant conversation history.
This improves recall but is not a prompt interceptor, so a client may still
ignore the instruction.

The routing tools advertise read-only, non-destructive, idempotent semantics.
Auto-Skill itself does not require per-task approval and never executes or
installs anything, although a client may display tool activity. Any later
shell, network, filesystem, deployment, or other side effect still follows the
client's normal permission and approval rules.

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
the proactive task-summary instruction above, but true raw-prompt Auto Mode for
those clients still requires a separate, tested adapter and is P1 work.

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

The important output is a route plan, not a directory result:

```text
$ auto-skill route "build a React landing page with Tailwind"
backend: self-hosted-route
route: skill
tier: full
skill plan (coding):
  policy: ponytail
  primary: frontend-design

compatibility selection:
1. frontend-design

next action: apply the returned skill plan in-turn; no skill installation is required
```

The policy and primary have different jobs. Ponytail constrains implementation
choices across the coding task; frontend-design supplies the domain workflow.
More specific user, project, and team instructions win if guidance conflicts.

## Prerequisites

- Python 3.10+
- `git`, for cloning this repository

## Connect the CLI

```bash
git clone https://github.com/auto-skill/auto-skill-connector
cd auto-skill-connector
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
auto-skill login
```

Start in explicit mode:

```bash
auto-skill doctor
auto-skill route "create an excel report with formulas and charts"
```

Installing the connector is not installing skills. Catalog skills remain in
Auto-Skill's hosted database and are retrieved for the current task. No prompt
hook is enabled by connector installation or by `doctor`.

## Commands

```bash
auto-skill search "<task>"
auto-skill route "<task>"
auto-skill route "<task>" --json --show-content
auto-skill route-prompt "<raw-user-prompt>"
auto-skill preview "<task-or-url>"
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

## Optional Developer Export

Normal Auto-Skill users do not install catalog skills. The legacy `install`
command remains available for authors and developers who explicitly want to
export and inspect a static `SKILL.md` locally:

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

This developer export is for static, instruction-only `SKILL.md` content. It
does not promise to fetch a complete bundle of scripts, references, assets, or
dependencies. Do not install an unfamiliar skill that relies on those
capabilities until they can be reviewed as a bundle. Managed update and
rollback are P1; back up an existing skill before using `--force`.

`install` also accepts a local `SKILL.md` path. Local installs are validated
first and refused if the structure fails the same gates routing applies to
fetched content.

## Authoring Skills from Team Standards

`skills/skill-creator/SKILL.md` is a meta-skill that turns a team's rules,
rubric, or style guide into a portable Agent Skill. Generation runs inside the
user's own agent (Claude Code, Copilot, Codex, Cursor) on their own model;
Auto-Skill contributes the deterministic parts:

```bash
auto-skill validate path/to/SKILL.md   # structural gates + discovery checks
auto-skill install path/to/SKILL.md --target claude
```

Validation applies the same content gates routing uses (stub bodies, HTML,
no-confirmation action language) plus authoring checks (frontmatter fields,
discovery-sized description with explicit trigger phrasing). For team-wide
automatic application, commit the generated skill to the repository's
`.github/skills/<slug>/SKILL.md`, which every supported client picks up on
checkout — including Copilot code review and cloud agents.

Standards that must govern every relevant task (review rubrics, security
policies) get two-layer delivery: skill discovery is opportunistic, so the
skill alone cannot guarantee it is always applied. skill-creator additionally
writes a short precedence kernel into the repo's always-on instructions files
(`CLAUDE.md`/`AGENTS.md`/`.github/copilot-instructions.md`) stating that the
standards skill applies first and wins over any other skill's guidance when
they conflict. The kernel stays small; extra skills layer on top per task,
under the standard's authority.

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

### GitHub Copilot CLI local MCP

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

### Launch-facing tools

- `route_task(task)` classifies a cleaned-up, privacy-minimized task and
  returns an ordered `skill_plan` plus `full`, `hint`, or no-route delivery.
  A full coding plan can contain a verified policy and a separate primary
  specialist. Large instructions are reduced to bounded context capsules.
- `route_prompt(prompt)` locally preflights a raw prompt, then routes only when
  it is task-shaped.
- `record_feedback(route_id, outcome)` records an enum-only privacy-safe
  outcome; it accepts no free-form notes.

`recommend_skill` is a deprecated compatibility preview surface. New clients
should use `route_task`. MCP exposes no skill/filesystem write tool over either
local stdio or hosted streamable HTTP; enum-only route feedback is optional.

Adding an MCP server opts into model-directed task-summary routing. Capable
clients are instructed to call `route_task` once for substantial tasks, but
MCP cannot force or invisibly intercept calls.

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

There is no invisible prompt interceptor for Codex or Cursor. Their connected
MCP models can proactively call `route_task`, but raw-prompt interception still
requires a tested client-specific adapter.
When a client cannot isolate a large skill, Auto-Skill falls back to a
deterministic capsule rather than silently injecting the full document.

## How Routing Works

Routing uses the service configured by `AUTOSKILL_URL`, defaulting to
`https://skills.autoskill.dev`. The backend follows a role-aware pipeline:

1. Classify the task family and action using the summary plus optional coarse
   language, framework, and project tags.
2. Select curated task-family policies through an allowlisted policy lane.
   Semantic similarity alone cannot make a skill an always-on policy.
3. Rank the primary specialist using relevance, quality, platform fit,
   provenance, evaluation/feedback evidence, and a soft popularity prior.
4. Gate integrations unless the task explicitly calls for an integration,
   service, platform, or external action.
5. Verify hashes and static capabilities, then compose a bounded ordered plan.

The precedence order is user/project/team instructions, policy, primary
specialist, then supporting skills. A conflict or failed verification removes
the lower-trust item instead of blindly merging instructions. Routing never
installs catalog skills or writes them to a client's skill directory.

The response tiers are:

- `full`: a verified plan whose active items cleared role, relevance, quality,
  hash, and static-capability gates. A plan may contain both a task-family
  policy and a primary specialist. Normal client permissions still govern all
  tools and side effects.
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
