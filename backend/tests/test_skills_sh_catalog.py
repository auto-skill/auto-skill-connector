from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import patch
from uuid import uuid4
import httpx

from skills_sh_catalog import SkillsShCatalog, SkillsShCatalogError
from query_compiler import compile_intent_query


def _catalog_transport(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/skills/search"):
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "acme/skills/reporting",
                        "name": "Reporting",
                        "source": "acme/skills",
                        "installs": 1234,
                        "installUrl": "https://github.com/acme/skills",
                        "url": "https://skills.sh/acme/skills/reporting",
                        "sourceType": "github",
                    }
                ]
            },
            request=request,
        )
    if path.endswith("/skills/acme/skills/reporting"):
        return httpx.Response(
            200,
            json={
                "id": "acme/skills/reporting",
                "slug": "reporting",
                "hash": "snapshot-123",
                "files": [
                    {
                        "path": "SKILL.md",
                        "contents": (
                            "---\nname: Reporting\ndescription: Build spreadsheet reports\n---\n"
                            "## Workflow\nCreate a spreadsheet report from the user's source data. "
                            "Inspect the workbook structure, normalize the input tables, and preserve "
                            "existing sheet names. Add formulas for the requested calculations, create "
                            "charts only when they clarify the result, and verify representative totals. "
                            "Explain the generated workbook and list any assumptions before returning it.\n"
                        ),
                    },
                    {"path": "examples/report.py", "contents": "print('report')"},
                ],
            },
            request=request,
        )
    if path.endswith("/skills/audit/acme/skills/reporting"):
        return httpx.Response(
            200,
            json={
                "audits": [
                    {"provider": "Socket", "status": "pass", "riskLevel": "LOW"},
                    {"provider": "Snyk", "status": "pass", "riskLevel": "LOW"},
                ]
            },
            request=request,
        )
    return httpx.Response(404, request=request)


def test_live_catalog_hydrates_shortlist_and_caches_search() -> None:
    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        transport=httpx.MockTransport(_catalog_transport),
    )

    async def run() -> tuple[list[dict], list[dict]]:
        first = await catalog.retrieve("create spreadsheet report", limit=3)
        second = await catalog.retrieve("create spreadsheet report", limit=3)
        return first, second

    first, second = asyncio.run(run())
    assert first == second
    assert len(first) == 1
    row = first[0]
    assert row["id"] == "acme/skills/reporting"
    assert row["source"] == "acme/skills"
    assert row["registry"] == "skills_sh"
    assert row["source_snapshot_hash"] == "snapshot-123"
    assert row["audit_status"] == "pass"
    assert row["risk_score"] == 0
    assert row["quality_status"] == "active"
    assert row["provenance_score"] == 0.45
    assert row["content_hash"]
    assert "spreadsheet report" in row["retrieval_text"].lower()


def test_persistent_mirror_serves_warm_retrieval_without_upstream() -> None:
    calls = 0

    def counting_transport(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return _catalog_transport(request)

    mirror_path = Path(__file__).resolve().parents[1] / f".test-skills-sh-mirror-{uuid4().hex}.db"
    mirror = str(mirror_path)
    first = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        mirror_db_path=mirror,
        mirror_enabled=True,
        transport=httpx.MockTransport(counting_transport),
    )
    first_rows = asyncio.run(first.retrieve("create spreadsheet report", limit=1))
    assert first_rows and calls == 3

    def should_not_call(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected upstream request: {request.url}")

    second = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        mirror_db_path=mirror,
        mirror_enabled=True,
        transport=httpx.MockTransport(should_not_call),
    )
    second_rows = asyncio.run(second.retrieve("spreadsheet report", limit=1))
    assert second_rows[0]["skills_sh_id"] == first_rows[0]["skills_sh_id"]
    assert second_rows[0]["retrieval_backend"] == "skills_sh_mirror"
    assert second_rows[0]["mirror_fresh"] is True
    for suffix in ("", "-wal", "-shm"):
        mirror_path.with_name(mirror_path.name + suffix).unlink(missing_ok=True)


