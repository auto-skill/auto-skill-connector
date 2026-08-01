"""Rebuild a compact, content-preserving skills.sh mirror snapshot.

The ingestion mirror is deliberately mutable while it is being warmed.  FTS
triggers and repeated upserts can make the live SQLite file much larger than
the canonical tables.  This command creates a new database from the canonical
mirror/source/attempt tables, rebuilding FTS exactly once.  The input is never
modified unless the caller explicitly opts into replacement.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
import uuid
from pathlib import Path
from skills_sh_catalog import _PersistentMirror


TABLES = (
    "skills_sh_mirror",
    "skills_sh_sources",
    "skills_sh_ingestion_attempts",
)


def _read_only_connection(path: Path) -> sqlite3.Connection:
    uri = f"file:{path.as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=60)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only=ON")
    return conn


def _columns(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})")]


def _table_digest(conn: sqlite3.Connection, table: str, columns: list[str]) -> str:
    digest = hashlib.sha256()
    select_columns = ", ".join(f'"{column}"' for column in columns)
    order = ", ".join(f'"{column}"' for column in columns)
    for row in conn.execute(
        f'SELECT {select_columns} FROM "{table}" ORDER BY {order}'
    ):
        for value in row:
            if value is None:
                encoded = b"<NULL>"
            elif isinstance(value, bytes):
                encoded = value
            else:
                encoded = str(value).encode("utf-8")
            digest.update(len(encoded).to_bytes(8, "big"))
            digest.update(encoded)
    return digest.hexdigest()


def _copy_rows(
    source: sqlite3.Connection,
    target: sqlite3.Connection,
    table: str,
    columns: list[str],
    *,
    batch_size: int = 128,
) -> int:
    quoted = ", ".join(f'"{column}"' for column in columns)
    placeholders = ", ".join("?" for _ in columns)
    insert = f'INSERT INTO "{table}" ({quoted}) VALUES ({placeholders})'
    count = 0
    batch: list[tuple[object, ...]] = []
    for row in source.execute(f'SELECT {quoted} FROM "{table}"'):
        batch.append(tuple(row[column] for column in columns))
        if len(batch) >= batch_size:
            target.executemany(insert, batch)
            count += len(batch)
            batch.clear()
    if batch:
        target.executemany(insert, batch)
        count += len(batch)
    return count


def _counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        table: int(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in TABLES
    }


def _source_bytes(conn: sqlite3.Connection) -> int:
    return int(
        conn.execute("SELECT COALESCE(SUM(byte_count), 0) FROM skills_sh_sources").fetchone()[0]
    )


def _integrity(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA integrity_check").fetchone()[0])


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path, help="live mirror SQLite file (read-only input)")
    parser.add_argument("output", type=Path, help="new compact SQLite snapshot to create")
    parser.add_argument(
        "--replace-source",
        action="store_true",
        help="after verification, replace the source with the compact snapshot and keep a backup",
    )
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    source = args.source.expanduser().resolve()
    output = args.output.expanduser().resolve()
    if not source.is_file():
        raise SystemExit(f"source database not found: {source}")
    if source == output:
        raise SystemExit("output must differ from source; use a separate snapshot path")
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise SystemExit(f"refusing to overwrite existing output: {output}")

    source_conn = _read_only_connection(source)
    temp = output.with_name(f".{output.name}.tmp-{uuid.uuid4().hex}")
    try:
        before_integrity = _integrity(source_conn)
        if before_integrity != "ok":
            raise SystemExit(f"source integrity check failed: {before_integrity}")
        source_columns = {table: _columns(source_conn, table) for table in TABLES}
        before_counts = _counts(source_conn)
        before_digests = {
            table: _table_digest(source_conn, table, source_columns[table])
            for table in TABLES
        }
        before_source_bytes = _source_bytes(source_conn)

        target = sqlite3.connect(temp, timeout=60)
        try:
            target.executescript(_PersistentMirror._SCHEMA)
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("PRAGMA synchronous=NORMAL")
            for table in TABLES:
                target_columns = _columns(target, table)
                if target_columns != source_columns[table]:
                    raise SystemExit(
                        f"schema mismatch for {table}: "
                        f"source={source_columns[table]} target={target_columns}"
                    )
                _copy_rows(source_conn, target, table, target_columns)
            target.commit()
            target.execute("PRAGMA optimize")
            target.execute("VACUUM")
            target.commit()
            after_integrity = _integrity(target)
            after_counts = _counts(target)
            after_digests = {
                table: _table_digest(target, table, source_columns[table])
                for table in TABLES
            }
            after_source_bytes = _source_bytes(target)
        finally:
            target.close()

        if after_integrity != "ok":
            raise SystemExit(f"compact snapshot integrity check failed: {after_integrity}")
        if after_counts != before_counts:
            raise SystemExit(f"row-count mismatch: before={before_counts} after={after_counts}")
        if after_digests != before_digests:
            raise SystemExit("canonical table digest mismatch; refusing snapshot")
        if after_source_bytes != before_source_bytes:
            raise SystemExit(
                f"source byte-count mismatch: before={before_source_bytes} after={after_source_bytes}"
            )
        os.replace(temp, output)

        result = {
            "status": "ok",
            "source": str(source),
            "output": str(output),
            "source_bytes_on_disk": source.stat().st_size,
            "output_bytes_on_disk": output.stat().st_size,
            "compression_ratio": round(output.stat().st_size / source.stat().st_size, 6),
            "integrity": after_integrity,
            "counts": after_counts,
            "source_content_bytes": after_source_bytes,
            "digests": after_digests,
        }

        if args.replace_source:
            backup = source.with_name(f"{source.name}.precompact-{uuid.uuid4().hex}.bak")
            os.replace(source, backup)
            try:
                os.replace(output, source)
            except Exception:
                os.replace(backup, source)
                raise
            result["replaced_source"] = str(source)
            result["backup"] = str(backup)
        print(json.dumps(result, sort_keys=True))
        return 0
    finally:
        source_conn.close()
        if temp.exists():
            temp.unlink()


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (sqlite3.Error, OSError, ValueError) as exc:
        print(f"compact failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
