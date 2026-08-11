# Auto-Skill ingestion — run 1 report

Run id `run1-20260802`. Executed 2026-08-02 UTC on the WSL workstation, additive only.
No git commit, no push, no write to prod, no change to any existing pipeline file.

**Headline:** the Oxcaml miss is a **discovery coverage gap**, not a pipeline bug and not a
GitHub index gap. The two-judge pass ran clean — 108 Luna calls, zero malformed, zero failed,
and **all 22 canaries came back `included`, so there is no prompt regression**. The two most
useful findings are negative: the judge's `risk_flags` are not usable as a security signal as
written, and the judge does **not** solve the genericity problem that wrecked the
Terminal-Bench run.

---

## 1. Preflight (Phase 0) — PASS

Full detail in [`RUN1_PREFLIGHT.md`](RUN1_PREFLIGHT.md).

| item | result |
|---|---|
| Primary judge | `gpt-5.6-luna`, effort `medium`, via `codex-cli 0.144.6` at `/home/sami/discord_codex/node_modules/.bin/codex`, `CODEX_HOME=/srv/mobile-codex/codex-home` |
| Secondary judge | Haiku subagent, self-reported `claude-haiku-4-5-20251001` |
| `sqlite3` | was missing; installed (`/usr/bin/sqlite3`) |
| Disk | 788 GB free of 1007 GB |
| Corpus DB | exactly one reachable, opened `mode=ro`, 271,957 skills |

The obvious Codex install fails twice over and is worth writing down: `/home/sami/.npm-global/bin/codex`
(`0.137.0`) has a **dead refresh token** (`401 invalid_refresh_token`) *and* is too old for Luna
(`"The 'gpt-5.6-luna' model requires a newer version of Codex"`). Only the Discord-Codex bundled
CLI + its Codex home works.

Every Luna call ran sealed: `env -i` (so no repo paths, no `DISCORD_TOKEN`, no GitHub token
reached it), `-s read-only`, `--ephemeral`, `--ignore-user-config`, cwd a fresh empty `mktemp -d`
deleted afterwards.

### Flags (recorded, dependent work adapted, run continued)

1. **Packages/library directory not reachable — prod snapshot needed from Sami.** The package
   bytes live only at `/opt/auto-skill-connector/backend/skills_library/`. Locally that directory
   has **0 files**, and `skill_package_files` stores hashes/sizes/roles but no bytes. Prod was not
   touched, per the plan.
2. **`crawl_state.json` not reachable**, because `scraper.py:539` puts it inside that same
   directory. Phase 2 question (b) therefore rests on measurement, not on the crawler's own
   timestamp.
3. **Skill bodies are not in the database.** `skills.raw` holds only
   `{parent_repo, path, frontmatter, stars, updated_at, valid_skill}` (mean 568 B) and
   `retrieval_text` is empty for every active row. Phase 5 consequently fetched entrypoints from
   **public** source, which is what Phase 4 already prescribes.

### Deviations from the letter of the plan

- `sqlite3` was **installed** rather than treated as a missing-tool blocker (Python's bundled
  SQLite 3.46.1 would have served identically).
- Run-1 artifacts are excluded through **`.git/info/exclude`**, not `.gitignore`. The frozen copy
  carries production `users` / `cli_tokens` / `oauth_identities` / `stripe_*` rows and sits in a
  repo whose `master` auto-deploys; `.git/info/exclude` is local-only and untracked, so this
  protects the tree while modifying **zero** tracked files.
- The frozen copy is byte-faithful **including** those PII tables — stripping them would have
  changed the control corpus. No phase reads them and no judge ever saw them.

## 2. Freeze — v0 control corpus (Phase 1)

| | |
|---|---|
| Path | `backend/corpus_v0_frozen/corpus_v0.sqlite` |
| Method | `sqlite3 .backup` (logical page copy) |
| Size | 2,238,582,784 B |
| SHA-256 | `c8485180753ed9aa5d36eabbe751127d2a4b3403ae3917245b12b340689a56f9` |
| `pragma integrity_check` | `ok` |
| Source SHA-256 | `922cfb367625062378ef4a8d1aa82a334e7ce82c7b65584f432000ef8242acc6` |

The two file hashes differ because `.backup` rewrites the page image; logical equality is
established by `integrity_check` plus matching row counts, not by file hash.

Contents: 271,957 skills — `rejected` 155,485, `active` 72,270, `metadata_only` 39,963,
`duplicate` 4,223, `pending` 16. 70,159 embedded. 398 packages / 4,576 package files /
**0** retrieval records. 667 scrape runs. Manifest: `corpus_v0_frozen/MANIFEST.json`.

