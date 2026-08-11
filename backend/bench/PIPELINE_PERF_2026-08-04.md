# Pipeline performance: profile, measurements, and what changed

Quality was the hard constraint throughout: nothing here trades accuracy for
speed. Every change is either free (idle capacity, wasted work) or was measured
to leave quality flat-or-better before shipping.

---

## The profile: where a batch cycle actually goes

Extracted per-batch from the daemon log across 52 clean batches. Summing medians
across *different* batches does not add up, so this is computed per batch and
then aggregated.

| stage | median | share | nature |
|---|---|---|---|
| primary (Luna) | 764s | **40%** | LLM, batched 8/call, concurrency 6 |
| package | 334s | 17% | local I/O + sqlite, single-threaded |
| haiku secondary | 314s | 16% | LLM, **1 call per skill**, concurrency 4 |
| combine | 272s | 14% | local; includes repair + closure + quarantine |
| fetch | 144s | 8% | network, already 16-way concurrent |
| **total** | **1916s** | | + ~77s build gap = **33 min/cycle** |

---

## What I measured before changing anything

### 1. Raising concurrency does NOT work — the box is CPU-bound, not API-bound

| added concurrency | throughput | median latency |
|---|---|---|
| +4 (10 total) | 3.26 calls/min | 68.0s |
| +8 (14 total) | 4.41 calls/min | 101.3s |

Doubling concurrency returned **68% of ideal scaling with +49% latency**. At the
time of measurement 20 `codex exec` processes were consuming **291% CPU on a
4-core box** (load 13). Each call is a Node process; the CPU cost is real and
local, not a server-side limit.

**So "just raise CONCURRENCY" is wrong** and would have inflated latency and
timeout risk for no throughput.

### 2. Disk is not the constraint either

Per-process I/O sampled over 20s: treeharvest 0.52 MB/s, codex 0.34 MB/s,
everything else ≈0. **Discovery is not stealing I/O from judging** — an earlier
hypothesis of mine that the measurement killed.

### 3. The per-call fixed tax

A trivial "reply OK" prompt through the codex CLI costs **12.6s minimum, 25.6s
mean under load** — against a ~47s batched judge call. So **~27% of every call is
process startup**, and it is exactly the CPU that caps concurrency.

That makes *calls per batch*, not concurrency, the lever worth pulling.

---

## Changes shipped

### A. Luna batch size 8 → 16 (primary stage, the 40%)

Measured with `exp_effort_batched.py` through the production `call_luna_batch`,
n=60 skills per size:

| batch size | canaries | verdicts returned | agreement vs stored medium | s/skill |
|---|---|---|---|---|
| 8 | 23/23 | 60/60 | 25/32 (78.1%) | 6.00 |
| **16** | **23/23** | **60/60** | **26/32 (81.2%)** | **5.07** |
| 24 | 23/23 | 60/60 | 27/32 (84.4%) | 4.51 |

Quality is flat-to-better at every size and canaries are perfect throughout, so
this is not a quality trade. **16% faster per skill, and calls per batch drop
77 → 39**, which halves the Node-process CPU load that was capping everything.

**Why 16 and not 24**, despite 24 measuring fastest: `LUNA_TIMEOUT` is 420s.
A bs=16 call is 76s (5.5x headroom); bs=24 is 102s (4x) — and latency was
measured to inflate ~50% under concurrency pressure, which eats that margin.
A failed call also drops 24 skills to individual fallback instead of 16.
24 is the next step **after** 16 proves out over several clean batches, matching
the daemon's existing streak-based promotion philosophy.

### B. Secondary judge concurrency 4 → 8 (the 16%)

The secondary stage was left at the default 4 workers for ~41 one-per-skill
calls. It runs strictly *after* primary, so the codex processes are already gone
and the box is otherwise idle — this was pure unused capacity, not contention.
8 was already proven safe: the fleet sustained 6-8 concurrent haiku judges
throughout the entire Luna failover with zero throttling. Same model, same
prompt, no quality surface.

### C. Package stage: parallel prepare + C-side manifest scan (the 17%)

