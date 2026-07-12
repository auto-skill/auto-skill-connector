from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import auth
import local_store
import scraper


class OrgStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        local_store.DB_PATH = Path(self.tmp.name) / "local_skills.db"
        local_store.init_db()

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path

    def _user(self, email: str) -> dict:
        return local_store.get_or_create_user(email, email.split("@")[0], None)

    def test_create_org_makes_creator_the_owner(self) -> None:
        owner = self._user("owner@example.com")
        org = local_store.create_org("Acme", owner["id"])
        self.assertEqual(local_store.org_role(org["id"], owner["id"]), "owner")
        self.assertEqual(local_store.list_orgs_for_user(owner["id"])[0]["role"], "owner")

    def test_membership_add_remove_and_owner_is_not_removable(self) -> None:
        owner = self._user("owner@example.com")
        member = self._user("member@example.com")
        org = local_store.create_org("Acme", owner["id"])

        self.assertTrue(local_store.add_org_member(org["id"], member["id"]))
        self.assertEqual(local_store.org_role(org["id"], member["id"]), "member")
        self.assertFalse(local_store.add_org_member(org["id"], member["id"]))  # idempotent

        self.assertTrue(local_store.remove_org_member(org["id"], member["id"]))
        self.assertIsNone(local_store.org_role(org["id"], member["id"]))
        self.assertFalse(local_store.remove_org_member(org["id"], owner["id"]))  # owner row stays

    def test_unknown_role_is_rejected(self) -> None:
        owner = self._user("owner@example.com")
        org = local_store.create_org("Acme", owner["id"])
        with self.assertRaises(ValueError):
            local_store.add_org_member(org["id"], owner["id"], role="superadmin")

    def test_org_skills_are_separate_from_personal_skills(self) -> None:
        owner = self._user("owner@example.com")
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_private_skill(owner["id"], "personal-notes", None, "PERSONAL")
        local_store.add_org_skill(org["id"], owner["id"], "acme-standards", None, "ORG")

        personal = local_store.list_private_skills(owner["id"])
        self.assertEqual([s["name"] for s in personal], ["personal-notes"])
        org_skills = local_store.list_org_skills(org["id"])
        self.assertEqual([s["name"] for s in org_skills], ["acme-standards"])

    def test_routable_skills_put_org_skills_first(self) -> None:
        owner = self._user("owner@example.com")
        member = self._user("member@example.com")
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_org_member(org["id"], member["id"])
        local_store.add_private_skill(member["id"], "my-notes", None, "PERSONAL")
        local_store.add_org_skill(org["id"], owner["id"], "acme-standards", None, "ORG")

        routable = local_store.list_routable_private_skills(member["id"])
        self.assertEqual([s["name"] for s in routable], ["acme-standards", "my-notes"])

        outsider = self._user("outsider@example.com")
        self.assertEqual(local_store.list_routable_private_skills(outsider["id"]), [])

    def test_personal_delete_cannot_remove_org_skills(self) -> None:
        owner = self._user("owner@example.com")
        org = local_store.create_org("Acme", owner["id"])
        skill = local_store.add_org_skill(org["id"], owner["id"], "acme-standards", None, "ORG")
        self.assertFalse(local_store.remove_private_skill(owner["id"], skill["id"]))
        self.assertTrue(local_store.remove_org_skill(org["id"], skill["id"]))


