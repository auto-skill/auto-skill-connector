from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

import auth
import billing_api
import local_store
import scraper

STRIPE_ENV = {
    "STRIPE_SECRET_KEY": "sk_test_x",
    "STRIPE_WEBHOOK_SECRET": "whsec_x",
    "STRIPE_PRICE_PRO": "price_pro",
    "STRIPE_PRICE_TEAM": "price_team",
    "STRIPE_PRICE_TEAM_SEAT": "price_seat",
}


class _FakeStripeObject(dict):
    """Models the one real-SDK incompatibility that caused a production
    500: stripe.StripeObject supports bracket access and .to_dict(), but
    NOT .get() (it raises AttributeError via __getattr__). A plain dict
    mock would silently hide that gap; this makes the same mistake fail
    the test the same way it failed in production."""

    def to_dict(self):
        return dict(self)

    def get(self, *_args, **_kwargs):
        raise AttributeError("get")


class _FakeStripeError(Exception):
    """Stand-in for stripe.StripeError. Must be a real exception class --
    billing_api.py's `except stripe.StripeError` binds to whatever `stripe`
    is patched to, and Python raises TypeError if that isn't an actual
    exception type, so a bare MagicMock attribute would break these tests
    before they ever reach the assertion."""

    def __init__(self, message: str = "stripe error") -> None:
        super().__init__(message)
        self.user_message = message