def test_concurrent_cold_retrieval_is_single_flight() -> None:
    calls = 0

    async def run() -> list[list[dict]]:
        nonlocal calls

        def counting_transport(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            return _catalog_transport(request)

        catalog = SkillsShCatalog(
            api_url="https://skills.test/api/v1",
            oidc_token="test-token",
            mirror_enabled=False,
            transport=httpx.MockTransport(counting_transport),
        )
        return await asyncio.gather(
            catalog.retrieve("create spreadsheet report", limit=1),
            catalog.retrieve("create spreadsheet report", limit=1),
        )

    rows = asyncio.run(run())
    assert rows[0] == rows[1]
    assert calls == 3


def test_missing_audit_is_not_treated_as_safe() -> None:
    def no_audit(request: httpx.Request) -> httpx.Response:
        response = _catalog_transport(request)
        if "/audit/" in request.url.path:
            return httpx.Response(404, request=request)
        return response

    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        transport=httpx.MockTransport(no_audit),
    )
    rows = asyncio.run(catalog.retrieve("create spreadsheet report", limit=1))
    assert rows[0]["audit_status"] == "unknown"
    assert rows[0]["risk_score"] == 1
    assert "audit-unavailable" in rows[0]["risk_flags"]


def test_catalog_retries_transient_rate_limit() -> None:
    calls = 0

    def rate_limited_once(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        if request.url.path.endswith("/skills/search"):
            calls += 1
            if calls == 1:
                return httpx.Response(429, headers={"Retry-After": "0"}, request=request)
        return _catalog_transport(request)

    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        transport=httpx.MockTransport(rate_limited_once),
        max_retries=1,
        retry_base_seconds=0,
    )
    rows = asyncio.run(catalog.search("create spreadsheet report", limit=1))
    assert calls == 2
    assert rows[0]["id"] == "acme/skills/reporting"


def test_authenticated_catalog_exposes_bounded_sync_sources() -> None:
    def sync_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/skills"):
            return httpx.Response(
                200,
                json={"data": [{"id": "acme/skills/reporting", "name": "Reporting"}]},
                request=request,
            )
        if request.url.path.endswith("/skills/curated"):
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "owner": "acme",
                            "skills": [{"id": "acme/skills/official", "name": "Official"}],
                        }
                    ]
                },
                request=request,
            )
        return httpx.Response(404, request=request)

    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        mirror_enabled=False,
        transport=httpx.MockTransport(sync_transport),
    )

    async def run() -> tuple[list[dict], list[dict]]:
        return await catalog.leaderboard(per_page=500), await catalog.curated()

    leaderboard, curated = asyncio.run(run())
    assert leaderboard[0]["id"] == "acme/skills/reporting"
    assert curated[0]["id"] == "acme/skills/official"


def test_leaderboard_page_preserves_pagination_metadata() -> None:
    def paged_transport(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/skills")
        return httpx.Response(
            200,
            json={
                "data": [{"id": "acme/skills/one", "name": "One"}],
                "pagination": {"page": 3, "perPage": 500, "total": 8420, "hasMore": True},
            },
            request=request,
        )

    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        mirror_enabled=False,
        transport=httpx.MockTransport(paged_transport),
    )
    page = asyncio.run(catalog.leaderboard_page(view="all-time", page=3, per_page=500))
    assert page["data"][0]["id"] == "acme/skills/one"
    assert page["pagination"]["total"] == 8420
    assert page["pagination"]["hasMore"] is True