**The packages directory was not frozen** (Flag 1). The v0 control is therefore
*database-complete but payload-incomplete*.

## 3. Oxcaml bisect (Phase 2) — verdict: **never-swept, coverage gap**

Full detail in [`RUN1_BISECT.md`](RUN1_BISECT.md); raw evidence in `run1_bisect_evidence.json`.

**(a) No row anywhere references `oxcaml/oxcaml`.** A substring scan across 17 non-PII tables
returned 3 `oxcaml` hits, none of them the target repo (a Nix skill mentioning "OxCaml" in prose,
and two skills.sh entries named `oxcaml` from `aresbit/matebot` and `smithery.ai`).

This rules out **found-then-dropped**: the pipeline *retains* rejects — 52,730 `rejected`
`github_skill_file` rows exist — so a fetched-then-failed skill would still be there. Nothing is.

**(b) The sweeps never ran to completion, and not close.**

| sweep query | live total | sampled | in corpus | coverage |
|---|---:|---:|---:|---:|
| `path:.claude/skills filename:SKILL.md` | 75,480 | 400 | 20 | **5.0 %** |
| `path:.claude/skills` | 108,224 | 400 | 6 | **1.5 %** |

Misses include repos the corpus already knows (58/380 and 78/394), e.g.
`linuxfoundation/crowd.dev`, `woocommerce/woocommerce-ios`, `srid/emanote`. The last
`scrape_runs` row is `2026-07-15T05:33:58Z`, status `stale`, error
*"Production scraping retired; collection moved off-host."*

**This is not an index gap.** The file returns `200` from the contents API (4,515 B), the repo is
public with **828 stars**, and `repo:oxcaml/oxcaml path:.claude/skills filename:SKILL.md` returns
it **today**. It was first committed **2026-02-03** and never modified — five months of active
collection before the crawler was retired.

## 4. Embedded-stratum sweep (Phase 4) — partial, as expected

Script `run1_sweep.py`; resume cursor `backend/sweep_state_v1.json`. Ran 00:33:11 → 00:48:36 UTC
(15m25s), spending **110 code-search requests** (the 10/min limit is the binding constraint) and
200 core requests.

| | |
|---|---|
| Sightings recorded | **8,765** unique (9,135 hits seen, deduped by URL) |
| Distinct repos sighted | 3,570 |
| Packages snapshotted | **100 / 100** attempted, 0 failed, 5,441,974 B |
| v1 library | 329 objects + 100 manifests, 11 MB |

**Exactly where it stopped**, so the next run resumes cleanly:

- `path:.claude/skills filename:SKILL.md` — **incomplete.** 14 size slices done, covering
  `31501..384000`. **4 slices pending: `[0,24000]`, `[24001,30000]`, `[30001,30750]`,
  `[30751,31500]`.** Zero errors, zero unreachable "oversized" buckets.
- `path:.claude/skills` — **not started.** Budget was exhausted first; `total_count` was never
  fetched. 1 pending slice: `[0, 384000]`.

**Read this honestly: the remaining work is the majority of the work.** Slicing descends from
large files, so the four pending slices cover `0..31,500` bytes — where most SKILL.md files
actually live. 8,765 sightings against a 75,480 total for query 1 alone is roughly 12 %, and
query 2 is at 0 %.

## 5. Two-judge enrichment (Phase 5)

Sample: `enrichment_sample_v1.json`, seed `20260802`, **199 skills** (target 200).
Buckets: 22 canary + 20 lowest-quality + 10 known-generic + 147 stratified-random over `source`.

**22 of 23 canaries are present in the corpus. The single absent one is
`oxcaml-address-review`** — independently confirming Phase 2.

Entrypoint resolvability across the sample was itself a finding: `repo_path` 108, `repo_root` 64,
`unresolvable` 15, `repo_dir` 12. Only `github_skill_file` rows carry an entrypoint path at all.

### Deterministic pre-filters (free — 91 verdicts, no model call)

| rule | n |
|---|---:|
| `entrypoint_absent` | 82 |
| `frontmatter_invalid` | 4 |
| `fetch_failed` | 3 |
| `frontmatter_missing` | 1 |
| `body_too_short` | 1 |

No exact-hash duplicates fired in this sample.

### Judge calls

| judge | model | calls | ok | malformed | failed | retries | tokens in | tokens out |
|---|---|---:|---:|---:|---:|---:|---:|---:|
| primary | `gpt-5.6-luna` @ medium | **108** | 108 | 0 | 0 | 0 | 1,611,158 | 40,171 |
| secondary | `claude-haiku-4-5-20251001` | **5** | 5 | 0 | 0 | — | 79,574 (total) | — |

