#!/bin/bash
# Continuous blob-sha harvest: one git/trees call per repo. Feeds dedupe-by-sha
# (inherit verdicts for identical content) and the zero-listing fetch fast path.
source /srv/mobile-codex/autoskill-daemon/bin/common.sh
UNIT=treeharvest
cd "$BENCH" || exit 1
CYCLE_FILE="$STATE_DIR/treeharvest_maintenance_cycle"
INHERIT_EVERY=3
POPULARITY_EVERY=20
while true; do
  wait_for_stop_clear "$UNIT"

  # Yield to the enrichment fetch stage, exactly as sweep-loop.sh and
  # expand-loop.sh already do. This unit was the ONLY GitHub consumer that never
  # called gh_yield, so its 1200-call iteration ran straight through enrich's
  # gh_claim window. Enrich's fetch stage is bounded by a 900s deadline; when a
  # harvest burst landed inside it, the stage ran out of time and the batch was
  # discarded -- 372 of 390 fetch failures were "stage deadline exceeded", and the
  # discards were periodic (batches 135/139/143/147/151/155/159/163/167, every
  # 4th batch) because they tracked this loop's own cycle rather than anything
  # about the skills being fetched.
  gh_yield

  # Budget 150, not 1200. gh_yield above only checks the lock at the ITERATION
  # boundary, and at 1200 calls x 3.0s pacing an iteration ran ~60 minutes -- so
  # a harvest that started before enrich claimed the lock kept hammering GitHub
  # straight through four of enrich's ~13min fetch windows. Observed live:
  # treeharvest 1287s in and enrich fetch 716s in, concurrently, with treeharvest
  # logging "403 ... backing off". Smaller chunks make the existing yield
  # effective (iteration ~7.5min < enrich's cycle) at no cost to total discovery,
  # which is already 7.9x ahead of judging. 150 matches sweep-loop.sh's budget.
  cycle=$(cat "$CYCLE_FILE" 2>/dev/null || echo 0)
  [[ "$cycle" =~ ^[0-9]+$ ]] || cycle=0
  cycle=$((cycle + 1))
  echo "$cycle" > "$CYCLE_FILE"

  summary_args=()
  (( cycle % INHERIT_EVERY == 0 )) && summary_args+=(--summary-counts)
  # Hard cap per iteration. Measured 2026-08-17: one iteration hung 13h in
  # D-state (folio_wait_bit_common, a WSL2 page-cache stall) after finishing its
  # maintenance pass -- the loop blocked on wait() and harvesting silently
  # stopped while the unit stayed "active". A budget-150 iteration takes ~20min;
  # 60min means it is wedged, not slow. timeout sends TERM then KILL 60s later.
  timeout --kill-after=60 3600 \
    python3 run2_treeharvest.py --budget 150 "${summary_args[@]}" >> "$LOG_DIR/treeharvest.log" 2>&1 \
    || echo "iteration killed by timeout guard rc=$? at $(date '+%F %T')" >> "$LOG_DIR/treeharvest.log"
  rc=$?

  # These are full-corpus local scans. They preserve data quality but do not
  # need to run after every 150-repo network slice; keeping inheritance within
  # three slices bounds duplicate-judging delay while taking the scans off the
  # enrichment critical path. Popularity is retrieval metadata, so it can lag.
  (( cycle % INHERIT_EVERY == 0 )) && python3 run2_inherit.py >> "$LOG_DIR/treeharvest.log" 2>&1 || true
  (( cycle % POPULARITY_EVERY == 0 )) && python3 run2_popularity.py >> "$LOG_DIR/treeharvest.log" 2>&1 || true

  if (( cycle % INHERIT_EVERY == 0 )); then
    left=$(python3 - <<'PY'
import sqlite3
c=sqlite3.connect("file:/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend/enrichment_v1.db?mode=ro",uri=True)
try:
    done=c.execute("select count(*) from repo_tree_meta").fetchone()[0]
    tot=c.execute("select count(distinct substr(external_id,1,instr(external_id,'::')-1)) from sightings where external_id like '%::%'").fetchone()[0]
    shas=c.execute("select count(distinct sha) from repo_trees where path like '%SKILL.md'").fetchone()[0]
    print(f"{done} {tot} {shas}")
except Exception: print("0 0 0")
PY
)
    set -- $left
    set -- "${1:-0}" "${2:-0}" "${3:-0}"
    status "$UNIT" "repos_harvested=$1/$2 distinct_skill_shas=$3 last_rc=$rc"
    [[ "$1" -ge "$2" && "$2" -gt 0 ]] && { status "$UNIT" "COMPLETE: $1 repos, $3 distinct skill shas"; notify ":white_check_mark: **Tree harvest complete** — $1 repos, $3 distinct SKILL.md blob shas. Dedupe-by-sha now has full coverage."; exit 0; }
  else
    log "treeharvest: deferred full-corpus maintenance (cycle $cycle)"
  fi
  sleep 120
done
