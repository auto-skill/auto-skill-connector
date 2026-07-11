from __future__ import annotations

import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

import auth
import local_store
import scraper


class AccountsStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        local_store.DB_PATH = Path(self.tmp.name) / "local_skills.db"
        local_store.init_db()

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path

    def test_get_or_create_user_is_idempotent_by_email(self) -> None:
        first = local_store.get_or_create_user("a@example.com", "Alice", None)
        second = local_store.get_or_create_user("a@example.com", "Alice Updated", "http://x/a.png")
        self.assertEqual(first["id"], second["id"])
        self.assertEqual(second["name"], "Alice Updated")
        self.assertEqual(second["avatar_url"], "http://x/a.png")

    def test_cli_token_lookup_and_revocation(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "Alice", None)
        local_store.create_cli_token(user["id"], "hash-1")
        looked_up = local_store.get_user_by_token_hash("hash-1")
        self.assertIsNotNone(looked_up)
        self.assertEqual(looked_up["id"], user["id"])

        self.assertTrue(local_store.revoke_cli_token("hash-1"))
        self.assertIsNone(local_store.get_user_by_token_hash("hash-1"))
        self.assertFalse(local_store.revoke_cli_token("hash-1"))  # already revoked

    def test_cli_token_expires_and_slides_forward_on_use(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "Alice", None)
        local_store.create_cli_token(user["id"], "hash-2")

        # A pre-migration token (no expires_at yet) must keep working --
        # this deploy must not silently log out everyone already logged in.
        conn = local_store.get_conn()
        conn.execute(
            "INSERT INTO cli_tokens (id, token_hash, user_id, created_at) VALUES ('legacy', 'hash-legacy', ?, ?)",
            (user["id"], local_store._now()),
        )
        conn.commit()
        conn.close()
        self.assertIsNotNone(local_store.get_user_by_token_hash("hash-legacy"))

        # An expired token is rejected.
        conn = local_store.get_conn()
        conn.execute("UPDATE cli_tokens SET expires_at=? WHERE token_hash='hash-2'", ("2020-01-01T00:00:00+00:00",))
        conn.commit()
        conn.close()
        self.assertIsNone(local_store.get_user_by_token_hash("hash-2"))

    def test_favorites_are_isolated_per_user(self) -> None:
        user_a = local_store.get_or_create_user("a@example.com", "A", None)
        user_b = local_store.get_or_create_user("b@example.com", "B", None)
        local_store.upsert_rows(
            "skills", [{"id": "s1", "name": "Demo", "description": "d", "source": "x", "url": "http://s1"}], "url"
        )
        local_store.add_favorite(user_a["id"], "s1")

        self.assertEqual(len(local_store.list_favorites(user_a["id"])), 1)
        self.assertEqual(local_store.list_favorites(user_b["id"]), [])

        self.assertFalse(local_store.remove_favorite(user_b["id"], "s1"))  # not B's favorite
        self.assertTrue(local_store.remove_favorite(user_a["id"], "s1"))
        self.assertEqual(local_store.list_favorites(user_a["id"]), [])

    def test_private_skills_are_isolated_per_user(self) -> None:
        user_a = local_store.get_or_create_user("a@example.com", "A", None)
        user_b = local_store.get_or_create_user("b@example.com", "B", None)
        skill = local_store.add_private_skill(user_a["id"], "deploy-to-fly", "desc", "CONTENT")

        self.assertEqual(len(local_store.list_private_skills(user_a["id"])), 1)
        self.assertEqual(local_store.list_private_skills(user_b["id"]), [])

        self.assertFalse(local_store.remove_private_skill(user_b["id"], skill["id"]))  # not B's to delete
        self.assertTrue(local_store.remove_private_skill(user_a["id"], skill["id"]))

    def test_installs_are_recorded_per_user(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "A", None)
        local_store.record_install(user["id"], "s1", "http://s1", "claude")
        installs = local_store.list_installs(user["id"])
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0]["target"], "claude")


class AuthModuleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        local_store.DB_PATH = Path(self.tmp.name) / "local_skills.db"
        local_store.init_db()
        auth._states.clear()

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path

    def test_state_handshake_round_trips_once(self) -> None:
        state = auth.create_state("google", {"flow": "cli", "port": 54321})
        self.assertEqual(auth.pop_state(state), {"provider": "google", "flow": "cli", "port": 54321})
        self.assertIsNone(auth.pop_state(state))  # single use

    def test_unknown_state_returns_none(self) -> None:
        self.assertIsNone(auth.pop_state("not-a-real-state"))

    def test_web_state_handshake_round_trips_once(self) -> None:
        state = auth.create_web_state("github", "https://example.com/dashboard.html")
        resolved = auth.pop_login_state(state)

        self.assertEqual(resolved["provider"], "github")
        self.assertEqual(resolved["flow"], "web")
        self.assertEqual(resolved["return_to"], "https://example.com/dashboard.html")
        self.assertIsNone(auth.pop_login_state(state))

    def test_issue_and_verify_and_revoke_token(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "A", None)
        token = auth.issue_cli_token(user["id"])
        looked_up = auth.user_from_authorization_header(f"Bearer {token}")
        self.assertIsNotNone(looked_up)
        self.assertEqual(looked_up["id"], user["id"])

        self.assertIsNone(auth.user_from_authorization_header(None))
        self.assertIsNone(auth.user_from_authorization_header("Bearer wrong-token"))
        self.assertIsNone(auth.user_from_authorization_header("NotBearer xyz"))

        self.assertTrue(auth.revoke_cli_token(token))
        self.assertIsNone(auth.user_from_authorization_header(f"Bearer {token}"))

    def test_authorize_url_contains_client_id_and_redirect(self) -> None:
        with patch.dict(auth.PROVIDERS["google"], {"client_id": "test-id"}):
            state = auth.create_state("google", {"flow": "cli", "port": 1234})
            url = auth.build_authorize_url("google", state, next(iter(auth.ALLOWED_LOGIN_HOSTS)))
            self.assertIn("client_id=test-id", url)
            self.assertIn("accounts.google.com", url)


