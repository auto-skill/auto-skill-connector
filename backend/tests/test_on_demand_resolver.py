from __future__ import annotations

import asyncio
import hashlib
from dataclasses import dataclass, replace

import httpx
import pytest

from capsule_compiler import compile_capsule
from on_demand_resolver import (
    FetchedSkill,
    GitHubImmutableFetcher,
    OnDemandProviderError,
    OnDemandResolver,
    ProviderCircuitBreaker,
    SkillSource,
    SkillsShOnDemandProvider,
    _HotRouteCache,
    _MetadataIndex,
)
from recommender import find_deliverable_primary_candidate
from skills_sh_catalog import SkillsShCatalog
from skills_sh_catalog import SkillsShCatalogError
from token_budget import FixedTokenCounter


VALID = (
    "---\n"
    "name: spreadsheet-reporter\n"
    "description: Build spreadsheet reports with formulas, charts, and validation.\n"
    "---\n\n"
    "## Workflow\n\n"
    "Inspect the source data, create the workbook, add formulas, preserve existing\n"
    "sheet names, verify representative totals, and explain assumptions before\n"
    "returning the generated report to the user.\n\n"
    "## Verification\n\n"
    "Validate formulas, chart ranges, headers, totals, identifiers, and representative\n"
    "cells. Record any assumptions and check that the output opens cleanly.\n"
)


def _fixture_commit(value: str | None) -> str | None:
    if value is None:
        return None
    if value.casefold() in {"head", "main", "master", "latest", "default"}:
        return value
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:40]


@dataclass
class FixtureProvider:
    sources: list[SkillSource]
    fetched: dict[str, FetchedSkill | None]
    name: str = "fixture-provider"
    fail_discovery: bool = False
    delay_seconds: float = 0.0

    discover_calls: int = 0
    fetch_calls: int = 0

    async def discover(
        self, query: str, limit: int, *, deadline: float | None = None
    ) -> list[SkillSource]:
        del query, limit
        if deadline is not None and deadline <= 0:
            raise OnDemandProviderError("deadline expired")
        self.discover_calls += 1
        if self.delay_seconds:
            await asyncio.sleep(self.delay_seconds)
        if self.fail_discovery:
            raise OnDemandProviderError("fixture unavailable")
        return list(self.sources)

    async def fetch(
        self,
        source: SkillSource,
        max_bytes: int,
        *,
        deadline: float | None = None,
    ) -> FetchedSkill | None:
        del max_bytes
        if deadline is not None and deadline <= 0:
            raise OnDemandProviderError("deadline expired")
        self.fetch_calls += 1
        return self.fetched.get(source.key)


def _source(*, snapshot: str | None = None) -> SkillSource:
    return SkillSource(
        key="acme/reporting",
        name="spreadsheet-reporter",
        description="Build spreadsheet reports with formulas, charts, and validation.",
        source_url="https://skills.sh/acme/reporting",
        snapshot_hash=snapshot,
        commit_sha=_fixture_commit(snapshot),
    )


def _fetched(content: str = VALID, *, snapshot: str | None = "snapshot-1") -> FetchedSkill:
    commit = _fixture_commit(snapshot)
    return FetchedSkill(
        content=content,
        snapshot_hash=commit,
        source_commit_sha=commit,
        audit_status="pass",
        audit_risk_level="low",
        risk_score=0,
    )


def _resolve(provider: FixtureProvider, **kwargs):
    resolver = OnDemandResolver(
        provider,
        max_provider_calls=kwargs.pop("max_provider_calls", 2),
        max_fetches=kwargs.pop("max_fetches", 2),
        max_bytes=kwargs.pop("max_bytes", 100_000),
        max_wall_ms=kwargs.pop("max_wall_ms", 500),
        fetch_top_k=kwargs.pop("fetch_top_k", 1),
        token_counter=kwargs.pop("token_counter", FixedTokenCounter()),
        **kwargs,
    )
    return resolver


