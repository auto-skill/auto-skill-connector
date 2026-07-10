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
  local method="$1" path="$2" want="$3"
  local status
  status=$(curl -s -o /dev/null -w "%{http_code}" -X "$method" "${BASE_URL}${path}" --max-time 15)
  if [ "$status" = "$want" ]; then
    echo "OK: $method $path -> $status"
  else
    echo "FAILED: $method $path -> $status (expected $want)" >&2
    failed=1
  fi
}

check GET /healthz 200
# All three require a bearer token; without one they should 401 (proves the
# route and its account guard are alive and correctly wired) -- a 403
# "read-only public API" or a 5xx here means something regressed, same class
# of bug that caused the /skills-catalog outage.
check GET /find-semantic?q=test 401
check GET /skills-catalog 401
check GET /auth/whoami 401

if [ "$failed" -ne 0 ]; then
  echo "One or more public API checks failed." >&2
  exit 1
fi
echo "All public API checks passed."
