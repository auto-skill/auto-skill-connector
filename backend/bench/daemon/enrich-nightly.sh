#!/bin/bash
# Auto-Skill enrichment — NIGHTLY WINDOW ONLY (00:00–08:00 local).
#
# Runs batches of 100 through the hardened harness:
#   fetch -> primary (Luna) -> haiku secondary (headless) -> ingest -> combine
#
# Hard rules:
#   * never runs outside the window (checked before every batch)
#   * daily ceiling on Luna calls, counted from the enrichment DB itself
#   * quota / auth failure => back off, do not burn the window, do not crash
#   * canary regression => raise_stop(): halts every unit and pings. A daemon
#     never continues past a STOP.
source /srv/mobile-codex/autoskill-daemon/bin/common.sh

UNIT=enrich
cd "$BENCH" || exit 1

# Window and ceiling are env-overridable so they can be adjusted without editing
# the unit, and so the pipeline can be exercised outside the window for testing.
WINDOW_START=${AUTOSKILL_WINDOW_START:-0}      # 00:00 local
WINDOW_END=${AUTOSKILL_WINDOW_END:-8}          # 08:00 local
DAILY_LUNA_CEILING=${AUTOSKILL_LUNA_CEILING:-5000}
# Escalating batch size: prove quality at a size before scaling up. Three
# consecutive batches that pass EVERY storage-quality gate promote the next
# size; any failure holds at the current size and the streak resets.
BATCH_SIZE=$(cat "$STATE_DIR/batch_size" 2>/dev/null || echo 100)
SIZE_LADDER="100 200 400 800"
PROMPT_VERSION=v2.1

in_window() {
  local h; h=$(date +%-H)
  [[ "$h" -ge "$WINDOW_START" && "$h" -lt "$WINDOW_END" ]]
}

luna_today() {
  python3 - <<'PY'
import sqlite3, datetime
BACKEND = "/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend"
day = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
try:
    con = sqlite3.connect(f"file:{BACKEND}/enrichment_v1.db?mode=ro", uri=True)
    n = con.execute("select count(*) from enrichments where judge_role='primary'"
                    " and created_at like ?", (day + "%",)).fetchone()[0]
    con.close()
except Exception:
    n = 0
print(n)
PY
}

progress_line() {
  python3 - <<'PY'
import sqlite3
BACKEND = "/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend"
try:
    con = sqlite3.connect(f"file:{BACKEND}/enrichment_v1.db?mode=ro", uri=True)
    judged = con.execute("select count(distinct norm_hash) from enrichments"
                         " where judge_role in ('primary','deterministic')"
                         " and norm_hash not like 'nofetch:%'").fetchone()[0]
    denom = con.execute("select count(*) from sightings where path like '%SKILL.md'").fetchone()[0]
    con.close()
except Exception:
    judged, denom = 0, 0
pct = (100.0 * judged / denom) if denom else 0.0
print(f"{judged} {denom} {pct:.2f}")
PY
}

