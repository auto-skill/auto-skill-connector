"""Phase 2B: honest whole-skill delivery and complete /content bytes.

Reconciliation note: delivery is no longer bounded to a small char budget --
a "full" result ships the whole curated, safety-stripped skill (see
context_guard.build_context_guard). These tests assert completeness, not
truncation.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import auth
import local_store
import scraper
from context_guard import (
    DEFAULT_INLINE_CHARS,
    build_context_guard,
)
from embeddings import LibraryContent
from quality import content_hash


VALID_SMALL = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas and charts.
---

## Workflow

Inspect the source data, create the workbook, add formulas, verify calculations,
and explain the generated file.
"""


def _large_static_skill() -> str:
    # Exceeds DEFAULT_INLINE_CHARS so we can assert the whole body still ships.
    body = VALID_SMALL + "\n## Reference\n" + ("Keep the workbook reproducible. " * 500)
    assert len(body) > DEFAULT_INLINE_CHARS
    return body


class DeliveryHonestyUnitTests(unittest.TestCase):
    def test_small_skill_is_delivered_whole(self) -> None:
        guard = build_context_guard(
            task="create an excel report with formulas",
            content=VALID_SMALL,
            content_hash="a" * 64,
            content_digest="b" * 64,
        )
        self.assertEqual(guard["delivery"], "capsule")
        self.assertTrue(guard["complete"])
        self.assertEqual(guard["capsule"], VALID_SMALL)
        self.assertIsNone(guard["fetch_hint"])

    def test_large_skill_is_delivered_whole_not_truncated(self) -> None:
        content = _large_static_skill()
        guard = build_context_guard(
            task="create an excel report with formulas",
            content=content,
            content_hash="c" * 64,
            content_digest="d" * 64,
            supports_isolation=False,
        )
        self.assertEqual(guard["delivery"], "capsule")
        self.assertTrue(guard["complete"])
        self.assertIsNone(guard["fetch_hint"])
        self.assertEqual(guard["capsule"], content)
        self.assertEqual(guard["capsule_chars"], len(content))

    def test_isolation_request_still_delivers_whole_content(self) -> None:
        content = _large_static_skill()
        guard = build_context_guard(
            task="create an excel report with formulas",
            content=content,
            content_hash="e" * 64,
            supports_isolation=True,
        )
        self.assertEqual(guard["delivery"], "capsule")
        self.assertTrue(guard["complete"])
        self.assertEqual(guard["capsule"], content)


class MeasurementModeAssignmentUnitTests(unittest.TestCase):
    """The hash-bucketing itself, independent of set_measurement_mode's
    clamp -- covers what the /route integration test intentionally no
    longer exercises end-to-end (see test_measurement_mode_holdout_..."""

    def test_arm_is_holdout_or_routed_at_the_rate_extremes(self) -> None:
        import recommender

        with patch(
            "recommender.store.get_measurement_mode_settings",
            return_value={"enabled": True, "holdout_rate": 1.0},
        ):
            result = recommender._measurement_mode_assignment("u1", "coding", "cli", 20, None, "route-1")
        self.assertEqual(result["arm"], "holdout")

        with patch(
            "recommender.store.get_measurement_mode_settings",
            return_value={"enabled": True, "holdout_rate": 0.0},
        ):
            result = recommender._measurement_mode_assignment("u1", "coding", "cli", 20, None, "route-2")
        self.assertEqual(result["arm"], "routed")

    def test_returns_none_when_not_opted_in(self) -> None:
        import recommender

        with patch(
            "recommender.store.get_measurement_mode_settings",
            return_value={"enabled": False, "holdout_rate": 0.5},
        ):
            result = recommender._measurement_mode_assignment("u1", "coding", "cli", 20, None, "route-3")
        self.assertIsNone(result)

    def test_assignment_is_sticky_to_session_id_across_different_routes(self) -> None:
        import recommender

        with patch(
            "recommender.store.get_measurement_mode_settings",
            return_value={"enabled": True, "holdout_rate": 0.5},
        ):
            first = recommender._measurement_mode_assignment("u1", "coding", "cli", 20, "session-a", "route-1")
            second = recommender._measurement_mode_assignment("u1", "coding", "cli", 20, "session-a", "route-2")
        self.assertEqual(first["arm"], second["arm"])


class ContentAndRouteDeliveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        self.db_path = Path(self.tmp.name) / "local_skills.db"
        self.library_dir = Path(self.tmp.name) / "skills_library"
        self.files_dir = self.library_dir / "files"
        self.files_dir.mkdir(parents=True)
        local_store.DB_PATH = self.db_path
        scraper.store.DB_PATH = self.db_path
        local_store.init_db()
        local_store.invalidate_vector_cache()
        self.embedding_status = patch(
            "scraper.embedding_model_status",
            return_value={"ready": True, "error": None},
        ).start()
        self.addCleanup(patch.stopall)
        self.client = TestClient(scraper.app)

    def tearDown(self) -> None:
        local_store.invalidate_vector_cache()
        local_store.DB_PATH = self.old_db_path
        scraper.store.DB_PATH = self.old_db_path

    def _auth_headers(self) -> dict:
        user = local_store.get_or_create_user("delivery-honesty@example.com", "Delivery Honesty", None)
        token = auth.issue_cli_token(user["id"])
        return {"Authorization": f"Bearer {token}"}

    def test_content_endpoint_returns_full_stored_body(self) -> None:
        body = _large_static_skill()
        chash = content_hash(body)
        filename = f"{chash}.md"
        (self.files_dir / filename).write_text(body, encoding="utf-8")
        index = {
            "https://example.com/large": {
                "file": filename,
                "content_hash": chash,
            }
        }
        (self.library_dir / "index.json").write_text(json.dumps(index), encoding="utf-8")

        with patch("recommender.LibraryContent", lambda: LibraryContent(self.library_dir)):
            response = self.client.get(f"/content/{chash}")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, body)
        self.assertGreater(len(response.text), DEFAULT_INLINE_CHARS)

    def test_validated_full_route_delivers_the_whole_skill(self) -> None:
        content = _large_static_skill()
        chash = content_hash(content)
        candidate = {
            "id": "skill-large",
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://example.com/large-spreadsheet",
            "risk_score": 0,
            "quality_status": "active",
            "quality_score": 90,
            "content_hash": chash,
            "rank": 1.0,
            "similarity": 0.95,
        }

        async def fake_retrieve(client, query, limit):
            del client, query, limit
            return [candidate]

        class FakeLibrary:
            def get(self, url: str) -> str:
                return content if url == candidate["url"] else ""

            def get_by_hash(self, hash_value: str) -> str:
                return content if hash_value == chash else ""

        with patch("recommender.retrieve_skills", fake_retrieve), patch(
            "recommender.LibraryContent", FakeLibrary
        ):
            response = self.client.post(
                "/route",
                json={"task": "create an excel report with formulas"},
                headers=self._auth_headers(),
            )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["tier"], "full")
        self.assertEqual(body["context_guard"]["delivery"], "capsule")
        self.assertTrue(body["context_guard"]["complete"])
        # No allowlist gate downgrades a verified, safety-stripped match --
        # the whole skill ships, not a bounded excerpt.
        self.assertEqual(body["context_guard"]["capsule"], content)

    def test_measurement_mode_holdout_withholds_an_otherwise_full_route(self) -> None:
        content = _large_static_skill()
        chash = content_hash(content)
        candidate = {
            "id": "skill-large",
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://example.com/large-spreadsheet",
            "risk_score": 0,
            "quality_status": "active",
            "quality_score": 90,
            "content_hash": chash,
            "rank": 1.0,
            "similarity": 0.95,
        }

        async def fake_retrieve(client, query, limit):
            del client, query, limit
            return [candidate]

        class FakeLibrary:
            def get(self, url: str) -> str:
                return content if url == candidate["url"] else ""

            def get_by_hash(self, hash_value: str) -> str:
                return content if hash_value == chash else ""

        headers = self._auth_headers()
        user = local_store.get_or_create_user("delivery-honesty@example.com", "Delivery Honesty", None)
        local_store.set_measurement_mode(user["id"], True, holdout_rate=0.5)

        # holdout_rate is clamped to <=0.5 (set_measurement_mode's own safety
        # rail), so it alone can never guarantee a specific arm here. Pin the
        # arm directly to test what this test is actually about: how /route
        # behaves given an assignment, not the hash bucketing math itself.
        with patch("recommender.retrieve_skills", fake_retrieve), patch(
            "recommender.LibraryContent", FakeLibrary
        ), patch(
            "recommender._measurement_mode_assignment",
            return_value={"arm": "holdout", "stratum": "coding:test:short"},
        ):
            held_out = self.client.post(
                "/route",
                json={"task": "create an excel report with formulas"},
                headers=headers,
            )
        self.assertEqual(held_out.status_code, 200)
        held_out_body = held_out.json()
        self.assertEqual(held_out_body["tier"], "hint")
        self.assertIsNone(held_out_body["content"])
        self.assertEqual(held_out_body["context_guard"]["reason"], "measurement_holdout")
        self.assertEqual(held_out_body["measurement"]["arm"], "holdout")
        self.assertTrue(any("held out" in w for w in held_out_body["score_debug"].get("warnings", [])))

        with patch("recommender.retrieve_skills", fake_retrieve), patch(
            "recommender.LibraryContent", FakeLibrary
        ), patch(
            "recommender._measurement_mode_assignment",
            return_value={"arm": "routed", "stratum": "coding:test:short"},
        ):
            routed = self.client.post(
                "/route",
                json={"task": "create an excel report with formulas"},
                headers=headers,
            )
        self.assertEqual(routed.status_code, 200)
        routed_body = routed.json()
        self.assertEqual(routed_body["tier"], "full")
        self.assertEqual(routed_body["measurement"]["arm"], "routed")


if __name__ == "__main__":
    unittest.main()
