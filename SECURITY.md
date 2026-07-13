# Security and Privacy

Auto-Skill discovers and applies reusable agent instructions. Treat a skill as
code: provenance and integrity checks reduce risk, but they do not make unknown
instructions trustworthy.

## Default Behavior

The CLI is on-demand and never intercepts prompts. Adding the MCP server is an
explicit opt-in to model-directed task-summary routing: capable clients are
instructed to call the read-only `route_task` once for substantial work. MCP
still cannot intercept prompts or force the client to call it. Text is sent to
a route service only when:

- a user or agent explicitly calls a search or routing command/tool; or
- a connected MCP client proactively submits a concise task summary with
  secrets, personal data, pasted content, and irrelevant history omitted; or
- the user has deliberately enabled a client-specific Auto Mode adapter.

An MCP server cannot intercept every client prompt by itself.

## Prompt Handling

Search and route requests go to `AUTOSKILL_URL`, which defaults to
`https://skills.autoskill.dev`. The service needs the submitted task text long
enough to rank candidates, but it does not retain raw prompt text or prompt
snippets.

Route events retain only operational metadata needed to evaluate the router,
such as:

- account or caller identifier when authentication is in use;
- prompt length;
- selected skill identifier, source, and route tier;
- latency and token-size estimates;
- client name/version and enum outcome.

Feedback notes are accepted only for old-client compatibility and are ignored;
they are never retained. There is no hosted raw-prompt diagnostics mode.

The optional Claude Code adapter filters acknowledgements, commands,
meta/status prompts, and pasted context locally. Skipped prompts are not sent
to `/route` or a separate skip-analytics endpoint. Its local routing log also
contains metadata only: no prompt body and no prompt snippet.

To disable remote routing entirely:

```bash
AUTOSKILL_URL=
```

## Auto Mode Is Explicit Opt-in

Auto Mode is a client adapter that sees eligible prompt text before the model
answers. Enabling it has privacy and latency consequences, so it requires an
explicit user action and confirmation.

The launch build includes a Claude Code adapter only. Codex and Cursor can use
MCP-guided proactive task-summary routing, but no raw-prompt Auto Mode adapter
is claimed for those clients yet. MCP availability cannot guarantee a call;
the client model must honor the server instruction.

Auto Mode applies only to the current task. It never installs a skill
persistently.

## What Content-hash Verification Means

At ingest, Auto-Skill normalizes valid public `SKILL.md` content and records a
canonical SHA-256 hash. Before a public full route, the local indexed snapshot
must match that canonical hash, and the response includes a separate raw
SHA-256 digest for the served bytes. This detects missing or stale local cache
content; it does not check the current upstream revision.

It does not establish:

- who controls the publisher account;
- whether a repository or release was compromised before indexing;
- whether the instructions are correct, current, or appropriate;
- whether referenced scripts, packages, APIs, or assets are safe; or
- whether two different skills with different hashes are semantically safe.

Publisher verification, signed releases, and managed trust policy are P1.

## Risk and Quality Gates

`risk_score` is produced by pattern-based heuristics over metadata and fetched
content. It can catch indicators such as obvious exfiltration language or
obfuscated payloads. A score of zero is not a security audit or malware
guarantee.

The router uses three tiers:

- **full**: current-task instructions only. Requires high routing confidence,
  `risk_score=0`, valid quality-gated `SKILL.md` content, and a matching indexed
  content hash. Static checks also reject explicit tool declarations, bundled
  scripts, dependency installation, network commands, and dangerous shell
  patterns from this tier.
- **hint**: metadata and up to three candidates. Used for ambiguous,
  unverified, risky, incomplete, platform-mismatched, or capability-bearing
  content. No skill body is treated as active instructions.
- **none**: nothing cleared the routing gates.

Static checks cannot understand every wording or indirect instruction. A full
route grants no tool, network, secret, permission, or side-effect capability;
the user and client permission system remain the final boundary.

## Prompt Injection and Side Effects

A skill is untrusted input, even when its content hash matches. A malicious or
poorly written skill can try to override the user's request, obtain secrets, or
cause side effects.

Before following unfamiliar instructions, check:

- the original source and publisher;
- the source snapshot, canonical hash, and served-byte digest;
- requested tools, scripts, dependencies, files, and network destinations;
- whether the skill asks the agent to bypass confirmation or permissions; and
- whether the action is reversible.

Client sandbox, permission, and approval controls still apply. Auto-Skill must
not weaken them.

## Manual Persistent Installation

Persistent installation is CLI-only in the launch scope. Default user paths
are:

- Claude Code: `~/.claude/skills`
- Codex: `~/.agents/skills`
- Cursor: `~/.agents/skills` (Cursor also recognizes `~/.cursor/skills`)

The CLI:

- displays source and destination before writing;
- supports a no-write `--dry-run` preview;
- asks interactively unless the user explicitly passes `--yes`; and
- refuses to overwrite an existing skill unless the user explicitly passes
  `--force`.

The launch installer handles static, instruction-only `SKILL.md` content. It
does not promise to fetch or verify a complete bundle of scripts, references,
assets, packages, or dependencies. Do not install a skill that depends on
those files as though the single `SKILL.md` were complete.

Automatic persistent install, one-time trust policies, managed updates, and
rollback are not implemented. Back up an existing skill before using
`--force`.

## Remote MCP

The hosted streamable-HTTP MCP connector is authenticated and exposes no
skill/filesystem write tool. It
can return route and preview information, but it cannot install files onto a
caller device or write skills on the server host. Public remote installation
is not a supported launch configuration.

Self-hosters should keep the same boundary. Exposing a streamable-HTTP MCP
endpoint requires HTTPS, authentication, host allowlisting, and normal network
hardening.

## Accounts and Private Data

Public discovery and routing must not require an individual account solely for
tracking. Authentication is appropriate for private skills, favorites,
caller-specific history, and hosted MCP identity. Authenticated route metadata
may be associated with the caller, but raw prompt text is never retained.

## Local Files

Local diagnostics are off by default. With `AUTOSKILL_DIAGNOSTICS=1`, the
optional Claude adapter can create a metadata-only routing log at
`~/.claude/auto-skill-routing.jsonl`. It is safe to delete. Protect the parent
directory with normal user-file permissions. On its next invocation, the
current hook also rewrites an older log to remove legacy prompt snippets and
prompt hashes. Users who have not upgraded the hook should delete that file
manually.

CLI authentication credentials live under the user's Auto-Skill configuration
directory. Do not copy credential files into a repository, support ticket, or
bug report.

## Reporting Issues

Report security issues privately to the maintainers before opening a public
issue. Include:

- the affected skill URL or content hash;
- the observed behavior;
- whether CLI, MCP, or an opt-in adapter was involved;
- client and Auto-Skill versions; and
- a reproduction with prompts, credentials, and customer data removed.
