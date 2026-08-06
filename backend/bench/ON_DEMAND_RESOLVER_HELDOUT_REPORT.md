# On-demand resolver experiment

Status: offline replay complete; no live network, deployment, database rewrite, CAS write, or broad hydration was used.
The fixture is deterministic and authored in-repo; it is not yet an external or human-labeled benchmark.

Command:

```text
python backend/bench/on_demand_route_replay.py --fixture backend/bench/on_demand_resolver_heldout.json --repeats 100 --json-out backend/bench/autoskill-route-heldout.json --markdown-out backend/bench/ON_DEMAND_RESOLVER_HELDOUT_REPORT.md
```

## Results

| Arm | p50 / p95 latency (ms) | Top-1 / Top-k success | Irrelevant/unsafe | Incomplete | False-positive | Capsule p50 / p95 chars | Python memory (tracemalloc / RSS) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Control | 0.005 / 0.889 | 0.800 / 0.800 | 0.000 | 0.000 | 0.000 | 0.0 / 436.0 | 2121 / 47300 KiB |
| Experiment | 0.017 / 0.284 | 1.000 / 1.000 | 0.000 | 0.000 | 0.000 | 0.0 / 703.0 | 5379 / 53144 KiB |

Cold/uncached latency (p50 / p95 ms): control 0.005 / 0.889; experiment 0.014 / 0.395.

Capsule tokens (p50 / p95): control 0.0 / 109.0; experiment 0.0 / 83.0.

DB-miss recovery: control {'recovered': 0, 'eligible': 300, 'rate': 0.0}; experiment {'recovered': 200, 'eligible': 300, 'rate': 0.666667}.

Experiment budgets: provider calls 40, fetches 22, bytes 7153, cache hits 1980, statuses {'cache_hit': 1980, 'no_match': 11, 'ok': 9}.

Exact capsule tokenizers observed: ['fixture-tokenizer-v1'].

## Task-level diagnostics

- control `feedback-clustering`: top-1 0.000, tier hint, selected data-summary, DB-miss 0/0.
- control `migration-checklist`: top-1 0.000, tier none, selected none, DB-miss 0/100.
- control `missing-audit`: top-1 0.000, tier none, selected none, DB-miss 0/100.
- control `terraform-drift`: top-1 0.000, tier none, selected none, DB-miss 0/100.
- experiment `missing-audit`: top-1 1.000, tier hint, selected security-triage, DB-miss 0/100.

## Recommendation

No-go for active fallback based on this fixture alone; this is not an improvement claim. Keep the feature flag off until a held-out replay has enough labeled tasks to establish non-inferior top-1/top-k relevance, zero unsafe/incomplete accepted routes, valid DB-miss recovery, and p95 latency within the route budget.
