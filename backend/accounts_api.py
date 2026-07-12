"""Accounts API: OAuth login (Google/GitHub) and per-user data (favorites,
installs, private skills). Self-contained APIRouter so scraper.py only needs:
    from accounts_api import router as accounts_router
    app.include_router(accounts_router)

All account data lives in the same local SQLite store (local_store.py) that
already backs the rest of the backend -- see auth.py's module docstring for
why there is no external database or auth framework here.

Endpoints:
  GET  /auth/{provider}/start?flow=cli|web|mcp&...  -> redirect into provider OAuth
  GET  /auth/{provider}/callback?code&state         -> redirect back per flow
  GET  /auth/whoami                           -> current user
  POST /auth/logout                           -> revoke the bearer token
  GET  /runs                                  -> current user's recent route_events (dashboard metrics)
  GET  /skills-catalog                        -> paginated skill browse (account-only)
  GET  /admin/stats                           -> operator-only: all users' stats + recent activity feed
  POST /admin/set-plan                        -> operator-only: manual free/pro/team plan flip
  POST /admin/set-org-seats                   -> operator-only: manual paid-seat bump for a team workspace
  GET/POST/DELETE /favorites[/{skill_id}]     -> per-user favorited skills
  GET/POST        /installs                   -> per-user install history (audited to the user's orgs)
  GET/POST/DELETE /private-skills[/{id}]      -> per-user private skill submissions (free plan capped)
  GET  /skills/{id}/versions                  -> content-hash version history for a catalog skill
  GET/POST/DELETE /pins[/{skill_id}]          -> pro: pin a skill to a version; routing serves the pinned content
  GET/POST/DELETE /watches[/{skill_id}]       -> pro: subscribe to change alerts for a skill
  GET  /alerts, POST /alerts/{skill_id}/ack   -> pro: watched skills whose content changed since last ack
  GET/POST/DELETE /collections[/{id}]         -> pro: personal collections; team: org-shared collections
  GET/POST/DELETE /collections/{id}/skills[/{sid}] -> skills in a collection
  GET/PUT /preferences                        -> pro: routing exclusions, applied server-side on every client
  GET  /analytics                             -> pro: per-user recommendation/outcome rollup
  GET/POST /orgs                              -> team-plan orgs (create requires team plan)
  GET/POST/DELETE /orgs/{id}/members[/{uid}]  -> org membership (owner-managed; members may leave)
  GET/POST/DELETE /orgs/{id}/skills[/{sid}]   -> org-shared skills, routed first for all members
                                                 (members submit as pending; owner approves)
  POST /orgs/{id}/skills/{sid}/approve        -> owner approves a member's pending submission
  GET/POST/DELETE /orgs/{id}/policies[/{sid}] -> owner-managed allow/block routing policies
  GET  /orgs/{id}/audit                       -> owner: install and change audit log
  GET  /orgs/{id}/analytics                   -> owner: team usage and outcome analytics
  GET  /signup                                -> account-required landing page (see scraper.py's account guard)
  GET  /account                               -> post-login confirmation, links out to the site's dashboard
"""
import json
import os
from urllib.parse import parse_qsl, quote, urlencode, urlparse, urlsplit, urlunparse, urlunsplit

from fastapi import APIRouter, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from pydantic import BaseModel, Field

import auth
import local_store as store
import mcp_oauth

router = APIRouter()

# Where the site's own full account UX (runs, connector URL) lives --
# autoskill.dev's root domain now actually serves the site (confirmed live).
# /signup's login lands on /account (this same backend domain) first, not
# here directly -- skills.autoskill.dev's own "/" is an internal admin
# scraper panel, not a page to show regular signed-up users, so /account
# exists as a small in-between confirmation before linking out to this.
SIGNUP_DASHBOARD_URL = os.getenv("SIGNUP_DASHBOARD_URL", "https://autoskill.dev/dashboard.html")

