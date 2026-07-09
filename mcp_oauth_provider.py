"""OAuthAuthorizationServerProvider for the hosted streamable-http connector.

This process never touches the backend's SQLite store directly -- like every
other connector call, it goes over HTTP to the backend (see backend/mcp_oauth.py
for the endpoints this hits, and backend/auth.py's module docstring for why
there is no external database or auth framework in this project). An MCP
access token issued here *is* a `cli_tokens` row: the backend's /mcp-oauth/token
mints it with the same `issue_cli_token` used by `auto-skill login`, so
verifying one is just calling the existing GET /auth/whoami, and revoking one
is just the existing POST /auth/logout.
"""
from __future__ import annotations

import os
from datetime import datetime
from urllib.parse import urlencode

import httpx
from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    OAuthAuthorizationServerProvider,
    RefreshToken,
    RegistrationError,
    TokenError,
)
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken


class BackendOAuthProvider(OAuthAuthorizationServerProvider[AuthorizationCode, RefreshToken, AccessToken]):
    def __init__(self, public_backend_url: str, loopback_backend_url: str | None = None):
        # Two different base URLs on purpose: `public` is handed to the
        # user's browser (authorize() redirects there, so it must be the
        # real public hostname); `loopback` is used for this process's own
        # server-to-server calls to the backend. They differ whenever the
        # connector and backend run on the same box behind split-horizon DNS
        # -- see start_connector_http.ps1's AUTOSKILL_URL note -- where the
        # public hostname doesn't resolve correctly from that machine itself.
        self.public_backend = public_backend_url.rstrip("/")
        self.backend = (loopback_backend_url or public_backend_url).rstrip("/")

    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.backend}/mcp-oauth/clients/{client_id}", timeout=10)
        if r.status_code != 200:
            return None
        return OAuthClientInformationFull.model_validate(r.json())

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        async with httpx.AsyncClient() as client:
            r = await client.post(
                f"{self.backend}/mcp-oauth/clients",
                json=client_info.model_dump(mode="json", exclude_none=True),
                timeout=10,
            )
        if r.status_code != 200:
            raise RegistrationError(error="invalid_client_metadata", error_description=r.text)

    async def authorize(self, client: OAuthClientInformationFull, params: AuthorizationParams) -> str:
        # Hands the browser off to the backend, which owns the actual
        # Google/GitHub login and the pending-request bridge (login_state).
        # This URL is followed by the user's browser, so it must be
        # public_backend, never the loopback address.
        query = {
            "client_id": client.client_id,
            "redirect_uri": str(params.redirect_uri),
            "code_challenge": params.code_challenge,
            "state": params.state or "",
        }
        if params.scopes:
            query["scope"] = " ".join(params.scopes)
        if params.resource:
            query["resource"] = params.resource
        return f"{self.public_backend}/mcp-oauth/authorize?{urlencode(query)}"

    async def load_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: str
    ) -> AuthorizationCode | None:
        async with httpx.AsyncClient() as client_:
            r = await client_.get(f"{self.backend}/mcp-oauth/codes/{authorization_code}", timeout=10)
        if r.status_code != 200:
            return None
        entry = r.json()
        return AuthorizationCode(
            code=authorization_code,
            scopes=entry["scopes"],
            expires_at=_iso_to_epoch(entry["expires_at"]),
            client_id=entry["client_id"],
            code_challenge=entry["code_challenge"],
            redirect_uri=entry["redirect_uri"],
            redirect_uri_provided_explicitly=True,
        )

    async def exchange_authorization_code(
        self, client: OAuthClientInformationFull, authorization_code: AuthorizationCode
    ) -> OAuthToken:
        async with httpx.AsyncClient() as client_:
            r = await client_.post(
                f"{self.backend}/mcp-oauth/token",
                json={"code": authorization_code.code, "client_id": client.client_id},
                timeout=10,
            )
        if r.status_code != 200:
            raise TokenError(error="invalid_grant", error_description=r.text)
        body = r.json()
        access_token = body["access_token"]
        # cli_tokens don't expire, but the `mcp` SDK's registration handler
        # requires every client to request the refresh_token grant, and
        # clients may treat a grant with no refresh_token as incomplete --
        # the access token is itself a valid input to POST /auth/refresh, so
        # it doubles as its own initial refresh token.
        return OAuthToken(
            access_token=access_token, token_type="Bearer", scope=body.get("scope"), refresh_token=access_token
        )

    async def load_refresh_token(self, client: OAuthClientInformationFull, refresh_token: str) -> RefreshToken | None:
        async with httpx.AsyncClient() as client_:
            r = await client_.get(
                f"{self.backend}/auth/whoami", headers={"Authorization": f"Bearer {refresh_token}"}, timeout=10
            )
        if r.status_code != 200:
            return None
        return RefreshToken(token=refresh_token, client_id=client.client_id, scopes=["route"])

    async def exchange_refresh_token(
        self, client: OAuthClientInformationFull, refresh_token: RefreshToken, scopes: list[str]
    ) -> OAuthToken:
        async with httpx.AsyncClient() as client_:
            r = await client_.post(
                f"{self.backend}/auth/refresh",
                headers={"Authorization": f"Bearer {refresh_token.token}"},
                timeout=10,
            )
        if r.status_code != 200:
            raise TokenError(error="invalid_grant", error_description=r.text)
        new_token = r.json()["access_token"]
        return OAuthToken(access_token=new_token, token_type="Bearer", scope="route", refresh_token=new_token)

    async def load_access_token(self, token: str) -> AccessToken | None:
        async with httpx.AsyncClient() as client:
            r = await client.get(f"{self.backend}/auth/whoami", headers={"Authorization": f"Bearer {token}"}, timeout=10)
        if r.status_code != 200:
            return None
        user = r.json()
        return AccessToken(token=token, client_id="*", scopes=["route"], subject=user["id"])

    async def revoke_token(self, token: AccessToken | RefreshToken) -> None:
        async with httpx.AsyncClient() as client:
            await client.post(
                f"{self.backend}/auth/logout", headers={"Authorization": f"Bearer {token.token}"}, timeout=10
            )


def _iso_to_epoch(value: str) -> float:
    return datetime.fromisoformat(value).timestamp()


def get_public_backend_base_url() -> str:
    return os.getenv("BACKEND_BASE_URL", "https://skills.avalahome.com").rstrip("/")


def get_loopback_backend_base_url() -> str | None:
    """AUTOSKILL_URL is the same env var auto_skill_core.py's get_autoskill_url()
    already reads for /route calls -- reuse it here so both codepaths agree on
    where "the backend, reached from this same box" actually is."""
    return os.getenv("AUTOSKILL_URL") or None
