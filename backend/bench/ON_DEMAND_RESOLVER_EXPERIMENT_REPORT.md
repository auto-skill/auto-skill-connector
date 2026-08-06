# On-demand resolver experiment

Status: offline replay complete; no live network, deployment, database rewrite, CAS write, or broad hydration was used.
The fixture is deterministic and authored in-repo; it is not yet an external or human-labeled benchmark.

Command:

```text
python backend/bench/on_demand_route_replay.py --fixture backend/bench/on_demand_resolver_fixtures.json --repeats 100 --json-out backend/bench/autoskill-route-replay.json --markdown-out backend/bench/ON_DEMAND_RESOLVER_EXPERIMENT_REPORT.md
```

## Results

| Arm | p50 / p95 latency (ms) | Top-1 / Top-k success | Irrelevant/unsafe | Incomplete | False-positive | Capsule p50 / p95 chars | Python memory (tracemalloc / RSS) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Control | 0.405 / 0.829 | 0.750 / 0.750 | 0.000 | 0.000 | 0.125 | 0.0 / 465.0 | 1329 / 46664 KiB |
| Experiment | 0.14 / 0.346 | 1.000 / 1.000 | 0.000 | 0.000 | 0.000 | 566.0 / 826.0 | 2915 / 50296 KiB |

Cold/uncached latency (p50 / p95 ms): control 0.405 / 0.829; experiment 0.352 / 0.73.

Capsule tokens (p50 / p95): control 0.0 / 117.0; experiment 62.0 / 92.0.

DB-miss recovery: control {'recovered': 0, 'eligible': 100, 'rate': 0.0}; experiment {'recovered': 100, 'eligible': 100, 'rate': 1.0}.

Experiment budgets: provider calls 16, fetches 8, bytes 3838, cache hits 792, statuses {'cache_hit': 792, 'no_match': 3, 'ok': 5}.

Exact capsule tokenizers observed: ['fixture-tokenizer-v1'].

## Task-level diagnostics

- control `github-issue-triage-db-miss`: top-1 0.000, tier none, selected none, DB-miss 0/100.
- control `unsafe-content`: top-1 0.000, tier hint, selected unsafe-spreadsheet-helper, DB-miss 0/0.

## Recommendation

No-go for active fallback based on this fixture alone; this is not an improvement claim. Keep the feature flag off until a held-out replay has enough labeled tasks to establish non-inferior top-1/top-k relevance, zero unsafe/incomplete accepted routes, valid DB-miss recovery, and p95 latency within the route budget.
