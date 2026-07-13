from __future__ import annotations

import json
import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import jwt
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi import HTTPException
from fastapi.testclient import TestClient

import admin_security
import auth
import local_store
import scraper


ACCESS_ENV = {
    "ADMIN_ACCESS_MODE": "cloudflare",
    "ADMIN_HOST": "admin.autoskill.dev",
    "ADMIN_EMAILS": "admin@example.com,other-admin@example.com",
    "CF_ACCESS_TEAM_DOMAIN": "test-team.cloudflareaccess.com",
    "CF_ACCESS_AUD": "access-audience",
}

SSH_ENV = {
    "ADMIN_ACCESS_MODE": "ssh",
    "ADMIN_HOST": "127.0.0.1",
    "ADMIN_EMAILS": "admin@example.com,other-admin@example.com",
}


class AdminSecurityTests(unittest.TestCase):
    def test_access_jwt_verifies_signature_issuer_audience_expiry_and_email(self) -> None:
        private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        now = datetime.now(timezone.utc)
        claims = {
            "iss": "https://test-team.cloudflareaccess.com",
            "aud": ["access-audience"],
            "email": "Admin@Example.com",
            "iat": now,
            "exp": now + timedelta(minutes=5),
        }
        token = jwt.encode(claims, private_key, algorithm="RS256", headers={"kid": "test-key"})
        client = MagicMock()
        client.get_signing_key_from_jwt.return_value.key = private_key.public_key()
        with patch.dict(os.environ, ACCESS_ENV), patch.dict(
            admin_security._JWK_CLIENTS,
            {"https://test-team.cloudflareaccess.com/cdn-cgi/access/certs": client},
            clear=True,
        ):
            self.assertEqual(admin_security.verify_access_assertion(token), "admin@example.com")
            wrong_aud = jwt.encode({**claims, "aud": ["wrong"]}, private_key, algorithm="RS256", headers={"kid": "test-key"})
            with self.assertRaises(HTTPException):
                admin_security.verify_access_assertion(wrong_aud)
            expired = jwt.encode({**claims, "exp": now - timedelta(seconds=1)}, private_key, algorithm="RS256", headers={"kid": "test-key"})
            with self.assertRaises(HTTPException):
                admin_security.verify_access_assertion(expired)

    def test_access_header_is_required_and_forwarded_email_is_not_a_substitute(self) -> None:
        with patch.dict(os.environ, ACCESS_ENV):
            with self.assertRaises(HTTPException) as raised:
                admin_security.verify_access_assertion(None)
            self.assertEqual(raised.exception.status_code, 403)


