# How Routing Works

Routing uses the service configured by `AUTOSKILL_URL`, defaulting to
`https://skills.autoskill.dev`. The backend follows a role-aware pipeline:

1. Classify the task family and action using the summary plus optional
   coarse language, framework, and project tags.
2. Select curated task-family policies through an allowlisted policy lane.
   Semantic similarity alone cannot make a skill an always-on policy.
3. Rank the primary specialist using relevance, quality, platform fit,
   provenance, evaluation/feedback evidence, and a soft popularity prior.
4. Gate integrations unless the task explicitly calls for an integration,
   service, platform, or external action.
5. Verify hashes and static capabilities, then compose a bounded ordered
   plan (policy + primary today; a supporting slot is deferred).

The precedence order is user/project/team instructions, then policy, then
primary specialist. A conflict or failed verification removes the
lower-trust item instead of blindly merging instructions. Routing never
installs catalog skills or writes them to a client's skill directory.

## Response tiers

- `full`: a verified plan whose active items cleared role, relevance,
  quality, hash, and static-capability gates. Today's plan is policy +
  primary specialist. Normal client permissions still govern all tools and
  side effects.
- `hint`: an ambiguous, unverified, risky, incomplete, platform-specific, or
  capability-bearing match. The response contains metadata and up to three
  candidates, not active instructions.
- `none`: no candidate cleared the routing floor.

If the service is unavailable, the client reports no route instead of
silently querying a stale fallback.

To disable hosted routing entirely:

```bash
AUTOSKILL_URL=
```

## Content delivered, not routing decisions, is what's injected

A route decision (`full`/`hint`/`none`) never hands an agent raw
`SKILL.md` bytes. A `full`-tier route delivers a safety-stripped, content-
hash-verified capsule, framed as a technique the model should use if it
doesn't already know a correct way to do the task, not an instruction that
overrides the model's own reasoning about what the user actually asked. See
`backend/bench/WEAK_MODEL_BENCHMARK_REPORT_20260803.md` for why that framing
matters in practice and how it was arrived at.

## Launch scope

What ships now:

- Hosted routing that requires login. Free accounts get **100 authenticated
  routes/month** (`AUTOSKILL_FREE_ROUTES_PER_MONTH`). CLI tokens use a
  **30-day sliding** TTL (refreshes on use; idle 30 days requires re-login).
- Deterministic task-family classification, role-aware ranking, and
  platform gates.
- Ordered `skill_plan` with **two slots today**: a task-family policy plus
  one primary specialist. A third supporting slot is deferred.
- A curated Ponytail coding-policy lane. Policy skills cannot self-promote
  into this lane through semantic similarity alone.
- Integration skills require an explicit integration, service, or platform
  signal; a generic integration listing cannot displace a coding
  specialist.
- Source provenance and normalized content hashes.
- A portability filter: content tied to one specific repository (repeated
  project-path segments) or to a specific agent's sandbox filesystem
  (e.g. Claude's own `/mnt/skills/`, `/home/claude/` mount points) is
  excluded from full-tier delivery, since it won't generalize to a
  stranger's machine.
- Full in-turn use only for high-confidence, risk-0 public content whose
  canonical hash and returned raw digest verify; ambiguous matches return
  hints.
- Explicit search, route, and preview commands for inspection and testing.
- Optional Auto Mode adapters for **Claude Code** (`auto-skill enable-hook`)
  and **Codex CLI** (`auto-skill enable-hook --target codex`).
- A hosted MCP connector that asks capable clients to preflight substantial
  tasks proactively using concise summaries, with no skill/filesystem
  writes; optional enum-only route feedback. Cursor and Copilot stay on
  this MCP path for inject (vendor hook limits).
- Skill authoring via `skills/skill-creator` (validate + install into repo
  standards).
- Health, readiness, eval, smoke-test, backup, and deploy checks.
- No server-side retention of raw prompts or prompt snippets.

For an indexed public skill, `content-hash verified` means the local
indexed snapshot matches its canonical normalized hash and the served bytes
carry a separate raw SHA-256 digest. It does not query the current upstream
revision, prove publisher identity, guarantee safety, or replace review of
an unfamiliar skill.

Not shipped yet:

- A third supporting-skill slot beyond policy + primary.
- A one-time publisher/permission trust policy.
- Publisher identity verification or signed releases.
- Managed local skill installation, updates, uninstall, or rollback. These
  are unnecessary for normal just-in-time routing.
- Raw-prompt Auto Mode inject for Cursor or GitHub Copilot (blocked on
  vendor hook APIs; MCP proactive task-summary routing still works).
- Complete installation of skill bundles that require scripts,
  dependencies, references, or assets.

Pro and Team plan features exist in code (see `PRICING.md`) but stay quiet
in launch copy until P1 trust work lands.

## Intentionally deferred

The Ollama `/chat` recommender UI, broad web crawling as a product moat,
consumer run analytics, public remote installs, a third supporting-skill
slot, hard-selling Pro/Team before P1 trust, and `recommend_skill` are not
launch priorities. Compatibility code may remain temporarily, but these
surfaces are not the product contract. Hosted routing already requires
login (see Launch scope above).
