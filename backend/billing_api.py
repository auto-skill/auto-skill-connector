"""Stripe billing: checkout, seats, portal, and the webhook that flips plans.

Stripe is the source of truth for subscription state; SQLite only mirrors the
resulting plan and seat_limit (see local_store.USER_COLUMN_DEFAULTS's
stripe_customer_id note). No card data ever touches this backend -- checkout
and payment management happen on Stripe-hosted pages, and the webhook is the
one write path back into our plan state. /admin/set-plan remains the manual
override for comps and support.

Endpoints:
  GET  /billing/status    -> configured flag + caller's plan (dashboard)
  POST /billing/checkout  -> Stripe Checkout session URL for pro or team
  POST /billing/seats     -> org owner sets paid extra seats on the team sub
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

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field

import auth
import local_store as store

try:
    import stripe
except ImportError:  # pragma: no cover - exercised only on unbuilt hosts
    stripe = None

router = APIRouter()

DASHBOARD_URL = os.getenv("SIGNUP_DASHBOARD_URL", "https://autoskill.dev/dashboard.html")

CHECKOUT_PLANS = {"pro": "STRIPE_PRICE_PRO", "team": "STRIPE_PRICE_TEAM"}


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


def _customer_id_for(user: dict) -> str:
    """Reuse the stored Stripe customer, creating one on first billing touch."""
    existing = user.get("stripe_customer_id")
    if existing:
        return existing
    customer = _stripe().Customer.create(email=user["email"], metadata={"user_id": user["id"]})
    store.set_stripe_customer_id(user["id"], customer["id"])
    return customer["id"]


@router.get("/billing/status")
async def billing_status(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {
        "configured": billing_configured(),
        "plan": user.get("plan") or "free",
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
    session = client.checkout.Session.create(
        mode="subscription",
        customer=_customer_id_for(user),
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


class SeatsRequest(BaseModel):
    org_id: str
    # 0 removes all paid seats (back to the included count).
    extra_seats: int = Field(ge=0, le=995)


def _active_team_subscription(client, customer_id: str) -> dict | None:
    # See billing_webhook's to_dict() note: list responses are SDK objects
    # too, and don't support .get() the way the rest of this module assumes.
    subs = client.Subscription.list(customer=customer_id, status="active", limit=10).to_dict()
    for sub in subs.get("data", []):
        if (sub.get("metadata") or {}).get("plan") == "team":
            return sub
    return None


@router.post("/billing/seats")
async def billing_seats(body: SeatsRequest, authorization: str | None = Header(None)):
    """Set the paid extra-seat quantity for a workspace. The seat_limit is
    updated optimistically here and again by the subscription webhook, so a
    missed webhook can't leave a paying org locked out of its seats."""
    user = _require_user(authorization)
    if store.org_role(body.org_id, user["id"]) != "owner":
        raise HTTPException(status_code=403, detail="org owner required")
    client = _stripe()
    customer_id = user.get("stripe_customer_id")
    sub = _active_team_subscription(client, customer_id) if customer_id else None
    if sub is None:
        raise HTTPException(status_code=402, detail="no active team subscription; subscribe to team first")

    seat_price = os.environ["STRIPE_PRICE_TEAM_SEAT"]
    seat_item = next(
        (item for item in sub["items"]["data"] if item["price"]["id"] == seat_price), None
    )
    if seat_item is None and body.extra_seats > 0:
        items = [{"price": seat_price, "quantity": body.extra_seats}]
    elif seat_item is not None:
        items = [{"id": seat_item["id"], "quantity": body.extra_seats}]
    else:
        items = []
    if items:
        client.Subscription.modify(sub["id"], items=items, metadata={**(sub.get("metadata") or {}), "org_id": body.org_id})
    seat_limit = store.TEAM_INCLUDED_MEMBERS + body.extra_seats
    store.set_org_seat_limit(body.org_id, seat_limit)
    store.record_org_audit(body.org_id, user["id"], "seats_changed", str(seat_limit))
    return {"ok": True, "org_id": body.org_id, "seat_limit": seat_limit}


@router.post("/billing/portal")
async def billing_portal(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    customer_id = user.get("stripe_customer_id")
    if not customer_id:
        raise HTTPException(status_code=404, detail="no billing history for this account")
    session = _stripe().billing_portal.Session.create(customer=customer_id, return_url=DASHBOARD_URL)
    return {"url": session["url"]}


def _user_id_from_subscription(sub: dict) -> str | None:
    user_id = (sub.get("metadata") or {}).get("user_id")
    if user_id:
        return user_id
    user = store.get_user_by_stripe_customer(sub.get("customer") or "")
    return user["id"] if user else None


def _sync_subscription(sub: dict) -> None:
    """Mirror one subscription's state into plan/seat_limit."""
    user_id = _user_id_from_subscription(sub)
    if user_id is None:
        return
    plan = (sub.get("metadata") or {}).get("plan")
    if plan not in ("pro", "team"):
        return
    status = sub.get("status")
    org_id = (sub.get("metadata") or {}).get("org_id")
    if status in ("active", "trialing", "past_due"):
        store.set_user_plan_by_id(user_id, plan)
        if plan == "team" and org_id:
            seat_price = os.getenv("STRIPE_PRICE_TEAM_SEAT", "")
            extra = sum(
                int(item.get("quantity") or 0)
                for item in (sub.get("items") or {}).get("data", [])
                if (item.get("price") or {}).get("id") == seat_price
            )
            store.set_org_seat_limit(org_id, store.TEAM_INCLUDED_MEMBERS + extra)
    else:
        # canceled / unpaid / incomplete_expired all mean no live entitlement.
        store.set_user_plan_by_id(user_id, "free")
        if org_id:
            store.set_org_seat_limit(org_id, None)


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
    kind = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}

    if kind == "checkout.session.completed":
        user_id = obj.get("client_reference_id") or (obj.get("metadata") or {}).get("user_id")
        plan = (obj.get("metadata") or {}).get("plan")
        if user_id and plan in ("pro", "team"):
            store.set_user_plan_by_id(user_id, plan)
    elif kind in ("customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted"):
        _sync_subscription(obj)

    return {"ok": True}
