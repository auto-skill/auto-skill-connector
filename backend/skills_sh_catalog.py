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
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit
from pathlib import Path

import httpx

from quality import content_hash, evaluate_quality


DEFAULT_API_URL = "https://skills.sh/api/v1"
DEFAULT_PUBLIC_SEARCH_URL = "https://skills.sh/api/search"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_SEARCH_TTL_SECONDS = 45.0
DEFAULT_DETAIL_TTL_SECONDS = 300.0
DEFAULT_AUDIT_TTL_SECONDS = 300.0
DEFAULT_MAX_CONCURRENT_REQUESTS = max(1, int(os.getenv("SKILLS_SH_MAX_CONCURRENT_REQUESTS", "4")))
DEFAULT_MAX_RETRIES = max(0, int(os.getenv("SKILLS_SH_MAX_RETRIES", "3")))
DEFAULT_RETRY_BASE_SECONDS = max(0.0, float(os.getenv("SKILLS_SH_RETRY_BASE_SECONDS", "0.5")))
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = max(
    0.0, float(os.getenv("SKILLS_SH_MIN_REQUEST_INTERVAL_SECONDS", "0.1"))
)
MAX_RETRY_DELAY_SECONDS = 30.0
MAX_SEARCH_LIMIT = 50
MAX_DETAIL_CANDIDATES = 12
MAX_CONTENT_CHARS = 300_000
MAX_RETRIEVAL_CHARS = 1_500
MAX_PUBLIC_DESCRIPTION_CHARS = 800
MAX_PUBLIC_PAGE_METADATA = min(3, max(0, int(os.getenv("SKILLS_SH_PUBLIC_PAGE_METADATA", "1"))))

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
        public_search_url: str | None = None,
        public_page_base_url: str | None = None,
        oidc_token: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        search_ttl_seconds: float = DEFAULT_SEARCH_TTL_SECONDS,
        detail_ttl_seconds: float = DEFAULT_DETAIL_TTL_SECONDS,
        audit_ttl_seconds: float = DEFAULT_AUDIT_TTL_SECONDS,
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_url = (api_url or os.getenv("SKILLS_SH_API_URL", DEFAULT_API_URL)).rstrip("/")
        self.public_search_url = public_search_url or os.getenv(
            "SKILLS_SH_PUBLIC_SEARCH_URL", DEFAULT_PUBLIC_SEARCH_URL
        )
        self.public_page_base_url = public_page_base_url or (
            f"{urlsplit(self.public_search_url).scheme}://{urlsplit(self.public_search_url).netloc}"
        )
        self._explicit_oidc_token = oidc_token
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.search_ttl_seconds = max(0.0, float(search_ttl_seconds))
        self.detail_ttl_seconds = max(0.0, float(detail_ttl_seconds))
        self.audit_ttl_seconds = max(0.0, float(audit_ttl_seconds))
        self.max_concurrent_requests = max(1, int(max_concurrent_requests))
        self.max_retries = max(0, int(max_retries))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self.transport = transport
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        # A catalog can be used by tests across multiple asyncio.run calls;
        # bind the semaphore lazily to the loop that owns the request.
        self._request_semaphore: asyncio.Semaphore | None = None
        self._request_loop: asyncio.AbstractEventLoop | None = None
        self._pacing_lock: asyncio.Lock | None = None
        self._pacing_loop: asyncio.AbstractEventLoop | None = None
        self._last_request_at = 0.0

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

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._request_semaphore is None or self._request_loop is not loop:
            self._request_loop = loop
            self._request_semaphore = asyncio.Semaphore(self.max_concurrent_requests)
        return self._request_semaphore

    def _pacer(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._pacing_lock is None or self._pacing_loop is not loop:
            self._pacing_loop = loop
            self._pacing_lock = asyncio.Lock()
            self._last_request_at = 0.0
        return self._pacing_lock

    async def _wait_for_request_slot(self) -> None:
        if self.min_request_interval_seconds <= 0:
            return
        async with self._pacer():
            elapsed = time.monotonic() - self._last_request_at
            delay = self.min_request_interval_seconds - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "").strip()
        try:
            delay = float(retry_after) if retry_after else self.retry_base_seconds * (2**attempt)
        except ValueError:
            delay = self.retry_base_seconds * (2**attempt)
        return min(MAX_RETRY_DELAY_SECONDS, max(0.0, delay))

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        retryable_statuses = {429, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            try:
                await self._wait_for_request_slot()
                async with self._semaphore():
                    response = await client.request(method, url, **kwargs)
            except (httpx.HTTPError, OSError) as exc:
                if attempt >= self.max_retries:
                    raise SkillsShCatalogError(f"skills.sh request failed: {type(exc).__name__}") from exc
                await asyncio.sleep(min(MAX_RETRY_DELAY_SECONDS, self.retry_base_seconds * (2**attempt)))
                continue
            if response.status_code not in retryable_statuses or attempt >= self.max_retries:
                return response
            await asyncio.sleep(self._retry_delay(response, attempt))
        raise SkillsShCatalogError("skills.sh request retry budget exhausted")

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        token = self._current_oidc_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await self._request_with_retry(
                client,
                "GET",
                f"{self.api_url}/{path.lstrip('/')}",
                params=params,
                headers=headers,
            )
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
        cache_key = f"{query.casefold()}::{limit}::{'auth' if self.configured else 'public'}"
        cached = self._cached("search", cache_key)
        if cached is not None:
            return [dict(item) for item in cached]
        try:
            payload = await self._get("skills/search", params={"q": query, "limit": str(limit)}) if self.configured else {}
            data = payload.get("data")
        except SkillsShCatalogError as exc:
            if "authentication rejected" not in str(exc):
                raise
            data = None
        if data is None:
            payload = await self._public_search(query, limit)
            data = payload.get("skills")
        rows = [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        if payload.get("skills") is not None:
            rows = [self._public_listing_row(item) for item in rows]
        # Keep the last listing metadata available for a follow-up selection.
        # This is intentionally a bounded cache; it is not a second catalog.
        for item in rows:
            skill_id = _stable_skill_id(item)
            if skill_id:
                self._put("listing", skill_id, dict(item), self.search_ttl_seconds)
        return [dict(item) for item in self._put("search", cache_key, rows, self.search_ttl_seconds)]

    async def _public_search(self, query: str, limit: int) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = await self._request_with_retry(
                    client,
                    "GET",
                    self.public_search_url,
                    params={"q": query, "limit": str(limit)},
                    headers={"Accept": "application/json"},
                )
        except SkillsShCatalogError as exc:
            raise SkillsShCatalogError(f"skills.sh public search failed: {exc}") from exc
        if response.status_code >= 400:
            raise SkillsShCatalogError(f"skills.sh public search returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillsShCatalogError("skills.sh public search returned invalid JSON") from exc
        return payload if isinstance(payload, dict) else {}

    async def _public_page_metadata(self, skill_id: str) -> dict[str, Any]:
        values = await self._public_page_metadata_many([skill_id])
        return values[0] if values else {}

    @staticmethod
    def _parse_public_page_metadata(text: str) -> dict[str, Any]:
        matches = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            text,
            flags=re.I | re.S,
        )
        for raw in matches:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("@type") == "SoftwareApplication":
                return {
                    "description": str(value.get("description") or "")[:MAX_PUBLIC_DESCRIPTION_CHARS],
                    "installs": (value.get("interactionStatistic") or {}).get("userInteractionCount"),
                }
        return {}

    async def _public_page_metadata_many(self, skill_ids: list[str]) -> list[dict[str, Any]]:
        if not self.public_page_base_url:
            return [{} for _ in skill_ids]
        pending: list[str] = []
        values: dict[str, dict[str, Any]] = {}
        for skill_id in dict.fromkeys(skill_ids):
            if not skill_id:
                continue
            cached = self._cached("page", skill_id)
            if cached is not None:
                values[skill_id] = dict(cached)
            else:
                pending.append(skill_id)
        if pending:
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                ) as client:
                    responses = await asyncio.gather(
                        *(
                            self._request_with_retry(
                                client,
                                "GET",
                                f"{self.public_page_base_url.rstrip('/')}/{quote(skill_id, safe='/')}",
                                headers={"Accept": "text/html"},
                            )
                            for skill_id in pending
                        ),
                        return_exceptions=True,
                    )
            except (httpx.HTTPError, OSError):
                responses = []
            for skill_id, response in zip(pending, responses):
                metadata = (
                    self._parse_public_page_metadata(response.text)
                    if isinstance(response, httpx.Response) and response.status_code < 400
                    else {}
                )
                values[skill_id] = dict(
                    self._put("page", skill_id, metadata, self.detail_ttl_seconds)
                ) if metadata else {}
        return [values.get(skill_id, {}) for skill_id in skill_ids]

    @staticmethod
    def _public_listing_row(item: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(item.get("id") or "").strip()
        source = str(item.get("source") or "").strip()
        skill_name = str(item.get("name") or item.get("skillId") or skill_id.rsplit("/", 1)[-1])
        install_url = source if (source.startswith(("http://", "https://")) or "/" in source) else ""
        return {
            "id": skill_id,
            "slug": str(item.get("skillId") or skill_name),
            "name": skill_name,
            "source": source,
            "installs": item.get("installs"),
            "sourceType": "github" if "/" in source else "well-known",
            "installUrl": install_url,
            "url": f"https://skills.sh/{skill_id}" if skill_id else "",
            "description": str(item.get("description") or ""),
            "_public_search_only": True,
        }

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

    async def _materialize(
        self,
        shortlist: list[dict[str, Any]],
        *,
        details: list[dict[str, Any] | None] | None = None,
        audits: list[list[dict[str, Any]] | None] | None = None,
        page_metadata: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Turn catalog listings into the bounded internal retrieval record."""
        shortlist = shortlist[:MAX_DETAIL_CANDIDATES]
        detail_jobs = [
            self.detail(_stable_skill_id(item)) if not item.get("_public_search_only") else asyncio.sleep(0, result=None)
            for item in shortlist
        ]
        audit_jobs = [
            self.audit(_stable_skill_id(item)) if not item.get("_public_search_only") else asyncio.sleep(0, result=None)
            for item in shortlist
        ]
        if details is None:
            details = list(await asyncio.gather(*detail_jobs))
        if audits is None:
            audits = list(await asyncio.gather(*audit_jobs))
        if page_metadata is None:
            page_ids = [
                _stable_skill_id(item) if item.get("_public_search_only") and index < MAX_PUBLIC_PAGE_METADATA else ""
                for index, item in enumerate(shortlist)
            ]
            page_metadata = await self._public_page_metadata_many(page_ids)
        rows: list[dict[str, Any]] = []
        for rank, (listing_item, detail, partner_audits, page_meta) in enumerate(
            zip(shortlist, details, audits, page_metadata)
        ):
            detail = detail or {}
            files = detail.get("files") if isinstance(detail.get("files"), list) else []
            entrypoint, content = _entrypoint(files)
            if len(content) > MAX_CONTENT_CHARS:
                content = ""
                entrypoint = ""
            fields = _frontmatter_fields(content)
            skill_id = _stable_skill_id(listing_item) or _stable_skill_id(detail)
            name = str(listing_item.get("name") or fields.get("name") or detail.get("slug") or skill_id)
            description = str(
                listing_item.get("description")
                or fields.get("description")
                or page_meta.get("description")
                or ""
            )
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
                "installs": listing_item.get("installs") or page_meta.get("installs"),
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
            if not content:
                # Public website search is a discovery-only lane. Keep the
                # record visible as a hint even when detail/audit endpoints
                # require OIDC, but never let missing bytes appear trusted.
                quality["content_hash"] = None
                quality["quality_status"] = "metadata_only"
                quality["quality_reasons"] = sorted(
                    set([*quality.get("quality_reasons", []), "detail-unavailable"])
                )
            row.update(quality)
            # An audit failure is a hard reject even if the static quality
            # scorer considers the markdown well-formed.
            if status == "fail":
                row["quality_status"] = "rejected"
                row["quality_reasons"] = sorted(set([*row.get("quality_reasons", []), "skills-sh-audit-fail"]))
            rows.append(row)
        return rows

    async def retrieve(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search, hydrate, and audit a bounded skills.sh shortlist."""
        listing = await self.search(query, limit=max(limit, 10))
        shortlist = listing[: min(max(1, int(limit)), MAX_DETAIL_CANDIDATES)]
        return await self._materialize(shortlist)

    async def retrieve_ids(self, skill_ids: list[str], limit: int = 10) -> list[dict[str, Any]]:
        """Rehydrate previously offered skills without consulting local storage.

        Follow-up requests may carry a stable skills.sh ID instead of a fresh
        natural-language query.  Authenticated callers get the authoritative
        detail/audit records.  Tokenless callers can only reuse a short-lived
        listing cache populated by public search, and therefore remain
        metadata-only hints.
        """
        ids = list(dict.fromkeys(str(value or "").strip() for value in skill_ids if str(value or "").strip()))
        ids = ids[: min(max(1, int(limit)), MAX_DETAIL_CANDIDATES)]
        if not ids:
            return []
        listings: list[dict[str, Any]] = []
        for skill_id in ids:
            listing = self._cached("listing", skill_id)
            if listing is not None:
                listings.append(dict(listing))
            else:
                listings.append({
                    "id": skill_id,
                    "name": skill_id.rsplit("/", 1)[-1],
                    "source": "/".join(skill_id.split("/")[:-1]),
                    "url": f"https://skills.sh/{skill_id}",
                    "_public_search_only": not self.configured,
                })
        if not self.configured:
            # A public page is not an authoritative detail/audit source.  Do
            # not turn an arbitrary user-provided ID into a trusted row.
            listings = [item for item in listings if self._cached("listing", _stable_skill_id(item)) is not None]
            if not listings:
                return []
        else:
            for item in listings:
                item.pop("_public_search_only", None)
        return await self._materialize(listings)


_DEFAULT_CATALOG: SkillsShCatalog | None = None


def default_catalog() -> SkillsShCatalog:
    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        _DEFAULT_CATALOG = SkillsShCatalog()
    return _DEFAULT_CATALOG
