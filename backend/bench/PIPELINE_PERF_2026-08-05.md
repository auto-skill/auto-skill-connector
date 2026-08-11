# Pipeline performance — 2026-08-05

## The finding: one bug, three copies, O(corpus) per batch

The object store is **content-addressed**, so the bytes behind a content sha are
immutable and their normalized hash is immutable with them. Computing that hash
more than once is pure waste.

Three stages each recomputed it for the **entire corpus on every run**, holding
only a per-run memo so every run started cold:

| stage | function | runs |
|---|---|---|
| `run2_build_batch.py` | `judged_shas` loop | every batch |
| `run2_popularity.py` | `norm_of()` | treeharvest loop, continuously |
| `run2_inherit.py` | `norm_for_content()` | treeharvest loop, continuously |

Each object costs a read + decode + normalize + sha256. On this box (WSL2,
~1.75 ms per small file read) that is ~23 ms per object.

### Measured, in production

Instrumented `bphase()` output from three consecutive batches:

```
build phases: {"judged_ids": 10.2,  "retryable_ids": 1.2, "prior_batch_files": 15.4, "select_and_shape": 2390.9}
build phases: {"judged_ids": 302.1, "retryable_ids": 2.3, "prior_batch_files":  9.4, "select_and_shape": 2644.0}
build phases: {"judged_ids": 73.8,  "retryable_ids": 0.7, "prior_batch_files":  6.6, "select_and_shape": 2924.0}
```

Direct measurement of the loop: **46.0 s per 2,000 distinct objects**, and
132,651 blob_index entries → **~3,052 s per batch**, which accounts for
essentially all of `select_and_shape`.

### It was not merely slow — it was a scaling term

Dead time between batches (previous batch `done` → next batch `fetch`), which is
where the build shows up once it overruns the prebuild overlap window:

```
batch  99  100   101   102   103   104   105   106   107   108
gap   7.5 -5.4  12.8   2.5  10.2   8.4  14.3  11.2  23.2  27.1  (minutes)
```

Negative gaps are the prebuild pipelining working. The trend is the point: the
cost grows with the corpus, had just overrun the overlap window, and would have
kept expanding until it dominated the cycle entirely.

Baseline over the last 10 batches: cycle median **41.2 min**, in-batch 25.4 min,
gap 10.7 min — but the last two cycles were 47.1 and 67.1 min.

### The continuous half was worse than the per-batch half

`treeharvest-loop.sh` is `while true` with a 120 s sleep, running `run2_inherit.py`
then `run2_popularity.py` back-to-back, forever:

* **`run2_popularity.py`** iterates all 696,650 known shas every iteration and
  calls `norm_of()` for each one carrying a content sha — **~36,000 object
  re-hashes plus ~96,000 stat calls, unconditionally, every single iteration**,
  computing an answer that cannot change.
* **`run2_inherit.py`** re-hashes up to ~70,000 objects on iterations with new
  candidates (last run: 69,598 inherited + 178 content_unjudged).

The box has **4 cores and one disk**, and load average sat at 7.4–8.2 — roughly
2× oversubscribed. `run2_build_batch` was running at **9.9 % CPU**: not compute,
pure I/O wait. The disk was close to fully committed to recomputing immutable
hashes, which is why *every* stage measured slower in production than in
isolation.

This also means the fix is not just "the build gets faster". Removing this
contention should speed up every concurrent stage, and it is why raising judge
concurrency would have been the wrong move — on a saturated 4-core box that adds
contention, it does not add throughput. Remove work first, then measure, then
tune.

## The fix

New `norm_hash_cache.py`: one shared, persistent `content_sha → norm_hash` index,
used by all three callers. Only genuinely new objects are ever hashed, so
steady-state cost is proportional to a *batch*, not to the corpus, and stops
growing.

### Correctness

* **All three callers compute the identical hash.** Verified on 400 real objects:
  the shared module, `run2_enrich.normalize`, and `run2_build_batch`'s original
  inline normalization agree **400/400, zero mismatches**. All decode
  `utf-8/replace`; all apply CRLF→LF, per-line rstrip, then strip.
* **Negative results are never persisted.** treeharvest routinely learns a blob
  sha *before* enrichment has stored the object, so "absent" is a normal
  transient state, not a fact. Persisting it would permanently mark a real object
  as missing. Absences cost one stat call per run and are recomputed.
