from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import auth
import local_store
import recommender
import scraper


def _seed_skill(skill_id: str, content_hash: str = "a" * 64) -> None:
    local_store.upsert_rows(
        "skills",
        [
            {
                "id": skill_id,
                "name": f"Demo {skill_id}",
                "description": "d",
                "source": "github",
                "url": f"http://{skill_id}",
                "content_hash": content_hash,
            }
        ],
        "url",
    )


class PlanFeatureStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        local_store.DB_PATH = Path(self.tmp.name) / "local_skills.db"
        local_store.init_db()

    def tearDown(self) -> None:
        local_store.invalidate_vector_cache()
        local_store.DB_PATH = self.old_db_path

    def _user(self, email: str) -> dict:
        return local_store.get_or_create_user(email, email.split("@")[0], None)

    def test_upserts_record_version_history(self) -> None:
        _seed_skill("s1", "a" * 64)
        _seed_skill("s1", "b" * 64)  # same url -> update in place
        hashes = {v["content_hash"] for v in local_store.list_skill_versions("s1")}
        self.assertEqual(hashes, {"a" * 64, "b" * 64})
        self.assertTrue(local_store.skill_version_exists("s1", "a" * 64))
        self.assertFalse(local_store.skill_version_exists("s1", "c" * 64))

    def test_pin_and_unpin(self) -> None:
        user = self._user("a@example.com")
        _seed_skill("s1")
        pin = local_store.pin_skill(user["id"], "s1", "a" * 64)
        self.assertEqual(pin["content_hash"], "a" * 64)
        self.assertEqual(local_store.get_pin(user["id"], "s1")["content_hash"], "a" * 64)
        # Re-pinning moves the pin (rollback / roll forward).
        local_store.pin_skill(user["id"], "s1", "b" * 64)
        self.assertEqual(local_store.get_pin(user["id"], "s1")["content_hash"], "b" * 64)
        self.assertTrue(local_store.unpin_skill(user["id"], "s1"))
        self.assertIsNone(local_store.get_pin(user["id"], "s1"))

    def test_watch_alert_and_ack_lifecycle(self) -> None:
        user = self._user("a@example.com")
        _seed_skill("s1", "a" * 64)
        local_store.watch_skill(user["id"], "s1")
        self.assertEqual(local_store.list_skill_alerts(user["id"]), [])

        _seed_skill("s1", "b" * 64)  # content changed upstream
        alerts = local_store.list_skill_alerts(user["id"])
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["current_hash"], "b" * 64)
        self.assertEqual(alerts[0]["last_seen_hash"], "a" * 64)

        self.assertTrue(local_store.ack_skill_alert(user["id"], "s1"))
        self.assertEqual(local_store.list_skill_alerts(user["id"]), [])

    def test_collections_personal_and_org(self) -> None:
        owner = self._user("owner@example.com")
        member = self._user("member@example.com")
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_org_member(org["id"], member["id"])
        _seed_skill("s1")

        personal = local_store.create_collection(owner["id"], "mine")
        shared = local_store.create_collection(owner["id"], "team-picks", org["id"])
        local_store.add_collection_skill(shared["id"], "s1")

        self.assertEqual(
            {c["name"] for c in local_store.list_collections(owner["id"])}, {"mine", "team-picks"}
        )
        # A member sees the shared collection but not the owner's personal one.
        self.assertEqual(
            {c["name"] for c in local_store.list_collections(member["id"])}, {"team-picks"}
        )
        self.assertEqual(local_store.list_collection_skills(shared["id"])[0]["skill_id"], "s1")
        self.assertTrue(local_store.delete_collection(personal["id"]))

    def test_routing_filters_combine_preferences_and_org_policies(self) -> None:
        owner = self._user("owner@example.com")
        member = self._user("member@example.com")
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_org_member(org["id"], member["id"])

        local_store.set_routing_preferences(member["id"], ["excluded-1"], ["gitlab"])
        local_store.set_org_skill_policy(org["id"], "blocked-1", "block")
        filters = local_store.routing_filters_for_user(member["id"])
        self.assertEqual(filters["excluded_ids"], {"excluded-1"})
        self.assertEqual(filters["excluded_sources"], {"gitlab"})
        self.assertEqual(filters["blocked_ids"], {"blocked-1"})
        self.assertIsNone(filters["allowed_ids"])

        local_store.set_org_skill_policy(org["id"], "standard-1", "allow")
        filters = local_store.routing_filters_for_user(member["id"])
        self.assertEqual(filters["allowed_ids"], {"standard-1"})

        with self.assertRaises(ValueError):
            local_store.set_org_skill_policy(org["id"], "x", "maybe")

    def test_passes_routing_filters(self) -> None:
        filters = {
            "excluded_ids": {"e1"},
            "excluded_sources": {"gitlab"},
            "blocked_ids": {"b1"},
            "allowed_ids": None,
        }
        self.assertFalse(recommender._passes_routing_filters({"id": "e1", "source": "github"}, filters))
        self.assertFalse(recommender._passes_routing_filters({"id": "b1", "source": "github"}, filters))
        self.assertFalse(recommender._passes_routing_filters({"id": "x", "source": "gitlab"}, filters))
        self.assertTrue(recommender._passes_routing_filters({"id": "x", "source": "github"}, filters))
        filters["allowed_ids"] = {"a1"}
        self.assertFalse(recommender._passes_routing_filters({"id": "x", "source": "github"}, filters))
        self.assertTrue(recommender._passes_routing_filters({"id": "a1", "source": "github"}, filters))

    def test_member_org_skill_submissions_route_only_after_approval(self) -> None:
        owner = self._user("owner@example.com")
        member = self._user("member@example.com")
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_org_member(org["id"], member["id"])

        skill = local_store.add_org_skill(org["id"], member["id"], "pending-skill", None, "X", status="pending")
        self.assertEqual(local_store.list_routable_private_skills(member["id"]), [])
        self.assertTrue(local_store.approve_org_skill(org["id"], skill["id"]))
        self.assertFalse(local_store.approve_org_skill(org["id"], skill["id"]))  # already approved
        routable = local_store.list_routable_private_skills(member["id"])
        self.assertEqual([s["name"] for s in routable], ["pending-skill"])

    def test_org_audit_log_records_and_lists(self) -> None:
        owner = self._user("owner@example.com")
        org = local_store.create_org("Acme", owner["id"])
        local_store.record_org_audit(org["id"], owner["id"], "org_skill_added", "deploy-standards")
        local_store.record_install_audit(owner["id"], "s1")
        entries = local_store.list_org_audit(org["id"])
        self.assertEqual([e["action"] for e in entries], ["skill_installed", "org_skill_added"])
        self.assertEqual(entries[0]["actor_email"], "owner@example.com")
        with self.assertRaises(ValueError):
            local_store.record_org_audit(org["id"], owner["id"], "made-up-action")


