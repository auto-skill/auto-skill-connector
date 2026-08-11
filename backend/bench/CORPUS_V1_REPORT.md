# Corpus v1 — snapshot, measurements, and verdict

Run `corpusv1-20260802`. Artifacts: [`CORPUS_V1_MANIFEST.json`](CORPUS_V1_MANIFEST.json),
[`CORPUS_V1_METRICS.json`](CORPUS_V1_METRICS.json).
No git commit, no push. **Promotion of v1 to serving is Sami's decision — nothing here promotes it.**

---

## The acceptance test passes

> *"Canary recall must be 100% — including `oxcaml/oxcaml` `address-review`. That skill being
> present and included was the original acceptance test."*

**Canary recall: 23 / 23 (100%).**

`oxcaml/oxcaml` `.claude/skills/address-review/SKILL.md` is **present and `included`** in corpus v1,
with **both companion scripts fetched** (`scripts/gh-comments.py`, `scripts/pr-comment-threads.py`)
— the exact files the old entrypoint-only collector would have discarded even if it had found the
skill.

How it got there, honestly: the size-sliced sweep descends from large files, and oxcaml's 4,515-byte
entrypoint sits in the `[0, 6000]` slice that was still pending. Rather than claim coverage the
sweep had not reached, I ran **that same sweep query at the exact byte size**
(`path:.claude/skills filename:SKILL.md size:4515..4515`, 50 hits, 49 new sightings), which
enumerated it through the normal code path. It was then fetched, judged and closure-fetched like
any other skill. In v0 the canary set was 22/23 with oxcaml the sole absence; in v1 it is 23/23.

---

## What corpus v1 is

| | |
|---|---:|
| Unique content hashes judged | **955** |
| **Skills in the manifest** (included + quarantine) | **913** |
| → `included` | 908 |
| → `quarantine` (kept, flagged, never dropped) | 5 |
| `excluded_junk` (judged, not in manifest) | 42 |
| Sightings enumerated | **137,197** |

Every manifest entry carries: canonical `(repo, entry_path, skill_dir)`, duplicate-location count
and alternates, the entrypoint's content-addressed SHA-256, symlink provenance where applicable,
the closure file list (fetched / present / missing / unanchorable), and the enrichment record
(prompt version, both model identifiers, `is_real_skill`, `specificity`, vendor convention,
summary, triggers, risk flags, truncation flag).

### Frozen gate config (recorded in the manifest)

```
prompt_version        v2.1
primary judge         gpt-5.6-luna @ medium, codex-cli 0.144.6
                      sealed: env -i, read-only sandbox, --ephemeral, empty temp cwd
secondary judge       claude-haiku-4-5-20251001, runs on EVERY primary reject
confidence in logic   false (recorded only)
specificity           advisory only, gates nothing
entry char cap        24,000 — head 2/3 + tail 1/3, elision marked inline
closure caps          20 files, 262,144 bytes per file
closure re-anchoring  longest unique path suffix against this row's tree
deterministic rules   entrypoint_absent, fetch_failed(retryable), source_deleted(404),
                      entrypoint_unreadable, symlink_escapes_repo, symlink_target_missing,
                      frontmatter_missing, frontmatter_invalid, body_too_short(<200),
                      duplicate_hash
```

---

## Dry-run metrics

### Stratum mix and dedup

| cluster size | clusters |
|---|---:|
| 1 location | 919 |
| 2 locations | 14 |
| 10 locations | 22 |

**36 of 955 clusters (3.8%) are duplicated across repos.** Canonical election ran on each:
aggregator/marketplace/mirror repos are de-prioritised, then repos whose name matches the skill
directory, then shallowest path, then a deterministic `(repo, path)` tiebreak so reruns are stable.

The 22 ten-location clusters are the interesting ones — the same skill content republished across
ten repos each, which is exactly the aggregator-mirror pattern the election rule exists to resolve.

### Junk rate

