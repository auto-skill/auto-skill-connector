# Auto-Skill benchmark iteration — 2026-07-21/22

## Decision

Keep the independent global-vector retrieval lane, bounded active-candidate filters,
and the separate pending-embedding hint lane. The blinded 89-task routing evaluation
shows a statistically supported retrieval gain. Do not enable silent public skill
injection: absolute candidate precision is still too low, and task-success A/Bs have
not shown a win over no Auto-Skill.

## Models and frozen inputs

- Terminal-Bench task execution: Harbor 0.20, Codex 0.145.0,
  `gpt-5.6-sol`, `reasoning_effort=max`, reasoning summaries and web search disabled.
- Route relevance judge: `gpt-5.6-sol`, max reasoning, Codex CLI
  `0.145.0-alpha.18`, executable SHA-256
  `20d611ef1c9851f4da1cb4609beb6763904f72275cb91517b2400639ca1c28c4`.
- Terminal-Bench dataset:
  `terminal-bench/terminal-bench-2-1@sha256:7d7bdc1cbedad549fc1140404bd4dc45e5fd0ea7c4186773687d177ad3a0699a`.
- Routing relevance uses only public task instructions and immutable skill snapshots.
  It does not read task verifiers, solutions, tests, or grading rubrics.

## Confirmed routing gain

The control is the prior production retrieval replay. The treatment adds an
independent global vector candidate lane, then applies the bounded active-candidate
filters and deterministic reranking. Each arm has 89 tasks and 445 top-five skill
bodies. A fresh blinded judge labeled every body E (exact), A (adjacent), or I
(irrelevant/unusable). Every prompt, raw model output, label, body digest, CLI event
stream, and token count is retained in the evaluation artifacts.

| Metric | Control | Treatment | Paired difference | 95% CI | Exact McNemar p |
|---|---:|---:|---:|---:|---:|
| Exact top-1 task hit | 0/89 (0.0%) | 8/89 (9.0%) | +9.0 pp | +3.4 to +15.7 pp | 0.0078125 |
| Any exact body in top 5 | 1/89 (1.1%) | 27/89 (30.3%) | +29.2 pp | +20.2 to +39.3 pp | 2.98e-8 |
| Exact candidate precision | 1/445 (0.2%) | 48/445 (10.8%) | +10.6 pp | +6.5 to +15.1 pp | task-cluster bootstrap |

The paired outcomes contain no losses: 8 wins/81 ties at top 1 and 26 wins/63
ties for top-five task coverage. Candidate confidence intervals resample whole task
clusters, not individual candidates.

The confidence gate surfaces 28/89 treatment tasks versus 3/89 control tasks. Among
surfaced treatment tasks, 14/28 contain an exact body in the top five (50.0% route
precision) versus 0/3 for control. Gate-aware exact top-five routing therefore rises
from 0/89 to 14/89: +15.7 pp, paired 95% CI +9.0 to +23.6 pp, 14 wins/0 losses,
McNemar p=0.000122. Gate-aware exact top-1 rises from 0/89 to 5/89, but its
two-sided p=0.0625 does not clear 0.05. The gate recalls 14/27 treatment tasks that
have an exact top-five candidate and surfaces 14 false positives. These absolute
figures reinforce the hint-only decision.

This supports the retrieval hypothesis. It does not support full injection. Even
after the gain, 215/445 treatment candidates are I and only 48/445 are E. New public
matches therefore remain hints unless the exact served-byte content digest has
separate outcome validation.

## Task-success evidence

The original full Terminal-Bench 2.1 run used `gpt-5-nano-2025-08-07`, one attempt
per arm: Auto-Skill 3/89, no Auto-Skill 3/89, 2 wins, 2 losses, 85 ties,
McNemar p=1.0. Routing collapsed: 74 of 75 selected skills were generic `ponytail`.

A prior controlled six-task response-quality A/B was negative: baseline mean 97.92,
routed mean 90.42, delta -7.50, paired bootstrap 95% CI -12.08 to -3.54,
0 wins/1 tie/5 losses.

Under the stronger Sol/max execution model, the first five-task extractive-capsule
screen was all ties because every control and treatment passed. A second two-task
screen (`financial-document-processor`, `git-leak-recovery`) was also all ties:
2/2 control and 2/2 treatment passed. Treatment was slower on both second-screen
tasks (mean +57.1 agent seconds). The invalid concurrent package-cache failure was
excluded and rerun alone. No three-replicate confirmation was launched because both
screens were ceiling cases.