_DEFAULT_WEB_RETURN_ORIGINS = {
    "https://autoskill.dev",
    "https://www.autoskill.dev",
    "https://auto-skill-site.pages.dev",
    "https://auto-skill-site.vercel.app",
    "https://skills.autoskill.dev",
    "https://skills.avalahome.com",
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:8000",
    "http://127.0.0.1:8000",
}


def _append_query(url: str, **params: str | None) -> str:
    parsed = urlparse(url)
    query = parse_qsl(parsed.query)
    query.extend((k, v) for k, v in params.items() if v is not None)
    return urlunparse(parsed._replace(query=urlencode(query)))


def _require_user(authorization: str | None) -> dict:
    user = auth.user_from_authorization_header(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return user


def _admin_emails() -> set[str]:
    return {e.strip().lower() for e in os.environ.get("ADMIN_EMAILS", "").split(",") if e.strip()}


def _require_admin(authorization: str | None) -> dict:
    """Same bearer check as every other account endpoint, plus an email
    allowlist -- there's no is_admin column, and this dashboard only ever
    needs to serve the product's own operator(s), not a general role system."""
    user = _require_user(authorization)
    if (user.get("email") or "").lower() not in _admin_emails():
        raise HTTPException(status_code=403, detail="not an admin account")
    return user


def _require_paid_plan(user: dict, feature: str) -> None:
    if (user.get("plan") or "free") == "free":
        raise HTTPException(status_code=402, detail=f"{feature} requires the pro plan")


def _origin_for_url(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), "", "", ""))


def _allowed_web_return_origins() -> set[str]:
    configured = os.environ.get("AUTO_SKILL_DASHBOARD_ORIGINS", "")
    values = [v.strip() for v in configured.split(",") if v.strip()]
    origins = {_origin_for_url(v) for v in values} if values else set(_DEFAULT_WEB_RETURN_ORIGINS)
    return {origin for origin in origins if origin}


def _safe_return_to(value: str) -> str:
    if not value or len(value) > 600:
        raise HTTPException(status_code=400, detail="invalid return_to")
    origin = _origin_for_url(value)
    if origin is None:
        raise HTTPException(status_code=400, detail="invalid return_to")
    if origin not in _allowed_web_return_origins():
        raise HTTPException(status_code=400, detail="return_to origin is not allowed")
    return value


def _with_token_fragment(return_to: str, token: str) -> str:
    parsed = urlsplit(return_to)
    params = dict(parse_qsl(parsed.fragment, keep_blank_values=True))
    params["token"] = token
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, parsed.query, urlencode(params)))


def _login_host(request: Request) -> str:
    """The hostname the caller actually used, validated against
    auth.ALLOWED_LOGIN_HOSTS -- this picks which OAuth client id/secret (for
    GitHub, a whole separate app) and which redirect_uri get used, so it must
    never come from an unvalidated header."""
    host = (request.headers.get("host") or "").split(":")[0].lower()
    if host not in auth.ALLOWED_LOGIN_HOSTS:
        raise HTTPException(status_code=400, detail=f"login is not served on host {host!r}")
    return host


@router.get("/auth/{provider}/start")
async def auth_start(
    request: Request,
    provider: str,
    port: int | None = None,
    flow: str = "cli",
    login_state: str | None = None,
    return_to: str | None = None,
):
    """Three login flavors share this one Google/GitHub handshake:
    flow=cli (default) is the `auto-skill login` loopback flow (needs `port`);
    flow=web is the auto-skill-site dashboard login (needs `return_to`);
    flow=mcp is the hosted MCP connector's OAuth authorize step (needs
    `login_state`, see mcp_oauth.py). See auth_callback for the payoff."""
    if provider not in auth.PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    host = _login_host(request)
    client_id = auth.PROVIDERS[provider]["client_id"] if provider == "google" else auth.github_credentials_for_host(host)["client_id"]
    if not client_id:
        raise HTTPException(status_code=503, detail=f"{provider} login is not configured on this server")
    flow = (flow or "cli").strip().lower()
    if flow == "cli" and port is None:
        raise HTTPException(status_code=400, detail="port is required for the cli flow")
    if flow == "web" and not return_to:
        raise HTTPException(status_code=400, detail="return_to is required for the web flow")
    if flow == "mcp" and not login_state:
        raise HTTPException(status_code=400, detail="login_state is required for the mcp flow")
    if flow not in {"cli", "web", "mcp"}:
        raise HTTPException(status_code=400, detail="unknown login flow")
    if flow == "cli" and (int(port or 0) <= 0 or int(port or 0) > 65535):
        raise HTTPException(status_code=400, detail="port is required for CLI login")
    if flow == "web":
        return_to = _safe_return_to(str(return_to or ""))
    state = auth.create_state(
        provider, {"flow": flow, "port": port, "login_state": login_state, "return_to": return_to, "host": host}
    )
    return RedirectResponse(auth.build_authorize_url(provider, state, host))


