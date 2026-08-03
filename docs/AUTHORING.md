# Authoring Skills and Optional Developer Export

## Authoring skills from team standards

`skills/skill-creator/SKILL.md` is a meta-skill that turns a team's rules,
rubric, or style guide into a portable Agent Skill. Generation runs inside
the user's own agent (Claude Code, Copilot, Codex, Cursor) on their own
model; Auto-Skill contributes the deterministic parts:

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
skill alone cannot guarantee it is always applied. skill-creator
additionally writes a short precedence kernel into the repo's always-on
instructions files (`CLAUDE.md`/`AGENTS.md`/`.github/copilot-instructions.md`)
stating that the standards skill applies first and wins over any other
skill's guidance when they conflict. The kernel stays small; extra skills
layer on top per task, under the standard's authority.

## Optional developer export

Normal Auto-Skill users do not install catalog skills. The legacy `install`
command remains available for authors and developers who explicitly want
to export and inspect a static `SKILL.md` locally:

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

This developer export is for static, instruction-only `SKILL.md` content.
It does not promise to fetch a complete bundle of scripts, references,
assets, or dependencies. Do not install an unfamiliar skill that relies on
those capabilities until they can be reviewed as a bundle. Managed update
and rollback are P1; back up an existing skill before using `--force`.

`install` also accepts a local `SKILL.md` path. Local installs are
validated first and refused if the structure fails the same gates routing
applies to fetched content.
