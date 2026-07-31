#!/usr/bin/env bash
set -euo pipefail

# Pull a fresh Vercel OIDC token into the host-side file mounted by Compose.
# The linked/project-scoped Vercel CLI is the only component that handles
# token acquisition; this script never prints the token or places it in the
# API environment. Run it as root (or arrange equivalent ownership) so the
# appuser container can read the 0640 file without making it world-readable.

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
OIDC_DIR="${OIDC_DIR:-$ROOT_DIR/backend/deploy/oidc}"
TOKEN_FILE="${TOKEN_FILE:-$OIDC_DIR/skills_sh_oidc_token}"
VERCEL_BIN="${VERCEL_BIN:-vercel}"
VERCEL_PROJECT="${VERCEL_PROJECT:-}"
VERCEL_SCOPE="${VERCEL_SCOPE:-}"
VERCEL_ENVIRONMENT="${VERCEL_ENVIRONMENT:-development}"

if [[ -z "$VERCEL_PROJECT" ]]; then
  echo "VERCEL_PROJECT is required (use the dedicated Auto-Skill Vercel project)" >&2
  exit 2
fi
if [[ "$(id -u)" -ne 0 ]]; then
  echo "run as root so the token file can be owned by gid 10001" >&2
  exit 2
fi

mkdir -p "$OIDC_DIR"
mkdir -p "$(dirname "$TOKEN_FILE")"
TMP_ENV="$(mktemp "$OIDC_DIR/.vercel-env.XXXXXX")"
TMP_TOKEN="$(mktemp "$OIDC_DIR/.skills-sh-oidc.XXXXXX")"
cleanup() {
  rm -f -- "$TMP_ENV" "$TMP_TOKEN"
}
trap cleanup EXIT
umask 077

PROJECT_ARGS=(--project "$VERCEL_PROJECT" --environment "$VERCEL_ENVIRONMENT" --yes)
if [[ -n "$VERCEL_SCOPE" ]]; then
  PROJECT_ARGS+=(--scope "$VERCEL_SCOPE")
fi

"$VERCEL_BIN" env pull "$TMP_ENV" "${PROJECT_ARGS[@]}" >/dev/null

TOKEN="$(awk -F= '$1 == "VERCEL_OIDC_TOKEN" || $1 == "SKILLS_SH_OIDC_TOKEN" {sub(/^[^=]*=/, ""); print; exit}' "$TMP_ENV")"
TOKEN="${TOKEN#\"}"
TOKEN="${TOKEN%\"}"
TOKEN="${TOKEN#\'}"
TOKEN="${TOKEN%\'}"
if [[ -z "$TOKEN" ]]; then
  echo "Vercel did not return an OIDC token; enable OIDC Federation on the project" >&2
  exit 3
fi

printf '%s\n' "$TOKEN" >"$TMP_TOKEN"
chown root:10001 "$TMP_TOKEN"
chmod 0640 "$TMP_TOKEN"
mv -f -- "$TMP_TOKEN" "$TOKEN_FILE"
chmod 0750 "$OIDC_DIR"
echo "refreshed skills.sh OIDC token at $TOKEN_FILE"
