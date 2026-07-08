"""Accounts API: OAuth login (Google/GitHub) and per-user data (favorites,
installs, private skills). Self-contained APIRouter so scraper.py only needs:
    from accounts_api import router as accounts_router
    app.include_router(accounts_router)

All account data lives in the same local SQLite store (local_store.py) that
already backs the rest of the backend -- see auth.py's module docstring for
why there is no external database or auth framework here.

Endpoints:
  GET  /auth/{provider}/start?port=...       -> redirect into provider OAuth
  GET  /auth/{provider}/callback?code&state   -> redirect to CLI loopback
  GET  /auth/whoami                           -> current user
  POST /auth/logout                           -> revoke the bearer token
  GET/POST/DELETE /favorites[/{skill_id}]     -> per-user favorited skills
  GET/POST        /installs                   -> per-user install history
  GET/POST/DELETE /private-skills[/{id}]      -> per-user private skill submissions
"""
from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import PlainTextResponse, RedirectResponse
from pydantic import BaseModel

import auth
import local_store as store

router = APIRouter()


def _require_user(authorization: str | None) -> dict:
    user = auth.user_from_authorization_header(authorization)
    if user is None:
        raise HTTPException(status_code=401, detail="missing or invalid bearer token")
    return user


@router.get("/auth/{provider}/start")
async def auth_start(provider: str, port: int):
    if provider not in auth.PROVIDERS:
        raise HTTPException(status_code=404, detail="unknown provider")
    if not auth.PROVIDERS[provider]["client_id"]:
        raise HTTPException(status_code=503, detail=f"{provider} login is not configured on this server")
    state = auth.create_state(provider, port)
    return RedirectResponse(auth.build_authorize_url(provider, state))


@router.get("/auth/{provider}/callback")
async def auth_callback(provider: str, code: str, state: str):
    resolved = auth.pop_state(state)
    if resolved is None or resolved[0] != provider:
        return PlainTextResponse("Login expired or invalid -- please retry `auto-skill login`.", status_code=400)
    _, port = resolved
    try:
        _user, cli_token = await auth.complete_login(provider, code)
    except Exception:
        return PlainTextResponse("Login failed while talking to the provider -- please retry.", status_code=502)
    return RedirectResponse(f"http://127.0.0.1:{port}/callback?token={cli_token}")


@router.get("/auth/whoami")
async def whoami(authorization: str | None = Header(None)):
    user = _require_user(authorization)
    return {"email": user["email"], "name": user["name"], "avatar_url": user["avatar_url"]}


@router.post("/auth/logout")
async def logout(authorization: str | None = Header(None)):
    if not authorization or not authorization.lower().startswith("bearer "):
        raise HTTPException(status_code=401, detail="missing bearer token")
    raw_token = authorization[len("Bearer "):].strip()
    auth.revoke_cli_token(raw_token)
    return {"ok": True}


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
    name: str
    description: str | None = None
    content: str


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
