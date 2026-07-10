from __future__ import annotations

import sqlite3
import tempfile
import unittest
import uuid
from pathlib import Path

import local_store


def _insert_skill(conn: sqlite3.Connection, skill_id: str, content_hash: str) -> None:
    conn.execute(
        """
        INSERT INTO skills (id, name, description, source, url, content_hash, quality_status, quality_score)
        VALUES (?, ?, ?, ?, ?, ?, 'active', 80)
        """,
        (skill_id, skill_id, "test skill", "test", f"https://example.com/{skill_id}", content_hash),
    )


def _insert_route_event(conn: sqlite3.Connection, skill_id: str, outcome: str, source: str = "") -> None:
    conn.execute(
        "INSERT INTO route_events (id, created_at, skill_id, outcome, feedback_source) VALUES (?, ?, ?, ?, ?)",
        (str(uuid.uuid4()), local_store._now(), skill_id, outcome, source),
    )


class RecomputeFeedbackScoresTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        local_store.DB_PATH = Path(self.tmp.name) / "local_skills.db"
        local_store.init_db()

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path

    def test_well_sampled_skill_moves_off_neutral(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "well-sampled", "hash-well-sampled")
            for _ in range(10):
                _insert_route_event(conn, "well-sampled", "used")
            conn.commit()
        finally:
            conn.close()

        local_store.recompute_feedback_scores(min_samples=8)

        conn = local_store.get_conn()
        try:
            row = conn.execute(
                "SELECT feedback_score FROM skills WHERE id=?", ("well-sampled",)
            ).fetchone()
        finally:
            conn.close()
        self.assertGreater(row["feedback_score"], 0.5)

    def test_sparse_feedback_stays_pinned_at_neutral(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "cold-start", "hash-cold-start")
            _insert_route_event(conn, "cold-start", "failed")
            _insert_route_event(conn, "cold-start", "used")
            conn.commit()
        finally:
            conn.close()

        local_store.recompute_feedback_scores(min_samples=8)

        conn = local_store.get_conn()
        try:
            row = conn.execute(
                "SELECT feedback_score FROM skills WHERE id=?", ("cold-start",)
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(row["feedback_score"], 0.5)

    def test_feedback_pools_across_forks_sharing_content_hash(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "fork-a", "hash-shared")
            _insert_skill(conn, "fork-b", "hash-shared")
            for _ in range(5):
                _insert_route_event(conn, "fork-a", "used")
            for _ in range(5):
                _insert_route_event(conn, "fork-b", "used")
            conn.commit()
        finally:
            conn.close()

        local_store.recompute_feedback_scores(min_samples=8)

        conn = local_store.get_conn()
        try:
            rows = conn.execute(
                "SELECT id, feedback_score FROM skills WHERE content_hash='hash-shared'"
            ).fetchall()
        finally:
            conn.close()
        scores = {row["id"]: row["feedback_score"] for row in rows}
        self.assertGreater(scores["fork-a"], 0.5)
        self.assertEqual(scores["fork-a"], scores["fork-b"])

    def test_automatic_hook_outcomes_do_not_train_ranking(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "automatic-only", "hash-automatic")
            for _ in range(10):
                _insert_route_event(conn, "automatic-only", "used", "auto-skill-hook")
            conn.commit()
        finally:
            conn.close()

        local_store.recompute_feedback_scores(min_samples=8)

        conn = local_store.get_conn()
        try:
            row = conn.execute("SELECT feedback_score FROM skills WHERE id=?", ("automatic-only",)).fetchone()
        finally:
            conn.close()
        self.assertIsNone(row["feedback_score"])

    def test_fts_uses_meaningful_terms_and_deduplicates_content(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "spreadsheet-a", "hash-spreadsheet")
            _insert_skill(conn, "spreadsheet-b", "hash-spreadsheet")
            conn.execute(
                "UPDATE skills SET description=? WHERE id IN ('spreadsheet-a', 'spreadsheet-b')",
                ("Spreadsheet report formulas and chart validation",),
            )
            conn.commit()
        finally:
            conn.close()

        results = local_store.search_skills_fts("please create a spreadsheet report with formulas", max_results=5)

        self.assertEqual(len(results), 1)
        self.assertIn(results[0]["id"], {"spreadsheet-a", "spreadsheet-b"})


if __name__ == "__main__":
    unittest.main()
