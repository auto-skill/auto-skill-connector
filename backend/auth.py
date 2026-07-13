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

# GitHub OAuth Apps only support one callback URL each, unlike Google (which
# allows multiple registered redirect URIs on the same app) -- so serving
# login on more than one live hostname (e.g. during the avalahome.com ->
# autoskill.dev migration) means a *separate* GitHub OAuth App per hostname,
# each with its own client id/secret. Google keeps using PROVIDERS["google"]
# unchanged; only the redirect_uri sent to Google varies by host.
GITHUB_CREDENTIALS_BY_HOST = {
    "skills.avalahome.com": {
        "client_id": os.getenv("GITHUB_CLIENT_ID", ""),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET", ""),
    },
    "skills.autoskill.dev": {
        "client_id": os.getenv("GITHUB_CLIENT_ID_AUTOSKILL", ""),
        "client_secret": os.getenv("GITHUB_CLIENT_SECRET_AUTOSKILL", ""),
    },
}

# Hosts the login flow will build redirect_uris for -- deliberately explicit
# rather than trusting any Host header a caller sends, since that header
# picks which OAuth client id/secret gets used.
ALLOWED_LOGIN_HOSTS = frozenset(GITHUB_CREDENTIALS_BY_HOST.keys())


def github_credentials_for_host(host: str) -> dict:
    """Caller must have already validated `host` against ALLOWED_LOGIN_HOSTS."""
    return GITHUB_CREDENTIALS_BY_HOST[host]

# In-memory state for OAuth handshakes. The payload carries whatever the caller
# needs after the provider redirect round-trip: CLI loopback, web dashboard
# return URL, or MCP OAuth pending login state.
_STATE_TTL_SECONDS = 600
_states: dict[str, dict] = {}


def _purge_expired_states() -> None:
    now = time.time()
    expired = [s for s, value in _states.items() if float(value.get("expires_at") or 0) < now]
    for s in expired:
        _states.pop(s, None)


def create_state(provider: str, payload: dict | None = None) -> str:
    _purge_expired_states()
    state = secrets.token_urlsafe(24)
    _states[state] = {"provider": provider, **(payload or {}), "expires_at": time.time() + _STATE_TTL_SECONDS}
    return state


def pop_state(state: str) -> dict | None:
    return pop_login_state(state)


def create_cli_state(provider: str, port: int) -> str:
    return create_state(provider, {"flow": "cli", "port": int(port)})


def create_web_state(provider: str, return_to: str) -> str:
    return create_state(provider, {"flow": "web", "return_to": return_to})


def pop_login_state(state: str) -> dict | None:
    _purge_expired_states()
    entry = _states.pop(state, None)
    if entry is None:
        return None
    return {key: value for key, value in entry.items() if key != "expires_at"}


def redirect_uri_for(provider: str, host: str) -> str:
    return f"https://{host}/auth/{provider}/callback"


def build_authorize_url(provider: str, state: str, host: str) -> str:
    cfg = PROVIDERS[provider]
    client_id = cfg["client_id"] if provider == "google" else github_credentials_for_host(host)["client_id"]
    params = {
        "client_id": client_id,
        "redirect_uri": redirect_uri_for(provider, host),
        "scope": cfg["scope"],
        "state": state,
        "response_type": "code",
    }
    if provider == "google":
        params["access_type"] = "online"
        params["prompt"] = "select_account"
    return f"{cfg['authorize_url']}?{urlencode(params)}"


async def exchange_code(client: httpx.AsyncClient, provider: str, code: str, host: str) -> str:
    """Exchange an authorization code for an access token. Returns the token."""
    cfg = PROVIDERS[provider]
    if provider == "google":
        client_id, client_secret = cfg["client_id"], cfg["client_secret"]
    else:
        github_creds = github_credentials_for_host(host)
        client_id, client_secret = github_creds["client_id"], github_creds["client_secret"]
    data = {
        "client_id": client_id,
        "client_secret": client_secret,
        "code": code,
        "redirect_uri": redirect_uri_for(provider, host),
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
        if profile.get("email_verified") is not True:
            raise ValueError("Google account email is not verified")
        return {
            "email": profile["email"],
            "name": profile.get("name"),
            "avatar_url": profile.get("picture"),
            "provider_user_id": profile["sub"],
        }

    # Email is an authorization attribute for the small admin allowlist, so
    # always use GitHub's verified-email endpoint rather than trusting the
    # optional public email field returned by /user.
    er = await client.get("https://api.github.com/user/emails", headers=headers)
    er.raise_for_status()
    emails = er.json()
    email = next(
        (entry.get("email") for entry in emails if entry.get("primary") and entry.get("verified")),
        None,
    )
    if not email:
        email = next((entry.get("email") for entry in emails if entry.get("verified")), None)
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
    user = store.get_user_by_token_hash(_hash_token(raw_token))
    return store.access_details_for_user(user) if user else None


async def complete_login(provider: str, code: str, host: str) -> tuple[dict, str]:
    """Run the code-exchange + profile-fetch + user upsert dance. Returns (user, cli_token)."""
    async with httpx.AsyncClient() as client:
        access_token = await exchange_code(client, provider, code, host)
        profile = await fetch_profile(client, provider, access_token)
    user = store.get_or_create_user(profile["email"], profile.get("name"), profile.get("avatar_url"))
    store.link_oauth_identity(user["id"], provider, profile["provider_user_id"])
    cli_token = issue_cli_token(user["id"])
    return user, cli_token
