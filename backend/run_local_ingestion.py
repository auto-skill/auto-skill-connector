"""Run the bounded local repair phases in dependency order.

The order is intentional:

1. materialize missing immutable single-file packages from cached content;
2. generate missing capability summaries/triggers and re-embed those rows;
3. refresh any remaining missing or stale embeddings.

All roots are explicit. The command never deletes database rows or CAS
objects, and each phase is independently resumable because it selects only
rows still missing the requested material.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

import backfill_capability_summary
import backfill_embeddings_local
import backfill_packages
import local_store


DEFAULT_LIBRARY_DIR = Path(__file__).parent / "skills_library"
DEFAULT_PACKAGE_ROOT = DEFAULT_LIBRARY_DIR / "packages"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--phase",
        choices=("all", "packages", "capability-summary", "embeddings"),
        default="all",
        help="run one phase or all phases in safe dependency order",
    )
    parser.add_argument("--db", type=Path, default=local_store.DB_PATH)
    parser.add_argument("--library-dir", type=Path, default=DEFAULT_LIBRARY_DIR)
    parser.add_argument("--package-root", type=Path, default=DEFAULT_PACKAGE_ROOT)
    parser.add_argument("--limit", type=int, default=0, help="maximum rows per phase; zero means all")
    parser.add_argument("--batch-size", type=int, default=8)
    parser.add_argument("--concurrency", type=int, default=2, help="capability-summary LLM concurrency (1-4)")
    parser.add_argument("--page-size", type=int, default=40)
    parser.add_argument("--ollama-url", default=None)
    parser.add_argument(
        "--deterministic-fallback",
        action="store_true",
        help="fill summary/triggers from metadata when Ollama is unavailable",
    )
    parser.add_argument("--refresh-stale", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if args.limit < 0:
        parser.error("--limit must be non-negative")
    if args.batch_size < 1:
        parser.error("--batch-size must be positive")
    if not 1 <= args.concurrency <= 4:
        parser.error("--concurrency must be between 1 and 4")

    phases = {
        "packages": args.phase in ("all", "packages"),
        "capability-summary": args.phase in ("all", "capability-summary"),
        "embeddings": args.phase in ("all", "embeddings"),
    }
    stats: dict[str, dict] = {}

    if phases["packages"]:
        stats["packages"] = backfill_packages.run(
            db_path=args.db,
            library_dir=args.library_dir,
            package_root=args.package_root,
            limit=args.limit,
            dry_run=args.dry_run,
        )
    if phases["capability-summary"]:
        stats["capability-summary"] = asyncio.run(
            backfill_capability_summary.run(
                db_path=args.db,
                library_dir=args.library_dir,
                limit=args.limit,
                dry_run=args.dry_run,
                concurrency=args.concurrency,
                page_size=args.page_size,
                ollama_url=args.ollama_url,
                deterministic_fallback=args.deterministic_fallback,
            )
        )
    if phases["embeddings"]:
        stats["embeddings"] = backfill_embeddings_local.run(
            db_path=args.db,
            library_dir=args.library_dir,
            limit=args.limit,
            batch_size=args.batch_size,
            refresh_stale=args.refresh_stale,
            dry_run=args.dry_run,
        )

    print(json.dumps({"phases": stats}, sort_keys=True), flush=True)
    return 1 if any(int(phase.get("errors", 0)) for phase in stats.values()) else 0


if __name__ == "__main__":
    raise SystemExit(main())
