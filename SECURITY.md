# Security

`auto-skill` helps agents discover and install reusable instructions. Treat
skills like code: read them before trusting them.

## Remote Search

Search and route queries are sent to the configured search backend:
`AUTOSKILL_URL`, defaulting to `https://skills.avalahome.com`. There is no
fallback backend -- if this is unreachable, search reports no route found
rather than silently querying a second, possibly stale service.

The optional Claude Code prompt hook sends a truncated copy of each eligible
prompt to the search backend. Do not enable the hook for conversations that may
contain secrets, private customer data, credentials, or sensitive source code.

To disable self-hosted search entirely, set:

```bash
AUTOSKILL_URL=
```

## What `risk_score` Actually Checks (and Doesn't)

`risk_score` is produced by pattern-based heuristics scanning a skill's
declared metadata and fetched content for known-bad indicators (e.g. obvious
exfiltration patterns, obfuscated payloads). **It is not a malware guarantee**,
and it does not evaluate whether a skill's instructions are something you'd
actually want auto-applied. Concretely, it does not currently catch:

- A skill that takes a real, hard-to-reverse action (sending a message,
  deleting something, calling a paid API) while explicitly instructing the
  agent not to confirm first. This happened during development: a
  `risk_score=0` skill read a bot token from a secrets file and said "send
  the message immediately -- do NOT ask for confirmation." A separate,
  narrower heuristic now demotes content matching an action-verb +
  no-confirmation pattern to hint-only (name/url, never auto-applied
  instructions) regardless of `risk_score` -- see
  `_is_unconfirmed_action_content` in `auto_skill_core.py` and
  `hooks/skill_suggest.py`. This is a stopgap pattern match, not a semantic
  understanding of what a skill does; a skill worded differently could still
  slip through.
- Whether a skill's instructions are simply bad advice, out of date, or a
  poor fit for your task -- that's still on you to judge before applying it.

## Injection Tiers

Automatic routing (the hook, `route_prompt`, `route_task`) decides how much
of a matched skill to hand back:

- **full** -- the whole `SKILL.md` content, meant to be applied immediately.
  Only reached if the match clears the similarity floor, isn't a stub/HTML
  fetch, and doesn't match the unconfirmed-action pattern above.
- **hint** -- just the skill's name, description, and URL. Used when the
  content-quality gates above catch something. Nothing here is auto-applied;
  a human or a subsequent explicit `recommend_skill` call decides.
- **none** -- nothing cleared the similarity floor; no suggestion at all.

`recommend_skill` (the direct MCP tool call, as opposed to the passive
routing paths) has no hint tier -- it either returns full content that passed
every gate, or reports nothing found. There is no menu it can silently pick
the wrong item from, but a demoted candidate is invisible to a caller that
only checks `found`.

## Routing Provenance Log

Every routing decision the hook makes (full, hint, or none) is appended to a
local JSONL log so a derailed session can be diagnosed after the fact --
see `~/.claude/auto-skill-routing.jsonl`. Each line records a timestamp, the
prompt's length and a truncated snippet, the tier, and the matched skill's
name/url/risk_score when applicable. This file is local-only, never
transmitted anywhere, and safe to delete; it exists purely so you (or a
future debugging session) can answer "what did auto-skill inject, and why"
without having to reproduce the exact prompt.

## Installing Skills

The CLI is intentionally conservative:

- It previews source and destination before install.
- It refuses non-interactive installs without `--yes`.
- It refuses to overwrite existing skills without `--force`.
- It supports permanent installs only for Claude-style skills today.

The MCP `install_skill` tool is also conservative: when the server runs as a
streamable HTTP connector, installs are disabled by default because that URL
may be reachable from outside your machine. Enable public installs only behind
your own access control by setting `AUTO_SKILL_ENABLE_PUBLIC_INSTALL=1`.

Before installing a skill, review:

- Source URL
- Skill instructions
- Risk score, when provided by the index
- Any scripts, references, or commands the skill asks the agent to run

## Routing And Content Quality

Auto-Skill filters out high-risk indexed entries, obvious HTML fetches, tiny
stub files, and path/link-only content before injecting skill instructions. It
also uses confidence tiers: high-confidence matches can inject full content,
medium-confidence matches produce a hint, and low-confidence matches stay
silent.

These checks are heuristics, not a malware guarantee. Review unfamiliar skills
before installing them permanently or letting an agent run commands from them.

## Reporting Issues

Please report security issues privately to the project maintainers before
opening a public issue. Include:

- The affected skill URL or command
- The observed behavior
- Whether a prompt hook, MCP call, or CLI command was involved
