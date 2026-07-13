# Auto-Skill Plans

Three plans. Billing runs through Stripe (`backend/billing_api.py`): the
dashboard starts a Stripe-hosted Checkout session (`POST /billing/checkout`),
the signature-verified webhook (`POST /billing/webhook`) mirrors subscription
state into `plan`/`seat_limit`, and the customer portal
(`POST /billing/portal`) handles cancels and card changes. Extra team seats
are `POST /billing/seats` ($10/seat/month). `POST /admin/set-plan` and
`POST /admin/set-org-seats` remain the manual override for comps and support.
Dollar amounts live only in the Stripe price objects, never in code.

## Free — $0

For individual users trying Auto-Skill.

| Feature | Where it lives |
| --- | --- |
| Claude, Codex, Cursor, and MCP support | connector adapters |
| Deterministic skill recommendations | `POST /route` (deterministic for every plan) |
| 1,000 routes/month | `AUTOSKILL_FREE_ROUTES_PER_MONTH` (default 1000, 0 disables); over-quota routes degrade to a `tier: none` payload with upgrade info |
| Public verified skill catalog | `GET /skills-catalog` |
| Basic install and compatibility checks | connector-side |
| Up to 10 personal private skills | `AUTOSKILL_FREE_PRIVATE_SKILLS` (default 10); the 11th `POST /private-skills` returns 402 |
| Basic safety and content verification | quality gates, risk scores, context guard |
| No message-content storage | route events are metadata-only (`ROUTE_EVENT_WRITE_COLUMNS` allowlist) |

## Pro — $7/month

For serious individual developers.

| Feature | Where it lives |
| --- | --- |
| Unlimited fair-use routing | marketed unlimited; internal abuse cap `AUTOSKILL_PRO_ROUTES_PER_MONTH` (default 15,000, never shown on pricing surfaces) |
| Client-LLM-assisted recommendations | connector-side (client's own LLM; no server gating needed) |
| Unlimited private skills | free-tier cap does not apply to pro/team |
| Skill version pinning and rollback | `GET /skills/{id}/versions`, `GET/POST/DELETE /pins`; `/route` serves the pinned version's content |
| Change alerts when a skill updates | `GET/POST/DELETE /watches`, `GET /alerts`, `POST /alerts/{id}/ack` |
| Personal favorites and collections | `/favorites` (all plans), `/collections` (pro+) |
| Recommendation/outcome analytics | `GET /analytics` |
| Custom exclusions and routing preferences | `GET/PUT /preferences`, applied inside `POST /route` |
| Cross-agent synchronization | preferences/pins/watches are account-level server state, so every connected agent applies them |
| Priority limits and faster support | operational, not in code |

Pro-only endpoints return `402` for free accounts.

## Team — $49/month per workspace

Includes up to 5 members, then $10 per additional member (self-serve via
`POST /billing/seats`; `POST /admin/set-org-seats` remains the manual
override).

| Feature | Where it lives |
| --- | --- |
| Shared organization skill library | `/orgs/{id}/skills` (org creation requires the team plan) |
| Approved team-standard skills routed first | org skills sort ahead of personal skills in `/route` |
| Owner approval before publishing skills | member submissions land `pending` and stay out of routing until `POST /orgs/{id}/skills/{sid}/approve` |
| Allow/block policies | `/orgs/{id}/policies`; blocks always exclude, any allow rows restrict public-catalog routing to the allowlist |
| Shared collections | `POST /collections` with `org_id`; members view/add, owner deletes |
| Install and change audit log | `GET /orgs/{id}/audit` (owner-only): installs, membership, skill, policy, and seat changes |
| Team usage and outcome analytics | `GET /orgs/{id}/analytics` (owner-only), includes pooled quota status |
| Pooled route quota | workspace shares `seat_limit x AUTOSKILL_PRO_ROUTES_PER_MONTH` per month across members |
| Member management | `/orgs/{id}/members`; `AUTOSKILL_TEAM_INCLUDED_MEMBERS` (default 5) seats included, 402 beyond until seats are raised |

## Env var summary

All metering knobs are env-overridable; `0` disables that limit (for
self-hosted deployments):

- `AUTOSKILL_FREE_ROUTES_PER_MONTH` — free monthly route quota (default 1000)
- `AUTOSKILL_PRO_ROUTES_PER_MONTH` — internal pro/team fair-use cap (default 15000)
- `AUTOSKILL_FREE_PRIVATE_SKILLS` — free private-skill cap (default 10)
- `AUTOSKILL_TEAM_INCLUDED_MEMBERS` — seats included per workspace (default 5)

## Stripe configuration

Billing endpoints return 503 (and the dashboard shows a manual-upgrade note)
until all five are set on the API host:

- `STRIPE_SECRET_KEY` — restricted or standard secret key
- `STRIPE_WEBHOOK_SECRET` — signing secret for the `/billing/webhook` endpoint
- `STRIPE_PRICE_PRO` — recurring monthly price id for Pro ($7)
- `STRIPE_PRICE_TEAM` — recurring monthly price id for Team ($49)
- `STRIPE_PRICE_TEAM_SEAT` — recurring monthly price id per extra seat ($10)

Stripe dashboard setup: create products Pro/Team/Team extra seat with those
monthly prices, then add a webhook endpoint for
`https://skills.autoskill.dev/billing/webhook` subscribed to
`checkout.session.completed`, `customer.subscription.created`,
`customer.subscription.updated`, and `customer.subscription.deleted`.
