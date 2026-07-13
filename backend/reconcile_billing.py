"""Founder-run Stripe-to-SQLite reconciliation; dry-run unless --apply is passed."""
from __future__ import annotations

import argparse
import sys

import billing_api
import local_store as store


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="write Stripe's current paid state to SQLite")
    args = parser.parse_args(argv)
    if not billing_api.billing_configured():
        print("billing reconciliation refused: Stripe configuration is incomplete", file=sys.stderr)
        return 2

    store.init_db()
    client = billing_api._stripe()
    failures = 0
    users = store.list_users_with_stripe_customer()
    for user in users:
        customer_id = user["stripe_customer_id"]
        try:
            response = billing_api._call_stripe(
                client.Subscription.list, customer=customer_id, status="all", limit=100
            ).to_dict()
            subscriptions = response.get("data", [])
            snapshots = [
                snapshot
                for subscription in subscriptions
                if (snapshot := billing_api._subscription_snapshot(subscription))
            ]
            if any(
                sub.get("status") in {"active", "trialing", "past_due"}
                and billing_api._plan_from_subscription(sub) is None
                for sub in subscriptions
            ):
                raise ValueError("active subscription has an unrecognized price")
            active = [
                snapshot
                for snapshot in snapshots
                if snapshot["status"] in {"active", "trialing", "past_due"}
            ]
            stripe_plan = max(
                (snapshot["plan"] for snapshot in active),
                key=lambda plan: store._PLAN_RANK[plan],
                default="free",
            )
            print(
                f"{user['email']}: local_paid={user.get('plan') or 'free'} "
                f"stripe_paid={stripe_plan} subscriptions={len(snapshots)} "
                f"mode={'apply' if args.apply else 'dry-run'}"
            )
            if args.apply:
                store.replace_stripe_customer_subscriptions(user["id"], customer_id, snapshots)
        except Exception as exc:
            failures += 1
            print(f"{user['email']}: FAILED: {exc}", file=sys.stderr)

    print(f"billing reconciliation: users={len(users)}, failures={failures}, applied={args.apply}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
