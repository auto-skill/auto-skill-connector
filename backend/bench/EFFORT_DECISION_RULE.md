# Effort selection: decision rule, written BEFORE looking at the results

Recorded up front so the choice is made by a rule rather than by whatever the
numbers happen to suggest afterwards. If a result is surprising, the rule is what
decides — not a fresh justification invented to fit it.

## What is being chosen

`AUTOSKILL_LUNA_EFFORT`, the reasoning effort for the primary judge. The model
accepts `none / low / medium / high / xhigh / max` (it rejects `minimal`).
Production has run on `medium` for all of run 2. Candidates below it: `none`,
`low`.

## Gates, in order. A candidate must clear every one.

1. **Canaries — hard gate, no tolerance.**
   Every canary must come back `is_real_skill: true`. These are hand-verified
   real skills; one miss halts the fleet in production and has already done so
   twice. A candidate that misses even one canary is disqualified regardless of
   how fast it is.

2. **Attack detection — no regression.**
   Must catch at least as many of the 5 confirmed attacks as `medium` does on
   the same sample. Cheap reasoning failing to notice an injection is the worst
   possible trade, because it is invisible until something ships.

3. **Agreement with stored `medium` — must be within the noise floor.**
   `medium` is re-run on the identical sample; its disagreement with its own
   stored verdicts is the floor. A candidate passes if its disagreement is
   within **floor + 3pp**. Judging is non-deterministic, so a candidate cannot
   be expected to beat medium's own reproducibility, and any threshold tighter
   than the floor would reject `medium` itself.

4. **Parse rate ≥ 98%.**
   Malformed output costs a retry, which erases the latency win and shows up as
   `malformed` verdicts. `none` already measured 88/88.

5. **Junk rejection — no material regression.**
   False accepts pollute the corpus and are far more expensive than false
   rejects, since a rejected skill can be re-judged but a bad one gets served.

## Then: confirm on the real path

The sweep judges ONE skill per call. Production packs 8 into a single call with
`enrichment_prompt_v2_batched.md`. The winner must therefore also pass
`exp_effort_batched.py`, which calls the production `call_luna_batch` directly:

- canaries still 22/22 under batching
- verdicts returned for ~all 8 slots (a shortfall falls back to single calls and
  erases the batching win)
- **index alignment verified, not assumed** — the verdict in slot N must actually
  describe the skill in slot N. A silent off-by-one would mislabel whole batches,
  and lower effort is exactly where that would start.

## Choice

Lowest effort that clears every gate above **and** the batched confirmation.
If two candidates tie on quality, take the faster one. If none clears, stay on
`medium` and report that the current setting is already the right one.

## Measured mid-run: single-call latency barely moves with effort

`none` 18.5s/call and `low` 19.1s/call, on identical prompts. Effectively no
difference — because at one skill per call, **codex CLI startup dominates** and
reasoning is a rounding error. A trivial "reply OK" prompt already costs ~10s.

So "lower effort = faster" is not automatically true, and the single-call numbers
must NOT be used to justify the switch. The batched path is where it could
matter: at batch size 8 medium measured 47s/call ≈ **6.5s/skill**, roughly 3x
better than single calls, and there the fixed startup is amortised while
reasoning scales with 8 skills of content. If effort buys anything, it shows up
there or nowhere.

This also reframes the whole question: if batching is the real lever and it is
already enabled, the honest answer may be that effort should stay at `medium`
because lowering it buys little. Worth stating plainly rather than switching for
the sake of it.

## Regardless of outcome

- The snapshot string embeds the effort, so verdicts stay attributable and a
  later paired calibration can compare levels the same way
  `judge_calibration.json` does for the Haiku failover.
- Canaries ride every batch, so if a lower effort degrades in ways this sample
  did not surface, the canary gate catches it on the next batch rather than
  silently corrupting the corpus.
- Switching effort does **not** trigger a mass re-judge:
  `run2_build_batch.judged_ids()` keys on `prompt_version`, not `model_snapshot`.