* **Writes merge under `flock`.** The enrichment pipeline and the treeharvest
  loop run concurrently; a blind overwrite would drop the other writer's entries.
* **The only behavioural difference** is an object deleted *after* being hashed.
  A grep for `unlink`/`rmtree`/`os.remove`/`shutil.rm` against the object store
  found **no deletion path** — the store is append-only. (And a judged verdict
  lives in the DB keyed by `norm_hash`; deleting cached bytes does not un-judge
  the content.)
* `judged_shas` is a pure function of this mapping and the judged-hash set, so an
  identical mapping gives an identical `judged_shas`, hence identical selection.

**Limitation, stated honestly:** an exact end-to-end diff of `run2_batch_N.json`
old-vs-new is *not* achievable. `run2_build_batch.py:143` folds every prior
`run2_batch_*.json` into `seen`, so a re-run is not comparable to itself, and the
corpus changes continuously as judgments land. The argument above is
compositional, not an output diff.

## Secondary findings

* **`run2_popularity.py` dies intermittently on `sqlite3.OperationalError:
  database is locked`** at the provenance-stamping step (line ~140) — **3 of the
  last 41 runs**. Pre-existing, not introduced here. Mitigated for this work by
  persisting the hash cache *before* that step, so a lock failure no longer
  discards the run's hashing. The underlying lock contention is still unfixed.
* **`run2_persist_quarantine.py` is O(batches)**: it re-reads all 108
  `run2_combined_b*.json` (135 MB) every batch. Bounded at a few seconds
  (sequential reads run ~517 MB/s here), so not worth touching yet — but it is
  the same quadratic shape and should be watched. `closure_from_citations` has
  the same pattern but is not in the per-batch path.
* **The `progress=%` metric chases a moving denominator.** It is
  judged_hashes / distinct SKILL.md sightings, and treeharvest keeps enlarging
  the denominator (67,452 repos harvested, ~62,279 still queued, 696,650 distinct
  SKILL.md blob shas known). Judging can accelerate while the percentage barely
  moves. Treat the percentage as a coverage ratio, not a completion estimate.

## Status

Cache warm-up required no extra work: batch 110's prebuild happened to start
between the two edits, so it ran the self-contained inline version, computed the
cold pass it was going to do anyway, and populated the cache for everything
after it.

## MEASURED RESULT

Cache artifact validated independently: **133,964 entries, 5,000 sampled and
recomputed from disk, 0 mismatches, 0 malformed.**

`select_and_shape`, the phase that contained the loop:

```
2568.3   3183.1     <- cold (old behaviour)
 265.8   1130.6   222.4   <- cache warm
```

Cache growth per batch confirms the design — only genuinely new objects are
hashed, never the corpus:

```
norm-hash cache: 0 -> 133,964      (one-time cold pass, batch 110)
norm-hash cache: +638   -> 134,602
norm-hash cache: +1,565 -> 136,167
norm-hash cache: +1,192 -> 137,359
```

Cycle time, batches 99–110 vs 111+:

```
batch   cycle   in-batch   gap
  108   67.1m     40.0m    27.1m
  109   28.4m     21.3m     7.1m
  110   55.8m     22.7m    33.0m   <- last cold build
  111   12.1m     24.9m   -12.9m   <- cache warm
  112   10.1m     15.4m    -5.3m
```

| | before (n=12) | after (n=2) |
|---|---|---|
| cycle median | 41.2 min | **11.1 min** |
| build gap | 10.7 min | **−9.1 min** |
| throughput | 1,166 skills/h | **4,334 skills/h** |

**3.72x cycle speedup.** The gap going *negative* means the next batch now starts
before the previous one finishes — the pipeline is genuinely overlapped rather
than serialised behind the build.

Load average fell from 7.4–8.2 to **1.95** on a 4-core box, confirming the
diagnosis that the machine was I/O-saturated by redundant hashing rather than
short on compute.

Quality unaffected: batches 111 and 112 both `[PASS]`, `canary=1.000`,
`text=1.000`, canaries 22/22, streak 73.

**Caveat: n=2 after the change.** The direction and magnitude are unambiguous but
the median will move as more batches land. Needs several more to firm up.