def test_cache_miss_fetches_bounded_shortlist_and_cache_hit_skips_provider() -> None:
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched()})
    resolver = _resolve(provider)

    first = asyncio.run(resolver.resolve("create an Excel report with formulas"))
    second = asyncio.run(resolver.resolve("create an Excel report with formulas"))

    assert first.status == "ok"
    assert first.cache_status == "miss"
    assert first.budget["fetches"] == 1
    assert first.candidates[0]["content_hash"]
    assert second.status == "cache_hit"
    assert second.cache_status == "hit"
    assert "_content" not in second.candidates[0]
    assert second.candidates[0]["_on_demand_context_guard"]["delivery"] == "capsule"
    selected, cached_text = asyncio.run(
        find_deliverable_primary_candidate(
            "create an Excel report with formulas",
            second.candidates,
            ranked=True,
        )
    )
    assert selected is not None
    assert cached_text == ""
    assert provider.discover_calls == 2
    assert provider.fetch_calls == 1


def test_metadata_cache_hit_avoids_discovery_but_refetches_request_scoped_body() -> None:
    source = replace(_source(), metadata={"content": "should-not-cache", "tags": ["report"]})
    provider = FixtureProvider([source], {"acme/reporting": _fetched()})
    resolver = _resolve(
        provider,
        cache=_HotRouteCache(ttl_seconds=0, max_entries=2, max_bytes=100_000),
        metadata_index=_MetadataIndex(ttl_seconds=60, max_entries=2),
    )

    first = asyncio.run(resolver.resolve("create an Excel report with formulas"))
    second = asyncio.run(resolver.resolve("create an Excel report with formulas"))

    assert first.status == "ok"
    assert second.status == "ok"
    assert any("metadata index hit" in warning for warning in second.warnings)
    assert second.budget["provider_calls"] == 0
    assert second.budget["fetches"] == 1
    assert provider.discover_calls == 2
    assert provider.fetch_calls == 2
    cached_source = next(iter(resolver.metadata_index._entries.values())).sources[0]
    assert "content" not in cached_source.metadata


def test_hot_route_cache_key_includes_tokenizer_version() -> None:
    cache = _HotRouteCache(ttl_seconds=60, max_entries=4, max_bytes=100_000)
    first_provider = FixtureProvider([_source()], {"acme/reporting": _fetched()})
    first_resolver = _resolve(
        first_provider,
        cache=cache,
        token_counter=FixedTokenCounter(tokenizer_id="fixture-tokenizer-a"),
    )
    first = asyncio.run(first_resolver.resolve("create an Excel report with formulas"))

    second_provider = FixtureProvider([_source()], {"acme/reporting": _fetched()})
    second_resolver = _resolve(
        second_provider,
        cache=cache,
        token_counter=FixedTokenCounter(tokenizer_id="fixture-tokenizer-b"),
    )
    second = asyncio.run(second_resolver.resolve("create an Excel report with formulas"))

    assert first.status == "ok"
    assert second.status == "ok"
    assert second.cache_status == "miss"
    assert second_provider.fetch_calls == 1


def test_provider_failure_fails_closed_without_cached_result() -> None:
    provider = FixtureProvider([_source()], {}, fail_discovery=True)
    resolver = _resolve(provider)

    result = asyncio.run(resolver.resolve("create an Excel report with formulas"))

    assert result.status == "provider_failure"
    assert result.candidates == []
    assert any("discovery failed" in warning for warning in result.warnings)


def test_stale_source_is_rejected_when_snapshot_changes() -> None:
    provider = FixtureProvider(
        [_source(snapshot="snapshot-old")],
        {"acme/reporting": _fetched(snapshot="snapshot-new")},
    )
    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert any("stale source rejected" in warning for warning in result.warnings)


def test_mutable_source_without_pinned_snapshot_is_rejected() -> None:
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched(snapshot="main")})
    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert any("mutable source rejected" in warning for warning in result.warnings)


def test_non_sha_commit_identity_is_rejected() -> None:
    fetched = FetchedSkill(
        content=VALID,
        snapshot_hash="snapshot-label",
        source_commit_sha="snapshot-label",
        audit_status="pass",
        audit_risk_level="low",
        risk_score=0,
    )
    provider = FixtureProvider([_source()], {"acme/reporting": fetched})

    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert any("immutable commit SHA" in warning for warning in result.warnings)


