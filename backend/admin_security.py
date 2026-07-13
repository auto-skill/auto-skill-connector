"""Fail-closed host and outer-surface checks for founder administration."""
from __future__ import annotations

import os
import re
from urllib.parse import urlsplit

from fastapi import HTTPException, Request

try:
    import jwt
except ImportError:  # pragma: no cover - deployment preflight installs it
    jwt = None

_TEAM_DOMAIN_RE = re.compile(r"^[a-z0-9-]+\.cloudflareaccess\.com$")
_JWK_CLIENTS: dict[str, object] = {}


def admin_access_mode() -> str:
    """Return the configured outer access mode, defaulting to disabled."""
    mode = os.getenv("ADMIN_ACCESS_MODE", "disabled").strip().lower()
    return mode if mode in {"disabled", "ssh", "cloudflare"} else "disabled"


def admin_host() -> str:
    return os.getenv("ADMIN_HOST", "127.0.0.1").strip().lower().rstrip(".")


def request_host(request: Request) -> str:
    raw = (request.headers.get("host") or "").strip()
    try:
        return (urlsplit(f"//{raw}").hostname or "").lower().rstrip(".")
    except ValueError:
        return ""


def admin_emails() -> set[str]:
    return {value.strip().lower() for value in os.getenv("ADMIN_EMAILS", "").split(",") if value.strip()}


def _team_domain() -> str:
    raw = os.getenv("CF_ACCESS_TEAM_DOMAIN", "").strip().lower().rstrip("/")
    host = (urlsplit(raw).hostname if "://" in raw else raw).rstrip(".")
    if not _TEAM_DOMAIN_RE.fullmatch(host):
        raise HTTPException(status_code=503, detail="admin Access verifier is not configured")
    return host


def verify_access_assertion(assertion: str | None) -> str:
    """Verify Cloudflare's signed Access JWT and return its normalized email."""
    if not assertion:
        raise HTTPException(status_code=403, detail="Cloudflare Access assertion required")
    audience = os.getenv("CF_ACCESS_AUD", "").strip()
    if not audience or jwt is None:
        raise HTTPException(status_code=503, detail="admin Access verifier is not configured")
    team_domain = _team_domain()
    issuer = f"https://{team_domain}"
    certs_url = f"{issuer}/cdn-cgi/access/certs"
    try:
        jwks = _JWK_CLIENTS.get(certs_url)
        if jwks is None:
            jwks = jwt.PyJWKClient(certs_url, cache_keys=True)
            _JWK_CLIENTS[certs_url] = jwks
        key = jwks.get_signing_key_from_jwt(assertion).key
        claims = jwt.decode(
            assertion,
            key,
            algorithms=["RS256"],
            audience=audience,
            issuer=issuer,
            options={"require": ["exp", "iat", "aud", "iss", "email"]},
        )
    except Exception as exc:
        raise HTTPException(status_code=403, detail="invalid Cloudflare Access assertion") from exc
    email = str(claims.get("email") or "").strip().lower()
    if not email or email not in admin_emails():
        raise HTTPException(status_code=403, detail="Access identity is not an admin")
    return email


def require_admin_surface(request: Request, assertion: str | None) -> str | None:
    """Authorize the outer admin surface.

    ``ssh`` is valid only on the dedicated loopback-published compose service;
    forwarded requests are rejected even if a caller supplies the right Host.
    The returned email is present only for Cloudflare mode, where it must later
    match the Auto-Skill bearer identity.
    """
    mode = admin_access_mode()
    if mode == "disabled":
        raise HTTPException(status_code=404, detail="not found")
    if request_host(request) != admin_host():
        raise HTTPException(status_code=404, detail="not found")
    if mode == "ssh":
        if request.headers.get("cf-connecting-ip") or request.headers.get("x-forwarded-for"):
            raise HTTPException(status_code=404, detail="not found")
        return None
    return verify_access_assertion(assertion)
