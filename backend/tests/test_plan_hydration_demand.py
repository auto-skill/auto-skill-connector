from __future__ import annotations

import json
import sqlite3

from plan_hydration_demand import plan


def _db(path):
    conn = sqlite3.connect(path)
    conn.executescript(
        """
        CREATE TABLE skills (
            id TEXT PRIMARY KEY,
            url TEXT,
            name TEXT,
            source TEXT,
            quality_status TEXT,
            quality_score INTEGER,
            package_hash TEXT,
            package_completeness TEXT,
            dependency_closure_status TEXT,
            raw TEXT
        );
        CREATE TABLE route_events (
            id TEXT PRIMARY KEY,
            skill_id TEXT,
            outcome TEXT
        );
        """
    )
    conn.executemany(
        "INSERT INTO skills VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                "popular",
                "https://github.com/acme/popular",
                "popular",
                "github",
                "active",
                90,
                None,
                None,
                None,
                json.dumps({"stars": 100}),
            ),
            (
                "complete",
                "https://github.com/acme/complete",
                "complete",
                "github",
                "active",
                99,
                "a" * 64,
                "complete",
                "complete",
                json.dumps({"stars": 9999}),
            ),
            (
                "other-source",
                "https://example.com/other",
                "other",
                "other",
                "active",
                100,
                None,
                None,
                None,
                "{}",
            ),
        ],
    )
    conn.executemany(
        "INSERT INTO route_events VALUES (?, ?, ?)",
        [
            ("r1", "popular", "used"),
            ("r2", "popular", "installed"),
            ("r3", "popular", "dismissed"),
        ],
    )
    conn.commit()
    conn.close()


def test_plan_is_read_only_and_prioritizes_positive_demand(tmp_path) -> None:
    db = tmp_path / "skills.db"
    _db(db)
    before = db.read_bytes()

    result = plan(db, limit=10)

    assert db.read_bytes() == before
    assert [item["skill_id"] for item in result["items"]] == ["popular"]
    assert result["items"][0]["demand"] == {
        "route_count": 3,
        "positive_routes": 2,
        "negative_routes": 1,
        "stars": 100,
    }