- Read-bytes-and-build-manifest is per-item pure work dominated by disk latency
  and SHA-256 (both release the GIL). Now runs 8-way. **Verified byte-identical:
  all 300 test items produced the same `package_hash` as the serial path**, with
  writes still serial and in original order so dedup semantics are unchanged.
  Measured 2.6x on that phase.
- `have_norm` was parsing ~29,000 manifest JSONs through Python **on every
  batch** — O(corpus) work that grows forever (16.7s under load, ~58s at 100k
  packages). Now `json_extract` in C: 2.5x faster, identical set.

**Honest note:** the parallel prepare is a smaller win than it first looked.
Isolated, prepare is only ~9.5ms/item, so it accounts for seconds, not the
stage's 334s. Three separate hypotheses about that 334s (manifest building,
object writes, the have_norm scan) each measured under 20s in isolation, which
means the cost is contention-dependent and only observable in situ. I therefore
added phase instrumentation to `run2_package.py` rather than guessing a fourth
time; the next real batch will report the true breakdown.

---

## Rejected, with reasons

- **Raise CONCURRENCY** — measured sublinear (68% of ideal, +49% latency).
  CPU-bound locally.
- **Throttle discovery to feed judging** — measured: discovery uses ~0.5 MB/s and
  ~5% CPU. It is not the competitor; codex startup is.
- **Lower reasoning effort** — measured separately: buys no latency at all
  (startup dominates) and collapses junk rejection 24/30 → 10/30. See
  `EFFORT_DECISION_RULE.md`.

---

## Remaining, ranked by value

1. **Pipeline the stages.** package (334s) + combine (272s) are local compute
   that could overlap the *next* batch's fetch + primary (network-bound). Up to
   ~600s/cycle, ~30%. Architectural change, needs care around the canary STOP
   gate which currently sits between them.
2. **Replace the codex CLI subprocess with a direct API call.** Removes the
   12.6-25.6s Node boot per call entirely and un-caps concurrency by freeing the
   CPU. Also *reduces* attack surface: content becomes an HTTPS request body
   instead of input to a local process. Biggest single lever; largest change.
3. **Batch the secondary judge** the way the primary is batched. Concurrency 8
   helps now, but 41 separate process boots remain.
4. **`persist_quarantine` replays all 69 batch artifacts every run** — another
   O(corpus) pattern inside combine's 272s. Should be incremental.


---

# Measured results — corrected baseline

## The baseline I first used was wrong

I initially compared against **1916s**, the median across all 52 clean batches.
That is not a valid control: it spans the entire run, including much earlier
conditions (smaller corpus, different load), and it made the post-change batches
look like a *regression* (-10%, +6%, +14%).

The correct control is the batches immediately before the change, at the same
corpus size and under the same load regime:

| pre-change | | post-change | |
|---|---|---|---|
| b60 | 2544s | b70 | 1719s |
| b61 | 2538s | b71 | 2039s |
| b62 | 2348s | b72 | 2175s |
| b63 | 2745s | | |
| b64 | 2716s | | |
| b65 | 2816s | | |
| b66 | 2781s | | |
| b67 | 3619s | | |
| b68 | 2923s | | |
| **median** | **2745s (45.8 min)** | **median** | **2039s (34.0 min)** |

## Result: **-26% cycle time, quality unchanged**

Skills judged per batch is comparable across the two groups (665/614/615 before,
612/588/600 after), so this is not a smaller-workload artifact. Effective judging
throughput went from ~827 to ~1,059 skills/hr, about **+28%**.

Quality across all three post-change batches: **canaries 22/22, storage-quality
gate PASS, streak 32 → 34**, zero regressions, halts or tracebacks in any of the
four units, and preflight green on every invariant.

### Per-stage, versus the pre-change median

| stage | pre | b70 | b71 | b72 | direction |
|---|---|---|---|---|---|
| primary | 764s | 538 | 618 | 691 | improved, variance high |
| **secondary** | **314s** | **266** | **210** | **197** | **consistently down, -15/-33/-37%** |
| package | 334s | 295 | 568 | 363 | noisy, contention-dependent |