def test_truncated_entrypoint_is_rejected_without_partial_delivery() -> None:
    fetched = replace(_fetched(), entrypoint_truncated=True)
    provider = FixtureProvider([_source()], {"acme/reporting": fetched})

    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert any("entrypoint was truncated" in warning for warning in result.warnings)


def test_declared_content_digest_mismatch_is_rejected() -> None:
    source = replace(_source(), declared_content_digest="0" * 64)
    provider = FixtureProvider([source], {"acme/reporting": _fetched()})

    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert any("content digest verification failed" in warning for warning in result.warnings)


def test_declared_content_hash_mismatch_is_rejected() -> None:
    source = replace(_source(), declared_content_hash="0" * 64)
    provider = FixtureProvider([source], {"acme/reporting": _fetched()})

    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert any("content hash verification failed" in warning for warning in result.warnings)


def test_fetched_commit_identity_mismatch_is_rejected() -> None:
    fetched = replace(_fetched(), source_commit_sha="different-commit")
    provider = FixtureProvider([_source()], {"acme/reporting": fetched})

    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert any("commit identity verification failed" in warning for warning in result.warnings)


def test_declared_unsupported_license_is_rejected_before_fetch() -> None:
    source = replace(_source(), license="GPL-3.0")
    provider = FixtureProvider([source], {"acme/reporting": _fetched()})

    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert provider.fetch_calls == 0
    assert any("unsupported license rejected" in warning for warning in result.warnings)


def test_malformed_skill_is_not_accepted() -> None:
    malformed = _fetched("This is not a SKILL.md document.")
    provider = FixtureProvider([_source()], {"acme/reporting": malformed})

    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.status == "no_match"
    assert result.candidates == []


def test_unsafe_content_cannot_become_deliverable_primary() -> None:
    unsafe = VALID + "\nUse curl to fetch the remote endpoint and install dependencies.\n"
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched(unsafe)})
    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    selected, text = asyncio.run(
        find_deliverable_primary_candidate(
            "create an Excel report with formulas",
            result.candidates,
            ranked=True,
        )
    )
    assert result.candidates == []
    assert selected is None
    assert text == ""


def test_missing_audit_data_is_advisory_but_not_approval() -> None:
    fetched = replace(_fetched(), audit_status="unknown", audit_risk_level="unknown", risk_score=1)
    provider = FixtureProvider([_source()], {"acme/reporting": fetched})
    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    selected, text = asyncio.run(
        find_deliverable_primary_candidate(
            "create an Excel report with formulas",
            result.candidates,
            ranked=True,
        )
    )
    assert result.candidates
    assert result.candidates[0]["audit_count"] == 0
    assert selected is None
    assert text == ""


def test_non_portable_content_cannot_become_deliverable_primary() -> None:
    non_portable = VALID + (
        "\nRead Calypso/tools/input.py, preserve Calypso/data/source.json, and write "
        "Calypso/output/report.csv after validation.\n"
    )
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched(non_portable)})
    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    selected, _text = asyncio.run(
        find_deliverable_primary_candidate(
            "create an Excel report with formulas",
            result.candidates,
            ranked=True,
        )
    )
    assert result.candidates == []
    assert selected is None
    assert any("safety or portability gate failed" in warning for warning in result.warnings)


def test_timeout_returns_bounded_failure_and_does_not_cache() -> None:
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched()}, delay_seconds=0.1)
    resolver = _resolve(provider, max_wall_ms=20)

    result = asyncio.run(resolver.resolve("create an Excel report with formulas"))

    assert result.status == "timeout"
    assert result.cache_status == "miss"
    assert result.candidates == []


def test_no_match_is_explicit_and_bounded() -> None:
    provider = FixtureProvider([], {})
    result = asyncio.run(_resolve(provider).resolve("an unrelated task with no skill"))

    assert result.status == "no_match"
    assert result.candidates == []
    assert result.budget["fetches"] == 0


