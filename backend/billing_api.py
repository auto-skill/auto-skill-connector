"""Stripe billing: checkout, seats, portal, and the webhook that flips plans.

Stripe is the source of truth for subscription state; SQLite only mirrors the
resulting paid plan and seat_limit (see local_store's Stripe subscription
snapshot). No card data ever touches this backend -- checkout
and payment management happen on Stripe-hosted pages, and the webhook is the
one write path back into paid-plan state. Complimentary access is stored
separately and never overwrites Stripe state.

Endpoints:
  GET  /billing/status    -> configured flag + caller's plan (dashboard)
  POST /billing/checkout  -> Stripe Checkout session URL for pro or team
  POST /billing/portal    -> Stripe customer portal session URL (manage/cancel)
  POST /billing/webhook   -> Stripe events -> plan/seat sync (signature-verified,
                             the only unauthenticated billing route)

Configuration (all required before checkout works; /billing/status reports
configured=false until then):
  STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
  STRIPE_PRICE_PRO ($7/mo), STRIPE_PRICE_TEAM ($49/mo workspace),
  STRIPE_PRICE_TEAM_SEAT ($10/mo per extra seat)
"""
import os
import threading

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel

import auth
import local_store as store

try:
    import stripe
except ImportError:  # pragma: no cover - exercised only on unbuilt hosts
    stripe = None

router = APIRouter()

DASHBOARD_URL = os.getenv("SIGNUP_DASHBOARD_URL", "https://autoskill.dev/dashboard.html")

CHECKOUT_PLANS = {"pro": "STRIPE_PRICE_PRO", "team": "STRIPE_PRICE_TEAM"}
_WEBHOOK_LOCK = threading.Lock()


