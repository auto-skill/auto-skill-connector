"""Bounded, resumable skills.sh mirror warm-up.

This is an operator/cron job, not a per-request path. It consumes a small
slice of the authenticated leaderboard and official curated set, hydrates only
new or stale IDs, and persists the normalized records in the shared mirror.
Rerunning it is safe: fresh mirror rows are skipped.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from skills_sh_catalog import SkillsShCatalogError, default_catalog


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--view", default="trending", choices=("all-time", "trending", "hot"))
    parser.add_argument("--pages", type=int, default=2, help="leaderboard pages to inspect")
    parser.add_argument("--per-page", type=int, default=50)
    parser.add_argument("--max-skills", type=int, default=100)
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--delay-seconds", type=float, default=0.25)
    parser.add_argument("--no-curated", action="store_true")
    return parser


async def sync_mirror(args: argparse.Namespace) -> dict[str, Any]:
    catalog = default_catalog()
    if not catalog.configured:
        raise SkillsShCatalogError("SKILLS_SH_OIDC_TOKEN or VERCEL_OIDC_TOKEN is required")
    max_skills = max(1, min(int(args.max_skills), 500))
    batch_size = max(1, min(int(args.batch_size), 12))
    listings: dict[str, dict[str, Any]] = {}
    curated_count = 0
    leaderboard_count = 0

    if not args.no_curated:
        curated = await catalog.curated()
        curated_count = len(curated)
        for row in curated:
            skill_id = str(row.get("id") or "").strip()
            if skill_id and not row.get("isDuplicate"):
                listings.setdefault(skill_id, row)

    for page in range(max(0, int(args.pages))):
        rows = await catalog.leaderboard(view=args.view, page=page, per_page=args.per_page)
        leaderboard_count += len(rows)
        for row in rows:
            skill_id = str(row.get("id") or "").strip()
            if skill_id and not row.get("isDuplicate"):
                listings.setdefault(skill_id, row)
        if len(listings) >= max_skills:
            break

    selected = list(listings.values())[:max_skills]
    cached = await catalog.cached_ids([str(row.get("id") or "") for row in selected])
    fresh_ids = {
        str(row.get("skills_sh_id") or row.get("id") or "")
        for row in cached
        if row.get("mirror_fresh") is not False
    }
    pending = [row for row in selected if str(row.get("id") or "") not in fresh_ids]
    hydrated = 0
    rejected = 0
    for offset in range(0, len(pending), batch_size):
        rows = await catalog.hydrate_listings(pending[offset : offset + batch_size], limit=batch_size)
        hydrated += len(rows)
        rejected += sum(1 for row in rows if row.get("quality_status") in {"rejected", "metadata_only"})
        if args.delay_seconds > 0 and offset + batch_size < len(pending):
            await asyncio.sleep(float(args.delay_seconds))
    return {
        "status": "ok",
        "view": args.view,
        "curated_listings": curated_count,
        "leaderboard_listings": leaderboard_count,
        "selected": len(selected),
        "already_fresh": len(fresh_ids),
        "pending": len(pending),
        "hydrated": hydrated,
        "rejected_or_metadata_only": rejected,
        "mirror_path": getattr(getattr(catalog, "_mirror", None), "path", None),
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