def test_fetch_and_byte_budgets_are_hard_limits() -> None:
    oversized = VALID + ("\nValidate the workbook output carefully." * 500)
    provider = FixtureProvider(
        [_source(), SkillSource("acme/second", "second", "Another report skill.", "https://skills.sh/acme/second")],
        {"acme/reporting": _fetched(oversized), "acme/second": _fetched()},
    )
    resolver = _resolve(provider, max_fetches=1, fetch_top_k=8, max_bytes=1_024)

    result = asyncio.run(resolver.resolve("create an Excel report with formulas"))

    assert provider.fetch_calls == 1
    assert result.budget["fetches"] == 1
    assert result.budget["bytes_read"] <= 1_024
    assert result.candidates == []
    assert any("byte budget" in warning for warning in result.warnings)


def test_capsule_compiler_is_used_for_experimental_content() -> None:
    compiled = compile_capsule(
        task="create an Excel report with formulas",
        content=VALID,
        source_url="https://skills.sh/acme/reporting",
        source_commit_sha=_fixture_commit("snapshot-1"),
    )
    assert compiled is not None
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched()})
    result = asyncio.run(_resolve(provider).resolve("create an Excel report with formulas"))

    assert result.candidates[0]["on_demand_capsule_ready"] is True
    assert result.candidates[0]["on_demand_capsule_digest"] == compiled.capsule_digest


def test_exact_tokenizer_is_required_for_internet_delivery() -> None:
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched()})
    resolver = _resolve(provider, token_counter=None)
    resolver.token_counter = None

    result = asyncio.run(resolver.resolve("create an Excel report with formulas"))

    assert result.status == "tokenizer_unavailable"
    assert result.candidates
    assert result.candidates[0]["on_demand_capsule_ready"] is False
    assert result.candidates[0]["on_demand_guard_reason"] == "tokenizer_unavailable"


def test_exact_token_budget_rejects_capsule_without_slicing() -> None:
    provider = FixtureProvider([_source()], {"acme/reporting": _fetched()})
    resolver = _resolve(
        provider,
        max_token_budget=2,
        token_counter=FixedTokenCounter(tokens_per_word=10),
    )

    result = asyncio.run(resolver.resolve("create an Excel report with formulas"))

    assert result.status == "tokenizer_unavailable"
    assert result.candidates[0]["on_demand_capsule_ready"] is False
    assert result.candidates[0]["on_demand_guard_reason"] == "capsule_token_budget_exceeded"


def test_provider_circuit_breaker_stops_repeated_discovery_failures() -> None:
    provider = FixtureProvider([_source()], {}, fail_discovery=True)
    breaker = ProviderCircuitBreaker(failure_threshold=1, cooldown_seconds=60)
    resolver = _resolve(provider, circuit_breaker=breaker)

    first = asyncio.run(resolver.resolve("create an Excel report with formulas"))
    second = asyncio.run(resolver.resolve("a different report task"))

    assert first.status == "provider_failure"
    assert second.status == "circuit_open"
    assert provider.discover_calls == 2


def test_github_fetcher_reads_pinned_blob_without_resolving_mutable_ref() -> None:
    commit = "b" * 40
    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == f"/acme/skills/{commit}/SKILL.md":
            return httpx.Response(200, text=VALID, request=request)
        return httpx.Response(404, request=request)

    source = SkillSource(
        key="acme/reporting",
        name="reporting",
        description="reporting",
        source_url="https://skills.sh/acme/reporting",
        repository="acme/skills",
        path="SKILL.md",
        commit_sha=commit,
        metadata={"audit_status": "pass", "audit_risk_level": "low", "risk_score": 0},
    )
    fetched = asyncio.run(
        GitHubImmutableFetcher(transport=httpx.MockTransport(transport)).fetch(
            source,
            max_bytes=100_000,
        )
    )

    assert fetched is not None
    assert fetched.source_commit_sha == commit
    assert fetched.content == VALID
    assert fetched.raw_content_digest
    assert fetched.risk_score == 0
    assert seen == [f"/acme/skills/{commit}/SKILL.md"]


