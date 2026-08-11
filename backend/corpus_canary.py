"""Measure whether known public SKILL.md files survive collection and quality gates.

This is intentionally corpus-only: it answers whether a frozen canary was
discovered as a raw ``github_skill_file`` row, became active, and still matches
its retained source digest. It does not make an agent-quality claim.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Sequence


DEFAULT_MANIFEST_PATH = Path(__file__).parent / "evals" / "corpus_canaries.json"
DEFAULT_DB_PATH = Path(
    os.getenv("LOCAL_DB_PATH", str(Path(__file__).parent / "local_skills.db"))
)


@dataclass(frozen=True)
class Canary:
    id: str
    parent_repo: str
    path: str
    source_url: str
    expected_name: str
    expected_content_hash: str | None


def load_canaries(path: Path = DEFAULT_MANIFEST_PATH) -> list[Canary]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ValueError(f"{path}: unsupported canary manifest")
    raw_canaries = data.get("canaries")
    if not isinstance(raw_canaries, list):
        raise ValueError(f"{path}: canaries must be a list")

    canaries: list[Canary] = []
    ids: set[str] = set()
    locations: set[tuple[str, str]] = set()
    for index, raw in enumerate(raw_canaries):
        if not isinstance(raw, dict):
            raise ValueError(f"{path}: canary {index} is not an object")
        expected_hash = raw.get("expected_content_hash")
        canary = Canary(
            id=str(raw.get("id") or "").strip(),
            parent_repo=str(raw.get("parent_repo") or "").strip().casefold(),
            path=str(raw.get("path") or "").strip(),
            source_url=str(raw.get("source_url") or "").strip(),
            expected_name=str(raw.get("expected_name") or "").strip(),
            expected_content_hash=str(expected_hash).strip() if expected_hash else None,
        )
        if not all(
            (
                canary.id,
                canary.parent_repo,
                canary.path,
                canary.source_url,
                canary.expected_name,
            )
        ):
            raise ValueError(f"{path}: canary {index} has a required blank field")
        location = (canary.parent_repo, canary.path)
        if canary.id in ids or location in locations:
            raise ValueError(f"{path}: duplicate canary {canary.id}")
        ids.add(canary.id)
        locations.add(location)
        canaries.append(canary)

    if not 20 <= len(canaries) <= 50:
        raise ValueError(f"{path}: expected 20-50 canaries, found {len(canaries)}")
    return canaries


def _decode_json_array(value: Any) -> list[str]:
    try:
        parsed = json.loads(value) if isinstance(value, str) else value
    except json.JSONDecodeError:
        return []
    return [str(item) for item in parsed] if isinstance(parsed, list) else []


def _record_drop_stage(records: list[dict[str, Any]]) -> str:
    if not records:
        return "discovery"
    if any(record["quality_status"] == "active" for record in records):
        return "none"
    if any(record["valid_skill"] is False for record in records):
        return "frontmatter"
    statuses = {record["quality_status"] for record in records}
    if "pending_package" in statuses:
        return "package_capture"
    if "duplicate" in statuses:
        return "deduplication"
    return "quality_gate"


def _records_for_canary(
    connection: sqlite3.Connection, canary: Canary
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT id, name, url, quality_status, quality_reasons, content_hash,
               package_completeness, source_commit_sha,
               json_extract(raw, '$.valid_skill') AS valid_skill
        FROM skills
        WHERE source = 'github_skill_file'
          AND lower(json_extract(raw, '$.parent_repo')) = ?
          AND json_extract(raw, '$.path') = ?
        ORDER BY discovered_at DESC, id
        """,
        (canary.parent_repo, canary.path),
    ).fetchall()
    records = []
    for row in rows:
        records.append(
            {
                "id": row[0],
                "name": row[1],
                "url": row[2],
                "quality_status": row[3],
                "quality_reasons": _decode_json_array(row[4]),
                "content_hash": row[5],
                "package_completeness": row[6],
                "source_commit_sha": row[7],
                "valid_skill": None if row[8] is None else bool(row[8]),
            }
        )
    return records


def evaluate_canaries(db_path: Path, canaries: Sequence[Canary]) -> dict[str, Any]:
    if not canaries:
        raise ValueError("at least one canary is required")
    connection = sqlite3.connect(f"{db_path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        results = []
        for canary in canaries:
            records = _records_for_canary(connection, canary)
            active = [record for record in records if record["quality_status"] == "active"]
            expected_hash = canary.expected_content_hash
            hash_match = None if not expected_hash else any(
                record["content_hash"] == expected_hash for record in records
            )
            results.append(
                {
                    **asdict(canary),
                    "raw_discovered": bool(records),
                    "active_catalog": bool(active),
                    "hash_match": hash_match,
                    "drop_stage": _record_drop_stage(records),
                    "records": records,
                }
            )
    finally:
        connection.close()

    total = len(results)
    raw_discovered = sum(result["raw_discovered"] for result in results)
    active_catalog = sum(result["active_catalog"] for result in results)
    hash_comparable = sum(result["hash_match"] is not None for result in results)
    hash_matches = sum(result["hash_match"] is True for result in results)
    stages = (
        "none",
        "discovery",
        "frontmatter",
        "package_capture",
        "quality_gate",
        "deduplication",
    )
    return {
        "schema_version": 1,
        "db_path": str(db_path.resolve()),
        "summary": {
            "canaries": total,
            "raw_discovered": raw_discovered,
            "raw_recall": raw_discovered / total,
            "active_catalog": active_catalog,
            "active_recall": active_catalog / total,
            "hash_comparable": hash_comparable,
            "hash_matches": hash_matches,
            "hash_match_recall": (
                hash_matches / hash_comparable if hash_comparable else None
            ),
            "by_drop_stage": {
                stage: sum(result["drop_stage"] == stage for result in results)
                for stage in stages
            },
        },
        "canaries": results,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Measure raw and active corpus recall for frozen SKILL.md canaries."
    )
    parser.add_argument("--db", type=Path, default=DEFAULT_DB_PATH)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST_PATH)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--fail-under-raw-recall", type=float)
    args = parser.parse_args()

    report = evaluate_canaries(args.db, load_canaries(args.manifest))
    rendered = json.dumps(report, indent=2, sort_keys=True) + "\n"
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(rendered, encoding="utf-8")
    print(rendered, end="")
    if (
        args.fail_under_raw_recall is not None
        and report["summary"]["raw_recall"] < args.fail_under_raw_recall
    ):
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