Caps respected: 108/250 Luna, 5/150 Haiku, concurrency 3.

**Luna cost estimate at $0.20 / $1.20 per M:** input $0.3222 + output $0.0482 = **≈ $0.37**.
Haiku's harness reports a single total per call with no in/out split, so no Haiku cost is
estimated rather than invented.

### Final labels

| label | n |
|---|---:|
| **included** | **103** |
| **excluded_junk** | **94** |
| **quarantine** | **2** |
| malformed | 0 |
| pending | 0 |

Decided by: deterministic 91, primary alone 103, both judges 5.

### Judge agreement — all 5 cases where both ran

| skill | Luna | Haiku | outcome |
|---|---|---|---|
| `TEMPLATE` | reject 0.99 | reject 0.98 | agree → `excluded_junk` |
| `changelog` | reject 0.98 | reject 0.75 | agree → `excluded_junk` |
| `BadSkill_Example` | reject 0.99 | reject 0.95 | agree → `excluded_junk` |
| `cs-aeo` | reject 0.98 | **keep 0.72** | **disagree → `quarantine`** |
| `sprint-health` | reject 0.93 | **keep 0.72** | **disagree → `quarantine`** |

3 agree, 2 disagree. Both disagreements were kept and penalised, never silently dropped.
The secondary judge only ran where the primary rejected or was under 0.6 confidence — Luna was
under 0.6 on **zero** rows, so all 5 escalations were rejections.

### Canaries — **no prompt regression**

**22 / 22 present canaries landed in `included`.** Nothing to flag.

### Closure fetch (bounded: repo-internal, ≤ 20 files/skill, ≤ 256 KB/file)

27 skills declared closure paths. **116 files fetched**, 0 already present, **1 missing**.
The single miss is a directory (`.claude/skills/canvas-design/canvas-fonts`) that the judge
listed as a dependency — which is also the sample's **only** path violation, caught by the
`closure_paths ⊆ tree` check.

8 of 108 judged inputs hit the 24k truncation cap and were labelled as truncated to the judge.

Vendor conventions: `generic` 46, `claude` 43, `unknown` 13, `codex` 4, `copilot` 2.

---

## 6. Findings worth acting on

**A. `risk_flags` is not a usable security signal as written.** Luna flagged `prompt_injection`
on **47** skills — and **46 of those 47 it also judged real and `included` at 0.97–0.99
confidence**. It is flagging ordinary imperative skill prose ("You are…", "Never do X") as an
injection attempt. That is a prompt-calibration defect in `enrichment_prompt_v1.md`, not a 43 %
attack rate. Same pattern for `destructive_commands` (9), which fired on `commit`,
`secret-hygiene` and `python-packaging` for legitimately mentioning git/`rm`. **Do not wire these
flags into any gate until the prompt distinguishes "the skill instructs the agent" (normal) from
"the data tries to override the auditor" (injection).**

**B. The judge does not solve the genericity problem.** All **10/10** deliberately generic skills
were `included` at 0.98–0.99 — including **`ponytail` itself**, the skill that was injected on
74/75 Terminal-Bench routes. The prompt tells the judge to reject "so generic it teaches
nothing," and it did not. Enrichment as built improves *junk* filtering, not *relevance*.

**C. Junk is concentrated by source, and it is a schema problem, not a quality problem.**

| source | n | included | excluded |
|---|---:|---:|---:|
| `github_skill_file` | 108 | 94 | 12 (+2 quarantine) |
| `skillsmp` | 10 | 9 | 1 |
| `github` | 56 | 0 | **56** |
| `awesome_list` | 5 | 0 | 5 |
| `npm` / `web_search` / 4 registries | 15 | 0 | 15 |
| `anthropic_docs` | 2 | 0 | 2 |

Every non-`github_skill_file` GitHub-ish row failed on `entrypoint_absent` — those rows point at
a **repo**, not a skill file. 82 of 94 exclusions are this one cause. That is ~44 % of the whole
sample carrying no entrypoint the router could ever inject.

**D. The lowest-quality stratum is not reliably junk.** Of the 20 lowest-`quality_score` rows,
**12 were `included`**, 6 excluded, 2 quarantined. The existing `quality_score` and the judges
disagree substantially.

## 7. Honest limits

1. **The sweep is ~12 % of query 1 and 0 % of query 2.** No completeness claim is made.
2. **No `crawl_state.json`**, so "never swept" rests on measured coverage, not the crawler's log.
3. **GitHub code search sorts by best-match, not randomly** — the 800-hit sample is biased toward
   the *most* visible results, which makes 5 % coverage a conservative reading, not an inflated one.
