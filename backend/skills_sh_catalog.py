"""Live skills.sh catalog access for task-time discovery.

skills.sh already owns the large public catalog. Auto-Skill should query that
catalog instead of mirroring every skill locally. This module keeps the
integration deliberately small: search the remote index, hydrate only a
bounded shortlist, fetch audit metadata, and shape the result into the
existing deterministic routing schema. The full source package is never
returned by this module as an active instruction payload.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote
from pathlib import Path

import httpx

from quality import content_hash, evaluate_quality


DEFAULT_API_URL = "https://skills.sh/api/v1"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_SEARCH_TTL_SECONDS = 45.0
DEFAULT_DETAIL_TTL_SECONDS = 300.0
DEFAULT_AUDIT_TTL_SECONDS = 300.0
MAX_SEARCH_LIMIT = 50
MAX_DETAIL_CANDIDATES = 12
MAX_CONTENT_CHARS = 300_000
MAX_RETRIEVAL_CHARS = 1_500

_FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_FIELD_RE = re.compile(r"^(name|description):[ \t]*(.*)$", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)


class SkillsShCatalogError(RuntimeError):
    """Raised when the live catalog cannot be queried safely."""


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: Any


def _frontmatter_fields(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        field = _FIELD_RE.match(line.strip())
        if field:
            fields[field.group(1).casefold()] = field.group(2).strip().strip('"\'')
    return fields


def _entrypoint(files: list[dict[str, Any]]) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    for file in files:
        if not isinstance(file, dict):
            continue
        path = str(file.get("path") or "").replace("\\", "/").strip("/")
        contents = file.get("contents")
        if not path or contents is None:
            continue
        if path.casefold() == "skill.md" or path.casefold().endswith("/skill.md"):
            candidates.append((path, str(contents)))
    return sorted(candidates, key=lambda item: (item[0].count("/"), item[0].casefold()))[0] if candidates else ("", "")


def _retrieval_text(name: str, description: str, content: str) -> str:
    body = _FRONTMATTER_RE.sub(" ", content or "", count=1)
    body = re.sub(r"[`*_>#]", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    value = " ".join(part for part in (name, description, body) if part).strip()
    return value[:MAX_RETRIEVAL_CHARS]


def _audit_summary(audits: list[dict[str, Any]] | None) -> tuple[str, str, int, list[str]]:
    """Collapse partner audits without treating missing audits as safe."""
    if not audits:
        return "unknown", "unknown", 1, ["audit-unavailable"]
    statuses = {str(item.get("status") or "").casefold() for item in audits if isinstance(item, dict)}
    risks = {str(item.get("riskLevel") or "").casefold() for item in audits if isinstance(item, dict)}
    if "fail" in statuses or risks & {"critical", "high"}:
        return "fail", "critical" if "critical" in risks else "high", 3, ["audit-fail"]
    if "warn" in statuses or risks & {"medium", "high"}:
        return "warn", "medium" if "medium" in risks else "warn", 1, ["audit-warn"]
    if statuses and statuses <= {"pass"}:
        return "pass", "low" if "low" in risks else "none", 0, []
    return "unknown", "unknown", 1, ["audit-incomplete"]


def _stable_skill_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


class SkillsShCatalog:
    """Small async client with bounded TTL caches for skills.sh."""

    def __init__(
        self,
        *,
        api_url: str | None = None,
        oidc_token: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        search_ttl_seconds: float = DEFAULT_SEARCH_TTL_SECONDS,
        detail_ttl_seconds: float = DEFAULT_DETAIL_TTL_SECONDS,
        audit_ttl_seconds: float = DEFAULT_AUDIT_TTL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_url = (api_url or os.getenv("SKILLS_SH_API_URL", DEFAULT_API_URL)).rstrip("/")
        self._explicit_oidc_token = oidc_token
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.search_ttl_seconds = max(0.0, float(search_ttl_seconds))
        self.detail_ttl_seconds = max(0.0, float(detail_ttl_seconds))
        self.audit_ttl_seconds = max(0.0, float(audit_ttl_seconds))
        self.transport = transport
        self._cache: dict[tuple[str, str], _CacheEntry] = {}

    @property
    def configured(self) -> bool:
        """The documented API requires a current Vercel OIDC bearer token."""
        return bool(self._current_oidc_token())

    def _current_oidc_token(self) -> str:
        if self._explicit_oidc_token is not None:
            return str(self._explicit_oidc_token).strip()
        token = os.getenv("SKILLS_SH_OIDC_TOKEN", "") or os.getenv("VERCEL_OIDC_TOKEN", "")
        token_file = os.getenv("SKILLS_SH_OIDC_TOKEN_FILE", "").strip()
        if not token and token_file:
            try:
                token = Path(token_file).read_text(encoding="utf-8").strip()
            except OSError:
                token = ""
        return token.strip()

    def _cached(self, kind: str, key: str) -> Any | None:
        entry = self._cache.get((kind, key))
        if entry and entry.expires_at > time.monotonic():
            return entry.value
        if entry:
            self._cache.pop((kind, key), None)
        return None

    def _put(self, kind: str, key: str, value: Any, ttl: float) -> Any:
        if ttl > 0:
            self._cache[(kind, key)] = _CacheEntry(time.monotonic() + ttl, value)
        return value

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        token = self._current_oidc_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=False,
            ) as client:
                response = await client.get(f"{self.api_url}/{path.lstrip('/')}", params=params, headers=headers)
        except (httpx.HTTPError, OSError) as exc:
            raise SkillsShCatalogError(f"skills.sh request failed: {type(exc).__name__}") from exc
        if response.status_code == 404:
            raise SkillsShCatalogError("skills.sh resource not found")
        if response.status_code in {401, 403}:
            raise SkillsShCatalogError("skills.sh authentication rejected")
        if response.status_code >= 400:
            raise SkillsShCatalogError(f"skills.sh returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillsShCatalogError("skills.sh returned invalid JSON") from exc
        return payload if isinstance(payload, dict) else {}

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = " ".join(str(query or "").split())
        if len(query) < 2:
            return []
        limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        cache_key = f"{query.casefold()}::{limit}"
        cached = self._cached("search", cache_key)
        if cached is not None:
            return [dict(item) for item in cached]
        payload = await self._get("skills/search", params={"q": query, "limit": str(limit)})
        data = payload.get("data")
        rows = [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        return [dict(item) for item in self._put("search", cache_key, rows, self.search_ttl_seconds)]

    async def detail(self, skill_id: str) -> dict[str, Any] | None:
        skill_id = _stable_skill_id({"id": skill_id})
        if not skill_id:
            return None
        cached = self._cached("detail", skill_id)
        if cached is not None:
            return dict(cached)
        encoded = quote(skill_id, safe="/")
        try:
            payload = await self._get(f"skills/{encoded}")
        except SkillsShCatalogError:
            return None
        return dict(self._put("detail", skill_id, payload, self.detail_ttl_seconds))

    async def audit(self, skill_id: str) -> list[dict[str, Any]] | None:
        skill_id = _stable_skill_id({"id": skill_id})
        if not skill_id:
            return None
        cached = self._cached("audit", skill_id)
        if cached is not None:
            return [dict(item) for item in cached]
        encoded = quote(skill_id, safe="/")
        try:
            payload = await self._get(f"skills/audit/{encoded}")
        except SkillsShCatalogError:
            return None
        audits = payload.get("audits")
        rows = [dict(item) for item in audits if isinstance(item, dict)] if isinstance(audits, list) else []
        return [dict(item) for item in self._put("audit", skill_id, rows, self.audit_ttl_seconds)]

    async def retrieve(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search, hydrate, and audit a bounded skills.sh shortlist."""
        listing = await self.search(query, limit=max(limit, 10))
        shortlist = listing[: min(max(1, int(limit)), MAX_DETAIL_CANDIDATES)]
        details = await asyncio.gather(*(self.detail(_stable_skill_id(item)) for item in shortlist))
        audits = await asyncio.gather(*(self.audit(_stable_skill_id(item)) for item in shortlist))
        rows: list[dict[str, Any]] = []
        for rank, (listing_item, detail, partner_audits) in enumerate(zip(shortlist, details, audits)):
            if not detail:
                continue
            files = detail.get("files") if isinstance(detail.get("files"), list) else []
            entrypoint, content = _entrypoint(files)
            if len(content) > MAX_CONTENT_CHARS:
                content = ""
                entrypoint = ""
            fields = _frontmatter_fields(content)
            skill_id = _stable_skill_id(listing_item) or _stable_skill_id(detail)
            name = str(listing_item.get("name") or fields.get("name") or detail.get("slug") or skill_id)
            description = str(listing_item.get("description") or fields.get("description") or "")
            install_url = str(listing_item.get("installUrl") or "")
            page_url = str(listing_item.get("url") or f"https://skills.sh/{skill_id}")
            row: dict[str, Any] = {
                "id": skill_id,
                "name": name,
                "description": description,
                # Preserve the publisher/source from skills.sh. The registry
                # itself is recorded separately so provenance never collapses
                # every publisher into one synthetic source name.
                "source": str(listing_item.get("source") or detail.get("source") or "skills_sh"),
                "registry": "skills_sh",
                "slug": str(listing_item.get("slug") or detail.get("slug") or ""),
                "url": install_url or page_url,
                "skills_sh_url": page_url,
                "install_url": install_url,
                "skills_sh_id": skill_id,
                "source_snapshot_hash": str(detail.get("hash") or "") or None,
                "is_duplicate": bool(listing_item.get("isDuplicate")),
                "stars": 0,
                "installs": listing_item.get("installs"),
                "source_type": listing_item.get("sourceType"),
                "rank": 1.0 / (60.0 + rank + 1.0),
                "similarity": None,
                "tags": [],
                "_content": content,
                "retrieval_text": _retrieval_text(name, description, content),
                "retrieval_text_hash": hashlib.sha256(_retrieval_text(name, description, content).encode()).hexdigest(),
                "source_commit_sha": None,
                "package_completeness": "complete" if files and detail.get("hash") else "unknown",
                "dependency_closure_status": "unresolved",
                "entrypoint_truncated": 0,
                "raw": {
                    "skills_sh_id": skill_id,
                    "skills_sh_url": page_url,
                    "install_url": install_url,
                    "installs": listing_item.get("installs"),
                    "source_type": listing_item.get("sourceType"),
                    "snapshot_hash": detail.get("hash"),
                    "entrypoint_path": entrypoint,
                    "audits": partner_audits or [],
                },
            }
            status, risk_level, risk_score, risk_flags = _audit_summary(partner_audits)
            row["audit_status"] = status
            row["audit_risk_level"] = risk_level
            row["audit_count"] = len(partner_audits or [])
            row["risk_score"] = risk_score
            row["risk_flags"] = risk_flags
            if content:
                row["content_hash"] = content_hash(content)
            quality = evaluate_quality(row, content)
            row.update(quality)
            # An audit failure is a hard reject even if the static quality
            # scorer considers the markdown well-formed.
            if status == "fail":
                row["quality_status"] = "rejected"
                row["quality_reasons"] = sorted(set([*row.get("quality_reasons", []), "skills-sh-audit-fail"]))
            rows.append(row)
        return rows


_DEFAULT_CATALOG: SkillsShCatalog | None = None


def default_catalog() -> SkillsShCatalog:
    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        _DEFAULT_CATALOG = SkillsShCatalog()
    return _DEFAULT_CATALOG
