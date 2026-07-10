#!/bin/sh
set -eu

start_delay="${LIBRARY_BACKUP_START_DELAY_SECONDS:-300}"
nice_level="${LIBRARY_BACKUP_NICE_LEVEL:-19}"

# A deploy recreates this container when its script changes. Let API/MCP
# traffic settle first; archiving and uploading a few hundred MiB on the
# smallest droplet otherwise competes directly with route embedding/search.
if [ "$start_delay" -gt 0 ] 2>/dev/null; then
  sleep "$start_delay"
fi

if ! command -v aws >/dev/null 2>&1; then
  apk add --no-cache aws-cli tar gzip >/dev/null
fi

interval="${LIBRARY_BACKUP_INTERVAL_SECONDS:-86400}"
prefix="${R2_LIBRARY_PREFIX:-skills-library}"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="/backups/skills_library_${stamp}.tgz"

  nice -n "$nice_level" tar -C /library -czf "$archive" .
  nice -n "$nice_level" aws --endpoint-url "$R2_ENDPOINT" s3 cp "$archive" "s3://${R2_BUCKET}/${prefix}/skills_library_${stamp}.tgz"
  find /backups -name "skills_library_*.tgz" -mtime +7 -delete

  sleep "$interval"
done
