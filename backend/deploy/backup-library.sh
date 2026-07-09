#!/bin/sh
set -eu

if ! command -v aws >/dev/null 2>&1; then
  apk add --no-cache aws-cli tar gzip >/dev/null
fi

interval="${LIBRARY_BACKUP_INTERVAL_SECONDS:-86400}"
prefix="${R2_LIBRARY_PREFIX:-skills-library}"

while true; do
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  archive="/backups/skills_library_${stamp}.tgz"

  tar -C /library -czf "$archive" .
  aws --endpoint-url "$R2_ENDPOINT" s3 cp "$archive" "s3://${R2_BUCKET}/${prefix}/skills_library_${stamp}.tgz"
  find /backups -name "skills_library_*.tgz" -mtime +7 -delete

  sleep "$interval"
done