The secondary-judge concurrency change is the cleanest, most reliable win: it
improved monotonically across all three batches. The batch-size change moved the
primary stage but with high variance — worth continuing to watch.

## Hypotheses this exercise killed

Recorded because each would have been a plausible-sounding "fix" that achieved
nothing, and several I nearly shipped:

- *Raise CONCURRENCY* → sublinear (68% of ideal, +49% latency); CPU-bound locally.
- *Throttle discovery to feed judging* → discovery uses ~0.5 MB/s and ~5% CPU.
- *Object-store writes are the bottleneck* → 0.2 ms per atomic write.
- *`persist_quarantine` replaying 73 artifacts every batch* → 1.5s.
- *`skill_package_files` DELETE is a full table scan* → there is an autoindex on
  `package_hash`; the plan is SEARCH USING INDEX.
- *Manifest building dominates* → 9.5 ms/item.
- *The batched-write change caused a regression* → the "regression" was a bad
  baseline. A read query untouched by that change (`scan_existing`) also swung
  0.8s → 51.1s → 0.8s across the same batches, which is contention, not code.

The one hypothesis that survived measurement was the per-call codex CLI startup
(12.6–25.6s, ~27% of a batched call) — which is why *calls per batch*, not
concurrency, was the lever that actually moved the number.

## Remaining, ranked

1. **Pipeline the stages** — package + combine are local compute that could
   overlap the next batch's network-bound fetch + primary. Up to ~30%.
   Needs care: the canary STOP gate currently sits between them.
2. **Replace the codex CLI subprocess with a direct API call** — removes the
   Node boot per call and un-caps concurrency. Also *reduces* attack surface.
3. **Batch the secondary judge** like the primary; 38 process boots remain.
4. **Promote batch size 16 → 24** once 16 has several more clean batches
   (measured 4.51 vs 5.07 s/skill, same quality).

---

# Round 2: the two structural bugs

Re-profiled after round 1 rather than optimising against the old shape. The
profile had moved: primary 594s (29%), **combine 496s (24%, up from 272s)**,
package 372s (18%), fetch 244s, haiku 238s.

## Bug 1 — closure fetching used 20% of its own concurrency

`stage_combine` fetches each kept skill's dependency files, and `fetch_closure`
parallelises **within one skill's file list**: `min(CLOSURE_CONCURRENCY, len(todo))`.
Skills carry ~4.0 closure files on average, so it spun up **4 of the 20
configured workers** and processed ~332 skills as ~332 *sequential* GitHub
round-trips:

    measured combine ~490s / 332 skills = 1.48s per skill = exactly one RTT

Fixed by fanning out **across** skills through one shared pool, and capping the
inner fan-out to 1 via a new `workers` parameter so the two levels multiply to a
bounded 20 concurrent GETs rather than 20 x 4 = 80. (GitHub's secondary rate
limit is a 403 on burst and this pipeline has been bitten by it before.)
Verified `workers=1` and `workers=20` return identical closures on 6 real skills.

## Bug 2 — fsync-per-object, and a measurement of mine that was simply wrong

The package stage's cost was `store.put`. `_atomic_write` calls `os.fsync()` on
every object file:

| | ms per atomic write |
|---|---|
| with fsync, real ext4 volume | **252** |
| without fsync, same volume | 0.20 |
| with fsync, /tmp | 0.31 |

**fsync is ~100% of the cost.** At ~2,280 file writes per batch that is **~575s
— the single largest line item in the entire pipeline.**

I had previously measured this at "0.2 ms" and dismissed object writes as a
bottleneck. That measurement ran in `tempfile.mkdtemp()`, which is **/tmp =
tmpfs = RAM**. The real volume is **856x slower**. The lesson is blunt: benchmark
on the medium you actually write to, or you will confidently rule out the true
bottleneck.

### Why deferring it is safe