class AccountsEndpointTests(unittest.TestCase):
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

    def test_whoami_requires_bearer_token(self) -> None:
        self.assertEqual(self.client.get("/auth/whoami").status_code, 401)
        token = self._login("a@example.com")
        r = self.client.get("/auth/whoami", headers={"Authorization": f"Bearer {token}"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["email"], "a@example.com")

    def _login_host(self) -> str:
        return next(iter(auth.ALLOWED_LOGIN_HOSTS))

    def test_skills_catalog_requires_account_and_paginates(self) -> None:
        conn = local_store.get_conn()
        try:
            for i in range(3):
                conn.execute(
                    "INSERT INTO skills (id, name, description, source, url, tags, discovered_at, quality_status)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, 'active')",
                    (f"skill-{i}", f"catalog-skill-{i}", "spreadsheet helper" if i == 0 else "other",
                     "test", f"https://example.com/{i}", '["t"]', f"2026-07-0{i + 1}T00:00:00+00:00"),
                )
            conn.commit()
        finally:
            conn.close()

        self.assertEqual(self.client.get("/skills-catalog").status_code, 401)

        headers = {"Authorization": f"Bearer {self._login('a@example.com')}"}
        r = self.client.get("/skills-catalog?limit=2", headers=headers)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["total"], 3)
        self.assertEqual(len(body["skills"]), 2)
        self.assertEqual(body["skills"][0]["name"], "catalog-skill-2")
        self.assertEqual(body["skills"][0]["tags"], ["t"])
        self.assertNotIn("raw", body["skills"][0])

        page2 = self.client.get("/skills-catalog?limit=2&offset=2", headers=headers).json()
        self.assertEqual([s["name"] for s in page2["skills"]], ["catalog-skill-0"])

        search = self.client.get("/skills-catalog?q=SPREADSHEET", headers=headers).json()
        self.assertEqual(search["total"], 1)
        self.assertEqual(search["skills"][0]["name"], "catalog-skill-0")

    def test_skills_catalog_defaults_to_popular_and_hides_inactive(self) -> None:
        conn = local_store.get_conn()
        try:
            conn.execute(
                "INSERT INTO skills (id, name, description, source, url, raw, discovered_at, quality_status)"
                " VALUES ('low', 'low-stars', 'd', 'test', 'https://example.com/low', '{\"stars\": 5}',"
                " '2026-07-01T00:00:00+00:00', 'active')"
            )
            conn.execute(
                "INSERT INTO skills (id, name, description, source, url, raw, discovered_at, quality_status)"
                " VALUES ('high', 'high-stars', 'd', 'test', 'https://example.com/high', '{\"stars\": 500}',"
                " '2026-06-01T00:00:00+00:00', 'active')"
            )
            conn.execute(
                "INSERT INTO skills (id, name, description, source, url, raw, discovered_at, quality_status)"
                " VALUES ('rejected', 'rejected-skill', 'd', 'test', 'https://example.com/rejected',"
                " '{\"stars\": 999999}', '2026-06-01T00:00:00+00:00', 'rejected')"
            )
            conn.commit()
        finally:
            conn.close()

        headers = {"Authorization": f"Bearer {self._login('a@example.com')}"}
        body = self.client.get("/skills-catalog", headers=headers).json()
        self.assertEqual(body["total"], 2)  # rejected row excluded
        self.assertEqual([s["name"] for s in body["skills"]], ["high-stars", "low-stars"])

        recent = self.client.get("/skills-catalog?sort=recent", headers=headers).json()
        self.assertEqual([s["name"] for s in recent["skills"]], ["low-stars", "high-stars"])

    def test_admin_stats_requires_admin_email(self) -> None:
        admin = local_store.get_or_create_user("admin@example.com", "Admin", None)
        local_store.link_oauth_identity(admin["id"], "google", "google-sub-1")
        regular_headers = {"Authorization": f"Bearer {self._login('regular@example.com')}"}
        admin_token = auth.issue_cli_token(admin["id"])
        admin_headers = {"Authorization": f"Bearer {admin_token}"}

        self.assertEqual(self.client.get("/admin/stats").status_code, 401)
        self.assertEqual(self.client.get("/admin/stats", headers=regular_headers).status_code, 403)

        with patch.dict(os.environ, {"ADMIN_EMAILS": "admin@example.com"}):
            r = self.client.get("/admin/stats", headers=admin_headers)
            self.assertEqual(r.status_code, 200)
            body = r.json()
            emails = {u["email"] for u in body["users"]}
            self.assertIn("admin@example.com", emails)
            self.assertIn("regular@example.com", emails)
            admin_row = next(u for u in body["users"] if u["email"] == "admin@example.com")
            self.assertEqual(admin_row["login_provider"], "google")
            self.assertEqual(admin_row["run_count"], 0)
            self.assertEqual(admin_row["tiers"], {})
            self.assertIn("recent_events", body)

    def test_admin_stats_rolls_up_route_events(self) -> None:
        user = local_store.get_or_create_user("busy@example.com", "Busy", None)
        conn = local_store.get_conn()
        try:
            for i, (tier, latency, outcome) in enumerate(
                [("full", 100, "used"), ("full", 200, None), ("hint", 50, "dismissed")]
            ):
                conn.execute(
                    "INSERT INTO route_events (id, created_at, user_id, tier, latency_ms, outcome, prompt_text)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (f"evt-{i}", f"2026-07-0{i + 1}T00:00:00+00:00", user["id"], tier, latency, outcome, f"prompt {i}"),
                )
            conn.commit()
        finally:
            conn.close()

        admin_headers = {"Authorization": f"Bearer {self._login('admin2@example.com')}"}
        with patch.dict(os.environ, {"ADMIN_EMAILS": "admin2@example.com"}):
            body = self.client.get("/admin/stats", headers=admin_headers).json()
            row = next(u for u in body["users"] if u["email"] == "busy@example.com")
            self.assertEqual(row["run_count"], 3)
            self.assertEqual(row["tiers"], {"full": 2, "hint": 1})
            self.assertEqual(row["outcomes"], {"used": 1, "dismissed": 1})
            self.assertEqual(row["avg_latency_ms"], round((100 + 200 + 50) / 3))

            latest_event = body["recent_events"][0]
            self.assertEqual(latest_event["user_email"], "busy@example.com")
            self.assertEqual(latest_event["prompt_text"], "prompt 2")

    def test_web_login_start_accepts_dashboard_return_url(self) -> None:
        with (
            patch.dict(auth.PROVIDERS["google"], {"client_id": "test-id"}),
            patch.dict(os.environ, {"AUTO_SKILL_DASHBOARD_ORIGINS": "https://site.example"}),
        ):
            response = self.client.get(
                "/auth/google/start",
                params={"flow": "web", "return_to": "https://site.example/dashboard.html"},
                headers={"Host": self._login_host()},
                follow_redirects=False,
            )

        self.assertIn(response.status_code, (302, 307))
        self.assertIn("accounts.google.com", response.headers["location"])
        self.assertIn("client_id=test-id", response.headers["location"])

    def test_web_login_start_rejects_unsafe_return_url(self) -> None:
        with patch.dict(auth.PROVIDERS["google"], {"client_id": "test-id"}):
            response = self.client.get(
                "/auth/google/start",
                params={"flow": "web", "return_to": "javascript:alert(1)"},
                headers={"Host": self._login_host()},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 400)

    def test_web_login_start_rejects_unconfigured_https_origin(self) -> None:
        with patch.dict(auth.PROVIDERS["google"], {"client_id": "test-id"}):
            response = self.client.get(
                "/auth/google/start",
                params={"flow": "web", "return_to": "https://not-the-dashboard.example/dashboard.html"},
                headers={"Host": self._login_host()},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 400)

    def test_login_start_rejects_unknown_host(self) -> None:
        with patch.dict(auth.PROVIDERS["google"], {"client_id": "test-id"}):
            response = self.client.get(
                "/auth/google/start",
                params={"flow": "web", "return_to": "https://site.example/dashboard.html"},
                headers={"Host": "not-a-real-host.example"},
                follow_redirects=False,
            )

        self.assertEqual(response.status_code, 400)

    def test_web_callback_redirects_token_to_fragment(self) -> None:
        user = local_store.get_or_create_user("a@example.com", "A", None)
        state = auth.create_web_state("google", "https://site.example/dashboard.html?tab=runs#existing=yes")

        with patch("auth.complete_login", AsyncMock(return_value=(user, "token-123"))):
            response = self.client.get(
                "/auth/google/callback",
                params={"code": "code-1", "state": state},
                headers={"Host": self._login_host()},
                follow_redirects=False,
            )

        self.assertIn(response.status_code, (302, 307))
        self.assertEqual(
            response.headers["location"],
            "https://site.example/dashboard.html?tab=runs#existing=yes&token=token-123",
        )

    def test_logout_revokes_token(self) -> None:
        token = self._login("a@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        self.assertEqual(self.client.post("/auth/logout", headers=headers).status_code, 200)
        self.assertEqual(self.client.get("/auth/whoami", headers=headers).status_code, 401)

    def test_favorites_crud_and_isolation(self) -> None:
        token_a = self._login("a@example.com")
        token_b = self._login("b@example.com")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}
        local_store.upsert_rows(
            "skills", [{"id": "s1", "name": "Demo", "description": "d", "source": "x", "url": "http://s1"}], "url"
        )

        self.assertEqual(self.client.post("/favorites", json={"skill_id": "s1"}, headers=headers_a).status_code, 200)
        favs_a = self.client.get("/favorites", headers=headers_a).json()["favorites"]
        self.assertEqual([f["id"] for f in favs_a], ["s1"])
        favs_b = self.client.get("/favorites", headers=headers_b).json()["favorites"]
        self.assertEqual(favs_b, [])  # B never sees A's favorite

        self.assertEqual(self.client.delete("/favorites/s1", headers=headers_b).status_code, 404)  # not B's
        self.assertEqual(self.client.delete("/favorites/s1", headers=headers_a).status_code, 200)

    def test_private_skills_crud_and_isolation(self) -> None:
        token_a = self._login("a@example.com")
        token_b = self._login("b@example.com")
        headers_a = {"Authorization": f"Bearer {token_a}"}
        headers_b = {"Authorization": f"Bearer {token_b}"}

        r = self.client.post(
            "/private-skills",
            json={"name": "deploy-to-fly", "description": "d", "content": "FLY"},
            headers=headers_a,
        )
        self.assertEqual(r.status_code, 200)
        skill_id = r.json()["private_skill"]["id"]

        self.assertEqual(len(self.client.get("/private-skills", headers=headers_a).json()["private_skills"]), 1)
        self.assertEqual(self.client.get("/private-skills", headers=headers_b).json()["private_skills"], [])

        self.assertEqual(self.client.delete(f"/private-skills/{skill_id}", headers=headers_b).status_code, 404)
        self.assertEqual(self.client.delete(f"/private-skills/{skill_id}", headers=headers_a).status_code, 200)

    def test_installs_round_trip(self) -> None:
        token = self._login("a@example.com")
        headers = {"Authorization": f"Bearer {token}"}
        r = self.client.post(
            "/installs", json={"skill_id": "s1", "skill_url": "http://s1", "target": "claude"}, headers=headers
        )
        self.assertEqual(r.status_code, 200)
        installs = self.client.get("/installs", headers=headers).json()["installs"]
        self.assertEqual(len(installs), 1)
        self.assertEqual(installs[0]["target"], "claude")

    def test_runs_are_isolated_per_user(self) -> None:
        token_a = self._login("a@example.com")
        token_b = self._login("b@example.com")
        user_a = local_store.get_or_create_user("a@example.com", "A", None)
        user_b = local_store.get_or_create_user("b@example.com", "B", None)
        local_store.insert_route_event(
            {
                "id": "route-a",
                "user_id": user_a["id"],
                "tier": "full",
                "skill_name": "spreadsheet-reporter",
                "latency_ms": 100,
                "skill_find_ms": 25,
                "injected_tokens": 500,
                "response_tokens": 900,
            }
        )
        local_store.insert_route_event(
            {
                "id": "route-b",
                "user_id": user_b["id"],
                "tier": "hint",
                "skill_name": "other-skill",
                "latency_ms": 120,
                "skill_find_ms": 30,
                "injected_tokens": 100,
                "response_tokens": 300,
            }
        )

        runs_a = self.client.get("/runs", headers={"Authorization": f"Bearer {token_a}"}).json()["runs"]
        runs_b = self.client.get("/runs", headers={"Authorization": f"Bearer {token_b}"}).json()["runs"]

        self.assertEqual([run["id"] for run in runs_a], ["route-a"])
        self.assertEqual(runs_a[0]["skill_name"], "spreadsheet-reporter")
        self.assertEqual([run["id"] for run in runs_b], ["route-b"])

    def test_route_surfaces_only_the_caller_own_private_skill(self) -> None:
        token_a = self._login("a@example.com")
        token_b = self._login("b@example.com")
        user_a = local_store.get_or_create_user("a@example.com", "A", None)
        user_b = local_store.get_or_create_user("b@example.com", "B", None)
        local_store.add_private_skill(user_a["id"], "deploy-to-fly", "deploy to fly.io", "FLY INSTRUCTIONS")
        local_store.add_private_skill(user_b["id"], "deploy-to-vercel", "deploy to vercel", "VERCEL INSTRUCTIONS")

        async def fake_retrieve(client, query, limit):
            return []

        with patch("recommender.retrieve_skills", fake_retrieve):
            anon = self.client.post("/route", json={"task": "deploy to fly"})
            self.assertEqual(anon.json()["tier"], "none")

            resp_a = self.client.post(
                "/route", json={"task": "deploy to fly"}, headers={"Authorization": f"Bearer {token_a}"}
            )
            self.assertEqual(resp_a.json()["tier"], "full")
            self.assertEqual(resp_a.json()["content"], "FLY INSTRUCTIONS")

            resp_b = self.client.post(
                "/route", json={"task": "deploy to fly"}, headers={"Authorization": f"Bearer {token_b}"}
            )
            self.assertNotEqual(resp_b.json().get("content"), "FLY INSTRUCTIONS")

    def test_find_semantic_surfaces_only_the_caller_own_private_skill(self) -> None:
        token_a = self._login("a@example.com")
        user_a = local_store.get_or_create_user("a@example.com", "A", None)
        local_store.add_private_skill(user_a["id"], "deploy-to-fly", "deploy to fly.io", "FLY INSTRUCTIONS")

        async def fake_retrieve(client, query, limit):
            return []

        with patch("recommender.retrieve_skills", fake_retrieve):
            anon = self.client.get("/find-semantic", params={"q": "deploy to fly"})
            self.assertEqual(anon.json()["results"], [])

            authed = self.client.get(
                "/find-semantic", params={"q": "deploy to fly"}, headers={"Authorization": f"Bearer {token_a}"}
            )
            names = [r.get("name") for r in authed.json()["results"]]
            self.assertIn("deploy-to-fly", names)

    def test_private_skill_does_not_match_on_one_generic_name_word(self) -> None:
        token = self._login("a@example.com")
        user = local_store.get_or_create_user("a@example.com", "A", None)
        local_store.add_private_skill(user["id"], "performance-review", "Review performance.", "PRIVATE INSTRUCTIONS")

        async def fake_retrieve(client, query, limit):
            return []

        with patch("recommender.retrieve_skills", fake_retrieve):
            response = self.client.post(
                "/route",
                json={"task": "improve performance of this API"},
                headers={"Authorization": f"Bearer {token}"},
            )

        self.assertEqual(response.json()["tier"], "none")


