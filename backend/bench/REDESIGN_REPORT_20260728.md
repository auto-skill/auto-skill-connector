# Evidence-backed Auto-Skill redesign — 2026-07-28

## Decision

The redesign is implemented behind conservative delivery gates. A frozen,
pre-existing 23-task dataset produced a provisional held-out top-1 retrieval
gain, but the sample is too small and its confidence interval includes zero.
No production win is claimed. Public routes remain hint-first; an exact
distilled-capsule digest needs independent outcome evidence before full capsule
delivery. Raw scraped `SKILL.md` is never delivered.

## Evidence retained

- Public-instruction query synthesis remains the strongest retrieval
  hypothesis: the controlled Terminal-Bench result improved exact top-5 from
  2/89 to 16/89 and top-1 from 0/89 to 7/89.
- Complete package capture is required for provenance and reference integrity,
  but package completeness is separated from embedding policy.
- One flat, entrypoint-first record capped at 1,500 characters is the retrieval
  representation. The prior 24-mapping result favored this over longer and
  all-file chunking.
- Raw-skill injection is rejected as a default treatment: the retained paired
  result was 0 wins, 1 loss, 2 ties, with control 3/3, treatment 2/3, and
  39.8% higher cost.
- Relevance is not utility. Candidates are classified as primary, supporting,
  policy, irrelevant, or harmful/conflicting before delivery.
- Abstention is the default. Supporting content cannot take the primary slot,
  and full delivery is keyed to the exact task/provenance-bound capsule digest.

## Hypotheses rejected or not promoted

- Embed every captured file or every chunk: rejected by the controlled oracle
  retrieval regression.
- Inject raw public skill bodies: rejected by the paired task result and the
  strategy-displacement risk.
- Treat semantic relevance as enough to route: rejected; the old synthesized
  gate surfaced 5 good and 4 false-positive tasks.
- Attribute hosted gains before corpus/vector parity: rejected. Hosted control
  was routing v3 while the branch is v6.
- Expand to an unbounded marketplace crawl: rejected. SkillsMP discovery is
  capped at 100 unique URLs per run by default; unpinned GitHub results are
  quarantined from embedding.

## Implemented controls

1. A deterministic structured query compiler extracts technology, operation,
   artifact, constraints, and failure mode. Retrieval fuses the original and
   compiled queries with reciprocal-rank fusion. The compiler accepts public
   task text and coarse project tags only, never task IDs, targets, or labels.
2. Immutable package capture stores complete bounded file sets, paths, SHA-256
   and Git blob hashes, source commit or registry snapshot identity, license,
   provenance, dependency closure, unresolved references, truncation state,
   and file roles. Forks deduplicate by package content while all source aliases
   remain in `skill_package_sources`.
3. Retrieval records are normalized, compact, versioned objects distinct from
   source packages. Only the entrypoint view is embedded by default.
4. The capsule compiler removes agent-control instructions and credential-like
   examples, omits unresolved-reference procedures, marks destructive and
   external actions, bounds output to 2,400 characters, pins provenance and
   confidence, and reiterates that the user task is primary.
5. Candidate-role thresholds are separate: primary 0.80, supporting 0.72, and
   curated policy 0.95. Harmful/conflicting candidates never surface.
6. Public full delivery requires `AUTOSKILL_VALIDATED_CAPSULE_DIGESTS`. The old
   raw-content digest gate was removed. Metrics count capsule tokens separately
   from raw-content tokens.
7. GitHub tree URLs are resolved to commit-pinned packages. The optional
   authenticated skills.sh curated API stores complete registry snapshots.
   Raw GitHub fetches without immutable package closure are `pending_package`
   and are not embedded.

## Held-out query result

Artifact: `evidence-query-heldout-20260728.json`

- Dataset: pre-existing `bench/tasks.jsonl`, SHA-256
  `1b971f9a29cb420c9bf4e1b4cae3e2bed4596c3f1eff6b09789380aa33e10353`.
- Split was fixed as `sha256(public_prompt) modulo 10 < 4`; 13/23 rows were
  held out. The compiler saw public prompt text only.
- Control: BM25 over the original prompt. Treatment: RRF over original plus
  structured query.
- Top-1: 12/13 to 13/13; 1 paired win, 0 losses, 12 ties; +7.69 percentage
  points; paired bootstrap 95% CI [0.00, 23.08]; exact McNemar p=1.0.
- Top-5: 13/13 in both arms, so the proxy was at ceiling.
- Target-record precision proxy: 100%, Wilson 95% CI [77.19%, 100%]. The lower
  bound does not clear the >=80% production precision target.

This is a genuine paired held-out top-1 gain, but it is not statistically
conclusive and the candidate corpus is an intent-retrieval proxy rather than
the production skill corpus. It is sufficient to retain the compiler for the
next gate, not to enable full delivery.

## Validation

- Backend suite: 306 tests, 0 failures, 0 errors.
- Root client suite: 146 tests passed.
- New evidence/package/capsule gates: 13 tests passed.
- Ruff passed for every changed Python file.
- No push or deployment was performed.

## Limitations

- Exact v6 production corpus/vector parity was not runnable from this worktree
  because no production v6 database snapshot was provided. The new `parity`
  command fails on missing or stale per-row retrieval hashes.
- No new agent-outcome A/B was run. It requires costly external task execution,
  at least two replicates per condition, and tasks where no-skill control is not
  already perfect. The new `outcomes` command enforces those requirements and
  reports pass rate, paired wins/losses, confidence intervals, cost, latency,
  tokens, safety failures, and strategy displacement.
- skills.sh GitHub snapshots expose a registry content hash but not an exact Git
  commit in the detail response. The registry hash is retained separately and
  is not represented as a Git commit SHA.
- The held-out sample is only 13 tasks and its top-5 metric is saturated.

## Next production gate

1. Freeze at least 60 fresh task/candidate pairs from domains not used in the
   89 Terminal-Bench tasks or this 23-task proxy. Pre-register the split and
   judging rubric before running the compiler.
2. Rebuild the v6 corpus exclusively from complete packages and 1,500-character
   records, regenerate every vector, and require exact parity with zero missing
   or mismatched rows before comparing to hosted routing v3.
3. Require surfaced-route precision >=80% with a confidence interval reported,
   zero harmful routes, and no material false-positive regression. Continue
   hint-only if the precision lower bound remains below 80%.
4. On non-ceiling tasks, run no-skill, raw-skill, and distilled-capsule with at
   least three replicates per condition. Promote only if distilled capsule has
   positive paired pass-rate evidence, no safety failures, no strategy
   displacement regression, and acceptable cost/latency/token deltas.
5. Add only the exact winning capsule digests to the full-delivery allowlist,
   then canary those digests. Everything else remains a hint or abstention.