`fsync=False` is opt-in; `scraper.py` keeps the default and is untouched.
Dropping the per-file fsync does **not** weaken atomicity — `os.replace` is
atomic against process crash regardless. It defers durability against *machine
power loss* only, and for this store that is recoverable by construction: the
filename **is** the sha256, so a truncated object is detectable by rehashing,
and every object is re-fetchable from its recorded `source_url` + `commit_sha`.
One `os.sync()` after the batch's final flush replaces ~2,280 per-file barriers.

Verified: manifest round-trip and every object byte-exact with fsync on and off.

## Expected effect

combine ~490s → ~100s, package ~372s → ~50s: roughly **-700s on a ~2060s cycle
(-34%)**, on top of round 1's -26%. Measuring on batch 75+.

---

# Round 3: the same root cause, one layer down

The fsync discovery generalised. If a per-file `os.fsync()` costs 252 ms on this
volume, so does SQLite's — and every writer was running `synchronous=FULL`, which
fsyncs the WAL on **every commit**:

| setting | ms per commit (250 rows, WAL, this volume) |
|---|---|
| FULL (what we had) | **244** |
| NORMAL | **2** |

122x, and six processes commit continuously against `enrichment_v1.db`.

`synchronous=NORMAL` is SQLite's own recommendation for WAL mode. It remains
safe from **corruption** and remains durable against **process/application
crash**; the only exposure is that a transaction committed shortly before a
machine power loss may roll back. That is the identical trade already accepted
for the object store, and it is recoverable the same way: the pipeline is
idempotent and re-runnable, verdicts are keyed by content hash, and a lost tail
of transactions simply gets re-judged.

Applied to the hot-path writers: `run2_enrich` (the shared `db()` helper),
`run2_package`, `run2_sweep`, `run2_expand`, `run2_persist_quarantine`.
(`run2_treeharvest` and `run2_harvest_deep` already had it.)

## Round 2 results, and one prediction that failed

| stage | pre (b60-68) | round 1 (b70-74) | round 2 (b75-76) |
|---|---|---|---|
| primary | 1286s | 571s | 823s* |
| combine | 400s | 573s | **480s** |
| package | 358s | 382s | **145s** |
| haiku | 416s | 210s | 229s |
| fetch | 195s | 261s | 275s |
| **total** | **2745s** | **2082s** | **1952s (-29%)** |

\* batch 75's primary is contaminated — I ran disk benchmarks (a 512 MB `dd` plus
~110 s of deliberate fsync storms) *during* it. My own measurement polluted the
thing being measured; the box is now left quiet during measurement windows.

**The fsync fix delivered**: package 358 → 145s, and the instrumentation shows
`write_serial` 609s → 58s directly.

**The closure fan-out did not.** I predicted combine 490 → ~100s; it measured
480s. The arithmetic (332 skills x 1.48s RTT) was sound but evidently not the
whole story, so combine now carries its own phase instrumentation
(`build_rows` / `closure_wait` / `finalize`) rather than a sixth hypothesis.

---

# Round 4: the workload is irreducible — the SHAPE was the problem

Asked to find order-of-magnitude gains rather than throttle discovery, the first
move was to attack the workload itself. Both attempts failed, and the failures
are worth recording because they close off the obvious ideas:

**Exact-content dedup is exhausted.** 37,990 distinct skill_ids collapse to
36,786 distinct norm_hashes — a ratio of **1.03**. Normalisation has already
taken everything it can.

**Near-duplicate collapsing is not available either.** Across 35,099 servable
packages: 25,743 distinct names (1.36x collapse, and 61 skills named `review`
are 61 genuinely different skills) and **35,087 distinct judge summaries out of
35,099 — a 1.00x collapse.** The corpus is genuinely diverse. There is no
near-dupe cluster to exploit.

So the 618k are real distinct skills and the work cannot be made smaller.

## Little's law: the pipeline runs at the SUM of its stages, not the max

| stage | median | resource |
|---|---|---|
| primary | 563s | Luna API |
| combine | 438s | GitHub |
| fetch | 294s | GitHub |
| haiku | 221s | Claude API |
| package | 117s | local disk |
| **serial total** | **1633s** | 1,323 skills/hr |
| **bottleneck alone** | **563s** | **3,837 skills/hr** |

