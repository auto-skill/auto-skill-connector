"""Hand-rolled OAuth2 (Google + GitHub) for Auto-Skill accounts. No auth
framework or hosted auth provider: this mints an opaque CLI token per login,
stored hashed in local_store's cli_tokens table, the same way GitHub stores
personal access tokens. All account data lives in the same local SQLite file
that already powers the rest of the backend -- there is no external database.
"""
import hashlib
import os
import secrets
import time
from urllib.parse import urlencode

import httpx

import local_store as store

BACKEND_BASE_URL = os.getenv("BACKEND_BASE_URL", "https://skills.avalahome.com").rstrip("/")

PROVIDERS = {
    "google": {
        "client_id": os.getenv("GOOGLE_CLIENT_ID", ""),
        "client_secret": os.getenv("GOOGLE_CLIENT_SECRET", ""),
        "authorize_url": "https://accounts.google.com/o/oauth2/v2/auth",
        "token_url": "https://oauth2.googleapis.com/token",
        "userinfo_url": "https://openidconnect.googleapis.com/v1/userinfo",
        "scope": "openid email profile",
    },
    "github": {
        "client_id": os.getenv("GITHUB_CLIENT_ID", ""),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET", ""),
        "authorize_url": "https://github.com/login/oauth/authorize",
        "token_url": "https://github.com/login/oauth/access_token",
        "userinfo_url": "https://api.github.com/user",
        "scope": "read:user user:email",
    },
}

# In-memory state -> (payload dict, expiry_epoch) for the login handshake.
# This is a single-process "alpha infra" service (see backend/README.md); a
# restart mid-login just means the user retries. The payload carries whatever
# the caller needs after the provider redirect round-trip: the CLI loopback
# flow stashes {"flow": "cli", "port": ...}, the web dashboard login stashes
# {"flow": "web", "return_to": ...}, and the MCP OAuth authorize step stashes
# {"flow": "mcp", "login_state": ...} pointing at the pending authorization
# request (see mcp_oauth.py).
_STATE_TTL_SECONDS = 600
_states: dict[str, tuple[dict, float]] = {}


def _purge_expired_states() -> None:
    now = time.time()
    expired = [s for s, (_, exp) in _states.items() if exp < now]
    for s in expired:
        _states.pop(s, None)


def create_state(provider: str, payload: dict | None = None) -> str:
    _purge_expired_states()
    state = secrets.token_urlsafe(24)
    _states[state] = ({"provider": provider, **(payload or {})}, time.time() + _STATE_TTL_SECONDS)
    return state


def pop_state(state: str) -> dict | None:
    _purge_expired_states()
    entry = _states.pop(state, None)
    if entry is None:
        return None
    payload, _ = entry
    return payload


def redirect_uri_for(provider: str) -> str:
    return f"{BACKEND_BASE_URL}/auth/{provider}/callback"


def build_authorize_url(provider: str, state: str) -> str:
    cfg = PROVIDERS[provider]
    params = {
        "client_id": cfg["client_id"],
        "redirect_uri": redirect_uri_for(provider),
        "scope": cfg["scope"],
        "state": state,
        "response_type": "code",
    }
    if provider == "google":
        params["access_type"] = "online"
        params["prompt"] = "select_account"
    return f"{cfg['authorize_url']}?{urlencode(params)}"


async def exchange_code(client: httpx.AsyncClient, provider: str, code: str) -> str:
    """Exchange an authorization code for an access token. Returns the token."""
    cfg = PROVIDERS[provider]
    data = {
        "client_id": cfg["client_id"],
        "client_secret": cfg["client_secret"],
        "code": code,
        "redirect_uri": redirect_uri_for(provider),
        "grant_type": "authorization_code",
    }
    r = await client.post(cfg["token_url"], data=data, headers={"Accept": "application/json"})
    r.raise_for_status()
    body = r.json()
    if "access_token" not in body:
        raise ValueError(f"{provider} token exchange failed: {body}")
    return body["access_token"]


async def fetch_profile(client: httpx.AsyncClient, provider: str, access_token: str) -> dict:
    """Return {"email", "name", "avatar_url", "provider_user_id"} for the given provider."""
    cfg = PROVIDERS[provider]
    headers = {"Authorization": f"Bearer {access_token}"}
    r = await client.get(cfg["userinfo_url"], headers=headers)
    r.raise_for_status()
    profile = r.json()

    if provider == "google":
        return {
            "email": profile["email"],
            "name": profile.get("name"),
            "avatar_url": profile.get("picture"),
            "provider_user_id": profile["sub"],
        }

    # GitHub: primary email is often null on /user when private; fetch /user/emails instead.
    email = profile.get("email")
    if not email:
        er = await client.get("https://api.github.com/user/emails", headers=headers)
        er.raise_for_status()
        for entry in er.json():
            if entry.get("primary") and entry.get("verified"):
                email = entry["email"]
                break
        else:
            for entry in er.json():
                if entry.get("verified"):
                    email = entry["email"]
                    break
    if not email:
        raise ValueError("GitHub account has no verified email available")
    return {
        "email": email,
        "name": profile.get("name") or profile.get("login"),
        "avatar_url": profile.get("avatar_url"),
        "provider_user_id": str(profile["id"]),
    }


def _hash_token(raw_token: str) -> str:
    return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()


def issue_cli_token(user_id: str) -> str:
    raw_token = secrets.token_urlsafe(32)
    store.create_cli_token(user_id, _hash_token(raw_token))
    return raw_token


def revoke_cli_token(raw_token: str) -> bool:
    return store.revoke_cli_token(_hash_token(raw_token))


def user_from_authorization_header(header: str | None) -> dict | None:
    """Resolve an optional `Authorization: Bearer <token>` header to a user row."""
    if not header or not header.lower().startswith("bearer "):
        return None
    raw_token = header[len("Bearer "):].strip()
    if not raw_token:
        return None
    return store.get_user_by_token_hash(_hash_token(raw_token))


async def complete_login(provider: str, code: str) -> tuple[dict, str]:
    """Run the code-exchange + profile-fetch + user upsert dance. Returns (user, cli_token)."""
    async with httpx.AsyncClient() as client:
        access_token = await exchange_code(client, provider, code)
        profile = await fetch_profile(client, provider, access_token)
    user = store.get_or_create_user(profile["email"], profile.get("name"), profile.get("avatar_url"))
    store.link_oauth_identity(user["id"], provider, profile["provider_user_id"])
    cli_token = issue_cli_token(user["id"])
    return user, cli_token
