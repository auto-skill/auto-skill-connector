"""Plan a bounded, demand-driven package hydration batch.

This command is deliberately read-only with respect to SQLite and the package
CAS.  It ranks package-backed rows that are still missing a complete package
using observed route demand, positive outcomes, and publisher popularity.  The
result can be passed to ``hydrate_github_packages.py --urls-file`` on a trusted
offline/staging worker.

The production API remains a serving process: it never fetches GitHub content
or waits for this planner.  A generated queue is only a worklist; a row becomes
eligible for full delivery after the normal package, content, and safety gates
and the final package audit pass.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


PACKAGE_SOURCES = ("github", "github_skill_file", "skillsmp", "awesome_list")


def _stars(raw: object) -> int:
    try:
        value = json.loads(str(raw or "{}"))
        return max(0, int(value.get("stars") or 0)) if isinstance(value, dict) else 0
    except (TypeError, ValueError, json.JSONDecodeError):
        return 0


def _priority(row: sqlite3.Row) -> float:
    """Favor skills users actually used, then repeated demand, then stars."""
    positive = int(row["positive_routes"] or 0)
    routes = int(row["route_count"] or 0)
    stars = _stars(row["raw"])
    quality = max(0, int(row["quality_score"] or 0))
    return round(
        positive * 1000.0
        + routes * 10.0
        + math.log1p(stars) * 2.0
        + quality * 0.01,
        6,
    )


def plan(db_path: Path, *, limit: int = 100) -> dict:
    """Return a deterministic hydration worklist without changing ``db_path``."""
    if limit < 1:
        raise ValueError("limit must be positive")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        placeholders = ",".join("?" for _ in PACKAGE_SOURCES)
        rows = conn.execute(
            f"""
            SELECT s.id, s.url, s.name, s.source, s.quality_status,
                   s.quality_score, s.package_hash, s.package_completeness,
                   s.dependency_closure_status, s.raw,
                   COUNT(re.id) AS route_count,
                   SUM(CASE WHEN re.outcome IN ('used', 'installed') THEN 1 ELSE 0 END)
                       AS positive_routes,
                   SUM(CASE WHEN re.outcome IN ('failed', 'dismissed') THEN 1 ELSE 0 END)
                       AS negative_routes
            FROM skills s
            LEFT JOIN route_events re ON re.skill_id = s.id
            WHERE s.source IN ({placeholders})
              AND COALESCE(s.quality_status, 'pending') IN ('active', 'metadata_only', 'pending')
              AND (
                    COALESCE(s.package_hash, '') = ''
                 OR COALESCE(s.package_completeness, '') != 'complete'
                 OR COALESCE(s.dependency_closure_status, '') != 'complete'
              )
            GROUP BY s.id
            ORDER BY positive_routes DESC, route_count DESC, s.quality_score DESC, s.url ASC
            LIMIT ?
            """,
            (*PACKAGE_SOURCES, limit),
        ).fetchall()
    finally:
        conn.close()

    items = []
    for row in rows:
        items.append(
            {
                "skill_id": row["id"],
                "url": row["url"],
                "name": row["name"],
                "source": row["source"],
                "quality_status": row["quality_status"],
                "package_hash": row["package_hash"],
                "package_completeness": row["package_completeness"],
                "dependency_closure_status": row["dependency_closure_status"],
                "priority": _priority(row),
                "demand": {
                    "route_count": int(row["route_count"] or 0),
                    "positive_routes": int(row["positive_routes"] or 0),
                    "negative_routes": int(row["negative_routes"] or 0),
                    "stars": _stars(row["raw"]),
                },
            }
        )
    items.sort(key=lambda item: (-item["priority"], item["url"] or ""))
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "count": len(items),
        "items": items,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args()
    if args.limit < 1:
        parser.error("--limit must be positive")
    result = plan(args.db, limit=args.limit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"output": str(args.output), "count": result["count"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