4. **No dated model snapshot for Luna.** `codex exec --json` emits no model field, and the model
   self-reported `"gpt-5"` / `"GPT-5"` (101/7), not a snapshot id. Rows record the requested slug,
   effort and CLI version. The judge is pinned by request; that is the strongest claim available.
5. **Haiku ran on 5 rows.** No meaningful inter-judge agreement rate can be computed from that.
6. **v0 payload bytes were never frozen**, so a v0-vs-v1 comparison is currently
   database-only.
7. **Nothing here measures retrieval or task success.** `included` means "a judge believes this is
   a real skill." It does not mean an agent would be better off with it.

## 8. Suggested next steps

1. Ship the two prod snapshots that unblock everything: `skills_library/` and `crawl_state.json`.
2. Re-run `run1_sweep.py` — it resumes at slice `[0,24000]` with no re-work, and the four pending
   slices plus query 2 are where the volume is.
3. Fix the `risk_flags` prompt (finding A) before any gate consumes it.
4. Treat genericity as a **separate** discriminator (finding B); the current judge cannot see it.
5. Decide what to do about the 44 % of corpus rows that have no entrypoint (finding C) — either
   resolve them to a skill file or stop counting them as skills.

---

## 9. Full hand-review lists for Sami

Every `quarantine` and `excluded_junk` skill, with a one-line reason.
Machine-readable equivalent: `run1_combined_v1.json`.

### Quarantine (2) — kept, penalised, never silently dropped

| skill | source | judges | one-line reason |
|---|---|---|---|
| `cs-aeo` | github_skill_file | Luna reject (0.98) vs Haiku keep (0.72) | judges disagree: primary reject, secondary keep |
| `sprint-health` | github_skill_file | Luna reject (0.93) vs Haiku keep (0.72) | judges disagree: primary reject, secondary keep |

### Excluded as junk (94)


**`entrypoint_absent` — 82**

