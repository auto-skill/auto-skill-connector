# Auto-Skill

**Just-in-time skill routing for coding agents.** Auto-Skill watches what a
task needs, hands the agent a verified, relevant technique for it, and gets
out of the way. No skill installation, no directory browsing, no
maintaining a local skills folder.

Works with **Claude Code**, **Codex CLI**, **Cursor**, and **GitHub
Copilot**.

## Does it actually help?

We measured it rather than assumed it. Routing a skill into context can
just as easily *override* a model's own reasoning as inform it — and our
first pass at this got that wrong, measurably. After fixing that (the
wrapper now says *"if you don't already know a correct way to do this, use
this technique — it should let you do this better than you could on your
own,"* not *"apply this now"*) and building a judge-graded benchmark to
check the result:

| condition | baseline pass rate | with Auto-Skill | lift |
| --- | --- | --- | --- |
| well-matched task + genuinely relevant skill | 50% | 90% | **+40 points** |
| general, unfiltered task mix | 18–59% | 24–65% | +6 to +12 points |

Both numbers are real, from the same investigation, on a local weak model
(`llama3.2:3b`) — not cherry-picked, not from a paid frontier model that
already knows most things unaided. The first row is what Auto-Skill's
mechanism does when the pieces line up (a real capability gap, a routed
skill that's actually right for it); the second is closer to what a
representative task mix looks like. Read both, not just the good one:
**[`backend/bench/WEAK_MODEL_BENCHMARK_REPORT_20260803.md`](backend/bench/WEAK_MODEL_BENCHMARK_REPORT_20260803.md)**
has the full methodology, every regression found along the way, and how
each was fixed.

## Quickstart

Requires Python 3.10+ and `git`.

```bash
git clone https://github.com/auto-skill/auto-skill-connector
cd auto-skill-connector
python -m venv .venv
.venv\Scripts\python -m pip install -e ".[dev]"
auto-skill login
auto-skill route "build a React landing page with Tailwind"
```

```text
backend: self-hosted-route
route: skill
tier: full
skill plan (coding):
  policy: ponytail
  primary: frontend-design

next action: apply the returned skill plan in-turn; no skill installation is required
```

Installing the connector does not install skills — catalog skills stay in
Auto-Skill's hosted database and are retrieved per task. Free accounts get
**100 routes/month**; the CLI session is a 30-day sliding token (refreshes
on use).

## How it works, briefly

1. A task gets classified (family + action), not just keyword-matched.
2. Auto-Skill composes an ordered plan: an always-on team/task-family
   policy, plus one primary specialist for this specific task.
3. Only high-confidence, content-hash-verified, risk-0 content is delivered
   as active guidance (`full` tier). Ambiguous matches come back as hints,
   not instructions. Content tied to one specific repo or to an agent's own
   sandbox filesystem is excluded — it wouldn't generalize to your machine.
4. Your own project/team instructions always win over a routed skill if
   they conflict.

Full pipeline details, response tiers, and current launch scope:
**[`docs/ROUTING.md`](docs/ROUTING.md)**.

## Using it

Three ways to get routing, in increasing order of automation:

| Mode | What it does | Setup |
| --- | --- | --- |
| **CLI, on demand** | You call `auto-skill route "<task>"` explicitly | Nothing extra |
| **MCP, proactive** | A connected agent calls `route_task` itself near the start of substantial work | Add the MCP server — see [`docs/MCP.md`](docs/MCP.md) |
| **Auto Mode, opt-in hook** | Every eligible prompt is preflighted locally and routed automatically | `auto-skill enable-hook` (Claude Code) or `auto-skill enable-hook --target codex` |

| Client | Delivery path | Skill install required? |
| --- | --- | --- |
| Claude Code | MCP or Auto Mode hook | No |
| Codex CLI | MCP or Auto Mode hook | No |
| Cursor | MCP only (no inject hook yet — vendor limit) | No |
| GitHub Copilot | MCP only (no inject hook yet — vendor limit) | No |

Auto Mode never means silent, persistent installation, and it's off until
you opt in — read **[`SECURITY.md`](SECURITY.md)** first, then
**[`docs/AUTO_MODE.md`](docs/AUTO_MODE.md)** for exactly what data moves
where and how to turn it off.

## Commands

```bash
auto-skill search "<task>"
auto-skill route "<task>"
auto-skill route "<task>" --json --show-content
auto-skill preview "<task-or-url>"
auto-skill doctor
auto-skill login
auto-skill enable-hook [--target codex]
auto-skill disable-hook [--target codex]
```

Full command reference, including `route-prompt`, `feedback`, `metrics`,
and the optional developer `install`/`validate` export for skill authors:
**[`docs/AUTHORING.md`](docs/AUTHORING.md)**.

## Repo layout

- **Client** (repo root): `auto_skill_cli.py`, `mcp_server.py`, `hooks/` —
  the package end users install. Talks to the hosted backend over HTTP.
- **Backend** (`backend/`): the FastAPI/SQLite service behind
  `skills.autoskill.dev` — ingestion, embeddings, routing, content storage,
  deploy scripts. Own run story and CI; not part of the pip package. See
  `backend/README.md` and `backend/RUNBOOK.md`.

The backend subtree tracks the standalone backend repository at
`https://github.com/auto-skill/auto-skill.git`. After syncing it, update
`.autoskill-backend-subtree.json` and run
`python scripts/check_backend_subtree.py --check-remote`.

## Security

Skills are instructions that can shape agent behavior. Content-hash
matching detects drift; it does not establish trust. Read
**[`SECURITY.md`](SECURITY.md)** before enabling Auto Mode or installing an
unfamiliar skill.

## More

- [`docs/ROUTING.md`](docs/ROUTING.md) — pipeline internals, response
  tiers, current launch scope, what's deferred.
- [`docs/MCP.md`](docs/MCP.md) — per-client MCP setup, self-hosting the
  connector, tunnels.
- [`docs/AUTO_MODE.md`](docs/AUTO_MODE.md) — the opt-in prompt hook, what
  data it sends, diagnostics and analytics (both off by default).
- [`docs/AUTHORING.md`](docs/AUTHORING.md) — turning team standards into a
  skill, the optional local `install`/`validate` export.
- [`backend/bench/WEAK_MODEL_BENCHMARK_REPORT_20260803.md`](backend/bench/WEAK_MODEL_BENCHMARK_REPORT_20260803.md) —
  does Auto-Skill actually help, measured.
- [`PRICING.md`](PRICING.md), [`ROADMAP.md`](ROADMAP.md),
  [`SECURITY.md`](SECURITY.md)
