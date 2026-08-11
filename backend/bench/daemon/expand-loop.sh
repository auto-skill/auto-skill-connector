#!/bin/bash
# Auto-Skill expansion pass — continuous, checkpoint-resumed.
#
# Every repo-level corpus row (no entrypoint path) gets ONE bounded recursive
# tree scan emitting skills-dir sightings. run2_expand.py records each scanned
# repo in expand_state_v1.json, so re-running only ever advances.
source /srv/mobile-codex/autoskill-daemon/bin/common.sh

UNIT=expand
cd "$BENCH" || exit 1

stopped && { log "STOP file present, refusing to start"; status "$UNIT" "idle: STOP file present"; exit 0; }

log "expand loop starting"
status "$UNIT" "starting: first iteration in progress (status refreshes each cycle)"
while true; do
  wait_for_stop_clear "$UNIT"

  gh_yield

  python3 run2_expand.py --budget 150 >> "$LOG_DIR/expand.log" 2>&1
  rc=$?

  read -r line < <(python3 - <<'PY'
import json, sqlite3
BACKEND = "/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend"
try:
    st = json.load(open(f"{BACKEND}/expand_state_v1.json"))
    scanned = len(st.get("done_repos", {}))
    hist = st.get("history", [])
    remaining = hist[-1].get("repos_remaining", -1) if hist else -1
except Exception:
    scanned, remaining = 0, -1
try:
    con = sqlite3.connect(f"file:{BACKEND}/enrichment_v1.db?mode=ro", uri=True)
    exp = con.execute("select count(*) from sightings where collector='repo_tree_expansion'").fetchone()[0]
    con.close()
except Exception:
    exp = 0
print(f"{scanned} {remaining} {exp}")
PY
)
  set -- $line
  scanned="${1:-0}"; remaining="${2:--1}"; expsight="${3:-0}"

  status "$UNIT" "repos_scanned=$scanned repos_remaining=$remaining expansion_sightings=$expsight last_rc=$rc"
  log "repos_scanned=$scanned remaining=$remaining expansion_sightings=$expsight rc=$rc"

  if [[ "$remaining" -eq 0 ]]; then
    milestone_once "expansion_100" ":white_check_mark: **Expansion 100%**
All repo-level rows tree-scanned: **$scanned** repos.
Expansion sightings: **$expsight**
Source: \`status/expand.status\`"
    status "$UNIT" "COMPLETE: all $scanned repo-level rows scanned, expansion_sightings=$expsight — idling"
    sleep 3600
  else
    sleep 15
  fi
done
