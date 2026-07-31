"""Bounded, resumable skills.sh mirror warm-up.

This is an operator/cron job, not a per-request path. It consumes a small
slice of the authenticated leaderboard and official curated set, hydrates only
new or stale IDs, and persists the normalized records in the shared mirror.
Rerunning it is safe: fresh mirror rows are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
from collections import Counter
import json
import sys
from typing import Any

from skills_sh_catalog import SkillsShCatalogError, default_catalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", default="trending", choices=("all-time", "trending", "hot"))
    parser.add_argument(
        "--views",
        default="",
        help="comma-separated discovery views; overrides --view (for example all-time,trending,hot)",
    )
    parser.add_argument("--pages", type=int, default=2, help="leaderboard pages to inspect")
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--max-skills", type=int, default=100)
    parser.add_argument(
        "--all-listings",
        action="store_true",
        help="index every leaderboard page as metadata-only; detail hydration stays bounded",
    )
    parser.add_argument(
        "--hydrate-top",
        type=int,
        default=100,
        help="in --all-listings mode, hydrate/audit only this many selected rows",
    )
    parser.add_argument(
        "--hydrate-all",
        action="store_true",
        help="in --all-listings mode, resume hydration across every indexed listing",
    )
    parser.add_argument(
        "--retry-failed",
        action="store_true",
        help="retry terminal failures for the same upstream snapshot",
    )
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--no-curated", action="store_true")
    return parser


def _attempt_is_retryable(row: dict[str, Any]) -> bool:
    reasons = {str(reason) for reason in (row.get("quality_reasons") or [])}
    return bool(
        row.get("quality_status") == "metadata_only"
        or row.get("audit_status") in {"unknown", "warn"}
        or reasons & {"detail-unavailable", "audit-unavailable", "audit-incomplete", "skills-sh-mirror-stale"}
    )


def _row_reason_counts(row: dict[str, Any]) -> Counter[str]:
    reasons = [str(reason) for reason in (row.get("quality_reasons") or []) if str(reason)]
    if not reasons:
        reasons = [str(row.get("quality_status") or "unknown")]
    return Counter(reasons)


async def sync_mirror(args: argparse.Namespace) -> dict[str, Any]:
    catalog = default_catalog()
    if not catalog.configured:
        raise SkillsShCatalogError("SKILLS_SH_OIDC_TOKEN or VERCEL_OIDC_TOKEN is required")
    all_listings = bool(getattr(args, "all_listings", False))
    max_skills = max(1, min(int(args.max_skills), 500))
    hydrate_top = max(0, min(int(getattr(args, "hydrate_top", 100)), 500))
    hydrate_all = bool(getattr(args, "hydrate_all", False))
    retry_failed = bool(getattr(args, "retry_failed", False))
    views = [
        value.strip()
        for value in str(getattr(args, "views", "") or "").split(",")
        if value.strip()
    ] or [str(args.view or "trending")]
    batch_size = max(1, min(int(args.batch_size), 12))
    listings: dict[str, dict[str, Any]] = {}
    curated_count = 0
    leaderboard_count = 0
    pages_fetched = 0
    metadata_indexed = 0
    reason_counts: Counter[str] = Counter()
    migrate_mirror = getattr(catalog, "migrate_mirror", None)
    migration = await migrate_mirror() if callable(migrate_mirror) else {"migrated": 0, "retained": 0}

    if not args.no_curated:
        curated = await catalog.curated()
        curated_count = len(curated)
        for row in curated:
            skill_id = str(row.get("id") or "").strip()
            if skill_id and not row.get("isDuplicate"):
                listings.setdefault(skill_id, row)
        if all_listings:
            metadata_indexed += await catalog.index_listings(curated)

    for current_view in views:
        page = 0
        while page < max(0, int(args.pages)) or all_listings:
            if all_listings:
                page_payload = await catalog.leaderboard_page(view=current_view, page=page, per_page=args.per_page)
                rows = page_payload.get("data") or []
                pagination = page_payload.get("pagination") or {}
            else:
                rows = await catalog.leaderboard(view=current_view, page=page, per_page=args.per_page)
                pagination = {}
            pages_fetched += 1
            leaderboard_count += len(rows)
            for row in rows:
                skill_id = str(row.get("id") or "").strip()
                if skill_id and not row.get("isDuplicate"):
                    listings.setdefault(skill_id, row)
            if all_listings:
                metadata_indexed += await catalog.index_listings(rows)
                if not rows or not bool(pagination.get("hasMore")):
                    break
                page += 1
                continue
            if len(listings) >= max_skills:
                break
            page += 1

    selected_limit = len(listings) if (all_listings and hydrate_all) else hydrate_top if all_listings else max_skills
    selected = list(listings.values())[:selected_limit]
    # SQLite builds cap bound parameters at 999 on common deployments. Keep
    # the resumable all-listings mode safe for a 10k+ row mirror.
    cached: list[dict[str, Any]] = []
    selected_ids = [str(row.get("id") or "") for row in selected]
    for offset in range(0, len(selected_ids), 900):
        cached.extend(await catalog.cached_ids(selected_ids[offset : offset + 900]))
    fresh_ids = {
        str(row.get("skills_sh_id") or row.get("id") or "")
        for row in cached
        if row.get("mirror_fresh") is not False
        and row.get("quality_status") == "active"
        and row.get("content_hash")
    }
    cached_by_id = {
        str(row.get("skills_sh_id") or row.get("id") or ""): row for row in cached
    }
    pending: list[dict[str, Any]] = []
    terminal_skipped = 0
    for row in selected:
        skill_id = str(row.get("id") or "")
        cached_row = cached_by_id.get(skill_id) or {}
        snapshot_hash = str(cached_row.get("source_snapshot_hash") or "") or None
        latest_reader = getattr(catalog, "latest_ingestion_attempt", None)
        latest = (
            await latest_reader(skill_id, snapshot_hash)
            if callable(latest_reader)
            else None
        )
        if skill_id in fresh_ids and not (latest and bool(latest.get("retryable"))):
            continue
        if latest and not retry_failed and not bool(latest.get("retryable")):
            terminal_skipped += 1
            continue
        pending.append(row)
    hydrated = 0
    rejected = 0
    for offset in range(0, len(pending), batch_size):
        rows = await catalog.hydrate_listings(pending[offset : offset + batch_size], limit=batch_size)
        hydrated += len(rows)
        rejected += sum(1 for row in rows if row.get("quality_status") in {"rejected", "metadata_only"})
        for row in rows:
            status = str(row.get("quality_status") or "unknown")
            reasons = _row_reason_counts(row)
            reason_counts.update(reasons)
            retryable = _attempt_is_retryable(row)
            record_attempt = getattr(catalog, "record_ingestion_attempt", None)
            if callable(record_attempt):
                await record_attempt(
                    skill_id=str(row.get("skills_sh_id") or row.get("id") or ""),
                    snapshot_hash=str(row.get("source_snapshot_hash") or "") or None,
                    status=status,
                    reason=";".join(sorted(reasons)),
                    retryable=retryable,
                    content_hash_value=str(row.get("content_hash") or "") or None,
                    byte_count=int(
                        row.get("source_total_bytes")
                        or len(str(row.get("_content") or "").encode("utf-8"))
                    ),
                )
        if args.delay_seconds > 0 and offset + batch_size < len(pending):
            await asyncio.sleep(float(args.delay_seconds))
    return {
        "status": "ok",
        "view": args.view,
        "curated_listings": curated_count,
        "leaderboard_listings": leaderboard_count,
        "selected": len(selected),
        "all_listings": all_listings,
        "hydrate_all": hydrate_all,
        "pages_fetched": pages_fetched,
        "metadata_indexed": metadata_indexed,
        "already_fresh": len(fresh_ids),
        "pending": len(pending),
        "hydrated": hydrated,
        "rejected_or_metadata_only": rejected,
        "mirror_path": getattr(getattr(catalog, "_mirror", None), "path", None),
        "views": views,
        "retry_failed": retry_failed,
        "terminal_skipped": terminal_skipped,
        "reason_counts": dict(sorted(reason_counts.items())),
        "migration": migration,
    }


def main() -> int:
    args = _parser().parse_args()
    try:
        result = asyncio.run(sync_mirror(args))
    except (SkillsShCatalogError, OSError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}), file=sys.stderr)
        return 2
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
