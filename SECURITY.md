# Security

`auto-skill` helps agents discover and install reusable instructions. Treat
skills like code: read them before trusting them.

## Remote Search

Search and route queries are sent to the configured search backend:

- `AUTOSKILL_URL`, defaulting to `https://skills.avalahome.com`
- Supabase fallback endpoints used by this connector

The optional Claude Code prompt hook sends a truncated copy of each eligible
prompt to the search backend. Do not enable the hook for conversations that may
contain secrets, private customer data, credentials, or sensitive source code.

To disable self-hosted search and rely on fallback behavior, set:

```bash
AUTOSKILL_URL=
```

## Installing Skills

The CLI is intentionally conservative:

- It previews source and destination before install.
- It refuses non-interactive installs without `--yes`.
- It refuses to overwrite existing skills without `--force`.
- It supports permanent installs only for Claude-style skills today.

Before installing a skill, review:

- Source URL
- Skill instructions
- Risk score, when provided by the index
- Any scripts, references, or commands the skill asks the agent to run

## Reporting Issues

Please report security issues privately to the project maintainers before
opening a public issue. Include:

- The affected skill URL or command
- The observed behavior
- Whether a prompt hook, MCP call, or CLI command was involved