Every stage waits for the previous one despite using a **disjoint resource**.
That is a **2.9x** loss for nothing: 18.3 days of backlog versus 6.3.

## What shipped

`run_batch_tail()` — secondary → combine → gates → package — now runs **forked**,
overlapping the next batch's fetch+primary. One tail in flight at a time (two
combines would race the same sqlite writers; two packagers the same dedup
snapshot). `AUTOSKILL_PIPELINE=0` reverts to serial instantly.

Three things had to be handled to make the tail safe to fork:

1. **`continue` in the canary-pending path.** Invalid in a function, and in a
   pipelined loop a 15-minute inline retry would stall the batch already judging
   behind it. Replaced with `return 1` — the rows are already marked retryable,
   so `run2_build_batch` re-offers them with no explicit retry needed.
2. **`raise_stop`** writes the global STOP file, which still works from a
   subshell. But the parent has already advanced, so a regression in batch N
   would have let batch N+1's tail package. Added an explicit `stopped` re-test
   immediately before `run2_package` — this is what keeps *"corpus not advanced"*
   literally true under pipelining.
3. **Batch-size promotion** mutates a loop variable that a subshell cannot
   propagate. It already persists to `$STATE_DIR/batch_size`, so the parent now
   re-reads that file each iteration.

**A first attempt at this was reverted.** Extracting the tail wholesale produced
a `bash -n` failure (the `continue` above), and a syntactically-valid-but-wrong
daemon is worse than a slow one. Restored from backup, verified the fleet was
untouched, then re-did it with the three cases handled explicitly.

## Round 4 measured: pipelining live, and the profile inverted

Batch-to-batch completion interval, which is the number that actually matters:

| | median | |
|---|---|---|
| serial (b≤79) | 2550s (42.5 min) | n=65 |
| **pipelined (b≥82)** | **1408s (23.5 min)** | n=4 |

**1.81x**, quality untouched: batches 80-85 all `canaries=22/22 quality=PASS`,
streak 42→47, zero errors/halts in any unit, and the new stop-guard has never
fired spuriously.

### The other two fixes landed hard

Stage medians under pipelining versus before:

| stage | before | now |
|---|---|---|
| **combine** | 438s | **66s** (blob-index buffering) |
| **fetch** | 294s | **48s** |
| primary | 563s | 592s |
| package | 117s | 321s |
| haiku | 221s | 233s |

The blob-index fix removed ~370s from combine exactly as the arithmetic
predicted (0.40s × 819 objects ≈ 327s).

### Head and tail are now balanced — and primary binds

    HEAD (fetch + primary, main loop) : 640s
    TAIL (haiku + combine + package)  : 620s
    cycle = max(head, tail)           = 640s = 10.7 min theoretical

The two sides came out within 3% of each other, which is what a well-formed
pipeline looks like. Measured interval (23.5 min) is still above the 10.7 min
theoretical, so there is remaining serialization to find — but the binding stage
is now unambiguous.

### Which makes batch size the highest-leverage lever left

`primary` is CPU-bound on codex Node process boots (~13s each), and batch size is
the ONLY knob that reduces CPU *per skill judged*:

| batch size | calls/batch | Node-boot CPU | vs bs=8 |
|---|---|---|---|
| 8 | 75.0 | 975s | 97% |
| 16 (current) | 37.5 | 488s | 49% |
| 24 | 25.0 | 325s | 32% |
| 32 | 18.8 | 244s | 24% |
| 48 | 12.5 | 162s | 16% |

Every doubling halves the binding constraint. Measured so far, all with
**canaries 23/23 and 60/60 verdicts returned**:

| bs | agreement | s/skill | s/call | timeout headroom |
|---|---|---|---|---|
| 8 | 25/32 | 6.00 | 45.9 | 9.2x |
| 16 | 26/32 | 5.07 | 76.5 | 5.5x |
| 24 | 27/32 | 4.51 | 102.1 | 4.1x |

32 and 48 measuring now. The gate is `LUNA_TIMEOUT=420s`: latency inflates ~50%
under concurrency pressure, so a size is only safe if its call time keeps real
headroom after that inflation.