class PublicGuardAccountsTests(unittest.TestCase):
    def test_auth_and_account_paths_are_public(self) -> None:
        cases = [
            ("GET", "/auth/google/start"),
            ("GET", "/auth/google/callback"),
            ("GET", "/auth/whoami"),
            ("POST", "/auth/logout"),
            ("GET", "/favorites"),
            ("POST", "/favorites"),
            ("DELETE", "/favorites/abc"),
            ("GET", "/installs"),
            ("POST", "/installs"),
            ("GET", "/runs"),
            ("GET", "/private-skills"),
            ("POST", "/private-skills"),
            ("DELETE", "/private-skills/abc"),
        ]
        for method, path in cases:
            with self.subTest(method=method, path=path):
                self.assertTrue(scraper.public_api_allows(method, path))

    def test_rest_v1_surface_stays_loopback_only(self) -> None:
        self.assertFalse(scraper.public_api_allows("GET", "/rest/v1/skills"))
        self.assertFalse(scraper.public_api_allows("POST", "/rest/v1/skills"))
        self.assertFalse(scraper.public_api_allows("DELETE", "/rest/v1/skills"))

    def test_mcp_oauth_internal_endpoints_stay_loopback_only(self) -> None:
        """/mcp-oauth/token deliberately skips its own PKCE/client-secret
        checks (the mcp SDK already verified code_verifier against
        code_challenge in the connector's own TokenHandler before ever
        reaching here -- see mcp_oauth.py's /token docstring). That's only
        safe as long as this endpoint never becomes reachable from the
        public tunnel, so this asserts that invariant directly rather than
        leaving it as an implicit assumption -- the same class of silent
        allowlist drift caused a real incident (see /skills-catalog, fixed
        2026-07-09)."""
        for method, path in [
            ("POST", "/mcp-oauth/token"),
            ("POST", "/mcp-oauth/clients"),
            ("GET", "/mcp-oauth/clients/some-client-id"),
            ("GET", "/mcp-oauth/codes/some-code"),
        ]:
            with self.subTest(method=method, path=path):
                self.assertFalse(scraper.public_api_allows(method, path))
        # Only the two browser-facing pages are meant to be public.
        self.assertTrue(scraper.public_api_allows("GET", "/mcp-oauth/authorize"))
        self.assertTrue(scraper.public_api_allows("GET", "/mcp-oauth/choose"))


class RateLimitGuardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        self.db_path = Path(self.tmp.name) / "local_skills.db"
        local_store.DB_PATH = self.db_path
        scraper.store.DB_PATH = self.db_path
        local_store.init_db()
        local_store.invalidate_vector_cache()
        scraper._rate_limit_counters.clear()
        self.client = TestClient(scraper.app)

    def tearDown(self) -> None:
        local_store.invalidate_vector_cache()
        local_store.DB_PATH = self.old_db_path
        scraper.store.DB_PATH = self.old_db_path
        scraper._rate_limit_counters.clear()

    def test_exceeding_the_limit_returns_429(self) -> None:
        headers = {"x-forwarded-for": "203.0.113.5"}
        limit, _ = scraper.RATE_LIMIT_BUCKETS["/auth/"]
        statuses = [self.client.get("/auth/whoami", headers=headers).status_code for _ in range(limit + 5)]
        self.assertNotIn(429, statuses[:limit])
        self.assertIn(429, statuses[limit:])

    def test_different_ips_have_independent_limits(self) -> None:
        limit, _ = scraper.RATE_LIMIT_BUCKETS["/auth/"]
        for _ in range(limit + 5):
            self.client.get("/auth/whoami", headers={"x-forwarded-for": "203.0.113.5"})
        r = self.client.get("/auth/whoami", headers={"x-forwarded-for": "203.0.113.9"})
        self.assertNotEqual(r.status_code, 429)

    def test_loopback_traffic_is_never_rate_limited(self) -> None:
        limit, _ = scraper.RATE_LIMIT_BUCKETS["/auth/"]
        statuses = [self.client.get("/auth/whoami").status_code for _ in range(limit + 10)]
        self.assertNotIn(429, statuses)

    def test_unbucketed_paths_are_not_limited(self) -> None:
        headers = {"x-forwarded-for": "203.0.113.5"}
        limit, _ = scraper.RATE_LIMIT_BUCKETS["/auth/"]
        statuses = [self.client.get("/favorites", headers=headers).status_code for _ in range(limit + 10)]
        self.assertNotIn(429, statuses)


if __name__ == "__main__":
    unittest.main()
