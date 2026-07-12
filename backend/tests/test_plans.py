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

    def test_pro_plan_is_not_metered_by_the_free_quota(self) -> None:
        token = self._login("a@example.com")
        local_store.set_user_plan("a@example.com", "pro")
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])),
            patch.object(local_store, "FREE_ROUTES_PER_MONTH", 1),
        ):
            for _ in range(3):
                payload = self._route(token)
                self.assertNotEqual(payload["score_debug"]["reason"], "quota-exceeded")

    def test_pro_plan_hits_the_internal_fair_use_cap(self) -> None:
        token = self._login("a@example.com")
        local_store.set_user_plan("a@example.com", "pro")
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])),
            patch.object(local_store, "PRO_ROUTES_PER_MONTH", 2),
        ):
            self._route(token)
            self._route(token)
            over = self._route(token)
            self.assertEqual(over["score_debug"]["reason"], "quota-exceeded")
            self.assertEqual(over["quota"]["plan"], "pro")
            self.assertEqual(over["quota"]["limit"], 2)

    def test_team_plan_pools_the_fair_use_cap_across_the_workspace(self) -> None:
        owner_token = self._login("owner@example.com")
        member_token = self._login("member@example.com")
        local_store.set_user_plan("owner@example.com", "team")
        local_store.set_user_plan("member@example.com", "team")
        owner = local_store.get_or_create_user("owner@example.com", "owner", None)
        member = local_store.get_or_create_user("member@example.com", "member", None)
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_org_member(org["id"], member["id"])
        local_store.set_org_seat_limit(org["id"], 2)
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])),
            patch.object(local_store, "PRO_ROUTES_PER_MONTH", 1),
        ):
            # Pool = 2 seats x 1 route. One route each drains the shared pool.
            self._route(owner_token)
            self._route(member_token)
            over = self._route(owner_token)
            self.assertEqual(over["score_debug"]["reason"], "quota-exceeded")
            self.assertEqual(over["quota"]["plan"], "team")
            self.assertEqual(over["quota"]["limit"], 2)
            self.assertEqual(over["quota"]["used"], 2)

    def test_team_plan_without_an_org_falls_back_to_the_per_user_cap(self) -> None:
        token = self._login("a@example.com")
        local_store.set_user_plan("a@example.com", "team")
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])),
            patch.object(local_store, "PRO_ROUTES_PER_MONTH", 1),
        ):
            self._route(token)
            over = self._route(token)
            self.assertEqual(over["score_debug"]["reason"], "quota-exceeded")

    def _post_private_skill(self, token: str, name: str) -> int:
        r = self.client.post(
            "/private-skills",
            json={"name": name, "content": "# skill"},
            headers={"Authorization": f"Bearer {token}"},
        )
        return r.status_code

    def test_free_plan_private_skill_cap(self) -> None:
        token = self._login("a@example.com")
        with patch.object(local_store, "FREE_PRIVATE_SKILLS", 2):
            self.assertEqual(self._post_private_skill(token, "one"), 200)
            self.assertEqual(self._post_private_skill(token, "two"), 200)
            self.assertEqual(self._post_private_skill(token, "three"), 402)
            local_store.set_user_plan("a@example.com", "pro")
            self.assertEqual(self._post_private_skill(token, "three"), 200)

    def test_org_seat_limit_blocks_extra_members_until_admin_adds_seats(self) -> None:
        owner_token = self._login("owner@example.com")
        owner = local_store.get_or_create_user("owner@example.com", "owner", None)
        local_store.get_or_create_user("b@example.com", "b", None)
        local_store.get_or_create_user("c@example.com", "c", None)
        org = local_store.create_org("Acme", owner["id"])

        def _add(email: str) -> int:
            r = self.client.post(
                f"/orgs/{org['id']}/members",
                json={"email": email},
                headers={"Authorization": f"Bearer {owner_token}"},
            )
            return r.status_code

        with patch.object(local_store, "TEAM_INCLUDED_MEMBERS", 2):
            self.assertEqual(_add("b@example.com"), 200)  # owner + b fill both seats
            self.assertEqual(_add("b@example.com"), 200)  # re-adding a member is not a new seat
            self.assertEqual(_add("c@example.com"), 402)
            with patch.dict("os.environ", {"ADMIN_EMAILS": "owner@example.com"}):
                r = self.client.post(
                    "/admin/set-org-seats",
                    json={"org_id": org["id"], "seats": 3},
                    headers={"Authorization": f"Bearer {owner_token}"},
                )
                self.assertEqual(r.status_code, 200)
            self.assertEqual(_add("c@example.com"), 200)

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
