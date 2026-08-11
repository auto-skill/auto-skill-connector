from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from corpus_canary import Canary, evaluate_canaries, load_canaries


def _create_db(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute(
        """
        CREATE TABLE skills (
            id TEXT, name TEXT, url TEXT, source TEXT, quality_status TEXT,
            quality_reasons TEXT, content_hash TEXT, package_completeness TEXT,
            source_commit_sha TEXT, raw TEXT, discovered_at TEXT
        )
        """
    )
    connection.executemany(
        "INSERT INTO skills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "active",
                "active-skill",
                "https://example.com/active",
                "github_skill_file",
                "active",
                "[]",
                "hash-active",
                "complete",
                "commit-active",
                json.dumps(
                    {
                        "parent_repo": "example/active",
                        "path": "skills/active/SKILL.md",
                        "valid_skill": True,
                    }
                ),
                "2026-08-01T00:00:00Z",
            ),
            (
                "invalid",
                "invalid-skill",
                "https://example.com/invalid",
                "github_skill_file",
                "rejected",
                "[\"missing-frontmatter\"]",
                None,
                "missing",
                None,
                json.dumps(
                    {
                        "parent_repo": "example/invalid",
                        "path": "skills/invalid/SKILL.md",
                        "valid_skill": False,
                    }
                ),
                "2026-08-01T00:00:00Z",
            ),
        ],
    )
    connection.commit()
    connection.close()


def test_manifest_is_a_bounded_unique_corpus_metric() -> None:
    canaries = load_canaries()

    assert 20 <= len(canaries) <= 50
    assert len({canary.id for canary in canaries}) == len(canaries)
    assert any(canary.parent_repo == "oxcaml/oxcaml" for canary in canaries)


def test_evaluator_reports_discovery_frontmatter_and_hash_states(tmp_path: Path) -> None:
    db_path = tmp_path / "canary.db"
    _create_db(db_path)
    canaries = [
        Canary(
            "active",
            "example/active",
            "skills/active/SKILL.md",
            "https://example.com/active",
            "active-skill",
            "hash-active",
        ),
        Canary(
            "invalid",
            "example/invalid",
            "skills/invalid/SKILL.md",
            "https://example.com/invalid",
            "invalid-skill",
            None,
        ),
        Canary(
            "missing",
            "example/missing",
            "skills/missing/SKILL.md",
            "https://example.com/missing",
            "missing-skill",
            "hash-missing",
        ),
    ]

    report = evaluate_canaries(db_path, canaries)
    by_id = {result["id"]: result for result in report["canaries"]}

    assert report["summary"]["raw_discovered"] == 2
    assert report["summary"]["active_catalog"] == 1
    assert report["summary"]["raw_recall"] == 2 / 3
    assert by_id["active"]["drop_stage"] == "none"
    assert by_id["active"]["hash_match"] is True
    assert by_id["invalid"]["drop_stage"] == "frontmatter"
    assert by_id["missing"]["drop_stage"] == "discovery"