class PlanFeatureEndpointTests(unittest.TestCase):
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

    def _login(self, email: str, plan: str = "free") -> str:
        user = local_store.get_or_create_user(email, email.split("@")[0], None)
        if plan != "free":
            local_store.set_user_plan(email, plan)
        return auth.issue_cli_token(user["id"])

    def _auth(self, token: str) -> dict:
        return {"Authorization": f"Bearer {token}"}

    def test_pins_are_pro_gated_and_validate_versions(self) -> None:
        free = self._login("free@example.com")
        pro = self._login("pro@example.com", "pro")
        _seed_skill("s1", "a" * 64)

        body = {"skill_id": "s1", "content_hash": "a" * 64}
        self.assertEqual(self.client.post("/pins", json=body, headers=self._auth(free)).status_code, 402)
        self.assertEqual(self.client.post("/pins", json=body, headers=self._auth(pro)).status_code, 200)
        missing = {"skill_id": "s1", "content_hash": "f" * 64}
        self.assertEqual(self.client.post("/pins", json=missing, headers=self._auth(pro)).status_code, 404)

        pins = self.client.get("/pins", headers=self._auth(pro)).json()["pins"]
        self.assertEqual(pins[0]["skill_id"], "s1")
        self.assertEqual(self.client.delete("/pins/s1", headers=self._auth(pro)).status_code, 200)
        self.assertEqual(self.client.delete("/pins/s1", headers=self._auth(pro)).status_code, 404)

    def test_skill_versions_endpoint(self) -> None:
        token = self._login("a@example.com")
        _seed_skill("s1", "a" * 64)
        _seed_skill("s1", "b" * 64)
        body = self.client.get("/skills/s1/versions", headers=self._auth(token)).json()
        self.assertEqual(body["current_hash"], "b" * 64)
        self.assertEqual({v["content_hash"] for v in body["versions"]}, {"a" * 64, "b" * 64})

    def test_watches_and_alerts_endpoints(self) -> None:
        free = self._login("free@example.com")
        pro = self._login("pro@example.com", "pro")
        _seed_skill("s1", "a" * 64)

        self.assertEqual(
            self.client.post("/watches", json={"skill_id": "s1"}, headers=self._auth(free)).status_code, 402
        )
        self.assertEqual(
            self.client.post("/watches", json={"skill_id": "nope"}, headers=self._auth(pro)).status_code, 404
        )
        self.assertEqual(
            self.client.post("/watches", json={"skill_id": "s1"}, headers=self._auth(pro)).status_code, 200
        )
        self.assertEqual(self.client.get("/alerts", headers=self._auth(pro)).json()["alerts"], [])

        _seed_skill("s1", "b" * 64)
        alerts = self.client.get("/alerts", headers=self._auth(pro)).json()["alerts"]
        self.assertEqual(len(alerts), 1)
        self.assertEqual(self.client.post("/alerts/s1/ack", headers=self._auth(pro)).status_code, 200)
        self.assertEqual(self.client.get("/alerts", headers=self._auth(pro)).json()["alerts"], [])
        self.assertEqual(self.client.delete("/watches/s1", headers=self._auth(pro)).status_code, 200)

    def test_collections_endpoints_and_org_sharing(self) -> None:
        free = self._login("free@example.com")
        owner_token = self._login("owner@example.com", "team")
        member_token = self._login("member@example.com", "team")
        owner = local_store.get_or_create_user("owner@example.com", "owner", None)
        member = local_store.get_or_create_user("member@example.com", "member", None)
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_org_member(org["id"], member["id"])
        _seed_skill("s1")

        self.assertEqual(
            self.client.post("/collections", json={"name": "x"}, headers=self._auth(free)).status_code, 402
        )
        shared = self.client.post(
            "/collections", json={"name": "team-picks", "org_id": org["id"]}, headers=self._auth(owner_token)
        ).json()["collection"]
        # Members may add skills to a shared collection but not delete it.
        r = self.client.post(
            f"/collections/{shared['id']}/skills", json={"skill_id": "s1"}, headers=self._auth(member_token)
        )
        self.assertEqual(r.status_code, 200)
        self.assertEqual(
            self.client.delete(f"/collections/{shared['id']}", headers=self._auth(member_token)).status_code,
            403,
        )
        self.assertEqual(
            self.client.delete(f"/collections/{shared['id']}", headers=self._auth(owner_token)).status_code,
            200,
        )

    def test_preferences_endpoints(self) -> None:
        free = self._login("free@example.com")
        pro = self._login("pro@example.com", "pro")
        prefs = {"excluded_skill_ids": ["s1"], "excluded_sources": ["gitlab"]}
        self.assertEqual(self.client.put("/preferences", json=prefs, headers=self._auth(free)).status_code, 402)
        r = self.client.put("/preferences", json=prefs, headers=self._auth(pro))
        self.assertEqual(r.status_code, 200)
        body = self.client.get("/preferences", headers=self._auth(pro)).json()["preferences"]
        self.assertEqual(body["excluded_skill_ids"], ["s1"])
        self.assertEqual(body["excluded_sources"], ["gitlab"])

    def test_impact_report_is_free_and_summarizes_context_efficiency(self) -> None:
        free = self._login("free@example.com")
        user = local_store.get_or_create_user("free@example.com", "free", None)
        # A full-tier delivery: 1000 raw tokens distilled to a 100-token capsule.
        local_store.insert_route_event(
            {
                "client": "cli",
                "user_id": user["id"],
                "tier": "full",
                "task_family": "coding",
                "skill_id": "s1",
                "skill_name": "Demo s1",
                "content_tokens": 1000,
                "capsule_tokens": 100,
                "injected_tokens": 120,
                "result_count": 5,
            }
        )
        # A declined route: no confident match, nothing injected.
        local_store.insert_route_event(
            {"client": "cli", "user_id": user["id"], "tier": "none", "task_family": "research"}
        )

        body = self.client.get("/impact-report", headers=self._auth(free)).json()["impact_report"]
        self.assertEqual(body["activity"]["substantial_tasks_routed"], 2)
        self.assertEqual(body["activity"]["tiers"], {"full": 1, "none": 1})
        self.assertEqual(body["activity"]["declined_uncertain_count"], 1)
        self.assertEqual(body["activity"]["specialists_discovered_without_install"], 1)
        self.assertEqual(body["context_efficiency"]["delivered_capsule_tokens"], 100)
        self.assertEqual(body["context_efficiency"]["eligible_raw_tokens"], 1000)
        self.assertEqual(body["context_efficiency"]["compression_ratio"], 0.9)
        self.assertFalse(body["measured_lift"]["available"])

    def test_route_survey_response_resets_cadence_and_rejects_bad_values(self) -> None:
        free = self._login("free@example.com")
        user = local_store.get_or_create_user("free@example.com", "free", None)
        bad = self.client.post(
            "/route-survey-response", json={"response": "love it"}, headers=self._auth(free)
        )
        self.assertEqual(bad.status_code, 400)

        ok = self.client.post(
            "/route-survey-response", json={"response": "not_useful"}, headers=self._auth(free)
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(ok.json(), {"ok": True, "response": "not_useful"})

        conn = local_store.get_conn()
        try:
            state = conn.execute(
                "SELECT routes_since_response, last_response FROM route_survey_state WHERE user_id=?",
                (user["id"],),
            ).fetchone()
        finally:
            conn.close()
        self.assertEqual(state["routes_since_response"], 0)
        self.assertEqual(state["last_response"], "not_useful")

    def test_route_response_includes_feedback_prompt_flag(self) -> None:
        pro = self._login("pro@example.com", "pro")
        candidate = {"id": "s1", "name": "Demo", "description": "d", "source": "github", "url": "http://s1"}
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[candidate])),
            patch("recommender.rerank_candidates", side_effect=lambda q, r: r),
        ):
            r = self.client.post(
                "/route",
                json={"task": "create an excel spreadsheet report"},
                headers=self._auth(pro),
            )
        self.assertEqual(r.status_code, 200)
        self.assertIn("feedback_prompt", r.json())
        self.assertFalse(r.json()["feedback_prompt"])

    def test_measurement_mode_settings_and_outcome_metrics_endpoints(self) -> None:
        free = self._login("free@example.com")
        user = local_store.get_or_create_user("free@example.com", "free", None)

        default = self.client.get("/measurement-mode", headers=self._auth(free)).json()["measurement_mode"]
        self.assertFalse(default["enabled"])

        updated = self.client.put(
            "/measurement-mode", json={"enabled": True, "holdout_rate": 0.2}, headers=self._auth(free)
        ).json()["measurement_mode"]
        self.assertTrue(updated["enabled"])
        self.assertEqual(updated["holdout_rate"], 0.2)

        local_store.insert_route_event({"id": "r1", "client": "cli", "user_id": user["id"], "tier": "full"})
        ok = self.client.post(
            "/route-outcome-metrics", json={"route_id": "r1", "turns": 4}, headers=self._auth(free)
        )
        self.assertEqual(ok.status_code, 200)
        missing = self.client.post(
            "/route-outcome-metrics", json={"route_id": "no-such-route", "turns": 4}, headers=self._auth(free)
        )
        self.assertEqual(missing.status_code, 404)

        disabled = self.client.put(
            "/measurement-mode", json={"enabled": False}, headers=self._auth(free)
        ).json()["measurement_mode"]
        self.assertFalse(disabled["enabled"])

    def test_route_applies_exclusions_and_org_blocks(self) -> None:
        pro = self._login("pro@example.com", "pro")
        user = local_store.get_or_create_user("pro@example.com", "pro", None)
        local_store.set_routing_preferences(user["id"], ["s1"], [])
        candidate = {"id": "s1", "name": "Demo", "description": "d", "source": "github", "url": "http://s1"}
        with (
            patch("recommender.retrieve_skills", new=AsyncMock(return_value=[candidate])),
            patch("recommender.rerank_candidates", side_effect=lambda q, r: r),
        ):
            r = self.client.post(
                "/route",
                json={"task": "create an excel spreadsheet report"},
                headers=self._auth(pro),
            )
            self.assertEqual(r.status_code, 200)
            payload = r.json()
            self.assertEqual(payload["tier"], "none")
            self.assertIsNone(payload["skill"])

    def test_analytics_is_pro_gated(self) -> None:
        free = self._login("free@example.com")
        pro = self._login("pro@example.com", "pro")
        self.assertEqual(self.client.get("/analytics", headers=self._auth(free)).status_code, 402)
        body = self.client.get("/analytics", headers=self._auth(pro)).json()["analytics"]
        self.assertEqual(body["total_routes"], 0)
        self.assertIn("routes_this_month", body)

    def _org_with_owner_and_member(self) -> tuple[dict, str, str]:
        owner_token = self._login("owner@example.com", "team")
        member_token = self._login("member@example.com", "team")
        owner = local_store.get_or_create_user("owner@example.com", "owner", None)
        member = local_store.get_or_create_user("member@example.com", "member", None)
        org = local_store.create_org("Acme", owner["id"])
        local_store.add_org_member(org["id"], member["id"])
        return org, owner_token, member_token

    def test_member_submissions_need_owner_approval(self) -> None:
        org, owner_token, member_token = self._org_with_owner_and_member()
        r = self.client.post(
            f"/orgs/{org['id']}/skills",
            json={"name": "member-skill", "content": "# skill"},
            headers=self._auth(member_token),
        )
        self.assertEqual(r.status_code, 200)
        skill = r.json()["org_skill"]
        self.assertEqual(skill["status"], "pending")

        member = local_store.get_or_create_user("member@example.com", "member", None)
        self.assertEqual(local_store.list_routable_private_skills(member["id"]), [])

        # Only the owner can approve.
        self.assertEqual(
            self.client.post(
                f"/orgs/{org['id']}/skills/{skill['id']}/approve", headers=self._auth(member_token)
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/orgs/{org['id']}/skills/{skill['id']}/approve", headers=self._auth(owner_token)
            ).status_code,
            200,
        )
        self.assertEqual(len(local_store.list_routable_private_skills(member["id"])), 1)

    def test_org_policies_endpoints(self) -> None:
        org, owner_token, member_token = self._org_with_owner_and_member()
        self.assertEqual(
            self.client.post(
                f"/orgs/{org['id']}/policies",
                json={"skill_id": "s1", "policy": "block"},
                headers=self._auth(member_token),
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                f"/orgs/{org['id']}/policies",
                json={"skill_id": "s1", "policy": "maybe"},
                headers=self._auth(owner_token),
            ).status_code,
            400,
        )
        self.assertEqual(
            self.client.post(
                f"/orgs/{org['id']}/policies",
                json={"skill_id": "s1", "policy": "block"},
                headers=self._auth(owner_token),
            ).status_code,
            200,
        )
        policies = self.client.get(f"/orgs/{org['id']}/policies", headers=self._auth(member_token)).json()
        self.assertEqual(policies["policies"][0]["policy"], "block")
        self.assertEqual(
            self.client.delete(
                f"/orgs/{org['id']}/policies/s1", headers=self._auth(owner_token)
            ).status_code,
            200,
        )

    def test_org_audit_and_analytics_are_owner_only(self) -> None:
        org, owner_token, member_token = self._org_with_owner_and_member()
        # Install by a member lands in the org audit log.
        r = self.client.post(
            "/installs",
            json={"skill_id": "s1", "target": "claude"},
            headers=self._auth(member_token),
        )
        self.assertEqual(r.status_code, 200)

        self.assertEqual(
            self.client.get(f"/orgs/{org['id']}/audit", headers=self._auth(member_token)).status_code, 403
        )
        audit = self.client.get(f"/orgs/{org['id']}/audit", headers=self._auth(owner_token)).json()["audit"]
        actions = [e["action"] for e in audit]
        self.assertIn("skill_installed", actions)

        self.assertEqual(
            self.client.get(f"/orgs/{org['id']}/analytics", headers=self._auth(member_token)).status_code,
            403,
        )
        analytics = self.client.get(
            f"/orgs/{org['id']}/analytics", headers=self._auth(owner_token)
        ).json()["analytics"]
        self.assertEqual(analytics["members"], 2)
        self.assertIn("pooled_routes_this_month", analytics)
        self.assertIn("pooled_route_limit", analytics)


if __name__ == "__main__":
    unittest.main()