@router.get("/auth/{provider}/callback")
async def auth_callback(request: Request, provider: str, code: str, state: str):
    resolved = auth.pop_state(state)
    if resolved is None or resolved.get("provider") != provider:
        return PlainTextResponse("Login expired or invalid -- please retry `auto-skill login`.", status_code=400)
    host = resolved.get("host") or _login_host(request)
    try:
        user, cli_token = await auth.complete_login(provider, code, host)
    except Exception:
        import traceback

        traceback.print_exc()
        return PlainTextResponse("Login failed while talking to the provider -- please retry.", status_code=502)

    flow = resolved.get("flow", "cli")

    if flow == "web":
        return RedirectResponse(_with_token_fragment(str(resolved.get("return_to") or ""), cli_token))

    if flow == "mcp":
        pending = mcp_oauth.pop_pending(resolved.get("login_state") or "")
        if pending is None:
            return PlainTextResponse(
                "Login expired or invalid -- please retry connecting in your MCP client.", status_code=400
            )
        auth_code = mcp_oauth.mint_authorization_code(pending, user["id"])
        redirect_url = _append_query(pending["redirect_uri"], code=auth_code, state=pending["mcp_state"])
        return RedirectResponse(redirect_url)

    return RedirectResponse(f"http://127.0.0.1:{int(resolved['port'])}/callback?token={cli_token}")


def _signup_page(host: str) -> HTMLResponse:
    """Landing page for anyone scraper.py's account guard turned away --
    shares mcp_oauth.card_page's chrome so a browser hitting this API
    directly doesn't get a bare 401 or an unstyled page. Logging in from
    here lands on /account (this same host) next, not the external site
    directly -- see _account_page()."""
    return_to = quote(f"https://{host}/account", safe="")
    buttons = "".join(
        f'<a class="button" href="/auth/{provider}/start?flow=web&return_to={return_to}">'
        f"Continue with {label}</a>"
        for provider, label in (("google", "Google"), ("github", "GitHub"))
    )
    body = (
        "<h1>An account is required</h1>"
        "<p>Auto-Skill's API is account-only. Sign in with Google or GitHub -- "
        "if you don't have an account yet, this creates one automatically.</p>"
        f'<div class="providers">{buttons}</div>'
    )
    return mcp_oauth.card_page(body)


@router.get("/signup")
async def signup(request: Request):
    return _signup_page(_login_host(request))


