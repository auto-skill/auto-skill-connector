from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import billing_api
import local_store
import reconcile_billing
from test_billing import STRIPE_ENV, _FakeStripeObject


class BillingReconciliationScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.old_db_path = local_store.DB_PATH
        local_store.DB_PATH = Path(self.tmp.name) / "local_skills.db"
        local_store.init_db()
        self.user = local_store.get_or_create_user("reconcile@example.com", "Reconcile", None)
        local_store.set_stripe_customer_id(self.user["id"], "cus_reconcile")

    def tearDown(self) -> None:
        local_store.DB_PATH = self.old_db_path

    def _stripe(self) -> MagicMock:
        mock = MagicMock()
        mock.Subscription.list.return_value = _FakeStripeObject(
            {
                "data": [
                    {
                        "id": "sub_reconcile",
                        "status": "active",
                        "customer": "cus_reconcile",
                        "metadata": {"user_id": self.user["id"]},
                        "items": {"data": [{"price": {"id": "price_pro"}, "quantity": 1}]},
                    }
                ]
            }
        )
        return mock

    def test_dry_run_then_apply(self) -> None:
        mock = self._stripe()
        with patch.object(billing_api, "stripe", mock), patch.dict("os.environ", STRIPE_ENV):
            self.assertEqual(reconcile_billing.main([]), 0)
            self.assertEqual(local_store.get_user_by_id(self.user["id"])["plan"], "free")
            self.assertEqual(reconcile_billing.main(["--apply"]), 0)
        self.assertEqual(local_store.get_user_by_id(self.user["id"])["plan"], "pro")


if __name__ == "__main__":
    unittest.main()
