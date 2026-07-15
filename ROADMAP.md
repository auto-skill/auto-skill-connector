# Roadmap

Auto-Skill's durable product is a trusted, compatible Agent Skill lifecycle
across Claude Code, Codex, and Cursor. Catalog size and prompt interception are
not the moat.

## P0: Launch Contract

- Portable `SKILL.md` compatibility for Claude Code, Codex, and Cursor.
- Deterministic task ranking with quality and platform gates.
- Source provenance, normalized content hashes, and immutable content lookup.
- Full current-task use only for high-confidence, risk-0 content that matches
  its indexed hash; ambiguous matches return two or three candidates.
- Explicit/on-demand CLI and MCP routing by default.
- An explicitly enabled Claude Code Auto Mode adapter; no claim that MCP alone
  intercepts prompts.
- No server or local-log retention of raw prompt text or prompt snippets.
- Manual, preview-first persistent install with overwrite protection.
- Hosted MCP kept authenticated and read-only.
- Backend health/readiness, evals, smoke tests, backup verification, and safe
  single-host deployment.

## P1: Trusted Lifecycle

### Verification and provenance

- Publisher identity and repository ownership verification.
- Pinned versions, signed release metadata, and visible verification status.
- Source/license/last-updated metadata and reproducible content scans.
- Clear separation between content integrity, publisher identity, and security
  review.

### Install, update, and rollback

- A one-time user-selected trust policy for persistent installation.
- Automatic install only for pinned, verified, static skills allowed by that
  policy.
- Mandatory ask/block behavior for scripts, network access, dependencies,
  unknown publishers, secrets, dangerous permissions, and irreversible side
  effects.
- Complete skill-bundle installation, including reviewed references and assets.
- Diff-before-update, atomic replacement, uninstall, backup, and rollback.
- Per-client install manifests without duplicating portable content.

### Client adapters

- Shipped: a Codex CLI `UserPromptSubmit` adapter (`auto-skill enable-hook
  --target codex`), built on the client's trusted hook flow. Codex's
  contract matches Claude Code's closely enough that `hooks/skill_suggest.py`
  runs unmodified for both; only the registration format differs
  (`~/.codex/config.toml` vs. `~/.claude/settings.json`).
- Blocked on the vendor, not on this project: a Cursor CLI adapter and a
  GitHub Copilot CLI adapter. Neither client's prompt-submission hook can
  inject context today (Cursor's `beforeSubmitPrompt` is allow/block only;
  Copilot's `userPromptSubmitted` output is never read at all). Re-evaluate
  when either ships a context-injection field; until then, explicit routing
  and the MCP proactive task-summary instruction remain the honest
  integration for those two clients.
- Shared adapter conformance tests for local filtering, privacy, timeouts, and
  full/hint/none behavior.
- Clear UI disclosure of which skill was selected and why.

### Teams and governance

- Team registries, allowlists, blocklists, and publisher policy.
- Admin-controlled trust roots and permission profiles.
- Review/approval workflows for new or changed skill versions.
- Privacy-safe audit events and retention controls.

## Explicitly Deprioritized

- Competing on the number of scraped skills.
- Broad web crawling as the core product moat.
- The Ollama `/chat` recommender UI.
- `recommend_skill` as a primary API or MCP tool.
- Raw prompt or route-skip analytics.
- Mandatory individual accounts for public search or routing.
- Consumer analytics and a premature Pro dashboard.
- Public remote installation.
- A large hosting or database migration before product trust requires it.

Compatibility code may remain temporarily when removal would create avoidable
launch risk, but these surfaces should not drive positioning or new work.

## Later, Only With Evidence

Managed hosting, private team registries, enterprise governance, and paid
features should follow demonstrated usage and a credible verification model.
Basic public search, preview, explicit routing, and safe manual local install
should remain available without a paid tier.
