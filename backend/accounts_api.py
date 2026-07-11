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
  GET/POST/DELETE /favorites[/{skill_id}]     -> per-user favorited skills
  GET/POST        /installs                   -> per-user install history
  GET/POST/DELETE /private-skills[/{id}]      -> per-user private skill submissions
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
    return {"id": user["id"], "email": user["email"], "name": user["name"], "avatar_url": user["avatar_url"]}


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
    skill = store.add_private_skill(user["id"], body.name, body.description, body.content)
    return {"private_skill": skill}


@router.delete("/private-skills/{skill_id}")
async def delete_private_skill(skill_id: str, authorization: str | None = Header(None)):
    user = _require_user(authorization)
    removed = store.remove_private_skill(user["id"], skill_id)
    if not removed:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}
