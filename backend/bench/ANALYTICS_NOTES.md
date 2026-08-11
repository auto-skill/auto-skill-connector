
## Judge failover window: 2026-08-03T~04:00Z -> Luna restored (expected 2026-08-07)

Luna (`gpt-5.6-luna@medium`) hit a multi-day usage limit. Rather than stop the
corpus growing for four days, primary judging failed over to Haiku. Those
verdicts carry `model_snapshot = claude-haiku-4-5-20251001/primary-failover`
and are never labelled as Luna verdicts.

Two systematic differences measured over the first ~400 failover verdicts.
Neither changes what gets SERVED, but both distort metadata comparisons across
this window:

| metric              | Luna  | Haiku failover | serving impact |
|---------------------|-------|----------------|----------------|
| include rate        | 95.8% | 95.1%          | none, equivalent |
| mean specificity    | 0.901 | 0.800          | none -- stored, never gated |
| `license_missing`   | 43%   | 0.5%           | none -- advisory flag |

Do NOT read the `license_missing` drop as licensing improving. It is a judge
artifact: Haiku rarely emits the flag. Any time-series over this window should
either segment by `model_snapshot` or exclude the flag entirely.

The specificity offset is a scoring-scale difference, not a quality change.
It is safe today only because nothing thresholds on specificity -- if a gate is
ever added there, these rows must be re-judged first.

Also note: with primary = Haiku and secondary = Haiku, the two-judge escalation
is same-model, so `decided_by='both'` in this window means agreement between two
runs of one model, not two independent judges. Treat the window as single-judge.
Rows are re-judgeable via `model_snapshot` when Luna returns.

### Specificity calibration across the failover window (measured 2026-08-04)

Paired measurement on 102 skills that BOTH judges scored from identical bytes:
haiku-failover reads **0.05 lower** than Luna (median; mean -0.047). The unpaired
means suggested -0.10, which was confounded by which skills each judge saw --
always pair on norm_hash before comparing judges.

More important than the offset: the between-judge stdev is **0.107** and they
agree within 0.05 only **36%** of the time. Specificity is a coarse signal. It
predicts informativeness at the band level (<0.75 -> 55% valuable, above -> ~77%)
and must not be used as a fine-grained sort key.

Constants in `judge_calibration.json`. Applied at READ time; stored verdicts are
never rewritten, so provenance survives and the constant stays revisable.