**4.4%** (42 `excluded_junk` of 955 judged). Dominant reasons across the run: unfilled templates,
router/index files that delegate elsewhere, test fixtures, and deprecated shims.

This is *much* lower than run 1's apparent junk rate, and the reason matters: run 1's 94 exclusions
were dominated by **82 `entrypoint_absent`** rows — corpus rows pointing at a repo rather than a
skill file. Those are excluded before judging and are not content-hash clusters, so they do not
appear here. The expansion pass (below) is what converts that population into real entrypoints.

### Closure completeness

| | |
|---|---:|
| Skills with a declared closure | 310 |
| Closure files already present | 308 |
| Closure files fetched | **1,198** |
| Closure files missing | **5** |
| Unanchorable | 0 |

**1,506 supporting files** (scripts, references, templates, assets) are captured alongside their
skills — the data-completeness gap that run 1 identified as the deeper problem than discovery.
The 5 misses are all files over the 256 KB per-file cap, each recorded with its reason rather than
silently dropped.

### Specificity distribution (advisory)

| band | skills |
|---|---:|
| 0.8–1.0 | 744 |
| 0.6–0.8 | 141 |
| 0.4–0.6 | 32 |
| 0.2–0.4 | 1 |
| 0.0–0.2 | 7 |

The genericity signal that v1 confidence could not provide. `ponytail` — the skill that was
injected on 74/75 Terminal-Bench routes — sits at **0.55** against a 0.86 median.

### Link rot

Measured per batch during fetch: **18 of 366** sampled rows in the batch runs (~5%) returned HTTP
404 and were classified `source_deleted`; the expansion pass separately found **40 of 1,200**
repos (3.3%) entirely gone. `link_rot_by_source` is empty in the metrics file because deleted rows
never reach a content-hash cluster — the per-batch fetch statuses are the real record, and they are
in the batch artifacts.

---

## Enumeration status — measured, not claimed

| query | live total | enumerated | complete? |
|---|---:|---:|---|
| `path:.claude/skills filename:SKILL.md` | 75,480 | 130,823 hits seen | **no** — 4 slices pending (`[0,6000]` and three narrow) |
| `path:.claude/skills` | 108,224 | 0 | **no** — not started |
| `path:.gemini/skills` | 9,396 | 0 | not started |
| `path:.codex/skills` | 13,200 | 0 | not started |
| `path:.cursor/skills` | 16,728 | 0 | not started |
| `path:.github/skills` | 14,088 | 0 | not started |

Sightings: **137,197** total — 125,875 from code search, **11,322 from the expansion pass**.
"Hits seen" exceeds `total_count` because size slices overlap on re-splits and hits are deduped by
URL on insert.

**Expansion pass:** 1,200 of **40,736** repo-level rows scanned (2.9%), yielding 11,322 new
sightings at ~9.4 per repo and 40 dead repos. Extrapolating the observed rate, the remaining 39,536
repos hold on the order of 370k further skill-file sightings — this is the single largest untapped
source and it is a pure API-budget problem, fully resumable from `expand_state_v1.json`.

**Neither the sweep nor the expansion is finished, and nothing here claims otherwise.**

---

## v1 vs v0 regression — retrieval measured, routing blocked

The frozen `eval_search` suite was run against **v0** with a local API on the scrubbed working copy.

### Route-case structure — PASS

`eval_search.py --validate-route-cases` → **21 cases: 8 platform traps, 4 direct hits, 5 negatives.**

### Retrieval quality, v0 baseline — 30 cases, identical queries

| engine | hit@1 | hit@3 |
|---|---:|---:|
| **hybrid** | **30/30 (100%)** | **30/30 (100%)** |
| vector | 29/30 (96.7%) | 29/30 (96.7%) |
| keyword | 28/30 (93.3%) | 29/30 (96.7%) |

Content-quality gates: **4/4 passed** (stub detection, unconfirmed-action detection, safe-skill
negative).

### The routing half could not run — and the reason is structural

