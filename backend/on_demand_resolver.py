"""Bounded, feature-gated internet skill resolution.

The production router still owns the normal indexed retrieval path.  This
module is an experiment seam for resolving a small shortlist just in time:

* providers discover metadata and fetch immutable ``SKILL.md`` entrypoints;
* the resolver applies hard budgets before every provider/fetch operation;
* only a bounded in-memory hot cache is used (no SQLite/CAS writes); and
* the returned rows intentionally match the existing deterministic reranker
  and route-contract inputs.

The first provider uses skills.sh search for metadata only and a separate
GitHub adapter for immutable commit/blob retrieval. The skills.sh detail and
audit endpoints are deliberately outside this request-time path.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import os
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Protocol
from urllib.parse import quote, urlsplit

import httpx

from capsule_compiler import CAPSULE_VERSION
from context_guard import (
    DEFAULT_CAPSULE_CHARS,
    POLICY_VERSION as CONTEXT_POLICY_VERSION,
    build_context_guard,
)
from quality import (
    CONFIG_VERSION as QUALITY_CONFIG_VERSION,
    canonicalize_skill_content,
    content_digest,
    content_hash,
    dedupe_by_content_hash,
    evaluate_quality,
    has_valid_skill_frontmatter,
    readiness_for_skill,
    rerank_candidates,
    skill_capability_flags,
)
from query_compiler import CompiledIntent, compile_intent_query
from skills_sh_catalog import SkillsShCatalog, SkillsShCatalogError
from token_budget import TokenCounter, default_token_counter


def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


def _env_float(name: str, default: float, minimum: float, maximum: float) -> float:
    try:
        value = float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        value = default
    return max(minimum, min(maximum, value))


ON_DEMAND_MAX_PROVIDER_CALLS = _env_int("AUTOSKILL_ON_DEMAND_MAX_PROVIDER_CALLS", 2, 1, 2)
ON_DEMAND_MAX_FETCHES = _env_int("AUTOSKILL_ON_DEMAND_MAX_FETCHES", 2, 1, 2)
ON_DEMAND_MAX_BYTES = _env_int("AUTOSKILL_ON_DEMAND_MAX_BYTES", 256 * 1024, 1_024, 256 * 1024)
ON_DEMAND_MAX_ENTRYPOINT_BYTES = _env_int(
    "AUTOSKILL_ON_DEMAND_MAX_ENTRYPOINT_BYTES", 128 * 1024, 1_024, 128 * 1024
)
_ROUTE_SKILL_FIND_BUDGET_MS = _env_int("ROUTE_SKILL_FIND_WARN_MS", 500, 50, 450)
ON_DEMAND_WALL_MS = min(
    _env_int("AUTOSKILL_ON_DEMAND_WALL_MS", 450, 50, 450),
    _ROUTE_SKILL_FIND_BUDGET_MS,
)
ON_DEMAND_FETCH_TOP_K = _env_int("AUTOSKILL_ON_DEMAND_FETCH_TOP_K", 2, 1, 2)
ON_DEMAND_CANDIDATE_LIMIT = _env_int("AUTOSKILL_ON_DEMAND_CANDIDATE_LIMIT", 6, 1, 6)
ON_DEMAND_TOKEN_BUDGET = _env_int("AUTOSKILL_ON_DEMAND_TOKEN_BUDGET", 1_000, 1, 4_000)
ON_DEMAND_TRUST_EPOCH = "trust-v1"
ON_DEMAND_CACHE_TTL_SECONDS = _env_float(
    "AUTOSKILL_ON_DEMAND_CACHE_TTL_SECONDS", 600.0, 0.0, 900.0
)
ON_DEMAND_CACHE_ENTRIES = _env_int("AUTOSKILL_ON_DEMAND_CACHE_ENTRIES", 32, 1, 256)
ON_DEMAND_CACHE_BYTES = _env_int("AUTOSKILL_ON_DEMAND_CACHE_BYTES", 2_000_000, 1_024, 8_000_000)
ON_DEMAND_METADATA_TTL_SECONDS = _env_float(
    "AUTOSKILL_ON_DEMAND_METADATA_TTL_SECONDS", 300.0, 0.0, 900.0
)
ON_DEMAND_METADATA_ENTRIES = _env_int("AUTOSKILL_ON_DEMAND_METADATA_ENTRIES", 128, 1, 512)
ON_DEMAND_PROVIDER_TIMEOUT_SECONDS = _env_float(
    "AUTOSKILL_ON_DEMAND_PROVIDER_TIMEOUT_SECONDS", 0.35, 0.05, 1.0
)
ON_DEMAND_GITHUB_API_URL = os.getenv("AUTOSKILL_GITHUB_API_URL", "https://api.github.com").rstrip("/")
ON_DEMAND_GITHUB_RAW_URL = os.getenv(
    "AUTOSKILL_GITHUB_RAW_URL", "https://raw.githubusercontent.com"
).rstrip("/")
_COMMIT_RE = re.compile(r"^[0-9a-f]{40}$", re.I)
_SAFE_REF_RE = re.compile(r"^[A-Za-z0-9._/-]{1,200}$")
_SUPPORTED_LICENSES = frozenset(
    {
        "0bsd",
        "apache-2.0",
        "bsd-2-clause",
        "bsd-3-clause",
        "isc",
        "mit",
        "mpl-2.0",
    }
)
_LICENSE_ALIASES = {
    "mit license": "mit",
    "apache license 2.0": "apache-2.0",
    "apache 2.0": "apache-2.0",
    "bsd 2-clause": "bsd-2-clause",
    "bsd 3-clause": "bsd-3-clause",
}


class OnDemandProviderError(RuntimeError):
    """A provider failed or could not establish immutable source provenance."""


class ResolverBudgetExceeded(RuntimeError):
    """The experiment hit a provider, fetch, byte, or wall-clock budget."""


@dataclass(frozen=True)
class SkillSource:
    """Metadata returned by a discovery provider; it contains no raw body."""

    key: str
    name: str
    description: str
    source_url: str
    provider: str = "skills_sh"
    rank: int = 0
    snapshot_hash: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    repository: str | None = None
    path: str = "SKILL.md"
    revision: str | None = None
    commit_sha: str | None = None
    declared_content_hash: str | None = None
    declared_content_digest: str | None = None
    license: str | None = None
    freshness: str | None = None


@dataclass(frozen=True)
class FetchedSkill:
    """A fetched entrypoint plus the provenance needed to pin it in memory."""

    content: str
    snapshot_hash: str | None
    source_commit_sha: str | None = None
    declared_content_hash: str | None = None
    raw_content_digest: str | None = None
    declared_content_digest: str | None = None
    source_url: str | None = None
    entrypoint_path: str = "SKILL.md"
    byte_count: int = 0
    audit_status: str = "unknown"
    audit_risk_level: str = "unknown"
    risk_score: int = 1
    risk_flags: tuple[str, ...] = ()
    entrypoint_truncated: bool = False
    rejection_reason: str | None = None


class SkillDiscoveryProvider(Protocol):
    """Metadata-only discovery boundary for on-demand resolution."""

    name: str

    async def discover(
        self, query: str, limit: int, *, deadline: float | None = None
    ) -> list[SkillSource]:
        ...


class ImmutableSkillFetcher(Protocol):
    """Immutable, bounded entrypoint retrieval boundary."""

    async def fetch(
        self,
        source: SkillSource,
        max_bytes: int,
        *,
        deadline: float | None = None,
    ) -> FetchedSkill | None:
        ...


class SkillSourceProvider(SkillDiscoveryProvider, ImmutableSkillFetcher, Protocol):
    """Composite adapter used by the resolver while keeping the seams separate."""

    pass


@dataclass
class ResolverBudget:
    max_provider_calls: int = ON_DEMAND_MAX_PROVIDER_CALLS
    max_fetches: int = ON_DEMAND_MAX_FETCHES
    max_bytes: int = ON_DEMAND_MAX_BYTES
    max_wall_ms: int = ON_DEMAND_WALL_MS
    started_at: float = field(default_factory=time.monotonic)
    provider_calls: int = 0
    fetches: int = 0
    bytes_read: int = 0

    def elapsed_ms(self) -> int:
        return int((time.monotonic() - self.started_at) * 1000)

    def reserve(self, kind: str) -> None:
        if self.elapsed_ms() >= self.max_wall_ms:
            raise ResolverBudgetExceeded("wall-clock budget exceeded")
        if kind == "discover" and self.provider_calls >= self.max_provider_calls:
            raise ResolverBudgetExceeded("provider-call budget exceeded")
        if kind == "fetch" and self.fetches >= self.max_fetches:
            raise ResolverBudgetExceeded("fetch-count budget exceeded")
        if kind == "discover":
            self.provider_calls += 1
        if kind == "fetch":
            self.fetches += 1

    def account_bytes(self, count: int) -> None:
        count = max(0, int(count))
        if self.bytes_read + count > self.max_bytes:
            raise ResolverBudgetExceeded("byte budget exceeded")
        self.bytes_read += count

    def as_dict(self) -> dict[str, int]:
        return {
            "provider_calls": self.provider_calls,
            "fetches": self.fetches,
            "bytes_read": self.bytes_read,
            "max_provider_calls": self.max_provider_calls,
            "max_fetches": self.max_fetches,
            "max_bytes": self.max_bytes,
            "max_wall_ms": self.max_wall_ms,
        }


@dataclass
class ResolveResult:
    candidates: list[dict[str, Any]]
    status: str
    cache_status: str
    provider: str
    budget: dict[str, int]
    latency_ms: int
    warnings: list[str] = field(default_factory=list)

    def debug(self) -> dict[str, Any]:
        """Return bounded, privacy-safe resolver diagnostics for route debug."""
        return {
            "variant": "on-demand",
            "provider": self.provider,
            "status": self.status,
            "cache": self.cache_status,
            "candidate_count": len(self.candidates),
            "latency_ms": self.latency_ms,
            "budget": dict(self.budget),
            "warnings": list(self.warnings),
        }


class ProviderCircuitBreaker:
    """Small process-local breaker so an outage cannot fan out per route."""

    def __init__(self, *, failure_threshold: int = 3, cooldown_seconds: float = 60.0) -> None:
        self.failure_threshold = max(1, int(failure_threshold))
        self.cooldown_seconds = max(1.0, float(cooldown_seconds))
        self._failures = 0
        self._opened_at = 0.0

    def allow(self) -> bool:
        if self._opened_at <= 0:
            return True
        if time.monotonic() - self._opened_at >= self.cooldown_seconds:
            self._opened_at = 0.0
            self._failures = 0
            return True
        return False

    def success(self) -> None:
        self._failures = 0
        self._opened_at = 0.0

    def failure(self) -> None:
        self._failures += 1
        if self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()

    def debug(self) -> dict[str, Any]:
        return {
            "open": not self.allow(),
            "failures": self._failures,
            "failure_threshold": self.failure_threshold,
            "cooldown_seconds": self.cooldown_seconds,
        }


@dataclass(frozen=True)
class _MetadataEntry:
    expires_at: float
    sources: tuple[SkillSource, ...]


class _MetadataIndex:
    """Bounded metadata-only query cache; it never stores skill bodies."""

    def __init__(self, *, ttl_seconds: float, max_entries: int) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self._entries: OrderedDict[str, _MetadataEntry] = OrderedDict()

    def get(self, key: str, limit: int) -> list[SkillSource] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            return None
        self._entries.move_to_end(key)
        return list(entry.sources[: max(1, int(limit))])

    def put(self, key: str, sources: list[SkillSource]) -> None:
        if self.ttl_seconds <= 0 or not sources:
            return
        safe_sources: list[SkillSource] = []
        raw_keys = {"_content", "body", "content", "contents", "files", "raw"}
        for source in sources[:ON_DEMAND_CANDIDATE_LIMIT]:
            safe_metadata: dict[str, Any] = {}
            for metadata_key, metadata_value in (source.metadata or {}).items():
                if str(metadata_key).casefold() in raw_keys:
                    continue
                if isinstance(metadata_value, (str, int, float, bool)):
                    safe_metadata[str(metadata_key)] = str(metadata_value)[:512]
                elif isinstance(metadata_value, (list, tuple)):
                    safe_metadata[str(metadata_key)] = [
                        str(item)[:128] for item in metadata_value[:16]
                    ]
            safe_sources.append(replace(source, metadata=safe_metadata))
        self._entries.pop(key, None)
        while len(self._entries) >= self.max_entries:
            self._entries.popitem(last=False)
        self._entries[key] = _MetadataEntry(
            expires_at=time.monotonic() + self.ttl_seconds,
            sources=tuple(copy.deepcopy(safe_sources)),
        )


@dataclass(frozen=True)
class _HotCacheEntry:
    expires_at: float
    value: tuple[dict[str, Any], ...]
    byte_count: int


class _HotRouteCache:
    """Small process-local cache for metadata plus a few hot entrypoints."""

    def __init__(self, *, ttl_seconds: float, max_entries: int, max_bytes: int) -> None:
        self.ttl_seconds = max(0.0, float(ttl_seconds))
        self.max_entries = max(1, int(max_entries))
        self.max_bytes = max(1, int(max_bytes))
        self._entries: OrderedDict[str, _HotCacheEntry] = OrderedDict()
        self._byte_total = 0

    @staticmethod
    def _content_bytes(rows: list[dict[str, Any]]) -> int:
        return sum(
            len(
                str(
                    (row.get("_on_demand_context_guard") or {}).get("capsule")
                    or ""
                ).encode("utf-8")
            )
            for row in rows
        )

    def get(self, key: str) -> list[dict[str, Any]] | None:
        entry = self._entries.get(key)
        if entry is None:
            return None
        if entry.expires_at <= time.monotonic():
            self._entries.pop(key, None)
            self._byte_total -= entry.byte_count
            return None
        self._entries.move_to_end(key)
        return copy.deepcopy(list(entry.value))

    def put(self, key: str, rows: list[dict[str, Any]]) -> None:
        if self.ttl_seconds <= 0:
            return
        value = copy.deepcopy(rows)
        for row in value:
            if isinstance(row, dict):
                # Verified capsules/provenance may remain hot; raw source
                # bytes never cross the request boundary into this cache.
                row.pop("_content", None)
                row["_on_demand_cached"] = True
        byte_count = self._content_bytes(value)
        if byte_count > self.max_bytes:
            return
        previous = self._entries.pop(key, None)
        if previous:
            self._byte_total -= previous.byte_count
        while self._entries and (
            len(self._entries) >= self.max_entries or self._byte_total + byte_count > self.max_bytes
        ):
            _old_key, old = self._entries.popitem(last=False)
            self._byte_total -= old.byte_count
        self._entries[key] = _HotCacheEntry(
            expires_at=time.monotonic() + self.ttl_seconds,
            value=tuple(value),
            byte_count=byte_count,
        )
        self._byte_total += byte_count


def _normalise_query(value: str) -> str:
    return " ".join(str(value or "").split())[:3_000]


def _cache_key(
    task: str,
    intent: CompiledIntent,
    limit: int,
    *,
    tokenizer_id: str,
    max_tokens: int,
    max_capsule_chars: int,
) -> str:
    material = "\x1f".join(
        [
            _normalise_query(task).casefold(),
            intent.compiler_version,
            intent.compressed_query,
            "\x1e".join(intent.query_variants),
            str(limit),
            tokenizer_id,
            str(max_tokens),
            str(max_capsule_chars),
            CAPSULE_VERSION,
            CONTEXT_POLICY_VERSION,
            ON_DEMAND_TRUST_EPOCH,
        ]
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _coerce_source(value: Any, rank: int) -> SkillSource | None:
    if isinstance(value, SkillSource):
        return value
    if not isinstance(value, dict):
        return None
    key = str(value.get("key") or value.get("id") or value.get("skills_sh_id") or "").strip()
    if not key:
        return None
    source_url = str(
        value.get("source_url")
        or value.get("url")
        or value.get("skills_sh_url")
        or ""
    ).strip()
    metadata = {
        str(k): v
        for k, v in value.items()
        if k
        not in {
            "key",
            "id",
            "skills_sh_id",
            "name",
            "description",
            "summary",
            "source_url",
            "url",
            "skills_sh_url",
            "provider",
            "rank",
            "snapshot_hash",
            "source_snapshot_hash",
            "repository",
            "repo",
            "path",
            "entrypoint_path",
            "skill_path",
            "revision",
            "ref",
            "commit_sha",
            "commitSha",
            "content_hash",
            "contentHash",
            "content_digest",
            "contentDigest",
            "license",
            "freshness",
            "_content",
        }
    }
    repository = str(
        value.get("repository")
        or value.get("repo")
        or value.get("source_repo")
        or value.get("install_url")
        or value.get("source")
        or ""
    ).strip()
    if repository.startswith(("https://", "http://")):
        parsed = urlsplit(repository)
        if parsed.netloc.casefold() in {"github.com", "www.github.com"}:
            repository = parsed.path.strip("/")
        else:
            repository = ""
    repository = repository.strip("/") or None
    path = str(
        value.get("path")
        or value.get("entrypoint_path")
        or value.get("skill_path")
        or metadata.get("entrypoint_path")
        or "SKILL.md"
    ).replace("\\", "/").strip("/")
    revision = str(value.get("revision") or value.get("ref") or "").strip() or None
    commit_sha = str(value.get("commit_sha") or value.get("commitSha") or "").strip() or None
    declared_digest = str(
        value.get("content_digest")
        or value.get("contentDigest")
        or ""
    ).strip() or None
    declared_hash = str(
        value.get("content_hash")
        or value.get("contentHash")
        or ""
    ).strip() or None
    try:
        source_rank = int(value.get("rank") or rank)
    except (TypeError, ValueError):
        source_rank = rank
    return SkillSource(
        key=key,
        name=str(value.get("name") or key.rsplit("/", 1)[-1])[:256],
        description=str(value.get("description") or value.get("summary") or "")[:2_000],
        source_url=source_url[:2_000],
        provider=str(value.get("provider") or "skills_sh"),
        rank=source_rank,
        snapshot_hash=str(value.get("snapshot_hash") or value.get("source_snapshot_hash") or "") or None,
        metadata=metadata,
        repository=repository,
        path=path or "SKILL.md",
        revision=revision,
        commit_sha=commit_sha,
        declared_content_hash=declared_hash,
        declared_content_digest=declared_digest,
        license=str(value.get("license") or "").strip() or None,
        freshness=str(value.get("freshness") or value.get("updated_at") or value.get("updatedAt") or "").strip() or None,
    )


def _coerce_fetch(value: Any) -> FetchedSkill | None:
    if value is None:
        return None
    if isinstance(value, FetchedSkill):
        return value
    if not isinstance(value, dict):
        return None
    risk_flags = value.get("risk_flags") or value.get("riskFlags") or ()
    if isinstance(risk_flags, str):
        risk_flags = (risk_flags,)
    raw_risk_score = value.get("risk_score")
    try:
        risk_score = int(raw_risk_score) if raw_risk_score is not None else 1
    except (TypeError, ValueError):
        risk_score = 1
    return FetchedSkill(
        content=str(value.get("content") or ""),
        snapshot_hash=str(value.get("snapshot_hash") or value.get("source_snapshot_hash") or "") or None,
        source_commit_sha=str(value.get("source_commit_sha") or value.get("commit_sha") or "") or None,
        declared_content_hash=str(value.get("declared_content_hash") or value.get("content_hash") or "") or None,
        raw_content_digest=str(value.get("raw_content_digest") or "") or None,
        declared_content_digest=str(
            value.get("declared_content_digest")
            or value.get("content_digest")
            or value.get("contentDigest")
            or ""
        ) or None,
        source_url=str(value.get("source_url") or "") or None,
        entrypoint_path=str(value.get("entrypoint_path") or "SKILL.md"),
        byte_count=int(value.get("byte_count") or 0),
        audit_status=str(value.get("audit_status") or "unknown"),
        audit_risk_level=str(value.get("audit_risk_level") or "unknown"),
        risk_score=risk_score,
        risk_flags=tuple(str(item) for item in risk_flags if item),
        entrypoint_truncated=bool(value.get("entrypoint_truncated") or value.get("truncated")),
        rejection_reason=str(value.get("rejection_reason") or "") or None,
    )


def _github_repo(value: str | None) -> str | None:
    raw = str(value or "").strip()
    if raw.startswith(("https://", "http://")):
        parsed = urlsplit(raw)
        if parsed.netloc.casefold() not in {"github.com", "www.github.com"}:
            return None
        raw = parsed.path.strip("/")
    raw = raw.strip("/")
    parts = raw.split("/")
    if len(parts) != 2 or any(not part or part in {".", ".."} for part in parts):
        return None
    if not all(re.fullmatch(r"[A-Za-z0-9_.-]+", part) for part in parts):
        return None
    return "/".join(parts)


def _safe_entrypoint_path(value: str | None) -> str | None:
    path = str(value or "").replace("\\", "/").strip("/")
    parts = path.split("/") if path else []
    if not parts or any(part in {"", ".", ".."} for part in parts):
        return None
    if parts[-1].casefold() != "skill.md":
        return None
    return "/".join(parts)


def _supported_license(value: str | None) -> bool:
    """Allow only a conservative SPDX-like license subset when declared."""

    normalized = " ".join(str(value or "").strip().casefold().split())
    if not normalized:
        # Discovery metadata is often incomplete; missing audit/license data
        # stays advisory. A declared unsupported license is fail-closed.
        return True
    normalized = _LICENSE_ALIASES.get(normalized, normalized)
    return normalized in _SUPPORTED_LICENSES


class GitHubImmutableFetcher:
    """Fetch one complete SKILL.md at a resolved Git commit.

    This adapter is intentionally text-only. It does not clone repositories,
    follow redirects, execute package code, or retain the response body after
    the caller receives the verified result.
    """

    name = "github_immutable_blob"

    def __init__(
        self,
        *,
        api_url: str | None = None,
        raw_url: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout_seconds: float = ON_DEMAND_PROVIDER_TIMEOUT_SECONDS,
        token: str | None = None,
    ) -> None:
        self.api_url = (api_url or ON_DEMAND_GITHUB_API_URL).rstrip("/")
        self.raw_url = (raw_url or ON_DEMAND_GITHUB_RAW_URL).rstrip("/")
        self.transport = transport
        self.timeout_seconds = max(0.05, float(timeout_seconds))
        self.token = token or os.getenv("AUTOSKILL_GITHUB_TOKEN", "").strip()

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "auto-skill-on-demand/1",
        }
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        return headers

    @staticmethod
    def _timeout(deadline: float | None, fallback: float) -> float:
        if deadline is None:
            return fallback
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise ResolverBudgetExceeded("wall-clock budget exceeded")
        return max(0.01, min(fallback, remaining))

    async def _resolve_commit(
        self,
        client: httpx.AsyncClient,
        source: SkillSource,
        *,
        deadline: float | None,
    ) -> str:
        supplied_commit = str(source.commit_sha or "").strip()
        if supplied_commit:
            if not _COMMIT_RE.fullmatch(supplied_commit):
                raise OnDemandProviderError("GitHub commit_sha is not immutable")
            return supplied_commit.lower()
        supplied = str(source.revision or "").strip()
        if _COMMIT_RE.fullmatch(supplied):
            return supplied.lower()
        if supplied and (
            not _SAFE_REF_RE.fullmatch(supplied)
            or any(part in {"", ".", ".."} for part in supplied.split("/"))
        ):
            raise OnDemandProviderError("unsafe GitHub revision")
        repository = _github_repo(source.repository)
        if not repository:
            raise OnDemandProviderError("GitHub repository is missing or invalid")
        ref = supplied or "HEAD"
        if ref.casefold() == "head":
            repository_response = await client.get(
                f"{self.api_url}/repos/{repository}",
                headers=self._headers(),
                timeout=self._timeout(deadline, self.timeout_seconds),
            )
            if 300 <= repository_response.status_code < 400:
                raise OnDemandProviderError("GitHub repository redirect rejected")
            if repository_response.status_code >= 400:
                raise OnDemandProviderError(
                    f"GitHub repository lookup failed ({repository_response.status_code})"
                )
            try:
                ref = str(repository_response.json().get("default_branch") or "").strip()
            except (AttributeError, TypeError, ValueError):
                ref = ""
            if not ref or not _SAFE_REF_RE.fullmatch(ref):
                raise OnDemandProviderError("GitHub default branch missing or unsafe")
        url = f"{self.api_url}/repos/{repository}/commits/{quote(ref, safe='')}"
        response = await client.get(
            url,
            headers=self._headers(),
            timeout=self._timeout(deadline, self.timeout_seconds),
        )
        if 300 <= response.status_code < 400:
            raise OnDemandProviderError("GitHub commit redirect rejected")
        if response.status_code >= 400:
            raise OnDemandProviderError(f"GitHub commit lookup failed ({response.status_code})")
        try:
            commit = str(response.json().get("sha") or "").strip()
        except (AttributeError, TypeError, ValueError):
            commit = ""
        if not _COMMIT_RE.fullmatch(commit):
            raise OnDemandProviderError("GitHub did not return an immutable commit")
        return commit.lower()

    async def fetch(
        self,
        source: SkillSource,
        max_bytes: int,
        *,
        deadline: float | None = None,
    ) -> FetchedSkill | None:
        repository = _github_repo(source.repository)
        path = _safe_entrypoint_path(source.path)
        if not repository or not path:
            return FetchedSkill(
                content="",
                snapshot_hash=None,
                rejection_reason="github-source-or-entrypoint-invalid",
            )
        if self.transport is None:
            if (
                urlsplit(self.api_url).scheme.casefold() != "https"
                or urlsplit(self.api_url).netloc.casefold() != "api.github.com"
            ):
                raise OnDemandProviderError("GitHub API host is not allowlisted")
            if (
                urlsplit(self.raw_url).scheme.casefold() != "https"
                or urlsplit(self.raw_url).netloc.casefold() != "raw.githubusercontent.com"
            ):
                raise OnDemandProviderError("GitHub raw host is not allowlisted")
        max_bytes = max(1, int(max_bytes))
        try:
            timeout = self._timeout(deadline, self.timeout_seconds)
            async with httpx.AsyncClient(
                transport=self.transport,
                follow_redirects=False,
                timeout=timeout,
            ) as client:
                commit = await self._resolve_commit(client, source, deadline=deadline)
                blob_url = f"{self.raw_url}/{repository}/{commit}/{quote(path, safe='/')}"
                declared_length: int | None = None
                async with client.stream(
                    "GET",
                    blob_url,
                    headers={**self._headers(), "Accept": "text/plain"},
                    timeout=self._timeout(deadline, self.timeout_seconds),
                ) as response:
                    if response.status_code in {401, 403, 429, 500, 502, 503, 504}:
                        raise OnDemandProviderError(
                            f"GitHub blob provider unavailable ({response.status_code})"
                        )
                    if 300 <= response.status_code < 400:
                        raise OnDemandProviderError("GitHub blob redirect rejected")
                    if response.status_code >= 400:
                        return FetchedSkill(
                            content="",
                            snapshot_hash=commit,
                            source_commit_sha=commit,
                            source_url=f"https://github.com/{repository}/blob/{commit}/{path}",
                            entrypoint_path=path,
                            rejection_reason=f"GitHub blob fetch failed ({response.status_code})",
                        )
                    content_length = response.headers.get("content-length")
                    try:
                        if content_length is not None:
                            declared_length = int(content_length)
                        if declared_length is not None and declared_length > max_bytes:
                            return FetchedSkill(
                                content="",
                                snapshot_hash=commit,
                                source_commit_sha=commit,
                                source_url=f"https://github.com/{repository}/blob/{commit}/{path}",
                                entrypoint_path=path,
                                rejection_reason="SKILL.md exceeds byte budget",
                            )
                    except (TypeError, ValueError):
                        pass
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > max_bytes:
                            return FetchedSkill(
                                content="",
                                snapshot_hash=commit,
                                source_commit_sha=commit,
                                source_url=f"https://github.com/{repository}/blob/{commit}/{path}",
                                entrypoint_path=path,
                                rejection_reason="SKILL.md exceeds byte budget",
                            )
                if declared_length is not None and len(body) != declared_length:
                    return FetchedSkill(
                        content="",
                        snapshot_hash=commit,
                        source_commit_sha=commit,
                        source_url=f"https://github.com/{repository}/blob/{commit}/{path}",
                        entrypoint_path=path,
                        rejection_reason="SKILL.md response ended before declared length",
                    )
        except asyncio.TimeoutError:
            raise
        except httpx.TimeoutException as exc:
            raise OnDemandProviderError("GitHub fetch timed out") from exc
        except (httpx.HTTPError, OSError) as exc:
            raise OnDemandProviderError(f"GitHub fetch failed ({type(exc).__name__})") from exc
        try:
            text = bytes(body).decode("utf-8")
        except UnicodeDecodeError:
            return FetchedSkill(
                content="",
                snapshot_hash=commit,
                source_commit_sha=commit,
                source_url=f"https://github.com/{repository}/blob/{commit}/{path}",
                entrypoint_path=path,
                rejection_reason="SKILL.md is not valid UTF-8",
            )
        if not text:
            return FetchedSkill(
                content="",
                snapshot_hash=commit,
                source_commit_sha=commit,
                source_url=f"https://github.com/{repository}/blob/{commit}/{path}",
                entrypoint_path=path,
                rejection_reason="SKILL.md is empty",
            )
        raw_digest = hashlib.sha256(bytes(body)).hexdigest()
        raw_risk_score = source.metadata.get("risk_score")
        try:
            risk_score = int(raw_risk_score) if raw_risk_score is not None else 1
        except (TypeError, ValueError):
            risk_score = 1
        return FetchedSkill(
            content=text,
            snapshot_hash=commit,
            source_commit_sha=commit,
            raw_content_digest=raw_digest,
            declared_content_hash=source.declared_content_hash,
            declared_content_digest=source.declared_content_digest,
            source_url=f"https://github.com/{repository}/blob/{commit}/{path}",
            entrypoint_path=path,
            byte_count=len(body),
            audit_status=str(source.metadata.get("audit_status") or "unknown"),
            audit_risk_level=str(source.metadata.get("audit_risk_level") or "unknown"),
            risk_score=risk_score,
            risk_flags=tuple(str(value) for value in source.metadata.get("risk_flags") or [] if value),
        )


class OnDemandResolver:
    """Resolve a task from a bounded provider shortlist."""

    def __init__(
        self,
        provider: SkillSourceProvider,
        *,
        max_provider_calls: int = ON_DEMAND_MAX_PROVIDER_CALLS,
        max_fetches: int = ON_DEMAND_MAX_FETCHES,
        max_bytes: int = ON_DEMAND_MAX_BYTES,
        max_entrypoint_bytes: int = ON_DEMAND_MAX_ENTRYPOINT_BYTES,
        max_wall_ms: int = ON_DEMAND_WALL_MS,
        fetch_top_k: int = ON_DEMAND_FETCH_TOP_K,
        max_token_budget: int = ON_DEMAND_TOKEN_BUDGET,
        token_counter: TokenCounter | None = None,
        cache: _HotRouteCache | None = None,
        metadata_index: _MetadataIndex | None = None,
        circuit_breaker: ProviderCircuitBreaker | None = None,
        max_capsule_chars: int = DEFAULT_CAPSULE_CHARS,
    ) -> None:
        self.provider = provider
        self.max_provider_calls = max(1, min(int(max_provider_calls), ON_DEMAND_MAX_PROVIDER_CALLS))
        self.max_fetches = max(1, min(int(max_fetches), ON_DEMAND_MAX_FETCHES))
        self.max_bytes = max(1_024, min(int(max_bytes), ON_DEMAND_MAX_BYTES))
        self.max_entrypoint_bytes = max(
            1_024,
            min(int(max_entrypoint_bytes), ON_DEMAND_MAX_ENTRYPOINT_BYTES, self.max_bytes),
        )
        self.max_wall_ms = max(50, min(int(max_wall_ms), ON_DEMAND_WALL_MS))
        self.fetch_top_k = max(1, min(int(fetch_top_k), ON_DEMAND_FETCH_TOP_K))
        self.max_token_budget = max(1, min(int(max_token_budget), ON_DEMAND_TOKEN_BUDGET))
        self.token_counter = token_counter if token_counter is not None else default_token_counter()
        self.max_capsule_chars = max(400, min(int(max_capsule_chars), DEFAULT_CAPSULE_CHARS))
        self.cache = cache or _HotRouteCache(
            ttl_seconds=ON_DEMAND_CACHE_TTL_SECONDS,
            max_entries=ON_DEMAND_CACHE_ENTRIES,
            max_bytes=ON_DEMAND_CACHE_BYTES,
        )
        self.metadata_index = metadata_index or _MetadataIndex(
            ttl_seconds=ON_DEMAND_METADATA_TTL_SECONDS,
            max_entries=ON_DEMAND_METADATA_ENTRIES,
        )
        self.circuit_breaker = circuit_breaker or ProviderCircuitBreaker()

    def _budget(self) -> ResolverBudget:
        return ResolverBudget(
            max_provider_calls=self.max_provider_calls,
            max_fetches=self.max_fetches,
            max_bytes=self.max_bytes,
            max_wall_ms=self.max_wall_ms,
        )

    async def resolve(
        self,
        task: str,
        *,
        intent: CompiledIntent | None = None,
        limit: int = 8,
        max_capsule_chars: int | None = None,
    ) -> ResolveResult:
        started_at = time.monotonic()
        task = _normalise_query(task)
        limit = max(1, min(int(limit or 8), 20))
        intent = intent or compile_intent_query(task)
        capsule_chars = max(
            400,
            min(
                int(max_capsule_chars or self.max_capsule_chars),
                self.max_capsule_chars,
            ),
        )
        tokenizer_id = str(getattr(self.token_counter, "tokenizer_id", "unavailable")) or "unavailable"
        key = _cache_key(
            task,
            intent,
            limit,
            tokenizer_id=tokenizer_id,
            max_tokens=self.max_token_budget,
            max_capsule_chars=capsule_chars,
        )
        cached = self.cache.get(key)
        provider_name = str(getattr(self.provider, "name", type(self.provider).__name__))
        if cached is not None:
            return ResolveResult(
                candidates=cached,
                status="cache_hit",
                cache_status="hit",
                provider=provider_name,
                budget={
                    "provider_calls": 0,
                    "fetches": 0,
                    # This is a remote-fetch budget. A cache hit reads no
                    # network bytes; capsule memory is tracked separately by
                    # the bounded hot-cache implementation.
                    "bytes_read": 0,
                    "max_provider_calls": self.max_provider_calls,
                    "max_fetches": self.max_fetches,
                    "max_bytes": self.max_bytes,
                    "max_wall_ms": self.max_wall_ms,
                },
                latency_ms=int((time.monotonic() - started_at) * 1000),
            )

        budget = self._budget()
        try:
            result = await asyncio.wait_for(
                self._resolve_uncached(task, intent, limit, budget, capsule_chars),
                timeout=self.max_wall_ms / 1000.0,
            )
        except asyncio.TimeoutError:
            self.circuit_breaker.failure()
            return ResolveResult(
                candidates=[],
                status="timeout",
                cache_status="miss",
                provider=provider_name,
                budget=budget.as_dict(),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                warnings=["on-demand resolver wall-clock budget exceeded"],
            )
        except ResolverBudgetExceeded as exc:
            return ResolveResult(
                candidates=[],
                status="budget_exceeded",
                cache_status="miss",
                provider=provider_name,
                budget=budget.as_dict(),
                latency_ms=int((time.monotonic() - started_at) * 1000),
                warnings=[f"on-demand resolver {exc}"],
            )
        result.latency_ms = int((time.monotonic() - started_at) * 1000)
        if result.status in {"ok", "no_match"}:
            self.cache.put(key, result.candidates)
        return result

    async def _resolve_uncached(
        self,
        task: str,
        intent: CompiledIntent,
        limit: int,
        budget: ResolverBudget,
        max_capsule_chars: int,
    ) -> ResolveResult:
        warnings: list[str] = []
        discovered: dict[str, dict[str, Any]] = {}
        deadline = budget.started_at + (budget.max_wall_ms / 1000.0)
        metadata_key = hashlib.sha256(
            "\x1f".join(
                [
                    _normalise_query(task).casefold(),
                    intent.compiler_version,
                    intent.compressed_query,
                ]
            ).encode("utf-8")
        ).hexdigest()
        variants = list(dict.fromkeys([task, *intent.query_variants, intent.compressed_query]))
        variants = [value for value in variants if _normalise_query(value)]
        variants = variants[: self.max_provider_calls]
        discovery_failures = 0
        cached_sources = self.metadata_index.get(metadata_key, ON_DEMAND_CANDIDATE_LIMIT)
        if cached_sources is not None:
            warnings.append("metadata index hit")
            discovery_rows = [(0, source) for source in cached_sources]
        else:
            if not self.circuit_breaker.allow():
                return ResolveResult(
                    candidates=[],
                    status="circuit_open",
                    cache_status="miss",
                    provider=str(getattr(self.provider, "name", type(self.provider).__name__)),
                    budget=budget.as_dict(),
                    latency_ms=budget.elapsed_ms(),
                    warnings=["provider circuit breaker is open"],
                )
            discovery_rows: list[tuple[int, Any]] = []
            for lane_index, query in enumerate(variants):
                budget.reserve("discover")
                try:
                    rows = await self.provider.discover(
                        query,
                        min(ON_DEMAND_CANDIDATE_LIMIT, max(1, int(limit))),
                        deadline=deadline,
                    )
                except (OnDemandProviderError, SkillsShCatalogError) as exc:
                    discovery_failures += 1
                    warnings.append(f"provider discovery failed ({type(exc).__name__})")
                    continue
                except Exception as exc:  # provider failures must fail closed
                    discovery_failures += 1
                    warnings.append(f"provider discovery failed ({type(exc).__name__})")
                    continue
                for rank, value in enumerate(
                    list(rows or [])[:ON_DEMAND_CANDIDATE_LIMIT]
                ):
                    discovery_rows.append((lane_index, value))
            if discovery_rows:
                self.circuit_breaker.success()
            elif discovery_failures:
                self.circuit_breaker.failure()

        discovered_sources: list[SkillSource] = []
        for lane_index, value in discovery_rows:
            source = value if isinstance(value, SkillSource) else _coerce_source(value, len(discovered_sources))
            if source is None:
                continue
            discovered_sources.append(source)
            current = discovered.setdefault(
                source.key,
                {
                    "source": source,
                    "query_rrf_score": 0.0,
                    "retrieval_priority": 0,
                    "retrieval_queries": [],
                },
            )
            rank = len(current["retrieval_queries"])
            current["query_rrf_score"] += 1.0 / (60 + rank + 1)
            current["retrieval_priority"] = max(
                current["retrieval_priority"], 1 if lane_index == 0 else 0
            )
            current["retrieval_queries"].append("original" if lane_index == 0 else "compiled")
            existing = current["source"]
            if source.commit_sha and not existing.commit_sha:
                current["source"] = source
        if cached_sources is None and discovered_sources:
            self.metadata_index.put(metadata_key, discovered_sources)

        if not discovered:
            status = "provider_failure" if discovery_failures == len(variants) and variants else "no_match"
            return ResolveResult(
                candidates=[],
                status=status,
                cache_status="miss",
                provider=str(getattr(self.provider, "name", type(self.provider).__name__)),
                budget=budget.as_dict(),
                latency_ms=budget.elapsed_ms(),
                warnings=warnings,
            )

        shortlist = sorted(
            discovered.values(),
            key=lambda item: (
                -float(item["query_rrf_score"]),
                int(item["source"].rank),
                item["source"].key,
            ),
        )[: min(self.fetch_top_k, self.max_fetches, ON_DEMAND_CANDIDATE_LIMIT, len(discovered))]
        candidates: list[dict[str, Any]] = []
        fetch_failures = 0
        rejected_for_tokenizer = False
        for shortlist_rank, item in enumerate(shortlist):
            source = item["source"]
            if not _supported_license(source.license):
                warnings.append(f"unsupported license rejected: {source.license}")
                continue
            budget.reserve("fetch")
            remaining = min(
                self.max_entrypoint_bytes,
                budget.max_bytes - budget.bytes_read,
            )
            if remaining <= 0:
                warnings.append("content rejected: on-demand byte budget exhausted")
                break
            try:
                fetched = await self.provider.fetch(
                    source,
                    max_bytes=remaining,
                    deadline=deadline,
                )
            except (OnDemandProviderError, SkillsShCatalogError) as exc:
                fetch_failures += 1
                warnings.append(f"provider fetch failed ({type(exc).__name__})")
                continue
            except Exception as exc:  # fail closed, preserve other candidates
                fetch_failures += 1
                warnings.append(f"provider fetch failed ({type(exc).__name__})")
                continue
            fetched = _coerce_fetch(fetched)
            if fetched is None:
                fetch_failures += 1
                warnings.append("provider returned no immutable SKILL.md content")
                continue
            if fetched.rejection_reason:
                warnings.append(f"provider rejected candidate: {fetched.rejection_reason}")
                continue
            if fetched.entrypoint_truncated:
                warnings.append("content rejected: SKILL.md entrypoint was truncated")
                continue
            expected_path = _safe_entrypoint_path(source.path)
            fetched_path = _safe_entrypoint_path(fetched.entrypoint_path or source.path)
            if not expected_path or not fetched_path or expected_path != fetched_path:
                warnings.append("unverifiable source path rejected")
                continue
            snapshot = str(fetched.snapshot_hash or "").strip()
            fetched_commit = str(fetched.source_commit_sha or snapshot).strip()
            if (
                fetched.source_commit_sha
                and snapshot
                and fetched.source_commit_sha.strip().casefold() != snapshot.casefold()
            ):
                warnings.append("commit identity verification failed")
                continue
            if not _COMMIT_RE.fullmatch(fetched_commit):
                warnings.append("mutable source rejected: missing immutable commit SHA")
                continue
            expected_commit = str(source.commit_sha or "").strip()
            if expected_commit and not _COMMIT_RE.fullmatch(expected_commit):
                warnings.append("unverifiable source rejected: discovery commit is not immutable")
                continue
            expected_snapshot = str(source.snapshot_hash or "").strip()
            expected_snapshot = expected_snapshot if _COMMIT_RE.fullmatch(expected_snapshot) else ""
            expected_commit = expected_commit or expected_snapshot
            if expected_commit and expected_commit.casefold() != fetched_commit.casefold():
                warnings.append("stale source rejected: snapshot changed between discovery and fetch")
                continue
            fetched_text = str(fetched.content or "")
            actual_raw_digest = hashlib.sha256(fetched_text.encode("utf-8")).hexdigest()
            if (
                fetched.raw_content_digest
                and fetched.raw_content_digest.casefold() != actual_raw_digest
            ):
                warnings.append("raw content digest verification failed")
                continue
            content = canonicalize_skill_content(fetched_text)
            content_bytes = len(content.encode("utf-8"))
            fetched_bytes = max(content_bytes, int(fetched.byte_count or 0))
            if content_bytes <= 0 or fetched_bytes > remaining:
                warnings.append("content rejected: on-demand byte budget exceeded")
                continue
            if not has_valid_skill_frontmatter(content):
                warnings.append("content rejected: malformed SKILL.md frontmatter")
                continue
            try:
                budget.account_bytes(fetched_bytes)
            except ResolverBudgetExceeded:
                warnings.append("content rejected: on-demand byte budget exceeded")
                continue
            verified_hash = content_hash(content)
            expected_hash = (
                fetched.declared_content_hash
                or source.declared_content_hash
                or source.metadata.get("content_hash")
            )
            if expected_hash and str(expected_hash).casefold() != verified_hash:
                warnings.append("content hash verification failed")
                continue
            raw_digest = actual_raw_digest
            expected_digest = (
                fetched.declared_content_digest
                or source.declared_content_digest
                or source.metadata.get("content_digest")
            )
            if expected_digest and str(expected_digest).casefold() != raw_digest:
                warnings.append("content digest verification failed")
                continue
            row = self._candidate_row(
                task,
                source,
                fetched,
                content,
                verified_hash,
                shortlist_rank,
                item,
                max_capsule_chars=max_capsule_chars,
            )
            if not row.get("on_demand_capsule_ready"):
                rejected_for_tokenizer = rejected_for_tokenizer or row.get(
                    "on_demand_guard_reason"
                ) in {"tokenizer_unavailable", "capsule_token_budget_exceeded"}
                if row.get("on_demand_guard_reason") in {
                    "unsafe_capability",
                    "non_portable_project_specific",
                }:
                    warnings.append("candidate rejected: safety or portability gate failed")
                    continue
                warnings.append(
                    "candidate rejected: exact capsule tokenizer unavailable, over budget, or portability gate failed"
                )
            candidates.append(row)

        if candidates:
            self.circuit_breaker.success()
            ranked = dedupe_by_content_hash(rerank_candidates(task, candidates))[:limit]
            status = "ok" if any(row.get("on_demand_capsule_ready") for row in ranked) else (
                "tokenizer_unavailable" if rejected_for_tokenizer else "no_match"
            )
        else:
            ranked = []
            if rejected_for_tokenizer:
                status = "tokenizer_unavailable"
            else:
                status = "provider_failure" if fetch_failures and fetch_failures == len(shortlist) else "no_match"
            if status == "provider_failure":
                self.circuit_breaker.failure()
        return ResolveResult(
            candidates=ranked,
            status=status,
            cache_status="miss",
            provider=str(getattr(self.provider, "name", type(self.provider).__name__)),
            budget=budget.as_dict(),
            latency_ms=budget.elapsed_ms(),
            warnings=warnings,
        )

    def _candidate_row(
        self,
        task: str,
        source: SkillSource,
        fetched: FetchedSkill,
        content: str,
        verified_hash: str,
        rank: int,
        fused: dict[str, Any],
        *,
        max_capsule_chars: int,
    ) -> dict[str, Any]:
        source_url = fetched.source_url or source.source_url
        source_commit = fetched.source_commit_sha or fetched.snapshot_hash
        row: dict[str, Any] = {
            "id": source.key,
            "name": source.name,
            "description": source.description,
            "source": source.provider or "skills_sh",
            "url": source_url,
            "source_url": source_url,
            # Only the first provider is allowed to use the existing skills.sh
            # authoritative-detail gate. Other providers remain conservative
            # hint candidates until they define their own trust integration.
            "retrieval_backend": "on_demand",
            "on_demand_mode": True,
            "rank": 1.0 / (60.0 + rank + 1.0),
            "similarity": None,
            "query_rrf_score": fused.get("query_rrf_score"),
            "retrieval_priority": fused.get("retrieval_priority", 0),
            "retrieval_queries": list(fused.get("retrieval_queries") or []),
            "query_variant": task,
            "tags": list(source.metadata.get("tags") or []),
            "platforms": list(source.metadata.get("platforms") or []),
            "source_snapshot_hash": source_commit,
            "source_commit_sha": source_commit,
            "entrypoint_path": fetched.entrypoint_path or source.path,
            "repository": source.repository,
            "license": source.license,
            "source_freshness": source.freshness,
            "audit_status": fetched.audit_status,
            "audit_risk_level": fetched.audit_risk_level,
            "audit_count": 1 if fetched.audit_status != "unknown" else 0,
            "risk_score": int(fetched.risk_score),
            "risk_flags": list(fetched.risk_flags),
            # On-demand intentionally pins only the fetched entrypoint. It
            # does not claim that an entire package/dependency closure exists.
            "package_completeness": "entrypoint-only",
            "dependency_closure_status": "entrypoint-only",
            "entrypoint_truncated": bool(fetched.entrypoint_truncated),
            "raw_content_digest": fetched.raw_content_digest or content_digest(content),
            "on_demand_tokenizer_id": None,
            "on_demand_capsule_compiler_version": CAPSULE_VERSION,
            "on_demand_context_policy": None,
            "on_demand_quality_version": QUALITY_CONFIG_VERSION,
            "on_demand_trust_epoch": ON_DEMAND_TRUST_EPOCH,
            "on_demand_gate_versions": {
                "quality": QUALITY_CONFIG_VERSION,
                "context": None,
                "capsule": CAPSULE_VERSION,
            },
            "_content": content,
            "raw": {
                "provider": source.provider,
                "source_snapshot_hash": fetched.snapshot_hash,
                "source_url": source_url,
                "repository": source.repository,
                "entrypoint_path": fetched.entrypoint_path or source.path,
            },
        }
        quality = evaluate_quality(row, content)
        row.update(quality)
        row["readiness"] = readiness_for_skill(row)

        capability_flags = skill_capability_flags(content)
        if capability_flags:
            row["risk_score"] = max(1, int(row.get("risk_score") or 0))
            row["risk_flags"] = sorted(
                set([*(row.get("risk_flags") or []), *capability_flags])
            )
            row["readiness"] = readiness_for_skill(row)
            row["on_demand_capsule_ready"] = False
            row["on_demand_capsule_digest"] = None
            row["on_demand_tokenizer_id"] = None
            row["on_demand_capsule_token_count"] = 0
            row["on_demand_guard_delivery"] = "hint"
            row["on_demand_guard_reason"] = "unsafe_capability"
            return row

        # The internet path requires the exact configured serving tokenizer.
        # The resulting guard is safe to retain in the hot cache; the raw body
        # remains request-scoped and is removed before cache insertion.
        guard = build_context_guard(
            task=task,
            content=content,
            content_hash=verified_hash,
            content_digest=content_digest(content),
            max_capsule_chars=max_capsule_chars,
            source_url=source_url or None,
            source_commit_sha=source_commit,
            bounded_delivery=True,
            token_counter=self.token_counter,
            max_tokens=self.max_token_budget,
        )
        if guard.get("delivery") != "capsule" or not guard.get("complete"):
            row["on_demand_capsule_ready"] = False
            row["on_demand_capsule_digest"] = None
            row["on_demand_tokenizer_id"] = None
            row["on_demand_capsule_token_count"] = 0
            row["on_demand_guard_delivery"] = guard.get("delivery")
            row["on_demand_guard_reason"] = guard.get("reason")
            return row
        capsule_digest = str(guard.get("capsule_digest") or "") or None
        tokenizer_id = str(guard.get("tokenizer_id") or "") or None
        row["on_demand_capsule_ready"] = True
        row["on_demand_capsule_digest"] = capsule_digest
        row["on_demand_tokenizer_id"] = tokenizer_id
        row["on_demand_context_policy"] = guard.get("policy")
        row["on_demand_gate_versions"] = {
            "quality": QUALITY_CONFIG_VERSION,
            "context": guard.get("policy"),
            "capsule": CAPSULE_VERSION,
        }
        row["on_demand_capsule_token_count"] = int(guard.get("estimated_tokens") or 0)
        row["on_demand_guard_delivery"] = guard.get("delivery")
        row["on_demand_guard_reason"] = guard.get("reason")
        row["on_demand_cache_key"] = ":".join(
            [
                verified_hash,
                content_digest(content),
                tokenizer_id or "unknown",
                CAPSULE_VERSION,
                str(guard.get("policy") or "unknown"),
                ON_DEMAND_TRUST_EPOCH,
            ]
        )
        row["_on_demand_context_guard"] = copy.deepcopy(guard)
        return row


class SkillsShMetadataProvider:
    """skills.sh search adapter; it never calls detail or audit endpoints."""

    name = "skills_sh_metadata"

    def __init__(
        self,
        catalog: SkillsShCatalog | None = None,
        *,
        fetcher: GitHubImmutableFetcher | None = None,
    ) -> None:
        self.catalog = catalog or SkillsShCatalog(
            mirror_enabled=False,
            timeout_seconds=ON_DEMAND_PROVIDER_TIMEOUT_SECONDS,
            search_ttl_seconds=30.0,
            detail_ttl_seconds=0.0,
            audit_ttl_seconds=0.0,
            max_retries=0,
            follow_redirects=False,
            metadata_only=True,
        )
        if getattr(self.catalog, "mirror_enabled", False) or getattr(self.catalog, "_mirror", None) is not None:
            raise ValueError("on-demand metadata provider cannot enable the raw skills.sh mirror")
        if getattr(self.catalog, "follow_redirects", True):
            raise ValueError("on-demand metadata provider requires redirects to be disabled")
        if not getattr(self.catalog, "metadata_only", False):
            raise ValueError("on-demand metadata provider requires metadata-only catalog results")
        if getattr(self.catalog, "transport", None) is None:
            allowed_hosts = {"skills.sh", "www.skills.sh", "api.skills.sh"}
            for endpoint in (
                getattr(self.catalog, "api_url", ""),
                getattr(self.catalog, "public_search_url", ""),
            ):
                parsed = urlsplit(str(endpoint))
                if parsed.scheme.casefold() != "https" or parsed.netloc.casefold() not in allowed_hosts:
                    raise ValueError("on-demand metadata provider endpoint is not allowlisted")
        self.fetcher = fetcher or GitHubImmutableFetcher(
            transport=getattr(self.catalog, "transport", None),
            timeout_seconds=ON_DEMAND_PROVIDER_TIMEOUT_SECONDS,
        )

    async def discover(
        self,
        query: str,
        limit: int,
        *,
        deadline: float | None = None,
    ) -> list[SkillSource]:
        del deadline  # SkillsShCatalog applies its own bounded client timeout.
        rows = await self.catalog.search(query, max(1, min(int(limit), ON_DEMAND_CANDIDATE_LIMIT)))
        values: list[SkillSource] = []
        for rank, row in enumerate(rows or []):
            if not isinstance(row, dict):
                continue
            source = _coerce_source(
                {
                    **row,
                    "key": row.get("id") or row.get("skills_sh_id"),
                    "source_url": row.get("skills_sh_url") or row.get("url"),
                    "repository": row.get("repository")
                    or row.get("repo")
                    or row.get("install_url")
                    or row.get("source"),
                    "path": row.get("entrypoint_path") or row.get("skill_path") or row.get("path") or "SKILL.md",
                    "revision": row.get("revision") or row.get("ref") or "HEAD",
                    "content_digest": row.get("content_digest") or row.get("contentDigest"),
                    "provider": "skills_sh",
                },
                rank,
            )
            if source is not None:
                values.append(source)
        return values[:ON_DEMAND_CANDIDATE_LIMIT]

    async def fetch(
        self,
        source: SkillSource,
        max_bytes: int,
        *,
        deadline: float | None = None,
    ) -> FetchedSkill | None:
        # The only body request is the immutable upstream GitHub blob. A
        # missing/ambiguous source remains metadata-only instead of falling
        # back to skills.sh detail or a mutable HTML/branch URL.
        return await self.fetcher.fetch(source, max_bytes, deadline=deadline)


# Compatibility name retained for callers/tests from the first experiment.
SkillsShOnDemandProvider = SkillsShMetadataProvider


_DEFAULT_RESOLVER: OnDemandResolver | None = None


def default_on_demand_resolver() -> OnDemandResolver:
    """Return the singleton experiment resolver; it has no durable storage."""
    global _DEFAULT_RESOLVER
    if _DEFAULT_RESOLVER is None:
        _DEFAULT_RESOLVER = OnDemandResolver(SkillsShMetadataProvider())
    return _DEFAULT_RESOLVER


__all__ = [
    "FetchedSkill",
    "GitHubImmutableFetcher",
    "ImmutableSkillFetcher",
    "OnDemandProviderError",
    "OnDemandResolver",
    "ProviderCircuitBreaker",
    "ResolveResult",
    "ResolverBudgetExceeded",
    "SkillDiscoveryProvider",
    "SkillSource",
    "SkillSourceProvider",
    "SkillsShOnDemandProvider",
    "SkillsShMetadataProvider",
    "default_on_demand_resolver",
]
