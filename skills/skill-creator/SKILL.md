---
name: skill-creator
description: Turn a team's rules, rubric, style guide, or standards document into a portable Agent Skill. Use when the user asks to create a skill from a document, encode team standards or conventions as a skill, convert a rubric or checklist into agent instructions, or make their coding/review standards apply automatically in Claude Code, Codex, Cursor, or GitHub Copilot.
---

# Skill Creator

Convert a standards document (rubric, style guide, review checklist, coding
conventions) into a `SKILL.md` that agents discover and apply automatically.

## Requirements

- The `auto-skill` CLI must be installed (`auto-skill doctor` to check). If it
  is missing, stop and tell the user how to install it.
- Work from ONE source document per skill. If the user offers several
  documents, make one skill per document rather than merging them.

## Workflow

1. **Read the source document fully.** Do not draft from a skim. If the
   document is longer than ~10,000 words, tell the user it should be split
   into multiple focused skills and ask which section to start with.

2. **Extract the enforceable rules.** Keep only instructions an agent can act
   on while doing work (naming rules, required checks, forbidden patterns,
   review criteria, output formats). Drop aspirational language, org history,
   and anything needing information an agent won't have. Preserve the
   document's own severity distinctions (must vs. should).

3. **Draft the SKILL.md** with:
   - `name:` — short, kebab-case, specific (e.g. `acme-api-review-standards`).
   - `description:` — one or two sentences stating WHEN the skill applies,
     with explicit "Use when ..." trigger phrasing naming the tasks, file
     types, or activities it governs. Discovery depends entirely on this line.
   - A body organized by activity (e.g. "When writing an endpoint", "When
     reviewing a PR"), not by the source document's chapter order. State each
     rule imperatively. Include the rule's WHY only when the agent needs it to
     resolve edge cases.
   - Never include instructions to skip confirmation for actions with side
     effects; routers demote such skills and orgs should not want them.

4. **Validate:** run `auto-skill validate <path-to-draft>` and fix every
   error and warning it reports. Re-run until it passes.

5. **Show the user 3 concrete pass/fail examples** — short work samples that
   the skill would flag or approve, derived from its rules. This is how the
   user verifies the skill encodes THEIR standards before it governs anything.
   Revise from their corrections and re-validate.

6. **Deliver where the user chooses:**
   - Personal, this machine: `auto-skill install <path> --target claude`
     (or `codex`, `cursor`, `copilot`).
   - Whole team via the repository: write it to
     `.github/skills/<slug>/SKILL.md` in the team's repo so every client —
     including Copilot code review and cloud agents — picks it up on checkout.
   - Team library on the hosted service: `auto-skill my-skills add` (requires
     `auto-skill login`).

## Boundaries

- Never invent rules the source document does not contain; if a needed rule
  is ambiguous, ask the user instead of guessing.
- The generated skill must stand alone: no references to internal wikis,
  paths, or systems the agent cannot reach.
