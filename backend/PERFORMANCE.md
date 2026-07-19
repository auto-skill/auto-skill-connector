# Route Performance Log

This log records reproducible measurements and deployment-gated changes. It
contains only aggregate timings and public benchmark case identifiers; route
prompts, responses, bearer tokens, and skill content do not belong here.

## Measurement Protocol

Capture an authenticated production snapshot before and after each route-path
change, using the same case file, case count, repetitions, and account:

```bash
uv run --with-requirements backend/requirements.txt python backend/route_profile.py \
  --json-out backend/eval-results/route-profile-before.json

uv run --with-requirements backend/requirements.txt python backend/route_profile_compare.py \
  backend/eval-results/route-profile-before.json \
  backend/eval-results/route-profile-after.json \
  --fail-on-regression
```

The snapshots are ignored local artifacts. The comparator blocks rollout when
status, tier, or selected skill changes, or when a shared latency metric
exceeds its configured slowdown tolerance.

## Pre-change Production Baseline (2026-07-19)

Authenticated `/route` sample: first five public route cases, three
repetitions each. This is a small, controlled sample, not a load test.

| Metric | p50 | p95 | max |
| --- | ---: | ---: | ---: |
| Server route latency | 374 ms | 567 ms | 597 ms |
| Client wall time | 493 ms | 711 ms | 740 ms |
| Retrieval | 351 ms | 541 ms | 565 ms |
| Skill find | 361 ms | 554 ms | 579 ms |
| Rerank and delivery checks | 9 ms | 12 ms | 13 ms |
| Injected tokens | 1,311 | 1,353 | 1,353 |

Retrieval is the dominant measured stage in this sample. The next deployment
adds detailed route-stage metrics so subsequent snapshots can distinguish
query embedding, primary search, policy lookup, filtering, content validation,
and policy construction.

## Pending Change Set: SQLite Request-Path Cleanup

The current branch makes two behavior-preserving storage changes:

1. WAL mode is enabled during database initialization, rather than on every
   new SQLite connection. WAL is persistent database state, so route
   connections no longer reassert it.
2. Lexical and vector candidate fetches select the metadata used by ranking and
   omit the packed embedding BLOB, which is already held in the vector matrix
   cache and was immediately discarded from result rows.

Local microbenchmarks are directional only:

| Microbenchmark | Previous | Changed | Result |
| --- | ---: | ---: | --- |
| Open SQLite connection, p50 | 0.767 ms | 0.111 ms | 85.5% lower |
| Cached 60-candidate SQL fetch, p95 | 2.634 ms | 2.188 ms | lower |

These do not establish a production latency claim. Production comparison is
required after deployment, with the route-profile behavior gate and host CPU,
memory, and disk-I/O observations from DigitalOcean.

## Current Gates

- Backend unit/API suite: 269 passing tests.
- Backend pytest suite: 286 passing tests, 48 subtests.
- Connector suite: 146 passing tests.
- Route case validation, Python compilation, and offline launch preflight pass.
- Docker Compose runtime validation remains pending on a Docker-capable runner;
  Docker is not installed in this WSL environment.