def _require_user(authorization: str | None) -> dict:
    user = auth.user_from_authorization_header(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return user


def billing_configured() -> bool:
    return bool(
        stripe
        and os.getenv("STRIPE_SECRET_KEY")
        and os.getenv("STRIPE_WEBHOOK_SECRET")
        and os.getenv("STRIPE_PRICE_PRO")
        and os.getenv("STRIPE_PRICE_TEAM")
        and os.getenv("STRIPE_PRICE_TEAM_SEAT")
    )


def _stripe():
    if not billing_configured():
        raise HTTPException(status_code=503, detail="billing is not configured on this server")
    stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    return stripe


def _call_stripe(fn, *args, **kwargs):
    """Run one outward Stripe API call, turning a network/API hiccup into a
    clean 502 instead of an unhandled 500 -- the 2026-07-13 incident was a
    different bug (a local .get() crash), but this closes the adjacent gap:
    nothing here was handling Stripe itself being slow, rate-limited, or
    briefly unreachable."""
    try:
        return fn(*args, **kwargs)
    except stripe.StripeError as exc:
        raise HTTPException(status_code=502, detail=f"Stripe request failed: {exc.user_message or str(exc)}")


def _customer_id_for(user: dict) -> str:
    """Reuse the stored Stripe customer, creating one on first billing touch."""
    existing = user.get("stripe_customer_id")
    if existing:
        return existing
    client = _stripe()
    customer = _call_stripe(client.Customer.create, email=user["email"], metadata={"user_id": user["id"]})
    store.set_stripe_customer_id(user["id"], customer["id"])
    return customer["id"]


def _reconcile_missed_grant(user: dict) -> dict:
    """Self-heal a paid-but-still-free account: this is exactly the failure
    mode from the 2026-07-13 incident (webhook delivery failed silently while
    the payment succeeded), so the dashboard's own status check doubles as
    the backstop instead of depending solely on webhook delivery ever
    working. Only heals a MISSED GRANT -- never auto-downgrades, since a real
    cancellation should always go through the webhook, and a transient
    Stripe API hiccup here must never look like a revoked entitlement."""
    if (user.get("paid_plan") or "free") != "free" or not user.get("stripe_customer_id") or not billing_configured():
        return user
    try:
        client = _stripe()
        _reconcile_customer_subscriptions(client, user["id"], user["stripe_customer_id"])
        refreshed = store.get_user_by_id(user["id"])
        return store.access_details_for_user(refreshed) if refreshed else user
    except Exception:
        pass  # best-effort; a reconciliation failure must not break /billing/status itself
    return user


@router.get("/billing/status")
async def billing_status(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    user = _reconcile_missed_grant(user)
    return {
        "configured": billing_configured(),
        "plan": user.get("plan") or "free",
        "paid_plan": user.get("paid_plan") or "free",
        "plan_source": user.get("plan_source") or "free",
        "complimentary_expires_at": user.get("complimentary_expires_at"),
        "has_stripe_customer": bool(user.get("stripe_customer_id")),
    }


class CheckoutRequest(BaseModel):
    plan: str


@router.post("/billing/checkout")
async def billing_checkout(body: CheckoutRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    if body.plan not in CHECKOUT_PLANS:
        raise HTTPException(status_code=400, detail="plan must be pro or team")
    client = _stripe()
    customer_id = _customer_id_for(user)
    existing = _call_stripe(client.Subscription.list, customer=customer_id, status="all", limit=100).to_dict()
    if any((sub.get("status") in {"active", "trialing", "past_due"}) for sub in existing.get("data", [])):
        raise HTTPException(status_code=409, detail="an active subscription already exists; use the billing portal")
    session = _call_stripe(
        client.checkout.Session.create,
        mode="subscription",
        customer=customer_id,
        line_items=[{"price": os.environ[CHECKOUT_PLANS[body.plan]], "quantity": 1}],
        # The webhook reads plan/user_id from subscription metadata for every
        # later lifecycle event, so they must live on the subscription, not
        # only on this one-shot session.
        subscription_data={"metadata": {"user_id": user["id"], "plan": body.plan}},
        client_reference_id=user["id"],
        metadata={"user_id": user["id"], "plan": body.plan},
        success_url=f"{DASHBOARD_URL}#billing=success",
        cancel_url=f"{DASHBOARD_URL}#billing=cancelled",
        allow_promotion_codes=True,
    )
    return {"url": session["url"]}


@router.post("/billing/portal")
async def billing_portal(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(status_code=404, detail="no billing history for this account")
    client = _stripe()
    session = _call_stripe(client.billing_portal.Session.create, customer=customer_id, return_url=DASHBOARD_URL)
    return {"url": session["url"]}


def _user_id_from_subscription(sub: dict) -> str | None:
    user_id = (sub.get("metadata") or {}).get("user_id")
    if user_id:
        return user_id
    user = store.get_user_by_stripe_customer(sub.get("customer") or "")
    return user["id"] if user else None


def _plan_from_subscription(sub: dict) -> str | None:
    prices = {
        (item.get("price") or {}).get("id")
        for item in (sub.get("items") or {}).get("data", [])
    }
    if os.getenv("STRIPE_PRICE_TEAM") in prices:
        return "team"
    if os.getenv("STRIPE_PRICE_PRO") in prices:
        return "pro"
    return None


def _subscription_snapshot(sub: dict) -> dict | None:
    plan = _plan_from_subscription(sub)
    subscription_id = sub.get("id")
    if not plan or not subscription_id:
        return None
    org_id = (sub.get("metadata") or {}).get("org_id") or store.stripe_subscription_org_id(subscription_id)
    extra = 0
    if plan == "team":
        seat_price = os.getenv("STRIPE_PRICE_TEAM_SEAT", "")
        extra = sum(
            int(item.get("quantity") or 0)
            for item in (sub.get("items") or {}).get("data", [])
            if (item.get("price") or {}).get("id") == seat_price
        )
    return {
        "subscription_id": subscription_id,
        "plan": plan,
        "status": sub.get("status") or "unknown",
        "org_id": org_id,
        "seat_limit": store.TEAM_INCLUDED_MEMBERS + extra if plan == "team" and org_id else None,
    }


def _reconcile_customer_subscriptions(client, user_id: str, customer_id: str) -> str:
    response = _call_stripe(client.Subscription.list, customer=customer_id, status="all", limit=100).to_dict()
    subscriptions = response.get("data", [])
    snapshots = [snapshot for sub in subscriptions if (snapshot := _subscription_snapshot(sub))]
    if any(
        sub.get("status") in {"active", "trialing", "past_due"} and _plan_from_subscription(sub) is None
        for sub in subscriptions
    ):
        raise HTTPException(status_code=502, detail="active Stripe subscription has an unrecognized price")
    return store.replace_stripe_customer_subscriptions(user_id, customer_id, snapshots)


@router.post("/billing/webhook")
async def billing_webhook(request: Request, stripe_signature: str | None = Header(None, alias="stripe-signature")):
    """Publicly reachable but authenticated by Stripe's webhook signature; an
    unverifiable payload is rejected before any state changes."""
    if not billing_configured():
        raise HTTPException(status_code=503, detail="billing is not configured on this server")
    payload = await request.body()
    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature or "", os.environ["STRIPE_WEBHOOK_SECRET"])
    except Exception:
        raise HTTPException(status_code=400, detail="invalid webhook signature")

    # construct_event returns SDK objects, not plain dicts -- StripeObject
    # supports attribute/item access but not .get(), which every handler
    # below relies on. to_dict() recursively converts the whole tree once,
    # up front, so the rest of this module can stay plain-dict code.
    event = event.to_dict()
    event_id = str(event.get("id") or "").strip()
    if not event_id:
        raise HTTPException(status_code=400, detail="Stripe event id is required")
    kind = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    with _WEBHOOK_LOCK:
        if store.stripe_event_processed(event_id):
            return {"ok": True, "duplicate": True}
        if kind == "checkout.session.completed":
            user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
            customer_id = obj.get("customer")
            if user_id and customer_id:
                store.set_stripe_customer_id(user_id, customer_id)
        elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
            user_id = _user_id_from_subscription(obj)
            customer_id = obj.get("customer")
            if user_id and customer_id:
                _reconcile_customer_subscriptions(_stripe(), user_id, customer_id)
        store.record_stripe_event(event_id, kind)

    return {"ok": True, "duplicate": False}