class OrgEndpointTests(unittest.TestCase):
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

    def _login(self, email: str, plan: str = "free") -> tuple[dict, dict]:
        user = local_store.get_or_create_user(email, email.split("@")[0], None)
        if plan != "free":
            local_store.set_user_plan(email, plan)
        token = auth.issue_cli_token(user["id"])
        return user, {"Authorization": f"Bearer {token}"}

    def _org(self, headers: dict, name: str = "Acme") -> dict:
        r = self.client.post("/orgs", json={"name": name}, headers=headers)
        self.assertEqual(r.status_code, 200)
        return r.json()["org"]

    def test_create_org_requires_team_plan(self) -> None:
        _, headers = self._login("free@example.com")
        r = self.client.post("/orgs", json={"name": "Acme"}, headers=headers)
        self.assertEqual(r.status_code, 402)

        _, team_headers = self._login("teamlead@example.com", plan="team")
        org = self._org(team_headers)
        self.assertEqual(org["name"], "Acme")

    def test_member_management_is_owner_only_and_members_can_leave(self) -> None:
        _, owner_headers = self._login("owner@example.com", plan="team")
        member, member_headers = self._login("member@example.com")
        org = self._org(owner_headers)

        add = self.client.post(
            f"/orgs/{org['id']}/members", json={"email": "member@example.com"}, headers=owner_headers
        )
        self.assertEqual(add.status_code, 200)

        # A plain member cannot add others...
        deny = self.client.post(
            f"/orgs/{org['id']}/members", json={"email": "owner@example.com"}, headers=member_headers
        )
        self.assertEqual(deny.status_code, 403)
        # ...but can leave.
        leave = self.client.delete(f"/orgs/{org['id']}/members/{member['id']}", headers=member_headers)
        self.assertEqual(leave.status_code, 200)
        self.assertIsNone(local_store.org_role(org["id"], member["id"]))

    def test_org_is_invisible_to_non_members(self) -> None:
        _, owner_headers = self._login("owner@example.com", plan="team")
        _, outsider_headers = self._login("outsider@example.com")
        org = self._org(owner_headers)

        for path in (f"/orgs/{org['id']}/members", f"/orgs/{org['id']}/skills"):
            self.assertEqual(self.client.get(path, headers=outsider_headers).status_code, 404)

    def test_adding_unknown_email_is_a_404(self) -> None:
        _, owner_headers = self._login("owner@example.com", plan="team")
        org = self._org(owner_headers)
        r = self.client.post(
            f"/orgs/{org['id']}/members", json={"email": "ghost@example.com"}, headers=owner_headers
        )
        self.assertEqual(r.status_code, 404)

    def test_org_skill_crud_and_member_visibility(self) -> None:
        _, owner_headers = self._login("owner@example.com", plan="team")
        _, member_headers = self._login("member@example.com")
        org = self._org(owner_headers)
        self.client.post(f"/orgs/{org['id']}/members", json={"email": "member@example.com"}, headers=owner_headers)

        create = self.client.post(
            f"/orgs/{org['id']}/skills",
            json={"name": "acme-standards", "description": "House rules.", "content": "Follow the rules."},
            headers=owner_headers,
        )
        self.assertEqual(create.status_code, 200)
        skill_id = create.json()["org_skill"]["id"]

        # Members read; a member's own submission lands as pending, not live.
        listing = self.client.get(f"/orgs/{org['id']}/skills", headers=member_headers)
        self.assertEqual([s["name"] for s in listing.json()["org_skills"]], ["acme-standards"])
        submit = self.client.post(
            f"/orgs/{org['id']}/skills",
            json={"name": "rogue", "content": "x" * 10},
            headers=member_headers,
        )
        self.assertEqual(submit.status_code, 200)
        self.assertEqual(submit.json()["org_skill"]["status"], "pending")

        delete = self.client.delete(f"/orgs/{org['id']}/skills/{skill_id}", headers=owner_headers)
        self.assertEqual(delete.status_code, 200)

    def test_route_prefers_org_skill_for_members(self) -> None:
        _, owner_headers = self._login("owner@example.com", plan="team")
        member, member_headers = self._login("member@example.com")
        org = self._org(owner_headers)
        self.client.post(f"/orgs/{org['id']}/members", json={"email": "member@example.com"}, headers=owner_headers)
        self.client.post(
            f"/orgs/{org['id']}/skills",
            json={"name": "acme review standards", "content": "Enforce the Acme review checklist rules."},
            headers=owner_headers,
        )

        with patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])):
            r = self.client.post(
                "/route",
                json={"task": "apply the acme review standards to this pull request"},
                headers=member_headers,
            )
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["tier"], "hint")
        self.assertEqual(body["skill"]["name"], "acme review standards")

        # An outsider with the same query never sees the org skill.
        _, outsider_headers = self._login("outsider@example.com")
        with patch("recommender.retrieve_skills", new=AsyncMock(return_value=[])):
            r = self.client.post(
                "/route",
                json={"task": "apply the acme review standards to this pull request"},
                headers=outsider_headers,
            )
        self.assertEqual(r.json()["tier"], "none")


if __name__ == "__main__":
    unittest.main()
