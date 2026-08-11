#!/bin/bash
# Auto-Skill enumeration sweep — continuous, checkpoint-resumed.
#
# run2_sweep.py already checkpoints slice progress after every slice and backs
# off on GitHub 403/429, so this loop just keeps handing it budget until every
# query reports complete, then idles cheaply and re-checks (the universe grows).
source /srv/mobile-codex/autoskill-daemon/bin/common.sh

UNIT=sweep
cd "$BENCH" || exit 1

stopped && { log "STOP file present, refusing to start"; status "$UNIT" "idle: STOP file present"; exit 0; }

log "sweep loop starting"
status "$UNIT" "starting: first iteration in progress (status refreshes each cycle)"
while true; do
  wait_for_stop_clear "$UNIT"

  gh_yield

  python3 run2_sweep.py --search-budget 150 >> "$LOG_DIR/sweep.log" 2>&1
  rc=$?

  read -r line < <(python3 - <<'PY'
import json, sqlite3, os
BACKEND = "/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend"
try:
    st = json.load(open(f"{BACKEND}/sweep_state_v2.json"))
except Exception:
    print("0 0 0 0"); raise SystemExit
qs = st.get("queries", {})
done = sum(1 for s in qs.values() if s.get("complete"))
pend = sum(len(s.get("pending_slices", [])) for s in qs.values())
hits = sum(s.get("hits_seen", 0) for s in qs.values())
try:
    con = sqlite3.connect(f"file:{BACKEND}/enrichment_v1.db?mode=ro", uri=True)
    sight = con.execute("select count(*) from sightings").fetchone()[0]
    con.close()
except Exception:
    sight = 0
print(f"{done} {len(qs)} {pend} {hits} {sight}")
PY
)
  set -- $line
  qdone="${1:-0}"; qtotal="${2:-0}"; pending="${3:-0}"; hits="${4:-0}"; sightings="${5:-0}"

  status "$UNIT" "queries_complete=$qdone/$qtotal pending_slices=$pending hits_seen=$hits sightings=$sightings last_rc=$rc"
  log "queries_complete=$qdone/$qtotal pending=$pending sightings=$sightings rc=$rc"

  if [[ "$qdone" -ge "$qtotal" ]]; then
    milestone_once "enumeration_100" ":white_check_mark: **Enumeration 100%**
All 6 sweep queries fully enumerated.
Sightings: **$sightings** · hits seen: $hits
Source: \`status/sweep.status\`"
    status "$UNIT" "COMPLETE: all 6 queries enumerated, sightings=$sightings — idling (re-checks hourly)"
    sleep 3600
  else
    sleep 20
  fi
done