def test_listing_index_is_metadata_only_and_searchable(tmp_path: Path) -> None:
    mirror_path = tmp_path / "listing-mirror.db"
    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        oidc_token="test-token",
        mirror_db_path=str(mirror_path),
        mirror_enabled=True,
        transport=httpx.MockTransport(lambda request: httpx.Response(500, request=request)),
    )

    async def run() -> list[dict]:
        indexed = await catalog.index_listings(
            [
                {
                    "id": "acme/skills/csv-cleaner",
                    "slug": "csv-cleaner",
                    "name": "CSV Cleaner",
                    "description": "Clean CSV datasets and normalize tabular data.",
                    "source": "acme/skills",
                    "sourceType": "github",
                    "installUrl": "https://github.com/acme/skills",
                    "url": "https://skills.sh/acme/skills/csv-cleaner",
                }
            ]
        )
        assert indexed == 1
        return await catalog.retrieve("clean CSV datasets", limit=1)

    rows = asyncio.run(run())
    assert rows[0]["quality_status"] == "metadata_only"
    assert rows[0]["content_hash"] is None
    assert rows[0]["retrieval_backend"] == "skills_sh_mirror"


def test_public_search_fallback_is_metadata_only_hint() -> None:
    def public_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(
                200,
                json={
                    "query": "react landing page",
                    "searchType": "semantic",
                    "skills": [
                        {
                            "id": "acme/skills/landing-page",
                            "skillId": "landing-page",
                            "name": "landing-page",
                            "source": "acme/skills",
                            "installs": 42,
                        }
                    ],
                    "count": 1,
                },
                request=request,
            )
        return httpx.Response(
            200,
            text='<script type="application/ld+json">{"@type":"SoftwareApplication","description":"Build landing pages.","interactionStatistic":{"userInteractionCount":42}}</script>',
            request=request,
        )

    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        public_search_url="https://skills.test/api/search",
        transport=httpx.MockTransport(public_transport),
    )
    rows = asyncio.run(catalog.retrieve("design a React landing page", limit=1))
    assert rows[0]["id"] == "acme/skills/landing-page"
    assert rows[0]["quality_status"] == "metadata_only"
    assert rows[0]["audit_status"] == "unknown"
    assert rows[0]["content_hash"] is None
    assert rows[0]["install_url"] == "acme/skills"
    assert rows[0]["description"] == "Build landing pages."


