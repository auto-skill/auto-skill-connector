# Luna efficiency audit — 2026-08-05

Quota, not wall-clock, is now the binding constraint on corpus growth. Total Luna
spend for a given corpus is **fixed regardless of speed** — going faster only
reaches the wall sooner. So this audit targets *Luna calls per skill judged*.

## Where the quota actually goes

Measured from production logs: **1,511,926 input tokens for 518 skills =
~2,919 tokens/skill.**

Composition (sampled 4,000 real objects + last fetch batch):

| component | cost | verdict |
|---|---|---|
| entry content (`ENTRY_CHAR_CAP=24,000`) | mean 8,069 billed chars ≈ 2,000–2,700 tok | **dominant** |
| file-tree listing (cap 200 paths) | median 1 entry, mean ~71 tok | negligible, never capped |
| prompt template (amortised over 32/call) | ~150–250 tok/skill | small |

Skill size distribution: p50 5,844 B, p90 21,157 B, p99 72,760 B, max 227,705 B.
Only **8.2%** exceed the cap.

## Levers evaluated

### 1. Pre-filter junk before Luna — DEAD END

**Luna keeps 95.9% of what it sees** (n=50,568); only 4.1% are rejected, mostly
"unfilled template / generic placeholder / stub". There is almost no junk to
filter, so a cheap pre-filter saves almost nothing. This was my first hypothesis
and the data killed it.

### 2. Lower the token cap — WEAK, and it trades quality

| cap | tokens/skill | saving | skills truncated |
|---|---|---|---|
| 24,000 (current) | 2,017 | — | 8.2% |
| 16,000 | 1,796 | 11.0% | 14.8% |
| 12,000 | 1,614 | 20.0% | 22.2% |
| 8,000 | 1,325 | 34.3% | 37.7% |

A 20% saving costs truncating 22% of skills. Head+tail sampling already exists
because head-only truncation caused a real false reject (the 73 KB
`land-and-deploy` case). Not worth the quality risk for a sub-2x gain.

### 3. Judge cascade — THE WIN: ~14x

Haiku judges everything; **Luna re-judges only what Haiku rejects.** Haiku runs on
a different provider, so shifting volume to it does not touch the constrained
budget.

Measured on 3,193 paired verdicts (identical bytes, both judges). Critically, the
secondary queue is *not* a random sample — it is all primary rejects **plus a
hash-keyed ~3% audit stream of keeps** (`nh[:8] % 33 == 0`). Only that audit
stream is unbiased, and its Luna keep-rate (95.7%) matches the population (95.9%),
confirming it. All rates below are reweighted to the population.

```
P(luna keeps)                = 0.959   (n=50,568)
P(haiku keeps | luna KEEP)   = 0.9486  (unbiased audit stream, n=1,189)
P(haiku keeps | luna REJECT) = 0.1890  (n=1,827)
```

Escalation threshold T (escalate haiku rejects, plus haiku accepts with conf < T):

| T | Luna volume | false accepts | real skills lost | reduction |
|---|---|---|---|---|
| **0.0** | **6.95%** | **0.776%** | **0.000%** | **14.4x** |
| 0.70 | 8.76% | 0.660% | 0.000% | 11.4x |
| 0.80 | 14.45% | 0.456% | 0.000% | 6.9x |
| 0.90 | 57.42% | 0.076% | 0.000% | 1.7x |

**Real skills lost is 0.000% at every threshold** — because every Haiku reject is
escalated to Luna, Haiku's 5.1% false-reject rate costs nothing but Haiku tokens.

Haiku confidence *is* monotonically informative (false-accept rate falls
100% → 15.8% as confidence rises 0.4 → 0.9), but it is an **inefficient** gate:
T=0.8 doubles Luna spend to only halve false accepts. Prefer T=0.

## The unresolved risk — metadata authorship

The judge call emits the keep/reject decision **and** the enrichment metadata
(`summary`, `specificity`, `triggers`) in one shot. Under the cascade, ~93% of the
corpus would carry **Haiku-authored metadata**, and retrieval ranks on those
fields.

* `specificity` is *calibratable*: paired mean delta −0.0469, stdev 0.1072,
  additive correction +0.05 (`judge_calibration.json`, n=102).
* `summary` quality under Haiku is **unmeasured**. Exact-match agreement is 0%,
  which is meaningless for free text and says nothing about retrieval utility.

This is the one thing standing between "14x measured" and "ship it".

## Recommendation

Do **not** flip the cascade on globally. A/B it:

1. Run the cascade on a minority of batches, tagging verdicts with their own
   snapshot so they stay attributable and re-judgeable (the failover path already
   does exactly this).
2. Compare retrieval quality against the existing benchmark (`retrieval_v1.db`,
   70.0% precision@1) between Luna-enriched and cascade-enriched skills.
3. Keep the 3% audit stream running to track the false-accept rate continuously.
4. Adopt only if retrieval quality holds.

If it holds, the corpus goal moves from **~23 quota resets to ~2**.

## Secondary findings

* **Concurrency 6 → 10 gives only 1.15x** (primary 494s → 431s, n=2). Wall-clock
  is near its ceiling; this does not reduce quota at all. Not worth pushing
  further.
* **Larger judge batches (32 → 64)** would only amortise the ~150–250 tok/skill
  template overhead — a ~4% saving. Not a lever.
* **4% duplicate primary rows** (61,090 rows over 58,578 distinct hashes). Minor,
  but worth confirming it is intentional re-judging and not waste.