class BillingEndpointTests(unittest.TestCase):
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

    def _mock_stripe(self) -> MagicMock:
        mock = MagicMock()
        mock.StripeError = _FakeStripeError
        mock.Customer.create.return_value = {"id": "cus_1"}
        mock.checkout.Session.create.return_value = {"url": "https://checkout.stripe.com/test"}
        mock.billing_portal.Session.create.return_value = {"url": "https://billing.stripe.com/test"}
        mock.Subscription.list.return_value = _FakeStripeObject({"data": []})
        return mock

    def test_unconfigured_billing_is_explicit(self) -> None:
        _, headers = self._login("a@example.com")
        with patch.object(billing_api, "stripe", None):
            status = self.client.get("/billing/status", headers=headers).json()
            self.assertFalse(status["configured"])
            r = self.client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
            self.assertEqual(r.status_code, 503)

    def test_checkout_creates_session_and_persists_customer(self) -> None:
        user, headers = self._login("a@example.com")
        mock = self._mock_stripe()
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            bad = self.client.post("/billing/checkout", json={"plan": "platinum"}, headers=headers)
            self.assertEqual(bad.status_code, 400)

            r = self.client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["url"], "https://checkout.stripe.com/test")

        stored = local_store.get_user_by_id(user["id"])
        self.assertEqual(stored["stripe_customer_id"], "cus_1")
        kwargs = mock.checkout.Session.create.call_args.kwargs
        self.assertEqual(kwargs["line_items"], [{"price": "price_pro", "quantity": 1}])
        self.assertEqual(kwargs["subscription_data"]["metadata"]["plan"], "pro")
        self.assertEqual(kwargs["client_reference_id"], user["id"])

    def test_checkout_reuses_existing_customer(self) -> None:
        user, headers = self._login("a@example.com")
        local_store.set_stripe_customer_id(user["id"], "cus_existing")
        mock = self._mock_stripe()
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            self.client.post("/billing/checkout", json={"plan": "team"}, headers=headers)
        mock.Customer.create.assert_not_called()
        self.assertEqual(mock.checkout.Session.create.call_args.kwargs["customer"], "cus_existing")

    def test_webhook_rejects_bad_signature(self) -> None:
        mock = self._mock_stripe()
        mock.Webhook.construct_event.side_effect = ValueError("bad sig")
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.post("/billing/webhook", content=b"{}")
            self.assertEqual(r.status_code, 400)

    def test_webhook_checkout_completed_does_not_grant_paid_plan(self) -> None:
        user, _ = self._login("a@example.com")
        mock = self._mock_stripe()
        mock.Webhook.construct_event.return_value = _FakeStripeObject(
            {
                "id": "evt_checkout_1",
                "type": "checkout.session.completed",
                "data": {"object": {"client_reference_id": user["id"], "customer": "cus_1", "metadata": {"plan": "pro"}}},
            }
        )
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.post("/billing/webhook", content=b"{}")
            self.assertEqual(r.status_code, 200)
        stored = local_store.get_user_by_id(user["id"])
        self.assertEqual(stored["plan"], "free")
        self.assertEqual(stored["stripe_customer_id"], "cus_1")

    def test_webhook_subscription_lifecycle_syncs_plan_and_seats(self) -> None:
        user, _ = self._login("owner@example.com")
        local_store.set_user_plan("owner@example.com", "team")
        org = local_store.create_org("Acme", user["id"])
        mock = self._mock_stripe()
        sub = {
            "id": "sub_1",
            "status": "active",
            "customer": "cus_1",
            "metadata": {"plan": "team", "user_id": user["id"], "org_id": org["id"]},
            "items": {"data": [
                {"id": "si_base", "price": {"id": "price_team"}, "quantity": 1},
                {"id": "si_1", "price": {"id": "price_seat"}, "quantity": 3},
            ]},
        }
        mock.Subscription.list.return_value = _FakeStripeObject({"data": [sub]})
        mock.Webhook.construct_event.return_value = _FakeStripeObject(
            {
                "id": "evt_sub_active",
                "type": "customer.subscription.updated",
                "data": {"object": sub},
            }
        )
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            self.client.post("/billing/webhook", content=b"{}")
            self.assertEqual(
                local_store.org_seat_limit(local_store.get_org(org["id"])),
                local_store.TEAM_INCLUDED_MEMBERS + 3,
            )

            cancelled = dict(sub, status="canceled")
            mock.Subscription.list.return_value = _FakeStripeObject({"data": [cancelled]})
            mock.Webhook.construct_event.return_value = _FakeStripeObject(
                {
                    "id": "evt_sub_cancelled",
                    "type": "customer.subscription.deleted",
                    "data": {"object": cancelled},
                }
            )
            self.client.post("/billing/webhook", content=b"{}")

        self.assertEqual(local_store.get_user_by_id(user["id"])["plan"], "free")
        self.assertEqual(
            local_store.org_seat_limit(local_store.get_org(org["id"])),
            local_store.TEAM_INCLUDED_MEMBERS,
        )

    def test_direct_seat_mutation_is_removed_in_favor_of_stripe_portal(self) -> None:
        _, owner_headers = self._login("owner@example.com", "team")
        response = self.client.post("/billing/seats", json={"org_id": "org", "extra_seats": 2}, headers=owner_headers)
        self.assertEqual(response.status_code, 404)

    def test_portal_requires_billing_history(self) -> None:
        user, headers = self._login("a@example.com")
        mock = self._mock_stripe()
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.post("/billing/portal", headers=headers)
            self.assertEqual(r.status_code, 404)
            local_store.set_stripe_customer_id(user["id"], "cus_1")
            # Re-issue: the cached user dict in the token path reloads from DB.
            r = self.client.post("/billing/portal", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["url"], "https://billing.stripe.com/test")

    def test_billing_status_self_heals_a_missed_webhook_grant(self) -> None:
        """Reproduces the 2026-07-13 incident directly: payment succeeded on
        Stripe, but the local plan is still free because webhook delivery
        never landed. /billing/status must notice and fix it without anyone
        needing to click Resend in the Stripe dashboard."""
        user, headers = self._login("a@example.com")
        local_store.set_stripe_customer_id(user["id"], "cus_1")
        mock = self._mock_stripe()
        mock.Subscription.list.return_value = _FakeStripeObject(
            {
                "data": [
                    {
                        "id": "sub_1",
                        "status": "active",
                        "metadata": {"plan": "pro", "user_id": user["id"]},
                        "items": {"data": [{"id": "si_base", "price": {"id": "price_pro"}, "quantity": 1}]},
                    }
                ]
            }
        )
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.get("/billing/status", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["plan"], "pro")
        self.assertEqual(local_store.get_user_by_id(user["id"])["plan"], "pro")

    def test_webhook_event_id_is_idempotent(self) -> None:
        user, _ = self._login("a@example.com")
        mock = self._mock_stripe()
        sub = {
            "id": "sub_1",
            "status": "active",
            "customer": "cus_1",
            "metadata": {"user_id": user["id"]},
            "items": {"data": [{"price": {"id": "price_pro"}, "quantity": 1}]},
        }
        mock.Subscription.list.return_value = _FakeStripeObject({"data": [sub]})
        mock.Webhook.construct_event.return_value = _FakeStripeObject(
            {"id": "evt_replay", "type": "customer.subscription.updated", "data": {"object": sub}}
        )
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            first = self.client.post("/billing/webhook", content=b"{}")
            second = self.client.post("/billing/webhook", content=b"{}")
        self.assertFalse(first.json()["duplicate"])
        self.assertTrue(second.json()["duplicate"])
        self.assertEqual(mock.Subscription.list.call_count, 1)
        conn = local_store.get_conn()
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM stripe_webhook_events WHERE event_id='evt_replay'").fetchone()[0], 1)
        conn.close()

    def test_failed_webhook_reconciliation_is_retryable(self) -> None:
        user, _ = self._login("retry@example.com")
        mock = self._mock_stripe()
        sub = {
            "id": "sub_retry", "status": "active", "customer": "cus_retry",
            "metadata": {"user_id": user["id"]},
            "items": {"data": [{"price": {"id": "price_pro"}, "quantity": 1}]},
        }
        mock.Webhook.construct_event.return_value = _FakeStripeObject(
            {"id": "evt_retry", "type": "customer.subscription.updated", "data": {"object": sub}}
        )
        mock.Subscription.list.side_effect = [
            _FakeStripeError("temporary failure"),
            _FakeStripeObject({"data": [sub]}),
        ]
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            self.assertEqual(self.client.post("/billing/webhook", content=b"{}").status_code, 502)
            self.assertFalse(local_store.stripe_event_processed("evt_retry"))
            self.assertEqual(self.client.post("/billing/webhook", content=b"{}").status_code, 200)
        self.assertTrue(local_store.stripe_event_processed("evt_retry"))
        self.assertEqual(local_store.get_user_by_id(user["id"])["plan"], "pro")

    def test_canceling_one_subscription_does_not_revoke_another_active_subscription(self) -> None:
        user, _ = self._login("multi@example.com")
        mock = self._mock_stripe()
        active_pro = {
            "id": "sub_pro", "status": "active", "customer": "cus_multi",
            "metadata": {"user_id": user["id"]},
            "items": {"data": [{"price": {"id": "price_pro"}, "quantity": 1}]},
        }
        canceled_team = {
            "id": "sub_team", "status": "canceled", "customer": "cus_multi",
            "metadata": {"user_id": user["id"]},
            "items": {"data": [{"price": {"id": "price_team"}, "quantity": 1}]},
        }
        mock.Subscription.list.return_value = _FakeStripeObject({"data": [active_pro, canceled_team]})
        mock.Webhook.construct_event.return_value = _FakeStripeObject(
            {"id": "evt_multi", "type": "customer.subscription.deleted", "data": {"object": canceled_team}}
        )
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            response = self.client.post("/billing/webhook", content=b"{}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(local_store.get_user_by_id(user["id"])["plan"], "pro")

    def test_team_workspace_binding_survives_portal_seat_webhooks(self) -> None:
        user, headers = self._login("workspace@example.com")
        mock = self._mock_stripe()
        sub = {
            "id": "sub_workspace", "status": "active", "customer": "cus_workspace",
            "metadata": {"user_id": user["id"]},
            "items": {"data": [{"price": {"id": "price_team"}, "quantity": 1}]},
        }
        mock.Subscription.list.return_value = _FakeStripeObject({"data": [sub]})
        mock.Webhook.construct_event.return_value = _FakeStripeObject(
            {"id": "evt_workspace_create", "type": "customer.subscription.created", "data": {"object": sub}}
        )
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            self.assertEqual(self.client.post("/billing/webhook", content=b"{}").status_code, 200)
            created = self.client.post("/orgs", json={"name": "Workspace"}, headers=headers)
            self.assertEqual(created.status_code, 200)
            org_id = created.json()["org"]["id"]
            updated = {
                **sub,
                "items": {"data": [
                    {"price": {"id": "price_team"}, "quantity": 1},
                    {"price": {"id": "price_seat"}, "quantity": 2},
                ]},
            }
            mock.Subscription.list.return_value = _FakeStripeObject({"data": [updated]})
            mock.Webhook.construct_event.return_value = _FakeStripeObject(
                {"id": "evt_workspace_seats", "type": "customer.subscription.updated", "data": {"object": updated}}
            )
            self.assertEqual(self.client.post("/billing/webhook", content=b"{}").status_code, 200)
        self.assertEqual(
            local_store.org_seat_limit(local_store.get_org(org_id)),
            local_store.TEAM_INCLUDED_MEMBERS + 2,
        )

    def test_unrecognized_active_price_fails_without_revoking_access(self) -> None:
        user, _ = self._login("unknown-price@example.com", "pro")
        mock = self._mock_stripe()
        sub = {
            "id": "sub_unknown", "status": "active", "customer": "cus_unknown",
            "metadata": {"user_id": user["id"]},
            "items": {"data": [{"price": {"id": "price_not_configured"}, "quantity": 1}]},
        }
        mock.Subscription.list.return_value = _FakeStripeObject({"data": [sub]})
        mock.Webhook.construct_event.return_value = _FakeStripeObject(
            {"id": "evt_unknown_price", "type": "customer.subscription.updated", "data": {"object": sub}}
        )
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            response = self.client.post("/billing/webhook", content=b"{}")
        self.assertEqual(response.status_code, 502)
        self.assertFalse(local_store.stripe_event_processed("evt_unknown_price"))
        self.assertEqual(local_store.get_user_by_id(user["id"])["plan"], "pro")

    def test_billing_status_never_auto_downgrades(self) -> None:
        """Reconciliation only heals a missed grant; it must never read an
        empty/errored Stripe response as grounds to revoke a plan someone
        is already correctly on -- cancellation must always go through the
        webhook, never a side effect of a status-check hiccup."""
        user, headers = self._login("a@example.com", "pro")
        local_store.set_stripe_customer_id(user["id"], "cus_1")
        mock = self._mock_stripe()
        mock.Subscription.list.return_value = _FakeStripeObject({"data": []})
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.get("/billing/status", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["plan"], "pro")
        self.assertEqual(local_store.get_user_by_id(user["id"])["plan"], "pro")

    def test_billing_status_swallows_stripe_errors_during_reconciliation(self) -> None:
        user, headers = self._login("a@example.com")
        local_store.set_stripe_customer_id(user["id"], "cus_1")
        mock = self._mock_stripe()
        mock.Subscription.list.side_effect = _FakeStripeError("stripe is down")
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.get("/billing/status", headers=headers)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.json()["plan"], "free")

    def test_checkout_surfaces_a_clean_error_on_stripe_failure(self) -> None:
        _, headers = self._login("a@example.com")
        mock = self._mock_stripe()
        mock.checkout.Session.create.side_effect = _FakeStripeError("your card was declined")
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.post("/billing/checkout", json={"plan": "pro"}, headers=headers)
            self.assertEqual(r.status_code, 502)
            self.assertIn("your card was declined", r.json()["detail"])

    def test_portal_surfaces_a_clean_error_on_stripe_failure(self) -> None:
        user, headers = self._login("a@example.com")
        local_store.set_stripe_customer_id(user["id"], "cus_1")
        mock = self._mock_stripe()
        mock.billing_portal.Session.create.side_effect = _FakeStripeError("temporarily unavailable")
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            r = self.client.post("/billing/portal", headers=headers)
            self.assertEqual(r.status_code, 502)


if __name__ == "__main__":
    unittest.main()