class LegacyPlanMigrationTests(unittest.TestCase):
    def test_unlinked_manual_paid_plan_becomes_reviewable_expiring_comp(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            old_path = local_store.DB_PATH
            path = Path(tmp) / "legacy.db"
            conn = sqlite3.connect(path)
            conn.execute(
                "CREATE TABLE users (id TEXT PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT, "
                "avatar_url TEXT, created_at TEXT, plan TEXT DEFAULT 'free')"
            )
            conn.execute(
                "INSERT INTO users (id, email, plan) VALUES ('legacy-user', 'legacy@example.com', 'pro')"
            )
            conn.commit()
            conn.close()
            try:
                local_store.DB_PATH = path
                local_store.init_db()
                raw = local_store.get_user_by_id("legacy-user")
                self.assertEqual(raw["plan"], "free")
                access = local_store.access_details_for_user(raw)
                self.assertEqual(access["plan"], "pro")
                self.assertEqual(access["plan_source"], "complimentary")
                audit = local_store.list_admin_audit()
                self.assertEqual(audit[0]["action"], "legacy_plan_migrated")
            finally:
                local_store.DB_PATH = old_path


class AdminEndpointTests(unittest.TestCase):
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
        self.admin = local_store.get_or_create_user("admin@example.com", "Admin", None)
        self.target = local_store.get_or_create_user("target@example.com", "Target", None)
        self.regular = local_store.get_or_create_user("regular@example.com", "Regular", None)
        self.admin_token = auth.issue_cli_token(self.admin["id"])
        self.regular_token = auth.issue_cli_token(self.regular["id"])

    def tearDown(self) -> None:
        local_store.invalidate_vector_cache()
        local_store.DB_PATH = self.old_db_path
        scraper.store.DB_PATH = self.old_db_path

    def _headers(self, token: str | None = None, host: str = "127.0.0.1") -> dict:
        headers = {"Host": host}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _access(self, email: str = "admin@example.com"):
        return patch.object(admin_security, "verify_access_assertion", return_value=email)

    def _future(self, days: int = 30) -> str:
        return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()

    def test_admin_requires_ssh_host_and_allowlisted_admin_bearer(self) -> None:
        with patch.dict(os.environ, SSH_ENV):
            self.assertEqual(self.client.get("/admin/stats", headers=self._headers()).status_code, 401)
            self.assertEqual(
                self.client.get("/admin/stats", headers=self._headers(self.regular_token)).status_code, 403
            )
            self.assertEqual(
                self.client.get(
                    "/admin/stats", headers=self._headers(self.admin_token, host="skills.autoskill.dev")
                ).status_code,
                404,
            )
            forwarded = self._headers(self.admin_token)
            forwarded["cf-connecting-ip"] = "203.0.113.20"
            self.assertEqual(self.client.get("/admin/stats", headers=forwarded).status_code, 403)
        with patch.dict(os.environ, {**SSH_ENV, "ADMIN_ACCESS_MODE": "disabled"}):
            self.assertEqual(
                self.client.get("/admin/stats", headers=self._headers(self.admin_token)).status_code, 404
            )

    def test_cloudflare_mode_still_requires_matching_access_identity(self) -> None:
        headers = self._headers(self.admin_token, host="admin.autoskill.dev")
        headers["Cf-Access-Jwt-Assertion"] = "signed-access-token"
        with patch.dict(os.environ, ACCESS_ENV), self._access("other-admin@example.com"):
            self.assertEqual(self.client.get("/admin/stats", headers=headers).status_code, 403)

    def test_admin_ui_is_access_only_no_store_and_session_scoped(self) -> None:
        with patch.dict(os.environ, SSH_ENV):
            response = self.client.get("/admin", headers=self._headers())
        self.assertEqual(response.status_code, 200)
        self.assertIn("sessionStorage", response.text)
        self.assertNotIn("localStorage", response.text)
        self.assertEqual(response.headers["cache-control"], "no-store, max-age=0")
        self.assertIn("frame-ancestors 'none'", response.headers["content-security-policy"])
        self.assertNotIn("__CSP_NONCE__", response.text)

    def test_complimentary_grant_expiry_revoke_and_append_only_audit(self) -> None:
        body = {
            "target_user_id": self.target["id"],
            "plan": "pro",
            "expires_at": self._future(),
            "reason": "Launch partner trial",
        }
        with patch.dict(os.environ, SSH_ENV):
            granted = self.client.post(
                "/admin/entitlements/grant", json=body, headers=self._headers(self.admin_token)
            )
            self.assertEqual(granted.status_code, 200, granted.text)
            entitlement_id = granted.json()["entitlement"]["entitlement_id"]
            who = auth.user_from_authorization_header(f"Bearer {auth.issue_cli_token(self.target['id'])}")
            self.assertEqual(who["plan"], "pro")
            self.assertEqual(who["plan_source"], "complimentary")
            audit = self.client.get("/admin/audit", headers=self._headers(self.admin_token)).json()["events"]
            self.assertEqual(audit[0]["action"], "complimentary_entitlement_granted")
            self.assertEqual(audit[0]["reason"], body["reason"])

            revoked = self.client.post(
                f"/admin/entitlements/{entitlement_id}/revoke",
                json={"reason": "Trial completed"},
                headers=self._headers(self.admin_token),
            )
            self.assertEqual(revoked.status_code, 200)
            self.assertEqual(
                auth.user_from_authorization_header(f"Bearer {auth.issue_cli_token(self.target['id'])}")["plan"],
                "free",
            )
            self.assertEqual(
                self.client.post(
                    f"/admin/entitlements/{entitlement_id}/revoke",
                    json={"reason": "Repeat revoke"},
                    headers=self._headers(self.admin_token),
                ).status_code,
                404,
            )

        conn = local_store.get_conn()
        with self.assertRaises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM admin_audit_log")
        conn.close()

        future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        local_store.grant_complimentary_entitlement(
            self.target["id"], "team", future, "Short support window", self.admin
        )
        raw = local_store.get_user_by_id(self.target["id"])
        with patch.object(local_store, "_now", return_value=(datetime.now(timezone.utc) + timedelta(hours=2)).isoformat()):
            self.assertEqual(local_store.access_details_for_user(raw)["plan"], "free")

    def test_paid_plan_survives_complimentary_revoke(self) -> None:
        local_store.set_user_plan_by_id(self.target["id"], "pro")
        grant = local_store.grant_complimentary_entitlement(
            self.target["id"], "team", self._future(), "Temporary team evaluation", self.admin
        )
        local_store.revoke_complimentary_entitlement(grant["entitlement_id"], "Evaluation ended", self.admin)
        self.assertEqual(local_store.access_details_for_user(local_store.get_user_by_id(self.target["id"]))["plan"], "pro")

    def test_team_comp_revoke_disables_workspace_and_member_inheritance(self) -> None:
        member = local_store.get_or_create_user("member@example.com", "Member", None)
        grant = local_store.grant_complimentary_entitlement(
            self.target["id"], "team", self._future(), "Temporary team workspace", self.admin
        )
        org = local_store.create_org("Trial workspace", self.target["id"])
        local_store.add_org_member(org["id"], member["id"])
        member_token = auth.issue_cli_token(member["id"])
        self.assertEqual(
            auth.user_from_authorization_header(f"Bearer {member_token}")["plan_source"], "team_org"
        )
        local_store.revoke_complimentary_entitlement(grant["entitlement_id"], "Workspace trial ended", self.admin)
        self.assertEqual(auth.user_from_authorization_header(f"Bearer {member_token}")["plan"], "free")
        response = self.client.get(
            f"/orgs/{org['id']}/members", headers={"Authorization": f"Bearer {member_token}"}
        )
        self.assertEqual(response.status_code, 402)

    def test_admin_surface_exposes_no_tokens_prompts_private_content_or_arbitrary_mutation(self) -> None:
        sentinel_token = "raw-token-sentinel"
        sentinel_private = "private-skill-content-sentinel"
        local_store.create_cli_token(self.target["id"], sentinel_token)
        local_store.add_private_skill(self.target["id"], "private", "desc", sentinel_private)
        with patch.dict(os.environ, {**SSH_ENV, "STRIPE_SECRET_KEY": "stripe-secret-sentinel"}):
            users = self.client.get("/admin/users", headers=self._headers(self.admin_token))
            stats = self.client.get("/admin/stats", headers=self._headers(self.admin_token))
            audit = self.client.get("/admin/audit", headers=self._headers(self.admin_token))
            serialized = json.dumps([users.json(), stats.json(), audit.json()])
            for sentinel in (sentinel_token, sentinel_private, "stripe-secret-sentinel", "prompt_text"):
                self.assertNotIn(sentinel, serialized)
            self.assertEqual(
                self.client.post(
                    "/admin/entitlements/grant",
                    json={
                        "target_user_id": self.target["id"], "plan": "pro", "expires_at": self._future(),
                        "reason": "Support case", "sql": "UPDATE users SET plan='team'",
                    },
                    headers=self._headers(self.admin_token),
                ).status_code,
                422,
            )
            self.assertEqual(
                self.client.post("/admin/set-plan", json={"email": "target@example.com", "plan": "team"}, headers=self._headers(self.admin_token)).status_code,
                404,
            )


if __name__ == "__main__":
    unittest.main()
