#!/bin/sh
# Purge database backup generations that can contain pre-privacy route text.
# Skill-library archives are deliberately outside these exact prefixes.
set -eu

: "${R2_ENDPOINT:?R2_ENDPOINT is required}"
: "${R2_BUCKET:?R2_BUCKET is required}"

litestream_prefix="${LITESTREAM_REPLICA_PREFIX:-local_skills.db}"
manual_prefix="${R2_DB_BACKUP_PREFIX:-alpha-host-backups}"
library_prefix="${R2_LIBRARY_PREFIX:-skills-library}"
litestream_prefix="${litestream_prefix%/}"
manual_prefix="${manual_prefix%/}"
library_prefix="${library_prefix%/}"

validate_prefix() {
  case "$1" in
    ""|"/"|"."|".."|"skills-library"|"skills-library/"|*"/../"*|*"../"*|*"/./"*|*"./"*)
      echo "refusing unsafe database-backup prefix" >&2
      exit 1
      ;;
  esac
}

validate_library_prefix() {
  case "$1" in
    ""|"/"|"."|".."|*"/../"*|*"../"*|*"/./"*|*"./"*)
      echo "refusing unsafe skill-library prefix" >&2
      exit 1
      ;;
  esac
}

object_count() {
  aws --endpoint-url "$R2_ENDPOINT" s3 ls "s3://$R2_BUCKET/$1/" --recursive 2>/dev/null \
    | wc -l \
    | tr -d ' '
}

validate_prefix "$litestream_prefix"
validate_prefix "$manual_prefix"
validate_library_prefix "$library_prefix"
case "$litestream_prefix/" in
  "$library_prefix/"*) echo "refusing to purge the configured skill-library prefix" >&2; exit 1 ;;
esac
case "$manual_prefix/" in
  "$library_prefix/"*) echo "refusing to purge the configured skill-library prefix" >&2; exit 1 ;;
esac

mode="${1:---purge}"
case "$mode" in
  --purge)
    before_litestream="$(object_count "$litestream_prefix")"
    before_manual="$(object_count "$manual_prefix")"
    aws --endpoint-url "$R2_ENDPOINT" s3 rm "s3://$R2_BUCKET/$litestream_prefix/" --recursive >/dev/null
    aws --endpoint-url "$R2_ENDPOINT" s3 rm "s3://$R2_BUCKET/$manual_prefix/" --recursive >/dev/null
    after_litestream="$(object_count "$litestream_prefix")"
    after_manual="$(object_count "$manual_prefix")"
    if [ "$after_litestream" -ne 0 ] || [ "$after_manual" -ne 0 ]; then
      echo "database backup purge verification failed" >&2
      exit 1
    fi
    echo "purged database backup objects: litestream=$before_litestream, manual=$before_manual"
    ;;
  --require-litestream)
    keys="$(aws --endpoint-url "$R2_ENDPOINT" s3api list-objects-v2 \
      --bucket "$R2_BUCKET" --prefix "$litestream_prefix/" \
      --max-items 200 --query 'Contents[].Key' --output text 2>/dev/null)"
    case "$keys" in
      *"generations/"*) ;;
      *) echo "no Litestream generation is visible in R2" >&2; exit 1 ;;
    esac
    case "$keys" in
      *"snapshot"*|*"snapshots/"*) ;;
      *) echo "no Litestream snapshot is visible in R2" >&2; exit 1 ;;
    esac
    echo "sanitized Litestream generation and snapshot are visible in R2"
    ;;
  *)
    echo "usage: $0 [--purge|--require-litestream]" >&2
    exit 2
    ;;
esac