def _account_page() -> HTMLResponse:
    """Small "you're logged in" confirmation, reached after /signup's login
    round-trip. Reads the #token fragment client-side (like the site's own
    dashboard.html) since a plain server-side redirect can't see a URL
    fragment -- browsers never send it. Deliberately doesn't duplicate the
    full dashboard UX; just confirms login and links out to it, carrying the
    token along in the fragment so the dashboard (a different origin, so no
    shared localStorage) doesn't ask for a second login."""
    body = f"""<h1 id="heading">Signing you in&hellip;</h1>
    <p id="detail">One moment.</p>
    <a class="button" id="dashboard-link" href="{SIGNUP_DASHBOARD_URL}" hidden>Go to your dashboard</a>
<script>
  (function () {{
    var match = /(?:^|#)token=([^&]+)/.exec(location.hash);
    var heading = document.getElementById("heading");
    var detail = document.getElementById("detail");
    var link = document.getElementById("dashboard-link");
    if (!match) {{
      heading.textContent = "Something went wrong";
      heading.className = "error";
      detail.textContent = "No login token was found. Please try signing in again.";
      return;
    }}
    var token = decodeURIComponent(match[1]);
    history.replaceState(null, "", location.pathname + location.search);
    link.href = {json.dumps(SIGNUP_DASHBOARD_URL)} + "#token=" + encodeURIComponent(token);
    fetch("/auth/whoami", {{ headers: {{ Authorization: "Bearer " + token }} }})
      .then(function (r) {{ return r.ok ? r.json() : Promise.reject(r.status); }})
      .then(function (user) {{
        heading.textContent = "You're signed in";
        var emailSpan = document.createElement("span");
        emailSpan.className = "email";
        emailSpan.textContent = user.email;
        detail.textContent = "";
        detail.appendChild(document.createTextNode("Signed in as "));
        detail.appendChild(emailSpan);
        detail.appendChild(document.createTextNode("."));
        link.hidden = false;
      }})
      .catch(function () {{
        heading.textContent = "Something went wrong";
        heading.className = "error";
        detail.textContent = "Login didn't complete. Please try signing in again.";
      }});
  }})();
</script>"""
    return mcp_oauth.card_page(body, title="Signed in to Auto-Skill")


@router.get("/account")
async def account():
    return _account_page()


