from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import auth
import local_store
import scraper


class PlanStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        local_store.DB_PATH = Path(self.tmp.name) / "local_skills.db"
        local_store.init_db()

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path

    def test_new_users_default_to_free_plan(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "A", None)
        self.assertEqual(user.get("plan"), "free")

    def test_set_user_plan(self) -> None:
        local_store.get_or_create_user("a@example.com", "A", None)
        self.assertTrue(local_store.set_user_plan("a@example.com", "pro"))
        user = local_store.get_or_create_user("a@example.com", "A", None)
        self.assertEqual(user["plan"], "pro")
        self.assertFalse(local_store.set_user_plan("nobody@example.com", "pro"))
        with self.assertRaises(ValueError):
            local_store.set_user_plan("a@example.com", "platinum")

    def test_route_usage_counts_within_the_current_month(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "A", None)
        self.assertEqual(local_store.get_route_usage(user["id"]), 0)
        self.assertEqual(local_store.increment_route_usage(user["id"]), 1)
        self.assertEqual(local_store.increment_route_usage(user["id"]), 2)
        self.assertEqual(local_store.get_route_usage(user["id"]), 2)

    def test_route_usage_resets_on_month_rollover(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "A", None)
        with patch.object(local_store, "_usage_month", return_value="2026-06"):
            local_store.increment_route_usage(user["id"])
            self.assertEqual(local_store.get_route_usage(user["id"]), 1)
        # A new calendar month starts a fresh counter; the old row stays put.
        self.assertEqual(local_store.get_route_usage(user["id"]), 0)

    def test_usage_is_isolated_per_user(self) -> None:
        user_a = local_store.get_or_create_user("a@example.com", "A", None)
        user_b = local_store.get_or_create_user("b@example.com", "B", None)
        local_store.increment_route_usage(user_a["id"])
        self.assertEqual(local_store.get_route_usage(user_b["id"]), 0)


class PlanEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        self.db_path = Path(self.tmp.name) / "local_skills.db"
        local_store.DB_PATH = self.db_path
        scraper.store.DB_PATH = self.db_path
        local_store.init_db()
        local_store.invalidate_vector_cache()
        auth._states.clear()
        self.client = TestClient(scraper.app)

    def tearDown(self) -> None:
        local_store.invalidate_vector_cache()
        local_store.DB_PATH = self.old_db_path
        scraper.store.DB_PATH = self.old_db_path

    def _login(self, email: str) -> str:
        user = local_store.get_or_create_user(email, email.split("@")[0], None)
        return auth.issue_cli_token(user["id"])

    def test_whoami_reports_plan_and_usage(self) -> None:
        token = self._login("a@example.com")
        body = self.client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(body["plan"], "free")
        self.assertEqual(body["routes_used_this_month"], 0)
        self.assertEqual(body["routes_limit"], local_store.FREE_ROUTES_PER_MONTH)

        local_store.set_user_plan("a@example.com", "pro")
        body = self.client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"}).json()
        self.assertEqual(body["plan"], "pro")
        self.assertIsNone(body["routes_limit"])

    def test_admin_set_plan_requires_admin(self) -> None:
        token = self._login("a@example.com")
        payload = {"email": "a@example.com", "plan": "pro"}
        r = self.client.post(
            "/admin/set-plan", json=payload, headers={"Authorization": f"Bearer {token}"}
        )
        self.assertEqual(r.status_code, 403)

        with patch.dict("os.environ", {"ADMIN_EMAILS": "a@example.com"}):
            r = self.client.post(
                "/admin/set-plan", json=payload, headers={"Authorization": f"Bearer {token}"}
            )
            self.assertEqual(r.status_code, 200)
            bad_plan = self.client.post(
                "/admin/set-plan",
                json={"email": "a@example.com", "plan": "platinum"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(bad_plan.status_code, 400)
            missing = self.client.post(
                "/admin/set-plan",
                json={"email": "nobody@example.com", "plan": "pro"},
                headers={"Authorization": f"Bearer {token}"},
            )
            self.assertEqual(missing.status_code, 404)

    def _route(self, token: str) -> dict:
        r = self.client.post(
            "/route",
            json={"task": "create an excel spreadsheet report"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200)
        return r.json()

    def test_free_plan_route_quota_degrades_gracefully(self) -> None:
        token = self._login("a@example.com")
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])),
            patch.object(local_store, "FREE_ROUTES_PER_MONTH", 2),
        ):
            self.assertEqual(self._route(token)["tier"], "none")
            self._route(token)
            over = self._route(token)
            # Over quota is still a 200 with a routable "none" payload so
            # clients degrade instead of erroring mid-conversation.
            self.assertEqual(over["tier"], "none")
            self.assertEqual(over["score_debug"]["reason"], "quota-exceeded")
            self.assertEqual(over["quota"]["limit"], 2)
            self.assertEqual(over["quota"]["used"], 2)
            self.assertIn("upgrade_url", over["quota"])

    def test_pro_plan_is_not_metered(self) -> None:
        token = self._login("a@example.com")
        local_store.set_user_plan("a@example.com", "pro")
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])),
            patch.object(local_store, "FREE_ROUTES_PER_MONTH", 1),
        ):
            for _ in range(3):
                payload = self._route(token)
                self.assertNotEqual(payload["score_debug"]["reason"], "quota-exceeded")

    def test_non_task_prompts_do_not_burn_quota(self) -> None:
        token = self._login("a@example.com")
        user = local_store.get_or_create_user("a@example.com", "a", None)
        r = self.client.post(
            "/route",
            json={"task": "thanks"},
            headers={"Authorization": f"Bearer {token}"},
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(local_store.get_route_usage(user["id"]), 0)


if __name__ == "__main__":
    unittest.main()