def test_public_follow_up_rehydrates_only_cached_skills_sh_listing() -> None:
    """A conversation may carry an ID, but it must not become a local DB lookup."""

    def public_transport(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/search":
            return httpx.Response(
                200,
                json={
                    "skills": [{
                        "id": "acme/skills/landing-page",
                        "skillId": "landing-page",
                        "name": "landing-page",
                        "source": "acme/skills",
                        "installs": 42,
                    }],
                },
                request=request,
            )
        return httpx.Response(200, text="", request=request)

    catalog = SkillsShCatalog(
        api_url="https://skills.test/api/v1",
        public_search_url="https://skills.test/api/search",
        transport=httpx.MockTransport(public_transport),
    )

    async def run() -> list[dict]:
        await catalog.search("landing page", limit=2)
        return await catalog.retrieve_ids(["acme/skills/landing-page"])

    rows = asyncio.run(run())
    assert rows[0]["id"] == "acme/skills/landing-page"
    assert rows[0]["quality_status"] == "metadata_only"
    assert rows[0]["registry"] == "skills_sh"


def test_live_follow_up_rejects_arbitrary_github_url() -> None:
    import recommender

    class FakeCatalog:
        async def retrieve_ids(self, skill_ids: list[str], limit: int) -> list[dict]:
            assert skill_ids == ["acme/skills/landing-page"]
            return [{"id": skill_ids[0], "name": "landing-page"}]

    async def run() -> list[dict]:
        with patch.object(recommender, "default_catalog", return_value=FakeCatalog()), patch.object(
            recommender, "SKILLS_SH_LIVE_ROUTING", True
        ):
            return await recommender.fetch_skills_by_urls(
                None,
                ["https://github.com/acme/skills", "acme/skills/landing-page"],
            )

    rows = asyncio.run(run())
    assert rows[0]["retrieval_backend"] == "skills_sh"


def test_catalog_reads_rotating_token_from_environment(monkeypatch) -> None:
    monkeypatch.delenv("SKILLS_SH_OIDC_TOKEN", raising=False)
    monkeypatch.setenv("VERCEL_OIDC_TOKEN", "rotated-token")
    catalog = SkillsShCatalog(api_url="https://skills.test/api/v1")
    assert catalog.configured is True
    monkeypatch.delenv("VERCEL_OIDC_TOKEN")
    assert catalog.configured is False


def test_recommender_uses_live_catalog_before_local_store() -> None:
    class FakeCatalog:
        configured = True

        async def retrieve(self, query: str, limit: int) -> list[dict]:
            return [
                {
                    "id": "acme/skills/reporting",
                    "name": "Reporting",
                    "description": "Build spreadsheet reports with formulas.",
                    "source": "skills_sh",
                    "url": "https://github.com/acme/skills",
                    "install_url": "https://github.com/acme/skills",
                    "skills_sh_id": "acme/skills/reporting",
                    "quality_status": "active",
                    "quality_score": 90,
                    "risk_score": 0,
                    "content_hash": "a" * 64,
                    "retrieval_text": "reporting spreadsheet formulas",
                    "rank": 0.03,
                    "similarity": None,
                }
            ]

    import recommender

    async def run() -> list[dict]:
        intent = compile_intent_query("create a spreadsheet report with formulas")
        with patch.object(recommender, "default_catalog", return_value=FakeCatalog()), patch.object(
            recommender, "SKILLS_SH_LIVE_ROUTING", True
        ), patch.object(
            recommender, "_retrieve_local_skills", side_effect=AssertionError("local fallback used")
        ):
            return await recommender.retrieve_skills_for_intent(None, intent, limit=3)

    rows = asyncio.run(run())
    assert rows[0]["retrieval_backend"] == "skills_sh"
    assert rows[0]["skills_sh_id"] == "acme/skills/reporting"


def test_recommender_calls_public_catalog_without_oidc_token() -> None:
    """Tokenless routing must use skills.sh public discovery, not skip it."""

    class PublicCatalog:
        configured = False

        async def retrieve(self, query: str, limit: int) -> list[dict]:
            assert query == "create a spreadsheet report"
            assert limit >= 3
            return [{"id": "acme/skills/reporting", "name": "Reporting"}]

    import recommender

    async def run() -> list[dict]:
        with patch.object(recommender, "default_catalog", return_value=PublicCatalog()), patch.object(
            recommender, "SKILLS_SH_LIVE_ROUTING", True
        ), patch.object(
            recommender, "_retrieve_local_skills", side_effect=AssertionError("public route bypassed skills.sh")
        ):
            return await recommender.retrieve_skills(None, "create a spreadsheet report", limit=3)

    rows = asyncio.run(run())
    assert rows[0]["retrieval_backend"] == "skills_sh"


def test_recommender_abstains_when_skills_sh_is_unavailable_by_default() -> None:
    class BrokenCatalog:
        configured = False

        async def retrieve(self, query: str, limit: int) -> list[dict]:
            raise SkillsShCatalogError("public search unavailable")

    import recommender

    async def run() -> list[dict]:
        with patch.object(recommender, "default_catalog", return_value=BrokenCatalog()), patch.object(
            recommender, "SKILLS_SH_LIVE_ROUTING", True
        ), patch.object(
            recommender, "ALLOW_LOCAL_RETRIEVAL_FALLBACK", False
        ), patch.object(
            recommender, "_retrieve_local_skills", side_effect=AssertionError("local fallback bypassed gate")
        ):
            return await recommender.retrieve_skills(None, "create a spreadsheet report", limit=3)

    assert asyncio.run(run()) == []
