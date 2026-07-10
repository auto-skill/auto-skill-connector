"""Audit and physically scrub legacy raw route data from local_skills.db.

The default mode is read-only and prints counts only. ``--apply`` requires an
exclusive write transaction, enables SQLite secure deletion, truncates the
WAL, and VACUUMs after a disk-space preflight. ``--logical-only`` is available
for an emergency low-disk cleanup, but does not claim a physical scrub.
"""
from __future__ import annotations

import argparse
import json
import shutil
import sqlite3
from pathlib import Path

import local_store


def _connect(db_path: Path, *, read_only: bool, timeout_seconds: float) -> sqlite3.Connection:
    if not db_path.is_file():
        raise RuntimeError("database does not exist")
    if read_only:
        conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True, timeout=timeout_seconds)
    else:
        conn = sqlite3.connect(db_path, timeout=timeout_seconds, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout={max(1, int(timeout_seconds * 1000))}")
    return conn


def _vacuum_space_preflight(db_path: Path) -> dict[str, int]:
    database_bytes = db_path.stat().st_size
    wal_path = Path(f"{db_path}-wal")
    wal_bytes = wal_path.stat().st_size if wal_path.exists() else 0
    # SQLite documents that VACUUM can need up to twice the database size in
    # free space. Include the current WAL and a small filesystem safety margin.
    required_free_bytes = (database_bytes * 2) + wal_bytes + (8 * 1024 * 1024)
    available_free_bytes = shutil.disk_usage(db_path.parent).free
    if available_free_bytes < required_free_bytes:
        raise RuntimeError("insufficient free disk space for safe VACUUM")
    return {
        "database_bytes": database_bytes,
        "wal_bytes": wal_bytes,
        "required_free_bytes": required_free_bytes,
        "available_free_bytes": available_free_bytes,
    }


def _checkpoint_truncate(conn: sqlite3.Connection) -> None:
    result = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
    if result is not None and int(result[0]) != 0:
        raise RuntimeError("WAL checkpoint was busy; stop every database service and retry")


def _integrity_ok(conn: sqlite3.Connection) -> bool:
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return len(rows) == 1 and str(rows[0][0]).lower() == "ok"


def inspect_database(db_path: Path, *, timeout_seconds: float = 5.0) -> dict:
    conn = _connect(db_path, read_only=True, timeout_seconds=timeout_seconds)
    try:
        return {"applied": False, **local_store.route_event_privacy_status(conn)}
    finally:
        conn.close()


def scrub_database(
    db_path: Path,
    *,
    vacuum: bool = True,
    timeout_seconds: float = 5.0,
) -> dict:
    """Scrub one stopped-service database and return counts, never row values."""
    db_path = db_path.resolve()
    space = _vacuum_space_preflight(db_path) if vacuum else {}
    conn = _connect(db_path, read_only=False, timeout_seconds=timeout_seconds)
    try:
        locking_mode = str(conn.execute("PRAGMA locking_mode=EXCLUSIVE").fetchone()[0]).lower()
        if locking_mode != "exclusive":
            raise RuntimeError("could not acquire exclusive SQLite locking mode")
        conn.execute("PRAGMA secure_delete=ON")
        conn.execute("BEGIN EXCLUSIVE")
        before = local_store.route_event_privacy_status(conn)
        local_store._scrub_route_event_privacy(conn)
        conn.commit()

        _checkpoint_truncate(conn)
        if vacuum:
            conn.execute("VACUUM")
            _checkpoint_truncate(conn)

        if not _integrity_ok(conn):
            raise RuntimeError("SQLite integrity check failed after scrub")
        after = local_store.route_event_privacy_status(conn)
        if not after["ok"]:
            raise RuntimeError("privacy violations remain after scrub")
        return {
            "applied": True,
            "vacuumed": vacuum,
            "physical_scrub_complete": vacuum,
            "integrity_ok": True,
            "before": before,
            "after": after,
            **space,
        }
    except Exception:
        if conn.in_transaction:
            conn.rollback()
        raise
    finally:
        conn.close()


def scrub_and_remove_backup_databases(
    roots: list[Path],
    *,
    timeout_seconds: float = 5.0,
) -> int:
    """Physically scrub exact local_skills.db backup copies, then remove them."""
    removed = 0
    seen: set[Path] = set()
    for root in roots:
        root = root.resolve()
        if not root.exists():
            continue
        if not root.is_dir():
            raise RuntimeError("backup purge root is not a directory")
        for candidate in root.rglob("local_skills.db"):
            candidate = candidate.resolve()
            if candidate in seen or root not in candidate.parents:
                continue
            seen.add(candidate)
            scrub_database(candidate, vacuum=True, timeout_seconds=timeout_seconds)
            candidate.unlink()
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{candidate}{suffix}")
                if sidecar.exists():
                    sidecar.unlink()
            removed += 1
    return removed


def remove_backup_archives(roots: list[Path]) -> int:
    """Remove seed archives that may contain a database copy.

    A ZIP is an opaque container; rewriting it safely is more complicated than
    deleting the pre-scrub seed packet. The migration intentionally removes
    the whole archive, while the separate skills-library backup prefix remains
    intact.
    """
    removed = 0
    for root in roots:
        root = root.resolve()
        if not root.exists():
            continue
        if not root.is_dir():
            raise RuntimeError("backup purge root is not a directory")
        for candidate in root.rglob("*.zip"):
            candidate = candidate.resolve()
            if root not in candidate.parents:
                continue
            candidate.unlink()
            removed += 1
    return removed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Audit or scrub legacy route prompt retention.")
    parser.add_argument("--db-path", type=Path, default=local_store.DB_PATH)
    parser.add_argument("--apply", action="store_true", help="Apply the scrub; default is a read-only count audit.")
    parser.add_argument(
        "--logical-only",
        action="store_true",
        help="Skip VACUUM after applying. This does not remove forensic remnants and is not a physical scrub.",
    )
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument(
        "--purge-backup-root",
        action="append",
        type=Path,
        default=[],
        help="With --apply, physically scrub then remove local_skills.db copies below this directory.",
    )
    parser.add_argument(
        "--purge-backups-only",
        action="store_true",
        help="Scrub/remove backup copies without reprocessing the primary database.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.logical_only and not args.apply:
        _parser().error("--logical-only requires --apply")
    if args.purge_backup_root and (not (args.apply or args.purge_backups_only) or args.logical_only):
        _parser().error("--purge-backup-root requires physical --apply or --purge-backups-only")
    if args.purge_backups_only and (args.apply or args.logical_only or not args.purge_backup_root):
        _parser().error("--purge-backups-only requires backup roots and cannot be combined with --apply")
    try:
        if args.purge_backups_only:
            result = {
                "applied": False,
                "ok": True,
                "purged_backup_databases": scrub_and_remove_backup_databases(
                    args.purge_backup_root,
                    timeout_seconds=args.timeout_seconds,
                ),
                "removed_backup_archives": remove_backup_archives(args.purge_backup_root),
            }
        elif args.apply:
            result = scrub_database(
                args.db_path,
                vacuum=not args.logical_only,
                timeout_seconds=args.timeout_seconds,
            )
            result["purged_backup_databases"] = scrub_and_remove_backup_databases(
                args.purge_backup_root,
                timeout_seconds=args.timeout_seconds,
            )
            result["removed_backup_archives"] = remove_backup_archives(args.purge_backup_root)
        else:
            result = inspect_database(args.db_path, timeout_seconds=args.timeout_seconds)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": str(exc)}, sort_keys=True))
        return 1
    print(json.dumps({"ok": bool(result.get("after", result).get("ok", False)), **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