@router.get("/auth/whoami")
async def whoami(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    plan = user.get("plan") or "free"
    limit = store.FREE_ROUTES_PER_MONTH if plan == "free" else None
    return {
        "id": user["id"],
        "email": user["email"],
        "name": user["name"],
        "avatar_url": user["avatar_url"],
        "plan": plan,
        "routes_used_this_month": store.get_route_usage(user["id"]),
        "routes_limit": limit,
    }


@router.get("/runs")
async def get_runs(limit: int = 50, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"runs": store.list_route_events_for_user(user["id"], limit)}


@router.get("/skills-catalog")
async def skills_catalog(
    q: str = "", limit: int = 50, offset: int = 0, sort: str = "popular", authorization: str | None = Header(None)
):
    """Browse the full skill corpus (site's skills.html). Account-only: the
    require_account_guard already turns away anonymous public callers, but
    require a bearer here too so the endpoint stays gated even for loopback
    or misconfigured-proxy traffic."""
    _require_user(authorization)
    return store.list_skills_catalog(q=q, limit=limit, offset=offset, sort=sort if sort == "recent" else "popular")


@router.get("/admin/stats")
async def admin_stats(events_limit: int = 100, authorization: str | None = Header(None)):
    """Operator-only rollup: every user's signup/login-provider/tier/outcome/
    token stats, plus a live feed of the most recent route_events across all
    users. Metadata-only, same as every other route_events consumer -- see
    ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS. Gated by ADMIN_EMAILS, not a public
    feature -- see _require_admin."""
    _require_admin(authorization)
    return {
        "users": store.admin_user_stats(),
        "recent_events": store.admin_recent_events(events_limit),
        # Keep anonymous cohort counts behind the existing operator gate. The
        # response contains only aggregate counts and short installation
        # prefixes; it never exposes raw IDs or prompt-derived data.
        "route_metrics": store.route_event_summary(hours=24),
    }


class SetPlanRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    plan: str


@router.post("/admin/set-plan")
async def admin_set_plan(body: SetPlanRequest, authorization: str | None = Header(None)):
    """Operator-only manual plan flip. There is deliberately no billing
    integration yet -- plans change by hand until someone is actually paying."""
    _require_admin(authorization)
    if body.plan not in store.USER_PLANS:
        raise HTTPException(status_code=400, detail=f"unknown plan; choose one of {', '.join(store.USER_PLANS)}")
    if not store.set_user_plan(body.email, body.plan):
        raise HTTPException(status_code=404, detail="no user with that email")
    return {"ok": True, "email": body.email, "plan": body.plan}


class SetOrgSeatsRequest(BaseModel):
    org_id: str
    seats: int = Field(ge=1, le=1000)


@router.post("/admin/set-org-seats")
async def admin_set_org_seats(body: SetOrgSeatsRequest, authorization: str | None = Header(None)):
    """Operator-only paid-seat bump. The team plan includes
    TEAM_INCLUDED_MEMBERS members per workspace; extra seats are billed by
    hand, same manual-billing stance as /admin/set-plan."""
    admin = _require_admin(authorization)
    if not store.set_org_seat_limit(body.org_id, body.seats):
        raise HTTPException(status_code=404, detail="no such org")
    store.record_org_audit(body.org_id, admin["id"], "seats_changed", str(body.seats))
    return {"ok": True, "org_id": body.org_id, "seat_limit": body.seats}


@router.post("/auth/logout")
async def logout(authorization: str | None = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    raw_token = authorization[len("Bearer "):].strip()
    auth.revoke_cli_token(raw_token)
    return {"ok": True}


@router.post("/auth/refresh")
async def refresh(authorization: str | None = Header(None)):
    """Rotate a still-valid bearer token: issue a new one and revoke the old.

    Backs the MCP OAuth refresh_token grant (mcp_oauth_provider.py) -- our
    tokens don't otherwise expire, but MCP clients that request the
    refresh_token grant type (required by the `mcp` SDK's own registration
    handler) still expect a working refresh path, not just a token that
    happens to never expire.
    """
    user = _require_user(authorization)
    raw_token = authorization[len("Bearer "):].strip()  # type: ignore[union-attr]
    new_token = auth.issue_cli_token(user["id"])
    auth.revoke_cli_token(raw_token)
    return {"access_token": new_token}


class FavoriteRequest(BaseModel):
    skill_id: str


@router.get("/favorites")
async def get_favorites(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"favorites": store.list_favorites(user["id"])}


@router.post("/favorites")
async def post_favorite(body: FavoriteRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    store.add_favorite(user["id"], body.skill_id)
    return {"ok": True}


@router.delete("/favorites/{skill_id}")
async def delete_favorite(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    removed = store.remove_favorite(user["id"], skill_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not favorited")
    return {"ok": True}


class InstallRequest(BaseModel):
    skill_id: str | None = None
    skill_url: str | None = None
    target: str


@router.get("/installs")
async def get_installs(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"installs": store.list_installs(user["id"])}


@router.post("/installs")
async def post_install(body: InstallRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    store.record_install(user["id"], body.skill_id, body.skill_url, body.target)
    store.record_install_audit(user["id"], body.skill_id or body.skill_url or "")
    return {"ok": True}


class PrivateSkillRequest(BaseModel):
    # Private skills are caller-owned, but still flow through a hosted service.
    # Keep storage and route payloads bounded before they ever reach SQLite.
    name: str = Field(min_length=2, max_length=120)
    description: str | None = Field(default=None, max_length=1_000)
    content: str = Field(min_length=1, max_length=48_000)


@router.get("/private-skills")
async def get_private_skills(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"private_skills": store.list_private_skills(user["id"])}


@router.post("/private-skills")
async def post_private_skill(body: PrivateSkillRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    plan = user.get("plan") or "free"
    if plan == "free" and store.FREE_PRIVATE_SKILLS > 0 and store.count_private_skills(user["id"]) >= store.FREE_PRIVATE_SKILLS:
        raise HTTPException(
            status_code=402,
            detail=f"the free plan includes up to {store.FREE_PRIVATE_SKILLS} private skills; upgrade to pro for unlimited",
        )
    skill = store.add_private_skill(user["id"], body.name, body.description, body.content)
    return {"private_skill": skill}


@router.delete("/private-skills/{skill_id}")
async def delete_private_skill(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    removed = store.remove_private_skill(user["id"], skill_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@router.get("/skills/{skill_id}/versions")
async def get_skill_versions(skill_id: str, authorization: str | None = Header(None)):
    _require_user(authorization)
    current = store.get_skill_hash(skill_id)
    return {"current_hash": current, "versions": store.list_skill_versions(skill_id)}


class PinRequest(BaseModel):
    skill_id: str
    content_hash: str = Field(min_length=8, max_length=128)


@router.get("/pins")
async def get_pins(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"pins": store.list_pins(user["id"])}


@router.post("/pins")
async def post_pin(body: PinRequest, authorization: str | None = Header(None)):
    """Pin (or roll back) a skill to a specific content version. Routing
    serves the pinned content until the pin is removed or re-pointed."""
    user = _require_user(authorization)
    _require_paid_plan(user, "version pinning")
    if not store.skill_version_exists(body.skill_id, body.content_hash):
        raise HTTPException(status_code=404, detail="no such version for that skill")
    return {"pin": store.pin_skill(user["id"], body.skill_id, body.content_hash)}


@router.delete("/pins/{skill_id}")
async def delete_pin(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    if not store.unpin_skill(user["id"], skill_id):
        raise HTTPException(status_code=404, detail="not pinned")
    return {"ok": True}


class WatchRequest(BaseModel):
    skill_id: str


@router.get("/watches")
async def get_watches(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"watches": store.list_watches(user["id"])}


@router.post("/watches")
async def post_watch(body: WatchRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_paid_plan(user, "change alerts")
    if store.get_skill_hash(body.skill_id) is None:
        raise HTTPException(status_code=404, detail="no such skill")
    return {"watch": store.watch_skill(user["id"], body.skill_id)}


@router.delete("/watches/{skill_id}")
async def delete_watch(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    if not store.unwatch_skill(user["id"], skill_id):
        raise HTTPException(status_code=404, detail="not watched")
    return {"ok": True}


@router.get("/alerts")
async def get_alerts(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"alerts": store.list_skill_alerts(user["id"])}


@router.post("/alerts/{skill_id}/ack")
async def ack_alert(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    if not store.ack_skill_alert(user["id"], skill_id):
        raise HTTPException(status_code=404, detail="not watched")
    return {"ok": True}


class CollectionRequest(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    org_id: str | None = None


class CollectionSkillRequest(BaseModel):
    skill_id: str


def _require_collection_access(collection_id: str, user: dict, *, modify: bool) -> dict:
    """Personal collections: owner only. Org collections: members view and
    add/remove skills, only the org owner deletes the collection itself."""
    collection = store.get_collection(collection_id)
    if collection is None:
        raise HTTPException(status_code=404, detail="no such collection")
    if collection.get("org_id"):
        role = store.org_role(collection["org_id"], user["id"])
        if role is None:
            raise HTTPException(status_code=404, detail="no such collection")
        if modify and role != "owner":
            raise HTTPException(status_code=403, detail="org owner required")
    elif collection["owner_user_id"] != user["id"]:
        raise HTTPException(status_code=404, detail="no such collection")
    return collection


@router.get("/collections")
async def get_collections(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"collections": store.list_collections(user["id"])}


@router.post("/collections")
async def post_collection(body: CollectionRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_paid_plan(user, "collections")
    if body.org_id:
        if store.org_role(body.org_id, user["id"]) != "owner":
            raise HTTPException(status_code=403, detail="org owner required for shared collections")
    return {"collection": store.create_collection(user["id"], body.name.strip(), body.org_id)}


@router.delete("/collections/{collection_id}")
async def delete_collection(collection_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_collection_access(collection_id, user, modify=True)
    store.delete_collection(collection_id)
    return {"ok": True}


@router.get("/collections/{collection_id}/skills")
async def get_collection_skills(collection_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_collection_access(collection_id, user, modify=False)
    return {"skills": store.list_collection_skills(collection_id)}


@router.post("/collections/{collection_id}/skills")
async def post_collection_skill(
    collection_id: str, body: CollectionSkillRequest, authorization: str | None = Header(None)
):
    user = _require_user(authorization)
    collection = _require_collection_access(collection_id, user, modify=False)
    if not collection.get("org_id"):
        _require_collection_access(collection_id, user, modify=True)
    store.add_collection_skill(collection_id, body.skill_id)
    return {"ok": True, "skills": store.list_collection_skills(collection_id)}


@router.delete("/collections/{collection_id}/skills/{skill_id}")
async def delete_collection_skill(collection_id: str, skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    collection = _require_collection_access(collection_id, user, modify=False)
    if not collection.get("org_id"):
        _require_collection_access(collection_id, user, modify=True)
    if not store.remove_collection_skill(collection_id, skill_id):
        raise HTTPException(status_code=404, detail="not in collection")
    return {"ok": True}


class PreferencesRequest(BaseModel):
    excluded_skill_ids: list[str] = Field(default_factory=list, max_length=200)
    excluded_sources: list[str] = Field(default_factory=list, max_length=50)


@router.get("/preferences")
async def get_preferences(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"preferences": store.get_routing_preferences(user["id"])}


@router.put("/preferences")
async def put_preferences(body: PreferencesRequest, authorization: str | None = Header(None)):
    """Custom exclusions and routing preferences. Stored server-side so every
    connected agent on every machine applies them -- this is what the plan
    sells as cross-agent synchronization."""
    user = _require_user(authorization)
    _require_paid_plan(user, "routing preferences")
    excluded_ids = [str(v)[:80] for v in body.excluded_skill_ids]
    excluded_sources = [str(v)[:80] for v in body.excluded_sources]
    return {"preferences": store.set_routing_preferences(user["id"], excluded_ids, excluded_sources)}


@router.get("/analytics")
async def get_analytics(days: int = 30, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_paid_plan(user, "recommendation analytics")
    return {"analytics": store.user_route_analytics(user["id"], days)}


def _require_org_member(org_id: str, user: dict) -> str:
    role = store.org_role(org_id, user["id"])
    if role is None:
        # 404 (not 403) for non-members so org ids aren't probeable.
        raise HTTPException(status_code=404, detail="no such org")
    return role


def _require_org_owner(org_id: str, user: dict) -> None:
    if _require_org_member(org_id, user) != "owner":
        raise HTTPException(status_code=403, detail="org owner required")


class CreateOrgRequest(BaseModel):
    name: str = Field(min_length=2, max_length=120)


@router.post("/orgs")
async def create_org(body: CreateOrgRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    if (user.get("plan") or "free") != "team":
        raise HTTPException(status_code=402, detail="orgs require the team plan")
    return {"org": store.create_org(body.name.strip(), user["id"])}


@router.get("/orgs")
async def get_orgs(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"orgs": store.list_orgs_for_user(user["id"])}


class OrgMemberRequest(BaseModel):
    email: str = Field(min_length=3, max_length=320)


@router.get("/orgs/{org_id}/members")
async def get_org_members(org_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_member(org_id, user)
    return {"members": store.list_org_members(org_id)}


@router.post("/orgs/{org_id}/members")
async def post_org_member(org_id: str, body: OrgMemberRequest, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_owner(org_id, user)
    member = store.get_user_by_email(body.email)
    if member is None:
        raise HTTPException(status_code=404, detail="no user with that email; they must sign up first")
    org = store.get_org(org_id)
    seat_limit = store.org_seat_limit(org or {})
    if store.org_role(org_id, member["id"]) is None and store.org_member_count(org_id) >= seat_limit:
        raise HTTPException(
            status_code=402,
            detail=f"this workspace includes {seat_limit} members; additional seats are billed -- contact support to add seats",
        )
    if store.add_org_member(org_id, member["id"]):
        store.record_org_audit(org_id, user["id"], "member_added", member["email"])
    return {"ok": True, "members": store.list_org_members(org_id)}


@router.delete("/orgs/{org_id}/members/{user_id}")
async def delete_org_member(org_id: str, user_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    # Owners remove anyone; a member may remove only themselves (leave).
    if user_id != user["id"]:
        _require_org_owner(org_id, user)
    else:
        _require_org_member(org_id, user)
    if not store.remove_org_member(org_id, user_id):
        raise HTTPException(status_code=404, detail="not a removable member")
    store.record_org_audit(org_id, user["id"], "member_removed", user_id)
    return {"ok": True}


@router.get("/orgs/{org_id}/skills")
async def get_org_skills(org_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_member(org_id, user)
    return {"org_skills": store.list_org_skills(org_id)}


@router.post("/orgs/{org_id}/skills")
async def post_org_skill(org_id: str, body: PrivateSkillRequest, authorization: str | None = Header(None)):
    """Owner publishes directly; a member's submission lands as 'pending' and
    stays out of routing until the owner approves it."""
    user = _require_user(authorization)
    role = _require_org_member(org_id, user)
    status = None if role == "owner" else "pending"
    skill = store.add_org_skill(org_id, user["id"], body.name, body.description, body.content, status=status)
    action = "org_skill_added" if role == "owner" else "org_skill_submitted"
    store.record_org_audit(org_id, user["id"], action, body.name)
    return {"org_skill": skill}


@router.post("/orgs/{org_id}/skills/{skill_id}/approve")
async def approve_org_skill(org_id: str, skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_owner(org_id, user)
    if not store.approve_org_skill(org_id, skill_id):
        raise HTTPException(status_code=404, detail="no pending skill with that id")
    store.record_org_audit(org_id, user["id"], "org_skill_approved", skill_id)
    return {"ok": True}


@router.delete("/orgs/{org_id}/skills/{skill_id}")
async def delete_org_skill(org_id: str, skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_owner(org_id, user)
    if not store.remove_org_skill(org_id, skill_id):
        raise HTTPException(status_code=404, detail="not found")
    store.record_org_audit(org_id, user["id"], "org_skill_removed", skill_id)
    return {"ok": True}


class OrgPolicyRequest(BaseModel):
    skill_id: str
    policy: str


@router.get("/orgs/{org_id}/policies")
async def get_org_policies(org_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_member(org_id, user)
    return {"policies": store.list_org_skill_policies(org_id)}


@router.post("/orgs/{org_id}/policies")
async def post_org_policy(org_id: str, body: OrgPolicyRequest, authorization: str | None = Header(None)):
    """Allow/block routing policies for the workspace. Blocked skills never
    route for members; if any allow rows exist, public-catalog routing is
    restricted to the allowlist (team-standard-only mode)."""
    user = _require_user(authorization)
    _require_org_owner(org_id, user)
    if body.policy not in store.ORG_SKILL_POLICIES:
        raise HTTPException(
            status_code=400, detail=f"unknown policy; choose one of {', '.join(store.ORG_SKILL_POLICIES)}"
        )
    policy = store.set_org_skill_policy(org_id, body.skill_id, body.policy)
    store.record_org_audit(org_id, user["id"], "policy_set", f"{body.policy}:{body.skill_id}")
    return {"policy": policy}


@router.delete("/orgs/{org_id}/policies/{skill_id}")
async def delete_org_policy(org_id: str, skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_owner(org_id, user)
    if not store.remove_org_skill_policy(org_id, skill_id):
        raise HTTPException(status_code=404, detail="no policy for that skill")
    store.record_org_audit(org_id, user["id"], "policy_removed", skill_id)
    return {"ok": True}


@router.get("/orgs/{org_id}/audit")
async def get_org_audit(org_id: str, limit: int = 100, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_owner(org_id, user)
    return {"audit": store.list_org_audit(org_id, limit)}


@router.get("/orgs/{org_id}/analytics")
async def get_org_analytics(org_id: str, days: int = 30, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    _require_org_owner(org_id, user)
    return {"analytics": store.org_route_analytics(org_id, days)}