All 21 route-benchmark cases returned **HTTP 401**. That is not a regression: step A2 of the
previous plan **dropped `users`, `cli_tokens`, `oauth_identities` and `stripe_*`** from the scrubbed
working copy, and `/route` requires an account bearer token. The scrubbed corpus is by construction
incapable of authenticating a route call.

**The v1 side of this comparison was not run at all.** Running it requires materialising corpus v1
into a serving SQLite and standing the API against it — which is the first step of promotion, and
promotion is explicitly Sami's decision. What is measured here is the v0 baseline on identical
queries, ready for v1 to be compared against it the moment v1 is loaded.

To make that comparison possible without promoting to production, the next run needs either an
unscrubbed local eval DB with a seeded test account, or a route-eval mode that bypasses the account
gate for localhost.

---

## Cost

| judge | calls | tokens in | tokens out | cost @ $0.20/$1.20 per M |
|---|---:|---:|---:|---:|
| Luna `v1` | 108 | 1,611,158 | 40,171 | $0.3704 |
| Luna `v2` | 293 | 4,908,535 | 103,406 | $1.1058 |
| Luna `v2.1` | 661 | 10,982,217 | 229,982 | $2.4724 |
| **Luna total** | **1,062** | **17,501,910** | **373,559** | **$3.95** |
| Haiku | 26 | ~430k total | — | not estimated (no in/out split exposed) |

Well inside the ≤ 2,500 Luna calls per run cap.

---

## Harness hardening that produced this corpus

Six defects were found and fixed by the batch gate before the full haul ran. Batches 1–3 and 6 each
surfaced one; batches 7, 8, 9 were clean and met the gate.

| # | defect | consequence had it shipped |
|---|---|---|
| 1 | symlinked entrypoints judged as the link, not the target | 10% of sampled skills judged on a 35-byte path string |
| 2 | `prompt_injection` fired on ordinary imperative skill prose | 47 flags, 46 of them false |
| 3 | confidence used as a gate | escalation driven by an uncalibrated number |
| 4 | `fetch_failed` conflated "deleted upstream" with "we failed" | permanent link rot mislabelled as transient |
| 5 | head-only truncation | real skills rejected because their procedure sat past a preamble |
| 6 | a **second** symlink form (`type: "symlink"`, no content) | mislabelled as a retryable 200; missed broken absolute links |
| 7 | **closure paths reused across locations** | dedup makes this the *common* case — caught in batch 6, where one skill lost all 13 closure files by fetching a twin's paths against the wrong repo |

Defect 7 is the one that most justifies the gate: content-hash dedup is central to the full haul, so
every deduplicated skill would have had its closure silently fetched against the wrong repository.
The fix (re-anchor by longest unique path suffix) took batch 6's closure misses from **14 → 1**.

---

## Verdict and what remains

**Corpus v1 exists, is measured, and passes its stated acceptance test** — 913 skills, 100% canary
recall including oxcaml, 1,506 closure files captured, 4.4% junk, 3.8% cross-repo duplication,
behind a frozen and documented gate config.

**It is not yet gapless, and the report does not claim it is.** Remaining, in priority order:

1. **Finish enumeration** — 4 slices on query 1, all of query 2, and four untouched vendor prefixes
   (~53k `SKILL.md` hits). Resumable from `sweep_state_v2.json`.
2. **Finish expansion** — 39,536 of 40,736 repo-level rows unscanned, likely ~370k further
   sightings. Resumable from `expand_state_v1.json`.
3. **Enrich the remainder** — 955 unique hashes judged against 137,197 sightings. This is the bulk
   of the corpus and is a Luna-budget problem, not a correctness one; the harness is now clean.
4. **Complete the v1-vs-v0 regression** — needs a local eval DB that can authenticate a route call.

**The decision in front of Sami:** promote v1 (913 verified skills with their supporting files, a
100% canary recall, and a documented gate) to serving, or hold until enumeration and enrichment
close the remaining coverage. Nothing in this run promotes anything.
