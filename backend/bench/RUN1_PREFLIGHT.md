# Ingestion run 1 — Phase 0 preflight

Run id: `run1-20260802`
Executed: 2026-08-02 (UTC), WSL2 Ubuntu on `DESKTOP-STI60DH`
Working copy: `/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector`, branch `master`

**Result: PASS.** No serious blocker. Three flags recorded (below); their dependent work is
adapted or skipped as noted, and nothing else stops.

---

## 1. Primary judge — Codex + Luna

| Item | Value |
|---|---|
| Binary | `/home/sami/discord_codex/node_modules/.bin/codex` |
| CLI version | `codex-cli 0.144.6` |
| `CODEX_HOME` | `/srv/mobile-codex/codex-home` |
| Model requested | `gpt-5.6-luna` |
| Model slug confirmed in `models_cache.json` | `gpt-5.6-luna` ("GPT-5.6-Luna", default effort `medium`) |
| Reasoning effort | `medium` (listed in `supported_reasoning_levels`) |
| Round trip | PASS — returned exactly `{"ok":true,"echo":"preflight"}` |
| Usage on probe | 13,463 in / 14 out |

Two candidate installs were tested; **only one works**, and this is worth recording because the
obvious one fails:

- `/home/sami/.npm-global/bin/codex` (`0.137.0`) with `CODEX_HOME=/home/sami/.codex`
  → **fails twice over**: that Codex home's refresh token is dead
  (`401 invalid_refresh_token`), and even with working auth the CLI is too old
  (`"The 'gpt-5.6-luna' model requires a newer version of Codex"`).
- `/home/sami/discord_codex/node_modules/.bin/codex` (`0.144.6`) with
  `CODEX_HOME=/srv/mobile-codex/codex-home` → **works**. This is the Discord-Codex service's
  Codex home and its OAuth session is live.

Sealed-launch recipe used for every primary-judge call (per ground rule 4):

```
env -i HOME=/home/sami PATH=/usr/bin:/bin CODEX_HOME=/srv/mobile-codex/codex-home TERM=dumb \
  codex exec --ephemeral --ignore-user-config --skip-git-repo-check \
             -s read-only -C <empty temp dir> \
             -m gpt-5.6-luna -c model_reasoning_effort="medium" --json < /dev/null
```

`env -i` clears the inherited environment, so no repo paths, no `DISCORD_TOKEN`, no GitHub token
and no database paths reach the judge. `-s read-only` is the most restrictive sandbox
`codex exec` offers (`--help` lists `read-only | workspace-write | danger-full-access`).
`--ignore-user-config` prevents `config.toml` from re-adding MCP servers, hooks or skills.
The cwd is a fresh empty `mktemp -d`, removed after each call.

**Model-identifier caveat (honest limitation).** `codex exec --json` emits
`thread.started / turn.started / item.completed / turn.completed`; **none of these carry a model
field**, so the CLI does not expose a resolved snapshot id. Per row we therefore record the
requested slug `gpt-5.6-luna`, `reasoning_effort=medium`, `codex_cli=0.144.6`, plus the model's
own `model_self_report` field inside the returned JSON. The judge is pinned by request; we cannot
independently prove a dated snapshot id, and no claim in the report depends on one.

## 2. Secondary judge — Haiku subagent

| Item | Value |
|---|---|
| Mechanism | Agent tool, `model: haiku` |
| Model identifier self-reported | `claude-haiku-4-5-20251001` |
| Round trip | PASS — returned exactly `{"ok": true, "echo": "preflight", "model_identifier": "claude-haiku-4-5-20251001"}` |

`claude-haiku-4-5-20251001` is the newest Haiku available to this harness. Subagents run with
`Do not use any tools` in the judge prompt and receive skill content only inside delimited data
blocks.

## 3. Tooling and disk

| Check | Result |
|---|---|
| `sqlite3` CLI | **Was missing**; installed `sqlite3` (Ubuntu `sqlite3` package) via the WSL root launcher. Now `/usr/bin/sqlite3`. |
| Python | 3.14.4, `sqlite3` module linked against SQLite 3.46.1 |
| Free disk on `/` | 788 GB free of 1007 GB (18% used) — far above the 5 GB floor |
| Frozen-copy headroom | needs ~2.3 GB; available |

## 4. Corpus location and read-only open

Exactly **one** corpus database is reachable from this machine, so there is no ambiguity about
which DB is authoritative:

| Path | Size | Notes |
|---|---|---|
| `workspace/.autoskill-private/production-db/autoskill-production-20260801T234559Z.sqlite` | 2,238,582,784 B (2.24 GB) | Consistent `.backup` snapshot taken from production on 2026-08-01, SHA-256 previously verified equal to the server-side copy |