---

# OUTCOME (measured 2026-08-04, exp_effort_sweep.py, n=88 x 3 arms)

| effort | parsed | agree vs stored medium | canaries | junk rejected | attacks | median s/call |
|---|---|---|---|---|---|---|
| none   | 88/88 | 66.7% | 23/23 | **10/30** | 1/5 | 18.5 |
| low    | 88/88 | 83.3% | 23/23 | **20/30** | 0/5 | 19.1 |
| medium | 88/88 | 90.0% | 23/23 | **24/30** | 0/5 | 18.9 |

## Decision: stay on `medium`. Both candidates fail, and for a reason worth keeping.

**There is no speed to buy.** 18.5s / 19.1s / 18.9s per call — identical within
noise. Effort is simply not the latency bottleneck; codex CLI startup is, and a
trivial "reply OK" prompt already costs ~10s. The premise that lower effort means
faster ingestion is false here, so every quality point given up would be paid for
nothing.

**And the quality cost is real.** Junk rejection is where it shows: `medium`
correctly rejects 24 of 30 known-junk skills, `low` 20, `none` only 10. At `none`
two thirds of known junk would be **accepted into the corpus**. Agreement with
stored medium (66.7% / 83.3%) is far outside the 10.0% noise floor that medium's
own re-run establishes.

## The gate that mattered was not the obvious one

**All three efforts passed the canary gate 23/23.** Canaries only prove a judge
still recognises good skills; they say nothing about whether it has stopped
rejecting bad ones. A sweep built only around canaries would have concluded
`none` was safe and admitted 2/3 of junk.

The `medium_rejected` arm is what caught it — and the general lesson is that a
quality gate needs negative examples, not just positive ones. Worth remembering
for the next gate we add.

## Notes

- Attack detection is weak at **every** effort (0-1 of 5), so this is not an
  effort regression. It matches the independent security-scan finding, and the
  fixture path gate — not the judge — is what actually keeps those out.
- Specificity offset for `low` is only -0.02 vs medium, well inside the 0.107
  between-judge noise measured in `judge_calibration.json`. Even that is not an
  argument for `low`, given the junk-rejection collapse.
- **If speed is wanted, the lever is batching, not effort.** At batch size 8,
  medium measured ~6.5s/skill against ~19s/skill single — roughly 3x, already
  enabled in production. Raising `AUTOSKILL_LUNA_BATCH` above 8 is the next thing
  worth measuring; it would need the same index-alignment check used here
  (verified 8/8 correct slots at size 8).

---

# Covert-execution rule: MEASURED, then REJECTED (2026-08-04)

The judge catches 0-1 of 5 confirmed attacks at every effort level, and the two
it consistently misses share one signature ("silently run", "silently execute").
That looked like an obvious case for a deterministic rule, so
`exp_covert_exec_rule.py` was written to measure one before wiring it into
`HARD_RISK_FLAGS`.

Result over all 28,246 served packages:

- 230 raw hits, 198 after discounting defensive/teaching context
- **canary hits: 0** — it passes the hard gate
- but the top matched phrases are: `run silently` (22), `install silently` (12),
  `silently run` (7), ``run `which gh` via bash silently`` (4),
  `do not tell the user to run` (4)

**Not shipped.** In software, "silently" means *without producing output* — a
silent install is a standard installer flag, and "fails silently" is a warning
about swallowed errors. `do not tell the user to run X` is ordinary UX advice
meaning "do it for them". Hand-reading the top hits found essentially no true
positives, so the rule would have quarantined ~198 benign skills.

This is the `malware_indicators` mistake (fired on 58 security skills, 0 malware)
with different words, and the only reason it did not ship is that the script was
written to measure and change nothing. **A safety rule that passes the canary
gate is not thereby safe** — canaries prove it does not hit known-good skills,
not that what it does hit is actually bad. Precision needs its own evidence.

The real defence for these cases remains the fixture path gate, which caught all
5 confirmed attacks including both the judge missed.
