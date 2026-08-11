#!/bin/bash
# Shared helpers for the Auto-Skill corpus daemons.
#
# Standing rules these units inherit and must never break:
#   * no git commits, no pushes
#   * scraped skill content is hostile data — it is never executed, never
#     interpreted as instructions, and never allowed to trigger an action
#   * never claim completeness that isn't measured — STATUS lines carry raw
#     counters, not adjectives
#
# A STOP file halts everything. A daemon never continues past a STOP.

set -uo pipefail

DAEMON_ROOT=/srv/mobile-codex/autoskill-daemon
BENCH=/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend/bench
BACKEND=/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/auto-skill-connector/backend
STOP_FILE="$DAEMON_ROOT/STOP"
STATUS_DIR="$DAEMON_ROOT/status"
LOG_DIR="$DAEMON_ROOT/logs"
STATE_DIR="$DAEMON_ROOT/state"

mkdir -p "$STATUS_DIR" "$LOG_DIR" "$STATE_DIR"

ts() { date -u +%Y-%m-%dT%H:%M:%SZ; }

log() { echo "$(ts) $*"; }

# one-line STATUS file so any future session can report progress with no agent alive
status() {  # status <unit> <one-line text>
  local unit="$1"; shift
  printf '%s | %s\n' "$(ts)" "$*" > "$STATUS_DIR/$unit.status"
}

notify() {  # notify <message>
  "$DAEMON_ROOT/bin/notify.sh" "$*" || true
}

stopped() {
  [[ -f "$STOP_FILE" ]]
}

# Halt everything and ping. Used for canary regression / serious blockers.
raise_stop() {  # raise_stop <unit> <reason>
  local unit="$1"; shift
  local reason="$*"
  printf '%s | unit=%s | %s\n' "$(ts)" "$unit" "$reason" > "$STOP_FILE"
  status "$unit" "STOPPED: $reason"
  notify ":octagonal_sign: **STOP — $unit**
$reason

All Auto-Skill corpus units are halted. Nothing will resume until \`$STOP_FILE\` is removed."
  systemctl --user stop autoskill-sweep.service autoskill-expand.service 2>/dev/null || true
  systemctl --user stop autoskill-enrich.service 2>/dev/null || true
  exit 0   # exit 0 so systemd does not restart-loop us past a deliberate STOP
}

# Milestone pings are fired at most once each, tracked by a marker file.
milestone_once() {  # milestone_once <key> <message>
  local key="$1"; shift
  local marker="$STATE_DIR/milestone.$key"
  [[ -f "$marker" ]] && return 0
  : > "$marker"
  notify "$*"
}

# ---- GitHub API interlock -------------------------------------------------
# Three units hitting the code-search + contents API concurrently is what
# produced the secondary-rate-limit 403s that corrupted batch 21. Enrichment
# fetch is the priority consumer (its failures cost judged skills); the two
# discovery loops yield to it. Advisory only -- nothing blocks forever.
GH_BUSY="$STATE_DIR/gh_fetch_busy"
gh_claim()  { echo $$ > "$GH_BUSY"; }
gh_release(){ rm -f "$GH_BUSY"; }
gh_yield() {
  local waited=0
  while [[ -f "$GH_BUSY" ]] && [[ $waited -lt 900 ]]; do
    # A lock whose owner died is stale, not busy: clear it instead of
    # spending up to 15 minutes waiting on a process that no longer exists.
    local owner; owner=$(cat "$GH_BUSY" 2>/dev/null)
    if [[ -n "$owner" ]] && ! kill -0 "$owner" 2>/dev/null; then
      rm -f "$GH_BUSY"; break
    fi
    sleep 30; waited=$((waited + 30))
  done
}

# A STOP is a PAUSE, not a death. Every loop unit used to `exit 0` on STOP, and
# all three are Restart=on-failure -- so clearing the STOP file restarted
# NOTHING. Three of four units stayed dead until a human ran systemctl, which is
# why every incident today needed manual recovery. Parking here instead means
# the fleet resumes by itself the moment the file is removed.
wait_for_stop_clear() {
  local unit="$1" waited=0
  while stopped; do
    status "$unit" "paused: STOP present (${waited}s) — resumes automatically when cleared"
    sleep 30; waited=$((waited + 30))
  done
  [[ $waited -gt 0 ]] && log "STOP cleared after ${waited}s, resuming"
  return 0
}