A repo-wide scan for `*.db` / `*.sqlite` / `*.sqlite3` over 1 MB returns **only** that file.
`backend/data/` does not exist and `backend/skills_library/` contains **0 files**, so there is no
competing local corpus.

Read-only open confirmed: `sqlite3 "file:<abs>?mode=ro"` returns 140 objects in `sqlite_master`
and `select count(*) from skills` → **271,957**.

Corpus shape (from the read-only snapshot):

| quality_status | rows |
|---|---:|
| rejected | 155,485 |
| active | 72,270 |
| metadata_only | 39,963 |
| duplicate | 4,223 |
| pending | 16 |

| source | rows |
|---|---:|
| github | 121,049 |
| github_skill_file | 118,160 |
| skillsmp | 16,416 |
| awesome_list | 9,090 |
| glama_registry | 3,895 |
| web_search | 999 |
| pulsemcp_registry | 917 |
| npm | 784 |
| smithery_registry | 575 |
| mcp_official_registry | 70 |
| anthropic_docs | 2 |

Other: `skill_packages` 398, `skill_package_files` 4,576, `skill_retrieval_records` 0,
skills with an embedding 70,159, `scrape_runs` 667.

## 5. GitHub token

**Available.** Recovered from the existing `gh` device-login credential at
`~/.config/gh/hosts.yml` (`gho_…`, user `Sami-ul`). Validated live:

- `GET /rate_limit` → 200. core 5000/5000 remaining; **code_search 10/min**; search 30/min.
- `GET /search/code?q=path:.claude/skills filename:SKILL.md` → 200, `total_count` **75,480**.

Phase 4 is therefore **not** blocked on a missing token. The binding constraint is the 10
requests/minute code-search limit, which is what makes the sweep a multi-run job.

---

## Flags (recorded; dependent work adapted or skipped, run continues)

**FLAG 1 — packages/library directory is not reachable; prod snapshot needed from Sami.**
The package payloads are content-addressed under `backend/skills_library/` on the production
host. Locally that directory holds **0 files**. `skill_package_files` in the snapshot stores only
`raw_sha256` / `git_blob_sha` / `size` / `role` — **no file bytes**. So Phase 1 can freeze the
database but not the package bytes. Per the plan's instruction, prod was **not** accessed. To
freeze the real v0 package store, Sami needs to ship a snapshot of
`/opt/auto-skill-connector/backend/skills_library/`.

**FLAG 2 — `crawl_state.json` is not reachable, which weakens Phase 2 question (b).**
`scraper.py:539` pins `CRAWL_STATE_PATH = backend/skills_library/crawl_state.json`, i.e. inside
the same unreachable directory. `deep_sweep.last_sweep_at` therefore cannot be read directly, so
"did the deep sweep ever complete, and when" cannot be answered from a timestamp. Phase 2
substitutes a measurable proxy — live GitHub code search for the two sweep queries, sampled and
checked against the frozen DB — and the verdict states plainly which part is proxy evidence.

**FLAG 3 — skill body text is not in the database, so Phase 5 fetches entrypoints from source.**
`skills.raw` holds only `{parent_repo, path, frontmatter, stars, updated_at, valid_skill}`
(mean 568 B, max 25 KB) and `skills.retrieval_text` is empty for every active row. The SKILL.md
body lives only in the unreachable library dir. Phase 5 consequently fetches each sampled
skill's entrypoint and file tree from its **public** source over the GitHub API and stores them
content-addressed in `backend/skills_library_v1/` — which is exactly what Phase 4 already
prescribes for sighted packages. Skills whose entrypoint cannot be fetched are resolved by the
plan's existing deterministic pre-filter `entrypoint absent`, with no model call.

## Deviations from the letter of the plan (all recorded, none silent)

1. **`sqlite3` was installed** rather than treated as a missing-tool blocker. It is a means, not
   an input; Python's bundled SQLite 3.46.1 could have served identically.
2. **Run-1 artifacts are excluded via `.git/info/exclude`, not `.gitignore`.** `backend/.gitignore`
   does not cover `corpus_v0_frozen/`, `enrichment_v1.db`, `skills_library_v1/` or
   `sweep_state_v1.json`, and the frozen copy contains production `users`, `cli_tokens`,
   `oauth_identities` and `stripe_*` rows sitting inside a repo whose `master` auto-deploys.
   `.git/info/exclude` is local-only and untracked, so this protects the tree while modifying
   **zero** tracked files. Verified with `git check-ignore -v` on all four paths.
3. **The frozen copy is byte-faithful, including PII tables.** It was not stripped, because
   stripping would change the control corpus. It is mode-600-adjacent inside a private tree,
   git-excluded, and no PII table is ever read by any phase or shown to any judge.

## Caps in force

≤ 250 Luna calls, ≤ 150 Haiku calls, concurrency ≤ 3, every judgement keyed by normalized
content hash so re-runs skip already-judged hashes.
