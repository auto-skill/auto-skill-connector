from __future__ import annotations

import json
import shutil
import sqlite3
import tempfile
import unittest
import uuid
import zipfile
from contextlib import redirect_stdout
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import local_store
import scrub_route_privacy


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


def _retrieval_row(skill_id: str, **overrides) -> dict:
    row = {
        "id": skill_id,
        "name": skill_id,
        "description": "bounded specialist guidance",
        "quality_status": "active",
        "risk_score": 0,
        "content_hash": skill_id,
        "rank": 1.0,
    }
    row.update(overrides)
    return row


class HybridRetrievalRecallTests(unittest.TestCase):
    # This session added two more fusion channels to hybrid_search_skills
    # (vector_search_tools, _fast_path_matches) alongside the two these tests
    # already mock -- neutralize both by default so a real, uninitialized
    # sqlite connection is never touched and these tests keep exercising only
    # the FTS/skill-vector lanes they're actually about.
    @patch("local_store.quality.meaningfulness_components", return_value={"prominence": 0, "provenance": 0, "meaningfulness": 0})
    @patch("local_store.quality.dedupe_by_content_hash", side_effect=lambda rows: rows)
    @patch("local_store._fast_path_matches", return_value={})
    @patch("local_store.vector_search_tools", return_value=[])
    @patch("local_store.vector_search_skills")
    @patch("local_store.search_skills_fts")
    def test_global_vector_lane_can_recover_specialist_outside_lexical_candidates(
        self, search_fts, vector_search, _tool_vec, _fast_path, _dedupe, _components
    ) -> None:
        search_fts.return_value = [_retrieval_row(f"decoy-{index}") for index in range(60)]
        vector_search.return_value = [_retrieval_row("semantic-specialist", rank=0.93)]

        results = local_store.hybrid_search_skills("generic boilerplate before niche terms", [0.1] * 384, 20)

        vector_search.assert_called_once_with([0.1] * 384, 60)
        specialist = next(row for row in results if row["id"] == "semantic-specialist")
        self.assertEqual(specialist["similarity"], 0.93)

    @patch("local_store.quality.meaningfulness_components", return_value={"prominence": 0, "provenance": 0, "meaningfulness": 0})
    @patch("local_store.quality.dedupe_by_content_hash", side_effect=lambda rows: rows)
    @patch("local_store._fast_path_matches", return_value={})
    @patch("local_store.vector_search_tools", return_value=[])
    @patch("local_store.vector_search_skills")
    @patch("local_store.search_skills_fts")
    def test_embedding_lane_retains_active_pending_embedding_and_excludes_prompt_dumps(
        self, search_fts, vector_search, _tool_vec, _fast_path, _dedupe, _components
    ) -> None:
        search_fts.return_value = [
            _retrieval_row("lexical-only"),
            _retrieval_row("semantic-specialist"),
        ]
        vector_search.return_value = [
            _retrieval_row("semantic-specialist", rank=0.91),
            _retrieval_row("metadata", rank=0.99, quality_status="metadata_only"),
            _retrieval_row("prompt-dump", rank=0.98, description="x" * 2001),
        ]

        results = local_store.hybrid_search_skills("specialized task", [0.1] * 384, 10)

        # metadata_only is discovery-eligible this session (quality.ACTIVE_STATUSES,
        # not active-only) -- it surfaces in the fused set alongside the two
        # active rows now, still subject to the same description-length bound.
        self.assertEqual(
            [row["id"] for row in results],
            ["semantic-specialist", "metadata", "lexical-only"],
        )
        self.assertIsNone(results[-1]["similarity"])

    @patch("local_store.quality.meaningfulness_components", return_value={"prominence": 0, "provenance": 0, "meaningfulness": 0})
    @patch("local_store.quality.dedupe_by_content_hash", side_effect=lambda rows: rows)
    @patch("local_store._fast_path_matches", return_value={})
    @patch("local_store.vector_search_tools", return_value=[])
    @patch("local_store.vector_search_skills")
    @patch("local_store.search_skills_fts")
    def test_pending_fts_saturation_cannot_truncate_semantic_lane(
        self, search_fts, vector_search, _tool_vec, _fast_path, _dedupe, _components
    ) -> None:
        search_fts.return_value = [
            _retrieval_row(f"pending-{index}") for index in range(60)
        ]
        vector_search.return_value = [
            _retrieval_row(f"semantic-{index}", rank=0.99 - index / 1000)
            for index in range(20)
        ]

        results = local_store.hybrid_search_skills("exact lexical saturation", [0.1] * 384, 10)

        semantic = [row for row in results if row["similarity"] is not None]
        pending = [row for row in results if row["similarity"] is None]
        self.assertEqual(len(semantic), 9)
        self.assertEqual(len(pending), 1)
        self.assertTrue(all(row["id"].startswith("semantic-") for row in semantic))
        self.assertEqual(results[-1]["id"], "pending-0")

    @patch("local_store.quality.meaningfulness_components", return_value={"prominence": 0, "provenance": 0, "meaningfulness": 0})
    @patch("local_store.quality.dedupe_by_content_hash", side_effect=lambda rows: rows)
    @patch("local_store._fast_path_matches", return_value={})
    @patch("local_store.search_skills_fts")
    def test_bounded_active_fts_remains_available_when_embedding_fails(
        self, search_fts, _fast_path, _dedupe, _components
    ) -> None:
        search_fts.return_value = [
            _retrieval_row("active-fallback"),
            _retrieval_row("metadata", quality_status="metadata_only"),
            _retrieval_row("prompt-dump", description="x" * 2001),
        ]

        results = local_store.hybrid_search_skills("specialized task", None, 10)

        # metadata_only is discovery-eligible this session -- it's no longer
        # excluded from the fused set, just from full/inject tier (a
        # separate, unchanged gate in quality.tier_for_ranked_candidates).
        self.assertEqual([row["id"] for row in results], ["active-fallback", "metadata"])


class RouteEventPrivacyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        self.db_path = Path(self.tmp.name) / "local_skills.db"
        local_store.DB_PATH = self.db_path
        local_store.init_db()

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path

    def _add_legacy_columns(self) -> None:
        conn = local_store.get_conn()
        try:
            existing = {row["name"] for row in conn.execute("PRAGMA table_info(route_events)")}
            for column in local_store.ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS:
                if column not in existing:
                    conn.execute(f"ALTER TABLE route_events ADD COLUMN {column} TEXT")
            conn.commit()
        finally:
            conn.close()

    def test_new_schema_omits_raw_and_free_form_columns(self) -> None:
        conn = local_store.get_conn()
        try:
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(route_events)")}
        finally:
            conn.close()

        self.assertTrue(set(local_store.ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS).isdisjoint(columns))

    def test_insert_route_event_drops_forbidden_and_arbitrary_fields(self) -> None:
        sentinel = f"private-prompt-{uuid.uuid4()}"
        local_store.insert_route_event(
            {
                "id": "privacy-event",
                "user_id": "user-1",
                "client": "auto-skill-hook",
                "client_version": "0.1.0",
                "query_chars": len(sentinel),
                "tier": "full",
                "prompt_text": sentinel,
                "query_hash": sentinel,
                "feedback_note": sentinel,
                "unexpected_raw_field": sentinel,
                "skip_reason": sentinel,
                "outcome": "raw prompt was excellent",
                "warnings": [],
            }
        )

        conn = local_store.get_conn()
        try:
            row = conn.execute("SELECT * FROM route_events WHERE id='privacy-event'").fetchone()
        finally:
            conn.close()

        self.assertEqual(row["query_chars"], len(sentinel))
        self.assertEqual(row["tier"], "full")
        self.assertIsNone(row["skip_reason"])
        self.assertIsNone(row["outcome"])
        self.assertNotIn(sentinel.encode(), self.db_path.read_bytes())

    def test_anonymous_id_hash_is_retained_only_when_well_formed(self) -> None:
        local_store.insert_route_event(
            {
                "id": "anonymous-valid",
                "tier": "hint",
                "anonymous_id_hash": "a" * 64,
            }
        )
        local_store.insert_route_event(
            {
                "id": "anonymous-invalid",
                "tier": "hint",
                "anonymous_id_hash": "not-a-user-id",
            }
        )
        conn = local_store.get_conn()
        try:
            valid = conn.execute(
                "SELECT anonymous_id_hash FROM route_events WHERE id='anonymous-valid'"
            ).fetchone()
            invalid = conn.execute(
                "SELECT anonymous_id_hash FROM route_events WHERE id='anonymous-invalid'"
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(valid["anonymous_id_hash"], "a" * 64)
        self.assertIsNone(invalid["anonymous_id_hash"])

    def test_ip_address_is_retained_only_when_a_real_address(self) -> None:
        local_store.insert_route_event(
            {"id": "ip-valid", "tier": "hint", "ip_address": "203.0.113.5"}
        )
        local_store.insert_route_event(
            {"id": "ip-invalid", "tier": "hint", "ip_address": "not-an-ip, 203.0.113.5"}
        )
        conn = local_store.get_conn()
        try:
            valid = conn.execute("SELECT ip_address FROM route_events WHERE id='ip-valid'").fetchone()
            invalid = conn.execute("SELECT ip_address FROM route_events WHERE id='ip-invalid'").fetchone()
        finally:
            conn.close()
        self.assertEqual(valid["ip_address"], "203.0.113.5")
        self.assertIsNone(invalid["ip_address"])

    def test_ip_address_expires_with_the_retention_window(self) -> None:
        stale = (
            datetime.now(timezone.utc)
            - timedelta(days=local_store.ANONYMOUS_ID_RETENTION_DAYS + 1)
        ).isoformat()
        conn = local_store.get_conn()
        try:
            conn.execute(
                "INSERT INTO route_events (id, created_at, tier, ip_address) VALUES (?, ?, 'hint', ?)",
                ("ip-stale", stale, "203.0.113.5"),
            )
            conn.commit()
        finally:
            conn.close()

        local_store.insert_route_event({"id": "ip-fresh", "tier": "hint", "ip_address": "203.0.113.6"})

        conn = local_store.get_conn()
        try:
            stale_row = conn.execute("SELECT ip_address FROM route_events WHERE id='ip-stale'").fetchone()
            fresh_row = conn.execute("SELECT ip_address FROM route_events WHERE id='ip-fresh'").fetchone()
        finally:
            conn.close()
        self.assertIsNone(stale_row["ip_address"])
        self.assertEqual(fresh_row["ip_address"], "203.0.113.6")

    def test_init_db_scrubs_legacy_columns_and_unsafe_skip_reason(self) -> None:
        self._add_legacy_columns()
        sentinel = f"legacy-private-prompt-{uuid.uuid4()}"
        conn = local_store.get_conn()
        try:
            conn.execute(
                """
                INSERT INTO route_events
                    (id, user_id, tier, prompt_text, query_hash, feedback_note, skip_reason)
                VALUES ('legacy-unsafe', 'user-1', 'skipped', ?, ?, ?, ?)
                """,
                (sentinel, sentinel, sentinel, sentinel),
            )
            conn.execute(
                "INSERT INTO route_events (id, user_id, tier, skip_reason) VALUES (?, ?, ?, ?)",
                ("legacy-safe", "user-1", "skipped", "too short"),
            )
            conn.commit()
        finally:
            conn.close()

        before = local_store.route_event_privacy_status()
        local_store.init_db()
        after = local_store.route_event_privacy_status()

        self.assertFalse(before["ok"])
        self.assertEqual(before["violations"], 4)
        self.assertTrue(after["ok"])
        conn = local_store.get_conn()
        try:
            unsafe = conn.execute("SELECT * FROM route_events WHERE id='legacy-unsafe'").fetchone()
            safe = conn.execute("SELECT skip_reason FROM route_events WHERE id='legacy-safe'").fetchone()
        finally:
            conn.close()
        self.assertIsNone(unsafe["prompt_text"])
        self.assertIsNone(unsafe["query_hash"])
        self.assertIsNone(unsafe["feedback_note"])
        self.assertIsNone(unsafe["skip_reason"])
        self.assertEqual(safe["skip_reason"], "too short")

    def test_runs_projection_never_returns_legacy_text(self) -> None:
        self._add_legacy_columns()
        sentinel = f"runs-private-prompt-{uuid.uuid4()}"
        conn = local_store.get_conn()
        try:
            conn.execute(
                """
                INSERT INTO route_events
                    (id, user_id, tier, prompt_text, query_hash, feedback_note, skip_reason, warnings)
                VALUES ('legacy-run', 'user-1', 'full', ?, ?, ?, 'too short', '[]')
                """,
                (sentinel, sentinel, sentinel),
            )
            conn.commit()
        finally:
            conn.close()

        runs = local_store.list_route_events_for_user("user-1")

        self.assertEqual([run["id"] for run in runs], ["legacy-run"])
        self.assertTrue(set(local_store.ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS).isdisjoint(runs[0]))
        self.assertNotIn(sentinel, json.dumps(runs))

    def test_feedback_ignores_free_form_note_and_rejects_unknown_outcome(self) -> None:
        sentinel = f"feedback-private-prompt-{uuid.uuid4()}"
        local_store.insert_route_event({"id": "feedback-event", "tier": "hint"})

        self.assertTrue(
            local_store.update_route_event_feedback(
                "feedback-event", "used", source="unit-test", note=sentinel
            )
        )
        self.assertFalse(
            local_store.update_route_event_feedback(
                "feedback-event", "raw prompt was excellent", source="unit-test", note=sentinel
            )
        )
        conn = local_store.get_conn()
        try:
            row = conn.execute(
                "SELECT outcome, feedback_source FROM route_events WHERE id='feedback-event'"
            ).fetchone()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        self.assertEqual(row["outcome"], "used")
        self.assertEqual(row["feedback_source"], "unit-test")
        self.assertNotIn(sentinel.encode(), self.db_path.read_bytes())

    def test_physical_scrub_removes_sentinel_from_database_and_wal(self) -> None:
        self._add_legacy_columns()
        sentinel = (f"PHYSICAL_PRIVATE_SENTINEL_{uuid.uuid4()}_" * 20).encode()
        text = sentinel.decode()
        conn = local_store.get_conn()
        try:
            conn.execute(
                """
                INSERT INTO route_events
                    (id, tier, prompt_text, query_hash, feedback_note, skip_reason)
                VALUES ('physical-legacy', 'skipped', ?, ?, ?, ?)
                """,
                (text, text, text, text),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        self.assertIn(sentinel, self.db_path.read_bytes())

        result = scrub_route_privacy.scrub_database(self.db_path, vacuum=True)

        self.assertTrue(result["physical_scrub_complete"])
        self.assertFalse(result["before"]["ok"])
        self.assertTrue(result["after"]["ok"])
        self.assertNotIn(sentinel, self.db_path.read_bytes())
        wal_path = Path(f"{self.db_path}-wal")
        if wal_path.exists():
            self.assertEqual(wal_path.stat().st_size, 0)
            self.assertNotIn(sentinel, wal_path.read_bytes())

    def test_scrub_cli_audit_prints_counts_never_values(self) -> None:
        self._add_legacy_columns()
        sentinel = f"cli-private-prompt-{uuid.uuid4()}"
        conn = local_store.get_conn()
        try:
            conn.execute(
                "INSERT INTO route_events (id, prompt_text, skip_reason) VALUES ('cli-legacy', ?, ?)",
                (sentinel, sentinel),
            )
            conn.commit()
        finally:
            conn.close()

        output = StringIO()
        with redirect_stdout(output):
            result = scrub_route_privacy.main(["--db-path", str(self.db_path)])

        self.assertEqual(result, 0)
        self.assertNotIn(sentinel, output.getvalue())
        payload = json.loads(output.getvalue())
        self.assertFalse(payload["ok"])
        self.assertEqual(payload["violations"], 2)

    def test_backup_purge_physically_scrubs_then_removes_exact_database_copies(self) -> None:
        self._add_legacy_columns()
        sentinel = f"backup-private-prompt-{uuid.uuid4()}"
        conn = local_store.get_conn()
        try:
            conn.execute(
                "INSERT INTO route_events (id, prompt_text) VALUES ('backup-legacy', ?)",
                (sentinel,),
            )
            conn.commit()
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        backup_root = Path(self.tmp.name) / "backups"
        backup_dir = backup_root / "old"
        backup_dir.mkdir(parents=True)
        backup_db = backup_dir / "local_skills.db"
        shutil.copy2(self.db_path, backup_db)

        removed = scrub_route_privacy.scrub_and_remove_backup_databases([backup_root])

        self.assertEqual(removed, 1)
        self.assertFalse(backup_db.exists())

    def test_backup_purge_removes_seed_zip_archives(self) -> None:
        seed_root = Path(self.tmp.name) / "seed-packets"
        seed_root.mkdir()
        archive = seed_root / "20260710T000000Z.zip"
        with zipfile.ZipFile(archive, "w") as packet:
            packet.writestr("local_skills.db", "legacy database bytes")

        removed = scrub_route_privacy.remove_backup_archives([seed_root])

        self.assertEqual(removed, 1)
        self.assertFalse(archive.exists())


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

    def test_fts_finds_skills_by_capability_summary_and_triggers_alone(self) -> None:
        """A metadata_only MCP server's name/description is often a thin
        registry blurb; the real signal lives in capability_summary/triggers
        (see scraper.scan_skill stage 3). FTS must index those columns too,
        not just name/description/tags."""
        conn = local_store.get_conn()
        try:
            conn.execute(
                """
                INSERT INTO skills
                    (id, name, description, source, url, content_hash, quality_status, quality_score,
                     capability_summary, triggers)
                VALUES (?, ?, ?, 'test', ?, 'hash-mcp', 'metadata_only', 40, ?, ?)
                """,
                (
                    "mcp-server",
                    "acme-mcp",
                    "A remote MCP server.",
                    "https://example.com/mcp-server",
                    "Lets an agent query flibbertigibbet widget telemetry over a websocket.",
                    json.dumps(["querying flibbertigibbet widget telemetry"]),
                ),
            )
            conn.commit()
        finally:
            conn.close()

        results = local_store.search_skills_fts("flibbertigibbet widget telemetry", max_results=5)

        self.assertEqual([r["id"] for r in results], ["mcp-server"])

    def test_vector_search_tools_rolls_up_to_best_tool_per_skill(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "multi-tool-server", "hash-multi-tool")
            conn.execute(
                "UPDATE skills SET quality_status='metadata_only' WHERE id='multi-tool-server'"
            )
            tools = [
                ("get_weather", [1.0, 0.0, 0.0] + [0.0] * 381),
                ("send_email", [0.0, 1.0, 0.0] + [0.0] * 381),
                ("list_files", [0.0, 0.0, 1.0] + [0.0] * 381),
            ]
            for name, vec in tools:
                conn.execute(
                    "INSERT INTO skill_tools (id, skill_url, tool_name, tool_description, embedding, created_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(uuid.uuid4()),
                        "https://example.com/multi-tool-server",
                        name,
                        f"Tool: {name}",
                        local_store.pack_embedding(vec),
                        local_store._now(),
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        query = [0.0, 1.0, 0.0] + [0.0] * 381  # matches send_email exactly
        results = local_store.vector_search_tools(query, match_count=5)

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["id"], "multi-tool-server")
        self.assertEqual(results[0]["matched_tool"], "send_email")
        self.assertAlmostEqual(results[0]["rank"], 1.0, places=5)

    def test_hybrid_search_surfaces_skill_via_tool_only_match(self) -> None:
        """A skill with no matching keywords/skill-level embedding should
        still surface if one of its tools matches the query well -- this is
        the whole point of the per-tool channel."""
        conn = local_store.get_conn()
        try:
            conn.execute(
                "INSERT INTO skills (id, name, description, source, url, content_hash, quality_status, quality_score, risk_score) "
                "VALUES ('tool-only-skill', 'zzz-unrelated-name', 'completely unrelated filler text', 'test', "
                "'https://example.com/tool-only-skill', 'hash-tool-only', 'metadata_only', 80, 0)"
            )
            conn.execute(
                "INSERT INTO skill_tools (id, skill_url, tool_name, tool_description, embedding, created_at) "
                "VALUES (?, 'https://example.com/tool-only-skill', 'exact_match_tool', 'does the exact thing', ?, ?)",
                (str(uuid.uuid4()), local_store.pack_embedding([1.0] + [0.0] * 383), local_store._now()),
            )
            conn.commit()
        finally:
            conn.close()

        query = [1.0] + [0.0] * 383
        results = local_store.hybrid_search_skills("unrelated query text with no overlap", query, match_count=10)

        self.assertIn("tool-only-skill", [r["id"] for r in results])

    def test_fast_path_forces_inclusion_of_trigger_match_missed_by_fts_and_vector(self) -> None:
        conn = local_store.get_conn()
        try:
            conn.execute(
                "INSERT INTO skills (id, name, description, source, url, content_hash, quality_status, quality_score, risk_score, triggers) "
                "VALUES ('trigger-only-skill', 'zzz-unrelated', 'nothing in common with the query text', 'test', "
                "'https://example.com/trigger-only-skill', 'hash-trigger-only', 'active', 80, 0, ?)",
                (json.dumps(["flibbertigibbet widget telemetry export"]),),
            )
            conn.commit()
        finally:
            conn.close()

        local_store.warm_lexical_index()  # also warms the fast-path index

        results = local_store.hybrid_search_skills(
            "please export the flibbertigibbet widget telemetry", None, match_count=10
        )

        self.assertIn("trigger-only-skill", [r["id"] for r in results])

    def test_hybrid_search_vector_channel_is_not_restricted_to_fts_hits(self) -> None:
        """Regression test for the FTS-gates-vector bug: hybrid_search_skills
        used to restrict the vector channel to FTS's candidate set whenever
        FTS found >=10 hits, so a semantically on-target skill sharing no
        keywords with the query could never surface via the vector channel.
        vector_search_skills must be called without candidate_ids."""
        conn = local_store.get_conn()
        try:
            for i in range(12):
                _insert_skill(conn, f"keyword-match-{i}", f"hash-kw-{i}")
                conn.execute(
                    "UPDATE skills SET description=? WHERE id=?",
                    ("banana banana banana banana", f"keyword-match-{i}"),
                )
            _insert_skill(conn, "semantic-only-match", "hash-semantic")
            conn.commit()
        finally:
            conn.close()

        captured = {}
        real_vector_search = local_store.vector_search_skills

        def spy(query_embedding, match_count=10, candidate_ids=None):
            captured["candidate_ids"] = candidate_ids
            return real_vector_search(query_embedding, match_count, candidate_ids=candidate_ids)

        with patch.object(local_store, "vector_search_skills", side_effect=spy):
            local_store.hybrid_search_skills("banana", [0.1] * 384, match_count=5)

        self.assertIsNone(captured["candidate_ids"])

    def test_top_scored_skill_ids_matches_legacy_score_then_id_order(self) -> None:
        scores = {
            "skill-a": 2.0,
            "skill-z": 2.0,
            "skill-b": 3.0,
            "skill-y": 3.0,
            "skill-q": 1.0,
        }

        self.assertEqual(
            local_store._top_scored_skill_ids(scores, 3),
            ["skill-y", "skill-b", "skill-z"],
        )
        self.assertEqual(
            local_store._top_scored_skill_ids(scores, 10),
            ["skill-y", "skill-b", "skill-z", "skill-a", "skill-q"],
        )
        self.assertEqual(
            local_store._top_scored_skill_ids(scores, 3, ["skill-z", "skill-y", "skill-q", "skill-b", "skill-a"]),
            ["skill-y", "skill-b", "skill-z"],
        )

    def test_init_db_persists_wal_mode_without_reasserting_it_per_connection(self) -> None:
        conn = local_store.get_conn()
        try:
            self.assertEqual(conn.execute("PRAGMA journal_mode").fetchone()[0], "wal")
        finally:
            conn.close()

        statements: list[str] = []
        real_connect = sqlite3.connect

        def traced_connect(*args, **kwargs):
            traced = real_connect(*args, **kwargs)
            traced.set_trace_callback(statements.append)
            return traced

        with patch.object(local_store.sqlite3, "connect", side_effect=traced_connect):
            conn = local_store.get_conn()
            try:
                conn.execute("SELECT 1").fetchone()
            finally:
                conn.close()

        self.assertFalse(any("journal_mode" in statement.lower() for statement in statements))

    def test_vector_candidate_fetch_omits_embedding_blob(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "vector-projection", "hash-vector-projection")
            conn.execute(
                "UPDATE skills SET embedding=?, raw=? WHERE id='vector-projection'",
                (
                    local_store.pack_embedding([1.0] + [0.0] * 383),
                    json.dumps({"stars": 42, "publisher_verified": True}),
                ),
            )
            conn.commit()
        finally:
            conn.close()
        local_store.invalidate_vector_cache()

        statements: list[str] = []
        original_get_conn = local_store.get_conn

        def traced_get_conn():
            traced = original_get_conn()
            traced.set_trace_callback(statements.append)
            return traced

        with patch.object(local_store, "get_conn", side_effect=traced_get_conn):
            results = local_store.vector_search_skills([1.0] + [0.0] * 383, match_count=1)

        candidate_queries = [statement for statement in statements if "FROM skills WHERE id IN" in statement]
        self.assertEqual(len(candidate_queries), 1)
        self.assertNotIn("embedding", local_store.SKILL_RETRIEVAL_COLUMNS)
        self.assertNotIn("SELECT *", candidate_queries[0].upper())
        self.assertEqual(results[0]["id"], "vector-projection")
        self.assertEqual(results[0]["stars"], 42)
        self.assertTrue(results[0]["raw"]["publisher_verified"])
        self.assertNotIn("embedding", results[0])

    def test_warm_vector_index_builds_a_cache_for_active_vectors(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "vector-ready", "hash-vector-ready")
            conn.execute(
                "UPDATE skills SET embedding=? WHERE id='vector-ready'",
                (local_store.pack_embedding([1.0] + [0.0] * 383),),
            )
            conn.commit()
        finally:
            conn.close()

        stats = local_store.warm_vector_index()

        self.assertTrue(stats["cache_ready"])
        self.assertTrue(stats["cache_current"])
        self.assertEqual(stats["cache_vectors"], 1)

    def test_invalidation_keeps_last_complete_matrix_until_background_warm(self) -> None:
        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "first-vector", "hash-first-vector")
            conn.execute(
                "UPDATE skills SET embedding=? WHERE id='first-vector'",
                (local_store.pack_embedding([1.0] + [0.0] * 383),),
            )
            conn.commit()
        finally:
            conn.close()

        local_store.warm_vector_index()

        conn = local_store.get_conn()
        try:
            _insert_skill(conn, "second-vector", "hash-second-vector")
            conn.execute(
                "UPDATE skills SET embedding=? WHERE id='second-vector'",
                (local_store.pack_embedding([0.0, 1.0] + [0.0] * 382),),
            )
            conn.commit()
        finally:
            conn.close()
        local_store.invalidate_vector_cache()

        stale = local_store.vector_index_stats()
        old_results = local_store.vector_search_skills([1.0] + [0.0] * 383, match_count=2)

        self.assertTrue(stale["cache_ready"])
        self.assertFalse(stale["cache_current"])
        self.assertEqual(stale["cache_vectors"], 1)
        self.assertEqual([row["id"] for row in old_results], ["first-vector"])

        refreshed = local_store.warm_vector_index()
        new_results = local_store.vector_search_skills([0.0, 1.0] + [0.0] * 382, match_count=2)

        self.assertTrue(refreshed["cache_current"])
        self.assertEqual(refreshed["cache_vectors"], 2)
        self.assertEqual(new_results[0]["id"], "second-vector")


if __name__ == "__main__":
    unittest.main()