def test_github_fetcher_rejects_arbitrary_hosts_and_redirects() -> None:
    commit = "1" * 40
    source = SkillSource(
        "acme/reporting",
        "reporting",
        "reporting",
        "https://skills.sh/acme/reporting",
        repository="acme/skills",
        commit_sha=commit,
    )

    with pytest.raises(OnDemandProviderError):
        asyncio.run(
            GitHubImmutableFetcher(
                api_url="https://evil.example/api",
                raw_url="https://evil.example/raw",
            ).fetch(source, max_bytes=100_000)
        )

    def redirect_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/acme/skills/{commit}/SKILL.md":
            return httpx.Response(302, headers={"location": "https://evil.example"}, request=request)
        return httpx.Response(404, request=request)

    with pytest.raises(OnDemandProviderError):
        asyncio.run(
            GitHubImmutableFetcher(
                transport=httpx.MockTransport(redirect_transport)
            ).fetch(source, max_bytes=100_000)
        )


def test_github_fetcher_rejects_oversized_body_before_delivery() -> None:
    commit = "c" * 40

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/acme/skills/{commit}/SKILL.md":
            return httpx.Response(200, text=VALID + ("x" * 100), request=request)
        return httpx.Response(404, request=request)

    source = SkillSource(
        "acme/reporting",
        "reporting",
        "reporting",
        "https://skills.sh/acme/reporting",
        repository="acme/skills",
        commit_sha=commit,
    )
    fetched = asyncio.run(
        GitHubImmutableFetcher(transport=httpx.MockTransport(transport)).fetch(
            source,
            max_bytes=len(VALID.encode("utf-8")) + 10,
        )
    )

    assert fetched is not None
    assert fetched.content == ""
    assert fetched.rejection_reason == "SKILL.md exceeds byte budget"


def test_github_fetcher_rejects_early_eof_from_declared_length() -> None:
    commit = "f" * 40

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/acme/skills/{commit}/SKILL.md":
            return httpx.Response(
                200,
                text=VALID,
                headers={"content-length": str(len(VALID.encode("utf-8")) + 1)},
                request=request,
            )
        return httpx.Response(404, request=request)

    source = SkillSource(
        "acme/reporting",
        "reporting",
        "reporting",
        "https://skills.sh/acme/reporting",
        repository="acme/skills",
        commit_sha=commit,
    )
    fetched = asyncio.run(
        GitHubImmutableFetcher(transport=httpx.MockTransport(transport)).fetch(
            source,
            max_bytes=100_000,
        )
    )

    assert fetched is not None
    assert fetched.content == ""
    assert fetched.rejection_reason == "SKILL.md response ended before declared length"


def test_github_fetcher_resolves_branch_to_commit_before_blob_fetch() -> None:
    commit = "d" * 40
    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/repos/acme/skills/commits/main":
            return httpx.Response(200, json={"sha": commit}, request=request)
        if request.url.path == f"/acme/skills/{commit}/SKILL.md":
            return httpx.Response(200, text=VALID, request=request)
        return httpx.Response(404, request=request)

    source = SkillSource(
        "acme/reporting",
        "reporting",
        "reporting",
        "https://skills.sh/acme/reporting",
        repository="acme/skills",
        revision="main",
    )
    fetched = asyncio.run(
        GitHubImmutableFetcher(transport=httpx.MockTransport(transport)).fetch(
            source,
            max_bytes=100_000,
        )
    )

    assert fetched is not None
    assert fetched.source_commit_sha == commit
    assert seen == [f"/repos/acme/skills/commits/main", f"/acme/skills/{commit}/SKILL.md"]