A later control-only screen deliberately selected tasks whose retrieved bodies were
judged E. `dna-insert` and `pypi-server` produced clean no-skill competence failures.
The production extractive capsule builder abstained on both despite the relevant
bodies, exposing a generation-layer recall failure. Sol/max task adaptation then
produced matched-budget capsules that independently cleared the offline audit, but
both adapted Harbor treatments still failed with the same verifier failure as their
controls: the DNA primers retained an 8.19 C Tm mismatch, and the local PyPI server
still did not provide `vectorops==0.1.0`. DNA treatment was 59.7 agent seconds slower;
PyPI treatment was 9.9 seconds faster, but neither changed reward. This adaptation
hypothesis is rejected for these two screens and was not escalated to replication.

Conclusion: Auto-Skill now beats the old retrieval control on routing relevance. It
does not yet beat no Auto-Skill on task pass rate.

Across all nine valid Sol/max task pairs run in this iteration, treatment recorded
0 wins, 0 losses, and 9 ties: seven tasks passed in both arms and two failed in both
arms. Treatment was slower in 8/9 pairs, with an unweighted mean agent-time delta of
about +44.4 seconds across heterogeneous extractive and adapted treatments. This is
not evidence of task utility; it is a reason to keep body adaptation out of the live
path until a better hypothesis survives a new control.

## Changes retained

1. Global vector retrieval is no longer restricted to the lexical top 60.
2. Only active candidates with bounded routing descriptions enter the public union.
3. Pending-embedding lexical candidates occupy at most the final hint slot and
   cannot truncate the semantic lane.
4. Generic family policies such as `ponytail` are modifiers, never fallback routes.
5. Public full delivery requires an independently validated exact served-byte
   SHA-256 digest; the check is repeated after version pinning and applies to policy
   items as well as specialists.
6. Benchmark acceptance now requires paired raw outcomes and decision-grade
   statistical evidence, not a positive point estimate.
7. Replay latency separates HTTP service time from queue/pacer and retry waits.
8. Harbor scheduling forbids concurrent cells for the same task, preventing the
   observed Windows package-cache extraction race.

## Hypotheses rejected or not promoted

- Generic `ponytail` fallback: rejected by the 74/75 collapse and zero task lift.
- Score-only full-delivery thresholds: rejected; no stable score threshold produced
  adequate exact-body precision.
- Blind extractive compression: rejected. It removed about 78% of text but omitted
  essential task procedure and scored poorly in independent audits.
- Raw extractive capsules: not promoted; 0 wins/7 ties in valid Sol/max screens and
  slower treatment execution in every measured pair.
- Planner/task adaptation: promising on a few cases but not promoted; the available
  task clusters are too few, earlier task-execution screens were ceilings, and the
  two later non-ceiling skill-adapted treatments reproduced their control failures.
- Full-delivery allowlisting by canonical content hash: rejected because
  normalized-equivalent upstream mutations could inherit evidence. Exact served
  bytes are required instead.

## Limitations and next experiment

- The local worktree does not contain the 202,367-row production SQLite corpus, so
  the complete retained production path (global top-60 vector + FTS union) cannot be
  replayed locally without a current read-only database snapshot. The judged
  treatment is the frozen top-20 lower-bound candidate replay; the current
  deterministic reranker preserves its top-five ordering on all 89 tasks.
- The strongest execution model creates substantial pass-rate ceiling effects.
  Continue control-only screening on tasks whose rank-1 body was judged E, and run a
  treatment only after a genuine competence failure.
- Absolute top-1 relevance is 9.0% and exact candidate precision is 10.8%. The next
  retrieval experiment should be content-aware reranking or abstention on the
  global-vector top 20, evaluated on a held-out task split before any production
  change.
- Measure production-path service latency with the corrected replay timer and a
  current corpus snapshot before deployment.

## Verification

The backend suite passes: 355 tests plus 55 subtests. No deployment was
performed.

## Oracle full-body smoke (2026-07-22)

To separate routing quality from task utility, the predeclared rank-1 E
candidate was injected as its frozen full body for eight tasks under the pinned
Sol/max Harbor configuration. Six paired task cells completed before Docker
became unavailable; two oracle cells exhausted infrastructure retries and their
control counterparts were not scored. Among the six complete pairs, oracle
full-body treatment recorded 3/6 passes versus 2/6 control passes: 1 win, 0
losses, and 5 ties (exact McNemar two-sided p=1.0). This is a screening result,
not evidence of a durable task-success lift. Treatment mean wall time was 481.7
seconds versus 319.3 seconds for control. The aggregate is preserved in
`harbor_oracle_smoke_results.json`; the manifest records the two excluded
infrastructure failures. The planned 24-task expansion remains blocked until
the Harbor Docker runtime is healthy.
