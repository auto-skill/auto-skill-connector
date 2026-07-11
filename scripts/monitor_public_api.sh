#!/usr/bin/env bash
# Exercises a handful of real public endpoints and exits non-zero if any of
# them don't return their expected status. /healthz alone missed the
# /skills-catalog outage (it can look "fine" while a different route silently
# 403s or 500s) -- this checks routes that actually exercise the guard chain
# and the DB, not just process-liveness.
set -euo pipefail

BASE_URL="${MONITOR_BASE_URL:-https://skills.autoskill.dev}"
failed=0

check() {
  local method="$1" path="$2" want="$3" data="${4:-}"
  local status
  if [ -n "$data" ]; then
    status=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "${BASE_URL}${path}" -H 'Content-Type: application/json' --data "$data" --max-time 15)
  else
    status=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "${BASE_URL}${path}" --max-time 15)
  fi
  if [ "$status" = "$want" ]; then
    echo "OK: $method $path -> $status"
  else
    echo "FAILED: $method $path -> $status (expected $want)" >&2
    failed=1
  fi
}

check GET /healthz 200
# Public discovery/routing are anonymous; account-only surfaces below should
# still return 401 without a bearer token.
check POST /find-semantic 200 '{"q":"create a spreadsheet report","limit":2}'
check GET /skills-catalog 401
check GET /auth/whoami 401

if [ "$failed" -ne 0 ]; then
  echo "One or more public API checks failed." >&2
  exit 1
fi
echo "All public API checks passed."