# --------------------------------------------------------------------------
# One batch's tail: secondary judging -> combine -> safety gates -> package.
# Split out so it can run FORKED, overlapping the next batch's fetch+primary.
#
# Measured justification: the five stages sum to 1633s but the slowest is only
# 563s. They also use disjoint resources -- primary is the Luna API, this tail
# is the Claude API plus GitHub plus local disk. Running them serially costs
# ~2.9x throughput for nothing.
#
# Batch context is passed in, not read from the parent's loop variables, because
# the parent has already advanced to the next batch by the time this executes.
#
# Safety is preserved: raise_stop writes the global STOP file, which the parent
# loop tests at the top of every iteration and which this function tests again
# before packaging -- so a canary regression still prevents the corpus from
# advancing, at worst one batch later than before.
# --------------------------------------------------------------------------
run_batch_tail() {
  local batch_n="$1" BATCH_SIZE="$2"
  local RUN2_SECONDARY_QUEUE="$3" RUN2_COMBINED="$4"
  export RUN2_PROMPT_VERSION="$PROMPT_VERSION"
  export RUN2_SAMPLE="run2_batch_${batch_n}.json"
  export RUN2_FETCH_CACHE="run2_fetch_b${batch_n}.json"
  export RUN2_SECONDARY_QUEUE RUN2_COMBINED
  log "batch $batch_n: haiku secondary"
  python3 run2_haiku_auto.py --queue "$RUN2_SECONDARY_QUEUE" \
    --out "run2_secres_b${batch_n}.json" --max-calls 150 >> "$LOG_DIR/enrich.log" 2>&1
  python3 run2_enrich.py --stage secondary-ingest \
    --results "run2_secres_b${batch_n}.json" >> "$LOG_DIR/enrich.log" 2>&1

  log "batch $batch_n: combine"
  python3 run2_enrich.py --stage combine >> "$LOG_DIR/enrich.log" 2>&1

  # ---- STOP condition: canary regression -----------------------------------
  # Two distinct failure shapes share the symptom "included < total":
  #   * a canary JUDGED not-real  -> genuine regression, halt the fleet
  #   * canaries merely PENDING   -> they were never fetched (quota exhaustion,
  #     transport failure). That is an availability incident; halting the fleet
  #     over it turned every quota blip into a manual recovery (batches 27, 28).
  #     Discard the batch artifacts and retry after a backoff instead.
  canary=$(python3 - "$RUN2_COMBINED" <<'PY'
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    c = d.get("canaries", {})
    print(f"{c.get('included', 0)} {c.get('total', 0)} "
          f"{c.get('pending_retryable', 0)} {len(c.get('regressions', []) or [])}")
except Exception:
    print("-1 -1 0 -1")
PY
)
  set -- $canary
  cinc="${1:--1}"; ctot="${2:--1}"; cpend="${3:-0}"; creg="${4:--1}"
  if [[ "$ctot" -gt 0 && "$cinc" -lt "$ctot" ]]; then
    if [[ "$creg" -eq 0 && "$cpend" -gt 0 ]]; then
      status "$UNIT" "batch $batch_n: $cpend/$ctot canaries unfetched (availability, not regression) — retrying batch after 15m backoff"
      log "batch $batch_n: canaries pending=$cpend, regressions=0 — transient, discarding artifacts and backing off"
      notify ":hourglass: **Batch $batch_n deferred** — $cpend/$ctot canaries unfetched (GitHub availability, not a judging regression). Re-running after 15m backoff."
      rm -f "$RUN2_FETCH_CACHE" "$RUN2_COMBINED"
      # Was: sleep 900 + continue (retry this batch inline). Unfetched canaries
      # mean a transport failure, and those rows are already retryable -- so
      # run2_build_batch re-offers them without any explicit retry here. In the
      # pipelined loop `continue` would also be invalid, and blocking 15 minutes
      # would stall the batch that is already judging behind this one.
      return 1
    fi
    raise_stop "$UNIT" "CANARY REGRESSION in batch $batch_n: only $cinc/$ctot canaries included (regressions=$creg pending=$cpend). Enrichment halted; corpus not advanced."
  fi

  # ---- repair: a throttled file is not a missing file ----------------------
  # Nothing used to go back for closure files GitHub refused mid-batch, so a
  # transient 403 became a permanent hole in the skill. Runs before the gate so
  # a batch is judged on what we can actually hold, not on transport luck.
  python3 run2_repair_closure.py --batches "$RUN2_COMBINED" >> "$LOG_DIR/enrich.log" 2>&1 || true
  # ...and files never attempted (caps, listing depth): fetch them and queue the
  # affected packages for rebuild, so a gate FAIL is self-healing instead of a
  # permanent hole the daemon walks past.
  python3 run2_complete_closure.py --batches "$RUN2_COMBINED" >> "$LOG_DIR/enrich.log" 2>&1 || true
  # Holds must land in the DB, not only in a batch file nobody queries.
  python3 run2_persist_quarantine.py >> "$LOG_DIR/enrich.log" 2>&1 || true

  # ---- package: judged bytes -> servable skills ---------------------------
  # Without this the pipeline judged skills and stored their content but never
  # wrote the manifest that makes one retrievable. 2,283 skills sat judged while
  # only 100 packages existed, all from the older run1 sweep (and 98% of those
  # pointed at objects that were never stored).
  # Inheritance is BACKGROUND RECONCILIATION, not a batch step. On the critical
  # path it grew to 6m/batch (+359%) scanning a quarter-million rows to transfer
  # a few thousand verdicts, and its long write transaction was the main source
  # of the lock contention that stalled judging for 1h44m. It now runs in the
  # treeharvest loop, where new blob shas actually appear.

  # Re-test STOP immediately before the corpus advances. The parent loop tests
  # it too, but the parent has already moved on to the next batch by now, so
  # without this a regression raised by batch N would still let batch N+1's tail
  # package. This is what keeps "corpus not advanced" true under pipelining.
  if stopped; then
    log "batch $batch_n: STOP raised — refusing to package"
    return 1
  fi

  log "batch $batch_n: package"
  python3 run2_package.py >> "$LOG_DIR/enrich.log" 2>&1 || \
    log "packaging rc=$? (non-fatal; retried next batch)"
  pkgs=$(python3 -c "
import sqlite3
c=sqlite3.connect('file:$BACKEND/corpus_v0_work.sqlite?mode=ro',uri=True)
print(c.execute('select count(*) from skill_packages').fetchone()[0])" 2>/dev/null || echo '?')

  read -r pj pd pp < <(progress_line)
  # ---- storage-quality gate: is what we stored actually usable? -------------
  if python3 run2_quality_audit.py --batches "$RUN2_COMBINED" >> "$LOG_DIR/enrich.log" 2>&1; then
    streak=$(( $(cat "$STATE_DIR/quality_streak" 2>/dev/null || echo 0) + 1 ))
    qflag="PASS"
  else
    streak=0
    qflag="FAIL"
    notify ":warning: **Storage-quality gate FAILED** — batch $batch_n (size $BATCH_SIZE)
Streak reset. Batch size held at $BATCH_SIZE.
See \`logs/enrich.log\` for the failing gate."
  fi
  echo "$streak" > "$STATE_DIR/quality_streak"

  if [[ "$streak" -ge 3 ]]; then
    nextsize=$BATCH_SIZE
    for s in $SIZE_LADDER; do
      if [[ "$s" -gt "$BATCH_SIZE" ]]; then nextsize=$s; break; fi
    done
    if [[ "$nextsize" != "$BATCH_SIZE" ]]; then
      echo "$nextsize" > "$STATE_DIR/batch_size"
      echo 0 > "$STATE_DIR/quality_streak"
      notify ":arrow_up: **Batch size promoted $BATCH_SIZE -> $nextsize**
Three consecutive batches passed every storage-quality gate
(text closure >= 0.98, entrypoints readable, canaries 22/22, no text throttling)."
      BATCH_SIZE=$nextsize
    fi
  fi

  status "$UNIT" "batch=$batch_n size=$BATCH_SIZE packages=$pkgs quality=$qflag streak=$streak/3 canaries=$cinc/$ctot judged_hashes=$pj of $pd SKILL.md sightings (${pp}%) luna_today=$(luna_today)/$DAILY_LUNA_CEILING"
  log "batch $batch_n done: canaries=$cinc/$ctot quality=$qflag streak=$streak size=$BATCH_SIZE progress=${pp}%"

  pct_int=${pp%.*}
  for m in 25 50 75 100; do
    if [[ "$pct_int" -ge "$m" ]]; then
      milestone_once "enrich_${m}" ":chart_with_upwards_trend: **Enrichment ${m}%**
Judged unique content hashes: **$pj** of $pd SKILL.md sightings (${pp}%)
Latest batch: $batch_n · canaries $cinc/$ctot
Source: \`status/enrich.status\`"
    fi
  done

  return 0
}

stopped && { log "STOP present, refusing"; status "$UNIT" "halted: STOP file present"; exit 0; }

if ! in_window; then
  status "$UNIT" "outside nightly window ($(date +%H:%M) local; window ${WINDOW_START}:00-${WINDOW_END}:00) — no work"
  log "outside window, exiting cleanly"
  exit 0
fi

# Preflight before any batch. A rule that has not been tested against the
# canary set has no business stopping the line -- the risk-gate outage cost the
# whole fleet because nothing checked it first.
if ! python3 run2_preflight.py >> "$LOG_DIR/enrich.log" 2>&1; then
  status "$UNIT" "PREFLIGHT FAILED — refusing to run (see logs/enrich.log)"
  log "preflight failed, refusing to start"
  notify ":octagonal_sign: **Preflight failed** — enrichment refused to start. Invariant broken; see \`logs/enrich.log\`."
  exit 1
fi

log "enrichment window open"
batch_n=$(cat "$STATE_DIR/next_batch" 2>/dev/null || echo 11)

while in_window; do
  stopped && { status "$UNIT" "halted: STOP file present"; exit 0; }

  used=$(luna_today)
  if [[ "$used" -ge "$DAILY_LUNA_CEILING" ]]; then
    status "$UNIT" "daily Luna ceiling reached ($used/$DAILY_LUNA_CEILING) — sleeping until window closes"
    log "daily ceiling $used/$DAILY_LUNA_CEILING reached"
    sleep 600; continue
  fi

  B="run2_batch_${batch_n}.json"
  export RUN2_PROMPT_VERSION="$PROMPT_VERSION"
  export RUN2_SAMPLE="$B"
  export RUN2_FETCH_CACHE="run2_fetch_b${batch_n}.json"
  export RUN2_SECONDARY_QUEUE="run2_secq_b${batch_n}.json"
  export RUN2_COMBINED="run2_combined_b${batch_n}.json"

  # Join any prebuild launched by the previous iteration BEFORE testing for the
  # batch file, so a half-written file can never be read and the same batch can
  # never be built twice concurrently.
  if [[ -n "${PREBUILD_PID:-}" ]]; then
    wait "$PREBUILD_PID" 2>/dev/null || true
    PREBUILD_PID=""
  fi

  if [[ ! -f "$B" ]]; then
    python3 run2_build_batch.py --batch "$batch_n" --size "$BATCH_SIZE" \
      --prompt-version "$PROMPT_VERSION" >> "$LOG_DIR/enrich.log" 2>&1 || {
        status "$UNIT" "batch $batch_n build failed — backing off"; sleep 300; continue; }
  fi

  log "batch $batch_n: fetch"
  gh_claim
  python3 run2_enrich.py --stage fetch >> "$LOG_DIR/enrich.log" 2>&1
  gh_release

  # Prebuild the NEXT batch's selection now. Measured: 386s median elapses
  # between "batch N done" and "batch N+1 fetch" with NOTHING else running --
  # 20% of the cycle, pure dead time. run2_build_batch only reads prior batch
  # files and the enrichment DB, both of which are already consistent here, and
  # the ~1250s of API/network-bound stages that follow cover its disk cost.
  NEXT_N=$(( batch_n + 1 ))
  if [[ ! -f "run2_batch_${NEXT_N}.json" ]]; then
    python3 run2_build_batch.py --batch "$NEXT_N" --size "$BATCH_SIZE" \
      --prompt-version "$PROMPT_VERSION" >> "$LOG_DIR/enrich.log" 2>&1 &
    PREBUILD_PID=$!
  fi

  # Live-tunable judge concurrency. run2_enrich.py is a fresh subprocess per
  # stage and reads AUTOSKILL_CONCURRENCY from the environment, so re-reading it
  # here lets us retune between batches WITHOUT a service restart -- a restart
  # discards the in-flight batch's work. Anything that is not a sane integer is
  # ignored, so a garbled state file cannot break judging.
  if [[ -s "$STATE_DIR/luna_concurrency" ]]; then
    _lc=$(tr -dc '0-9' < "$STATE_DIR/luna_concurrency" | head -c 3)
    if [[ -n "$_lc" && "$_lc" -ge 1 && "$_lc" -le 32 ]]; then
      export AUTOSKILL_CONCURRENCY="$_lc"
    fi
  fi

  # Live-tunable FETCH concurrency, same rationale. This is a different limit
  # from judge concurrency and from AUTOSKILL_API_CONCURRENCY: skill fetches go
  # to raw.githubusercontent first, which is OFF the REST quota (measured 41-91
  # files/sec at 20-96 way, zero errors). Only the authenticated contents-API
  # fallback is rate-limited, and that is bounded separately by _API_GATE. The
  # unit shipped 3 here, a leftover from when contents API was the primary path;
  # it throttled every batch to the speed of a limit we no longer hit.
  if [[ -s "$STATE_DIR/fetch_concurrency" ]]; then
    _fc=$(tr -dc '0-9' < "$STATE_DIR/fetch_concurrency" | head -c 3)
    if [[ -n "$_fc" && "$_fc" -ge 1 && "$_fc" -le 64 ]]; then
      export AUTOSKILL_FETCH_CONCURRENCY="$_fc"
    fi
  fi
  # Canary FETCH gate, BEFORE any Luna spend. The post-combine canary gate
  # discards a batch whose canaries came back unfetched -- but by then the batch
  # has already been judged, so ~520 Luna calls die with it. Six batches went
  # that way on 2026-08-06 (128/135/143/147/152/155), ~5.4% of a full quota
  # cycle spent on work that was thrown away. Whether a canary fetched is known
  # the moment fetch ends, so the same decision is made here for the price of a
  # GitHub round trip instead of a judging round.
  # rc=2 -> unfetched canaries, discard now. rc=1 -> could not evaluate, so fall
  # through and let the existing post-combine gate decide (never block a healthy
  # batch on this check).
  python3 run2_canary_preflight.py --batch "$batch_n" \
    --fetch-cache "$RUN2_FETCH_CACHE" >> "$LOG_DIR/enrich.log" 2>&1
  cpf=$?
  if [[ $cpf -eq 2 ]]; then
    status "$UNIT" "batch $batch_n: canaries unfetched at preflight — discarding BEFORE judging (no Luna spent)"
    log "batch $batch_n: canary preflight failed — discarding before judging, 0 luna calls spent"
    # Deliberately NOT notify(): this is a routine, self-healing, zero-cost
    # event. The rows stay retryable, no Luna is spent, and the next batch picks
    # them up. Paging on it produced a burst of Discord alerts (batches
    # 177/178/179/180/185) while the pipeline was in fact healthy and the streak
    # was climbing -- which is how you train someone to ignore real alerts.
    # status()/log() still record every occurrence for anyone looking.
    rm -f "$RUN2_FETCH_CACHE" "$RUN2_COMBINED"
    # This block is in the MAIN while-loop, not inside run_batch_tail(), so
    # `return` here is a no-op: batches 159 and 163 logged the discard and then
    # judged anyway, spending exactly the Luna this gate exists to protect.
    # `continue` is the loop's own bail-out idiom -- but batch_n only advances at
    # the bottom, so a bare continue would re-run the same batch forever.
    # Advance first: the unfetched rows stay retryable and run2_build_batch
    # re-offers them, so skipping forward loses nothing. No sleep, deliberately --
    # the next iteration refetches, which paces this naturally, and blocking here
    # would stall the batch already judging behind us.
    batch_n=$((batch_n + 1))
    echo "$batch_n" > "$STATE_DIR/next_batch"
    continue
  fi

  log "batch $batch_n: primary (luna, concurrency=${AUTOSKILL_CONCURRENCY:-?})"
  python3 run2_enrich.py --stage primary >> "$LOG_DIR/enrich.log" 2>&1
  prc=$?
  if [[ $prc -ne 0 ]]; then
    # A locked DB is contention, not a quota problem. Misreading it as
    # "quota/auth?" and sleeping 15 minutes turned six retryable lock errors
    # into 1h44m of dead time -- 29% of a six-hour window, ~90 min of it pure
    # sleep. Retry contention immediately; reserve the long backoff for real
    # quota/auth failures.
    if tail -20 "$LOG_DIR/enrich.log" | grep -qi "database is locked"; then
      status "$UNIT" "batch $batch_n: sqlite contention — retrying immediately"
      log "primary rc=$prc: database locked, immediate retry"
      sleep 15; continue
    fi
    status "$UNIT" "batch $batch_n primary exited rc=$prc — backing off 15m (quota/auth?)"
    log "primary rc=$prc, backing off"
    sleep 900; continue
  fi

  # A real Luna blackout leaves the primary rows intentionally unjudged and
  # emits no secondary queue. Retain this batch and wait for the next probe;
  # never run the tail against a queue that cannot exist or restart-loop.
  if tail -80 "$LOG_DIR/enrich.log" | grep -q "LUNA BLACKOUT:"; then
    remaining=$(python3 - <<'PY'
from pathlib import Path
import time
path = Path("/srv/mobile-codex/autoskill-daemon/state/luna_blackout_until")
try:
    print(max(0, int(float(path.read_text()) - time.time())))
except Exception:
    print(900)
PY
)
    status "$UNIT" "batch $batch_n: Luna blackout; retaining retryable work and re-probing in 15m (${remaining}s recorded)"
    log "batch $batch_n: Luna blackout — no secondary queue; retaining batch and re-probing in 15m"
    sleep 900; continue
  fi

  # Quota/auth detection must not fire on ordinary numbers. The bare pattern "401"
  # matched inside a token count of 1401608 and idled enrichment for 30 minutes
  # after a batch where all 92 Luna calls succeeded. Require real HTTP/auth context.
  if grep -qiE "(HTTP[ /]?(401|429)|status[_ ]?(code)?[= ]*(401|429)|quota exceeded|rate limit(ed|ing)?|unauthorized|invalid_refresh_token|token (has )?expired)" \
       <(tail -50 "$LOG_DIR/enrich.log"); then
    status "$UNIT" "batch $batch_n: quota/auth signal in log — backing off 30m"
    log "quota/auth signal, backing off 30m"
    sleep 1800; continue
  fi

  # ---- PIPELINE ------------------------------------------------------------
  # Set AUTOSKILL_PIPELINE=0 for the serial path if this ever misbehaves.
  if [[ "${AUTOSKILL_PIPELINE:-1}" == "1" ]]; then
    # Exactly one tail in flight: two combines would race the same sqlite
    # writers and two packagers the same dedup snapshot.
    if [[ -n "${TAIL_PID:-}" ]]; then
      wait "$TAIL_PID" 2>/dev/null || true
      TAIL_PID=""
    fi
    run_batch_tail "$batch_n" "$BATCH_SIZE" "$RUN2_SECONDARY_QUEUE" "$RUN2_COMBINED" &
    TAIL_PID=$!
    batch_n=$((batch_n + 1))
    echo "$batch_n" > "$STATE_DIR/next_batch"
    # The tail promotes batch size by writing the state file; re-read it here so
    # the promotion survives the subshell boundary.
    BATCH_SIZE=$(cat "$STATE_DIR/batch_size" 2>/dev/null || echo "$BATCH_SIZE")
    continue
  fi

  run_batch_tail "$batch_n" "$BATCH_SIZE" "$RUN2_SECONDARY_QUEUE" "$RUN2_COMBINED"
  batch_n=$((batch_n + 1))
  echo "$batch_n" > "$STATE_DIR/next_batch"
done

status "$UNIT" "nightly window closed at $(date +%H:%M) local; next batch=$batch_n; luna_today=$(luna_today)/$DAILY_LUNA_CEILING"
log "window closed, exiting cleanly"
exit 0