| skill | source | one-line reason |
|---|---|---|
| `main docs` | anthropic_docs | entrypoint absent |
| `main docs` | anthropic_docs | entrypoint absent |
| `**claude-code-container**` | awesome_list | entrypoint absent |
| `MarceauSolutions/md-to-pdf-mcp` | awesome_list | entrypoint absent |
| `cablate/mcp-google-map` | awesome_list | entrypoint absent |
| `juergenkoller-software/freezetext-mcp` | awesome_list | entrypoint absent |
| `kaggle-skill` | awesome_list | entrypoint absent |
| `AI-Engineering-Team-Demo` | github | entrypoint absent |
| `Ai-engineering-From-Scratch-` | github | entrypoint absent |
| `AnthropicSkillJar` | github | entrypoint absent |
| `Budy` | github | entrypoint absent |
| `Codepet-ver-1.2` | github | entrypoint absent |
| `MannokIntegrations` | github | entrypoint absent |
| `ORRERY` | github | entrypoint absent |
| `Project-PKL` | github | entrypoint absent |
| `SemiFin-AI-Daily` | github | entrypoint absent |
| `TheHOG-GTM-OS` | github | entrypoint absent |
| `agent` | github | entrypoint absent |
| `agent-skills` | github | entrypoint absent |
| `agentic-coding-standards` | github | entrypoint absent |
| `andale` | github | entrypoint absent |
| `at-dev-harness` | github | entrypoint absent |
| `audiobook` | github | entrypoint absent |
| `caelo` | github | entrypoint absent |
| `claude-app-server` | github | entrypoint absent |
| `claude-config` | github | entrypoint absent |
| `claude-handoff` | github | entrypoint absent |
| `claudedotmd` | github | entrypoint absent |
| `commit-report-tool` | github | entrypoint absent |
| `context-engineer` | github | entrypoint absent |
| `cs-automation-skills` | github | entrypoint absent |
| `decantr` | github | entrypoint absent |
| `douzonebot-plugin` | github | entrypoint absent |
| `e2e-agent-skills` | github | entrypoint absent |
| `fleet-skill` | github | entrypoint absent |
| `gaozhong-yuwen-skills` | github | entrypoint absent |
| `imessage-rich-search` | github | entrypoint absent |
| `impeccable` | github | entrypoint absent |
| `industry-atlas` | github | entrypoint absent |
| `insurancexdate-mcp` | github | entrypoint absent |
| `janus` | github | entrypoint absent |
| `m5-petit-relations` | github | entrypoint absent |
| `mcp-twfood` | github | entrypoint absent |
| `openclaw-data-analyst-skills` | github | entrypoint absent |
| `paper-reads` | github | entrypoint absent |
| `paseo-relay` | github | entrypoint absent |
| `pbi-theme` | github | entrypoint absent |
| `pessoa` | github | entrypoint absent |
| `ping` | github | entrypoint absent |
| `pm-operating-system` | github | entrypoint absent |
| `prd-scorer` | github | entrypoint absent |
| `proactive-digital-twin-workshop` | github | entrypoint absent |
| `pursuitvision` | github | entrypoint absent |
| `qkt-lab` | github | entrypoint absent |
| `research-harness-template` | github | entrypoint absent |
| `rizzdev-detective` | github | entrypoint absent |
| `security-posture-skill` | github | entrypoint absent |
| `self` | github | entrypoint absent |
| `skills` | github | entrypoint absent |
| `suno-mcp` | github | entrypoint absent |
| `wise-mcp` | github | entrypoint absent |
| `worklog` | github | entrypoint absent |
| `yi5oyu` | github | entrypoint absent |
| `appicon` | glama_registry | entrypoint absent |
| `arena-mcp` | glama_registry | entrypoint absent |
| `localllm-MCP` | glama_registry | entrypoint absent |
| `ai.agentberg/agentberg` | mcp_official_registry | entrypoint absent |
| `ai.apithreshold/apithreshold` | mcp_official_registry | entrypoint absent |
| `ai.auteng/docs` | mcp_official_registry | entrypoint absent |
| `@kubb/mcp` | npm | entrypoint absent |
| `@rex_koh/subagent-budget-guard` | npm | entrypoint absent |
| `square-mcp-server` | npm | entrypoint absent |
| `ActuallyCare` | pulsemcp_registry | entrypoint absent |
| `Templonix Lite` | pulsemcp_registry | entrypoint absent |
| `io.github.Akhilgovind02/india-stock-mcp` | pulsemcp_registry | entrypoint absent |
| `skill-doc-enhancer` | skillsmp | entrypoint absent |
| `Financial Modeling Prep` | smithery_registry | entrypoint absent |
| `LittleSis API Server` | smithery_registry | entrypoint absent |
| `RSS Reader` | smithery_registry | entrypoint absent |
| `/clev?event=StartpageResultClick&sc=AR5cSbeljcuusy7sX8EUeNwyFSiCMTu2UZEoabalF1WicLMd3c8sFXqAdzyS89mqTJk6cFr3FH2z8Pl0y8C8QAWgtmLk3H&payload={"bdsSessionId":"e995ef2960d140f0ab32056a8b3c5c15","cheqId":"","countryCode":"US","deviceType":"desktop","endpoint":"search.serp","hasGoogleAds":true,"page_id":"YaKtzFbddk0DMqXi","queryCategory":"web","segment":"startpage.udog","session_id":"rVyfrrmGGb1ibIAH","surface":"serp-web","transport":"href-request"}` | web_search | entrypoint absent |
| `Claude Code MCP: Give Your Coding Agent a Real... - Browserbeam` | web_search | entrypoint absent |
| `Top 5 GitHub Repositories for Free Claude Skills (1000+ Skills)` | web_search | entrypoint absent |

**`frontmatter_invalid` — 4**

| skill | source | one-line reason |
|---|---|---|
| `chaos-experiment` | github_skill_file | frontmatter has no name/title |
| `cs-frontend-review` | github_skill_file | frontmatter has no name/title |
| `poster` | github_skill_file | frontmatter has no name/title |
| `slo-design` | github_skill_file | frontmatter has no name/title |

**`judged: both` — 3**

| skill | source | one-line reason |
|---|---|---|
| `BadSkill_Example` | github_skill_file | This is benchmark fixture content, not a usable skill with procedural guidance. |
| `TEMPLATE` | github_skill_file | This is a placeholder template with unfilled fields and no usable procedure. |
| `changelog` | github_skill_file | It provides usage examples but no self-contained procedural guidance, and its referenced dependencies are absent from the file tree. |

**`fetch_failed` — 3**

| skill | source | one-line reason |
|---|---|---|
| `bakabo-popup-ai` | github_skill_file | entrypoint could not be fetched from source |
| `promote-skill` | github_skill_file | entrypoint could not be fetched from source |
| `writing-skills` | github_skill_file | entrypoint could not be fetched from source |

**`frontmatter_missing` — 1**

| skill | source | one-line reason |
|---|---|---|
| `README` | github_skill_file | missing YAML frontmatter |

**`body_too_short` — 1**

| skill | source | one-line reason |
|---|---|---|
| `template-skill` | github_skill_file | body under 200 chars |
