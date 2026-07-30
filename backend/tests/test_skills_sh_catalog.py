from __future__ import annotations

import asyncio
from unittest.mock import patch
import httpx

from skills_sh_catalog import SkillsShCatalog
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