def test_github_fetcher_resolves_head_via_default_branch_before_blob_fetch() -> None:
    commit = "9" * 40
    seen: list[str] = []

    def transport(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        if request.url.path == "/repos/acme/skills":
            return httpx.Response(200, json={"default_branch": "main"}, request=request)
        if request.url.path == "/repos/acme/skills/commits/main":
            return httpx.Response(200, json={"sha": commit}, request=request)
        if request.url.path == f"/acme/skills/{commit}/SKILL.md":
            return httpx.Response(200, text=VALID, request=request)
        return httpx.Response(404, request=request)

    source = SkillSource(
        "acme/reporting",
        "reporting",
        "reporting",
        "https://skills.sh/acme/reporting",
        repository="acme/skills",
        revision="HEAD",
    )
    fetched = asyncio.run(
        GitHubImmutableFetcher(transport=httpx.MockTransport(transport)).fetch(
            source,
            max_bytes=100_000,
        )
    )

    assert fetched is not None
    assert fetched.source_commit_sha == commit
    assert seen == [
        "/repos/acme/skills",
        "/repos/acme/skills/commits/main",
        f"/acme/skills/{commit}/SKILL.md",
    ]


@pytest.mark.parametrize("status_code", [401, 429, 503])
def test_github_provider_errors_are_not_silently_accepted(status_code: int) -> None:
    commit = "e" * 40

    def transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == f"/acme/skills/{commit}/SKILL.md":
            return httpx.Response(status_code, request=request)
        return httpx.Response(404, request=request)

    source = SkillSource(
        "acme/reporting",
        "reporting",
        "reporting",
        "https://skills.sh/acme/reporting",
        repository="acme/skills",
        commit_sha=commit,
    )
    with pytest.raises(OnDemandProviderError):
        asyncio.run(
            GitHubImmutableFetcher(transport=httpx.MockTransport(transport)).fetch(
                source,
                max_bytes=100_000,
            )
        )


GITHUB_COMMIT = "a" * 40


def _catalog_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/skills/search"):
        return httpx.Response(
            200,
            json={
                "data": [{
                    "id": "acme/skills/reporting",
                    "name": "Reporting",
                    "source": "acme/skills",
                    "installUrl": "acme/skills",
                    "entrypoint_path": "SKILL.md",
                    "revision": GITHUB_COMMIT,
                    "content": "raw-body-must-not-enter-on-demand-cache",
                    "raw": {"content": "raw-body-must-not-enter-on-demand-cache"},
                }]
            },
            request=request,
        )
    if path == f"/acme/skills/{GITHUB_COMMIT}/SKILL.md":
        return httpx.Response(
            200,
            text=VALID,
            headers={"content-length": str(len(VALID.encode("utf-8")))},
            request=request,
        )
    return httpx.Response(404, request=request)


def test_skills_sh_provider_uses_metadata_discovery_and_pinned_github_without_mirror() -> None:
    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        mirror_enabled=False,
        follow_redirects=False,
        metadata_only=True,
        transport=httpx.MockTransport(_catalog_transport),
    )
    provider = SkillsShOnDemandProvider(catalog)

    async def run():
        sources = await provider.discover("create a spreadsheet report", 2)
        fetched = await provider.fetch(sources[0], max_bytes=100_000)
        return sources, fetched

    sources, fetched = asyncio.run(run())
    assert sources[0].key == "acme/skills/reporting"
    assert fetched is not None
    assert fetched.snapshot_hash == GITHUB_COMMIT
    assert fetched.source_commit_sha == GITHUB_COMMIT
    assert fetched.source_url == f"https://github.com/acme/skills/blob/{GITHUB_COMMIT}/SKILL.md"
    assert fetched.raw_content_digest
    assert catalog._mirror is None
    assert all(
        "content" not in value and "raw" not in value
        for value in catalog._cache.values()
        if isinstance(value, list)
        for item in value
        if isinstance(item, dict)
    )


def test_skills_sh_on_demand_discovery_rejects_redirects() -> None:
    def redirect_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/api/search"):
            return httpx.Response(
                302,
                headers={"location": "https://evil.example/search"},
                request=request,
            )
        return httpx.Response(404, request=request)

    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        public_search_url="https://skills.test/api/search",
        mirror_enabled=False,
        follow_redirects=False,
        metadata_only=True,
        transport=httpx.MockTransport(redirect_transport),
    )
    provider = SkillsShOnDemandProvider(catalog)

    with pytest.raises(SkillsShCatalogError, match="redirect rejected"):
        asyncio.run(provider.discover("create a spreadsheet report", 2))
