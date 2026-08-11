# Ingestion run 1 — Phase 2: Oxcaml bisect

Diagnosis only. No pipeline code was changed, and no fix was applied.

Inputs: frozen v0 corpus `backend/corpus_v0_frozen/corpus_v0.sqlite`
(SHA-256 `c8485180753e…`, 271,957 skills, `integrity_check ok`) plus live GitHub metadata.
Evidence file: `backend/bench/run1_bisect_evidence.json`. Script: `backend/bench/run1_oxcaml_bisect.py`.

---

## Verdict: **never-swept — coverage gap**

`oxcaml/oxcaml` was never discovered. It was not found and then dropped, and GitHub's index is
not at fault: the file is indexed and returned by the exact deep-sweep query today. The deep
sweep simply never enumerated the result set that contains it, and the live sample says it never
came close.

---

## (a) Does ANY row anywhere reference `oxcaml/oxcaml`? — **No.**

A substring scan for `oxcaml` ran across **17 tables** — every non-PII table in the snapshot,
covering `skills`, `skill_packages`, `skill_package_files`, `skill_package_sources`,
`skill_retrieval_records`, `skill_versions`, `skill_pins`, `skill_watches`, `skill_tools`,
`private_skills`, `collections`, `collection_skills`, `scrape_runs`, `skills_sh_mirror`,
`skills_sh_sources`, `skills_sh_ingestion_attempts`, `schema_migrations`.

Three substring hits. **None is the target repo:**

| table | row | what it actually is |
|---|---|---|
| `skills_sh_mirror` | `mizchi/skills/nix-setup` | unrelated Nix skill whose description happens to list "OxCaml" among language templates |
| `skills_sh_mirror` | `aresbit/matebot/oxcaml` | a skills.sh entry named `oxcaml` from `aresbit/matebot`, not `oxcaml/oxcaml` |
| `skills_sh_mirror` | `smithery.ai/oxcaml` | a skills.sh registry listing named `oxcaml` |

Zero rows in `skills`, `skill_packages`, `skill_package_sources`, or any crawl artifact.

**Why this rules out "found-then-dropped."** The pipeline does not delete rejects, it keeps them:
the snapshot holds **52,730 `rejected` `github_skill_file` rows** and 89,672 `rejected` `github`
rows. A skill that was fetched and then failed frontmatter validation or a quality gate would
still be sitting there with `quality_status='rejected'` and a `raw.parent_repo` of
`oxcaml/oxcaml`. Nothing is. The record was never created, so the drop happened **before**
persistence — i.e. at discovery.

## (b) Did the deep sweeps ever run to completion? — **No, and not close.**

`deep_sweep.last_sweep_at` could not be read directly: `scraper.py:539` puts
`CRAWL_STATE_PATH` at `backend/skills_library/crawl_state.json`, inside the package directory
that exists only on the production host, and this run is barred from touching prod
(**FLAG 2** in the preflight). The question is therefore answered by measurement rather than by
a stored timestamp.

**Proxy: live code search vs. corpus coverage.** For each sweep query, sample the live result
set and ask how many of those `(repo, path)` pairs the corpus already knows.

| sweep query | live `total_count` | sampled | present in corpus | coverage |
|---|---:|---:|---:|---:|
| `path:.claude/skills filename:SKILL.md` | 75,480 | 400 | 20 | **5.0 %** |
| `path:.claude/skills` | 108,224 | 400 | 6 | **1.5 %** |

The corpus holds 118,160 `(repo, path)` pairs across 9,152 distinct repos, and 38,400 rows whose
path sits under `.claude/skills/`. Against that, a 5 % hit rate on the query's own top results is
not a rounding error — it is a sweep that stopped early.

The misses are not exotic repos, and many are in repos the corpus **already knows**
(58/380 and 78/394 misses are `repo_known_to_corpus: true`) — e.g. `linuxfoundation/crowd.dev`
`.claude/skills/dco/SKILL.md`, `woocommerce/woocommerce-ios` `.claude/skills/pr/SKILL.md`,
`srid/emanote` `.claude/skills/be/SKILL.md`. The crawler reached those repositories and still did
not enumerate their skills files.

**Corroborating: collection has been stopped since 2026-07-15.** The most recent `scrape_runs`
row is `2026-07-15T05:33:58Z`, status `stale`, error
`"Production scraping retired; collection moved off-host."` The last successful GitHub run
finished `2026-07-15T05:33:40Z`. So the sweep rotation has not advanced in the ~18 days before
the snapshot, and whatever slice cursor it held is frozen mid-rotation.

## The target itself is healthy and indexed — this is not an index gap

| probe | result |
|---|---|
| `GET /repos/oxcaml/oxcaml/contents/.claude/skills/address-review/SKILL.md` | **200**, size 4,515 B, sha `c5d08115…` |
| repo metadata | public, **828 stars**, not a fork, not archived, default branch `main`, pushed `2026-08-02` |
| `repo:oxcaml/oxcaml path:.claude/skills filename:SKILL.md` | **1 hit** — `.claude/skills/address-review/SKILL.md` |
| `repo:oxcaml/oxcaml path:.claude/skills` | **3 hits** — `SKILL.md` + `scripts/gh-comments.py` + `scripts/pr-comment-threads.py` |
| `repo:oxcaml/oxcaml filename:SKILL.md` | **1 hit** |
| file first committed | **2026-02-03T22:20:46Z** ("Address review skill (#5367)"), single commit, never modified |

The file has been public, indexed, and unchanged since **2026-02-03** — more than five months of
active collection before the crawler was retired on 2026-07-15. GitHub returns it for the exact
query the sweep uses. There was ample opportunity and no index obstacle.

Worth noting for Phase 4/5: the `path:.claude/skills` query also surfaces the skill's two
**companion scripts**, which the current entrypoint-only collector would not retain even if it
had found the SKILL.md. That is the separate data-completeness problem, not this one.

---

## What this rules in and out

| hypothesis | status | basis |
|---|---|---|
| found-then-dropped (pipeline bug in tree crawl / frontmatter / quality gate) | **ruled out** | rejects are retained (52,730 rejected `github_skill_file` rows); no oxcaml row in any state |
| swept-but-absent (GitHub index gap) | **ruled out** | the exact sweep query returns the file today, from a 828-star public repo, unchanged since 2026-02-03 |
| **never-swept (coverage gap)** | **confirmed** | 5.0 % / 1.5 % coverage of the live sweep result sets; sweep rotation frozen since 2026-07-15; misses include repos already in the corpus |

## Honest limits of this diagnosis

1. **No `crawl_state.json`**, so there is no timestamp proving a sweep was or was not marked
   complete. The completeness claim rests on measured coverage, not on the crawler's own record.
   Shipping `backend/skills_library/crawl_state.json` from prod would upgrade this from proxy to
   direct evidence.
2. **GitHub code search sorts by "best match," not randomly**, so the 800 sampled hits are not a
   uniform sample. This biases the sample toward the *most* visible results — which makes a 5 %
   coverage rate a conservative reading, not an inflated one.
3. Coverage was measured on `(repo, path)` pairs extracted from `skills.raw`. Rows without both
   fields cannot participate; 118,160 of 271,957 rows carry both.
4. This phase explains why `oxcaml/oxcaml` is missing. It does **not** establish that fixing
   discovery would improve routing or agent task success — those are separate, still-unproven
   claims.
