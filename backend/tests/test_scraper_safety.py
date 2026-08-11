from __future__ import annotations

import asyncio
import unittest
from unittest.mock import patch

import scraper
from scraper import (
    CrawlState,
    RunBudget,
    _format_mcp_tools,
    _is_probably_binary,
    _skill_bundle_sibling_paths,
    collect_repo_candidates,
    curate_skill_bundle,
    fetch_github_raw_content,
    fetch_smithery_tool_list,
    scan_skill,
    scrape_skills_sh,
    targeted_tree_crawl_repos,
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", json_data=None, content: bytes | None = None, headers=None):
        self.status_code = status_code
        self.text = text
        self.content = content if content is not None else text.encode("utf-8")
        self._json = json_data
        self.headers = headers or {}

    def json(self):
        return self._json


class _FakeClient:
    """Maps exact URLs to canned responses; missing URLs 404. A response may
    also be a callable(kwargs) -> _FakeResponse, for endpoints (like Ollama's
    /api/chat) where the same URL serves different models/requests."""

    def __init__(self, responses: dict):
        self._responses = responses

    def _resolve(self, url: str, **kwargs):
        resp = self._responses.get(url, _FakeResponse(404))
        return resp(kwargs) if callable(resp) else resp

    async def get(self, url: str, **kwargs):
        return self._resolve(url, **kwargs)

    async def post(self, url: str, **kwargs):
        return self._resolve(url, **kwargs)


VALID_CONTENT = """---
name: spreadsheet-reporter
description: Build spreadsheet reports with formulas and charts.
---

## Workflow

Use this skill when the user needs a spreadsheet report. Inspect the data,
create formulas, verify calculations, add charts, and validate the workbook
before returning it. Explain important assumptions to the user.
Preserve leading-zero identifiers, confirm worksheet names, and check totals
against representative source rows. Document calculation choices, inspect
chart ranges, and ensure the finished workbook opens without formula errors.
"""


class ScraperSafetyTests(unittest.TestCase):
    def test_targeted_tree_crawl_repos_bypass_global_search_discovery(self) -> None:
        with patch.dict(
            "os.environ",
            {"AUTOSKILL_TARGETED_TREE_REPOS": "Example/Private-Repo,not a repo"},
            clear=False,
        ):
            repos = targeted_tree_crawl_repos()
            candidates = collect_repo_candidates([], CrawlState({}))

        candidate_repos = {repo for repo, _ in candidates}
        self.assertIn("oxcaml/oxcaml", repos)
        self.assertIn("github/awesome-copilot", repos)
        self.assertIn("example/private-repo", repos)
        self.assertNotIn("not a repo", repos)
        self.assertTrue(set(repos).issubset(candidate_repos))

    def test_unauthenticated_budget_is_a_small_incremental_crawl(self) -> None:
        budget = RunBudget(authenticated=False)

        self.assertTrue(all(budget.take("core") for _ in range(12)))
        self.assertFalse(budget.take("core"))
        self.assertTrue(all(budget.take("search") for _ in range(8)))
        self.assertFalse(budget.take("search"))
        self.assertFalse(budget.take("code_search"))

    def test_rescan_invalidates_embedding_when_content_changes(self) -> None:
        skill = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://github.com/example/repo/blob/main/SKILL.md",
            "content_hash": "old-content-hash",
            "embedding": [1.0] * 384,
            "embedding_text_hash": "old-embedding-hash",
            "embedded_at": "2026-07-01T00:00:00+00:00",
            "_content": VALID_CONTENT,
        }

        asyncio.run(scan_skill(None, skill))

        self.assertEqual(skill["quality_status"], "active")
        self.assertIsNone(skill["embedding"])
        self.assertIsNone(skill["embedding_text_hash"])
        self.assertIsNone(skill["embedded_at"])


    def test_format_mcp_tools_renders_names_descriptions_and_params(self) -> None:
        text = _format_mcp_tools([
            {"name": "read_url", "description": "Fetch a page.", "inputSchema": {"properties": {"url": {}}}},
            {"name": "no_params", "description": "Does a thing."},
            {"name": "", "description": "Skipped: no name"},
        ])

        self.assertIn("- read_url: Fetch a page. (params: url)", text)
        self.assertIn("- no_params: Does a thing.", text)
        self.assertNotIn("Skipped: no name", text)

    def test_format_mcp_tools_empty_list_returns_empty_string(self) -> None:
        self.assertEqual(_format_mcp_tools([]), "")
        self.assertEqual(_format_mcp_tools(None), "")

    def test_skill_bundle_sibling_paths_includes_everything_under_dir_unfiltered(self) -> None:
        all_paths = [
            "skills/foo/SKILL.md",
            "skills/foo/reference.md",
            "skills/foo/scripts/run.py",
            "skills/foo/resources/template.xlsx",
            "skills/foo/anything.bin",
            "skills/bar/SKILL.md",
        ]

        siblings = _skill_bundle_sibling_paths("skills/foo", "skills/foo/SKILL.md", all_paths)

        self.assertEqual(
            set(siblings),
            {
                "skills/foo/reference.md",
                "skills/foo/scripts/run.py",
                "skills/foo/resources/template.xlsx",
                "skills/foo/anything.bin",
            },
        )
        self.assertNotIn("skills/foo/SKILL.md", siblings)
        self.assertNotIn("skills/bar/SKILL.md", siblings)

    def test_fetch_github_raw_content_lists_full_tree_and_fetches_everything(self) -> None:
        client = _FakeClient({
            "https://api.github.com/repos/acme/tool/git/trees/HEAD": _FakeResponse(
                200,
                json_data={"tree": [
                    {"path": "SKILL.md", "type": "blob"},
                    {"path": "README.md", "type": "blob"},
                    {"path": "assets/logo.png", "type": "blob"},
                    {"path": "docs", "type": "tree"},  # directories are not blobs, must be skipped
                ]},
            ),
            "https://raw.githubusercontent.com/acme/tool/HEAD/SKILL.md": _FakeResponse(200, "SKILL BODY " * 20),
            "https://raw.githubusercontent.com/acme/tool/HEAD/README.md": _FakeResponse(200, "README BODY " * 20),
            "https://raw.githubusercontent.com/acme/tool/HEAD/assets/logo.png": _FakeResponse(
                200, content=b"\x89PNG\r\n\x1a\n\x00\x00\x00"
            ),
        })

        content = asyncio.run(fetch_github_raw_content(client, "acme", "tool"))

        self.assertIn("SKILL BODY", content)
        self.assertIn("README BODY", content)
        self.assertIn("## assets/logo.png", content)
        self.assertIn("[binary file, not indexed]", content)
        self.assertNotIn("docs", content)

    def test_fetch_github_raw_content_empty_tree_returns_empty(self) -> None:
        client = _FakeClient({
            "https://api.github.com/repos/acme/tool/git/trees/HEAD": _FakeResponse(404),
        })

        content = asyncio.run(fetch_github_raw_content(client, "acme", "tool"))

        self.assertEqual(content, "")

    def test_fetch_smithery_tool_list_parses_detail_endpoint(self) -> None:
        client = _FakeClient({
            "https://registry.smithery.ai/servers/acme-tool": _FakeResponse(
                200,
                json_data={"tools": [{"name": "search", "description": "Search the web."}]},
            ),
        })

        tools = asyncio.run(fetch_smithery_tool_list(client, "acme-tool"))

        self.assertEqual(tools, [{"name": "search", "description": "Search the web."}])
        self.assertIn("- search: Search the web.", _format_mcp_tools(tools))

    def test_scan_skill_smithery_registry_fetches_tools(self) -> None:
        client = _FakeClient({
            "https://registry.smithery.ai/servers/acme-tool": _FakeResponse(
                200,
                json_data={"tools": [
                    {
                        "name": "search",
                        "description": (
                            "Use when the user needs to search the web for current "
                            "information. You must provide a query string; do not call "
                            "without one, and always check the returned results."
                        ),
                    },
                    {
                        "name": "read_url",
                        "description": "Fetch and read the content of a specific web page URL.",
                    },
                ]},
            ),
        })
        skill = {
            "name": "Acme Tool",
            "description": "An MCP server.",
            "source": "smithery_registry",
            "url": "https://smithery.ai/server/acme-tool",
            "raw": {"qualified_name": "acme-tool"},
        }

        asyncio.run(scan_skill(client, skill))

        # MCP registry content never carries SKILL.md frontmatter, so it stays
        # metadata_only (never auto-injected) -- but content_hash proves the
        # tool list was actually fetched and scanned, not silently skipped.
        self.assertNotEqual(skill["content_hash"], "")
        self.assertEqual(skill["quality_status"], "metadata_only")

    def test_scan_skill_glama_registry_follows_repo_url(self) -> None:
        readme = (
            "## Usage\n\n"
            "Use when the user needs to manage their project's incident tabletop "
            "exercises. You must call the list tool first, then the schedule tool. "
            "Do not skip evidence collection before marking an exercise complete, "
            "and always confirm the participant roster and the exercise scenario "
            "name before generating the final evidence packet for compliance review.\n"
        )
        client = _FakeClient({
            "https://api.github.com/repos/acme/tool/git/trees/HEAD": _FakeResponse(
                200, json_data={"tree": [{"path": "README.md", "type": "blob"}]},
            ),
            "https://raw.githubusercontent.com/acme/tool/HEAD/README.md": _FakeResponse(200, readme),
        })
        skill = {
            "name": "Acme Tool",
            "description": "An MCP server.",
            "source": "glama_registry",
            "url": "https://glama.ai/mcp/servers/abc123",
            "raw": {"repo_url": "https://github.com/acme/tool"},
        }

        asyncio.run(scan_skill(client, skill))

        # MCP registry content never carries SKILL.md frontmatter, so it stays
        # metadata_only (never auto-injected) -- but content_hash proves the
        # README was actually followed and fetched via repo_url, not skipped.
        self.assertNotEqual(skill["content_hash"], "")
        self.assertEqual(skill["quality_status"], "metadata_only")

    def test_is_probably_binary_detects_by_extension_and_bytes(self) -> None:
        self.assertTrue(_is_probably_binary("assets/logo.png", b"\x89PNG\r\n\x1a\n"))
        self.assertTrue(_is_probably_binary("data.bin", b"plain text but binary extension"))
        self.assertTrue(_is_probably_binary("weird_file", b"\x00\x01\x02\x03"))
        self.assertFalse(_is_probably_binary("README.md", b"# Hello\n\nJust plain text."))

    def test_curate_skill_bundle_returns_model_output_on_success(self) -> None:
        ollama_chat = f"{scraper.OLLAMA_URL}/api/chat"
        client = _FakeClient({
            ollama_chat: _FakeResponse(
                200, json_data={"message": {"content": "## SKILL.md\n\ncurated body"}}
            ),
        })

        curated = asyncio.run(curate_skill_bundle(client, "Acme Tool", "desc", "## SKILL.md\n\nraw noisy body"))

        self.assertEqual(curated, "## SKILL.md\n\ncurated body")

    def test_curate_skill_bundle_falls_back_to_raw_on_failure(self) -> None:
        ollama_chat = f"{scraper.OLLAMA_URL}/api/chat"
        client = _FakeClient({ollama_chat: _FakeResponse(500)})
        raw = "## SKILL.md\n\nraw noisy body"

        curated = asyncio.run(curate_skill_bundle(client, "Acme Tool", "desc", raw))

        self.assertEqual(curated, raw)

    def test_curate_skill_bundle_empty_input_short_circuits(self) -> None:
        client = _FakeClient({})

        curated = asyncio.run(curate_skill_bundle(client, "Acme Tool", "desc", "   "))

        self.assertEqual(curated, "   ")

    def test_scan_skill_runs_curation_and_capability_summary_inline(self) -> None:
        valid_content = VALID_CONTENT
        ollama_chat = f"{scraper.OLLAMA_URL}/api/chat"

        def respond(kwargs):
            model = kwargs["json"]["model"]
            if model == scraper.CURATION_MODEL:
                return _FakeResponse(200, json_data={"message": {"content": valid_content}})
            return _FakeResponse(
                200,
                json_data={"message": {"content": '{"summary": "Builds spreadsheet reports with formulas."}'}},
            )

        client = _FakeClient({ollama_chat: respond})
        skill = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://github.com/example/repo/blob/main/SKILL.md",
            "_content": valid_content,
        }

        fake_vector = [0.0] * 384
        with patch.object(scraper, "embed_texts", return_value=[fake_vector]) as mock_embed:
            asyncio.run(scan_skill(client, skill))

        self.assertEqual(skill["quality_status"], "active")
        self.assertEqual(skill["capability_summary"], "Builds spreadsheet reports with formulas.")
        self.assertEqual(skill["embedding"], fake_vector)
        self.assertIsNotNone(skill["embedded_at"])
        mock_embed.assert_called_once()

    def test_scan_skill_skips_stage3_when_summary_already_present_and_content_unchanged(self) -> None:
        valid_content = VALID_CONTENT
        from quality import canonicalize_skill_content, content_hash as quality_content_hash

        skill = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://github.com/example/repo/blob/main/SKILL.md",
            "content_hash": quality_content_hash(canonicalize_skill_content(valid_content)),
            "capability_summary": "Already summarized.",
            "_content": valid_content,
        }

        with patch.object(scraper, "generate_capability_summary") as mock_summary:
            asyncio.run(scan_skill(None, skill))

        mock_summary.assert_not_called()
        self.assertEqual(skill["capability_summary"], "Already summarized.")

    def test_scan_skill_indexes_metadata_only_mcp_server_stage3(self) -> None:
        """The active gate widened to quality.ACTIVE_STATUSES: a real MCP
        server (no SKILL.md frontmatter, so it can never be more than
        metadata_only) must still get a capability_summary and embedding --
        it's just never eligible for full/inject tier (see
        test_quality_routing.test_metadata_only_never_full_routes)."""
        ollama_chat = f"{scraper.OLLAMA_URL}/api/chat"

        def respond(kwargs):
            model = kwargs["json"]["model"]
            if model == scraper.CURATION_MODEL:
                return _FakeResponse(200, json_data={"message": {"content": kwargs["json"]["messages"][1]["content"]}})
            return _FakeResponse(
                200,
                json_data={"message": {"content": '{"summary": "Lets agents query survey results over HTTP."}'}},
            )

        client = _FakeClient({
            ollama_chat: respond,
            "https://registry.smithery.ai/servers/acme-tool": _FakeResponse(
                200,
                json_data={"tools": [
                    {
                        "name": "get_results",
                        "description": (
                            "Fetch survey results for a project, including NPS, CSAT, CES, and "
                            "PMF scores, individual responses, and shareable links to dashboards."
                        ),
                    },
                    {
                        "name": "list_surveys",
                        "description": "List every survey configured for the current project, with status and response counts.",
                    },
                ]},
            ),
        })
        skill = {
            "name": "Acme Surveys",
            "description": "An MCP server for surveys.",
            "source": "smithery_registry",
            "url": "https://smithery.ai/server/acme-tool",
            "raw": {"qualified_name": "acme-tool"},
        }

        fake_vector = [0.0] * 384
        with patch.object(scraper, "embed_texts", side_effect=lambda texts, *a, **k: [fake_vector] * len(texts)) as mock_embed:
            asyncio.run(scan_skill(client, skill))

        self.assertEqual(skill["quality_status"], "metadata_only")
        self.assertEqual(skill["capability_summary"], "Lets agents query survey results over HTTP.")
        self.assertEqual(skill["embedding"], fake_vector)
        self.assertIsNotNone(skill["embedded_at"])
        # One call for the skill-level embedding, one for the per-tool batch
        # (skill_tools) -- see refresh_skill_tools/vector_search_tools.
        self.assertEqual(mock_embed.call_count, 2)
        tool_rows = skill.get("_tool_rows")
        self.assertEqual(len(tool_rows), 2)
        self.assertEqual({r["tool_name"] for r in tool_rows}, {"get_results", "list_surveys"})
        self.assertTrue(all(r["skill_url"] == skill["url"] for r in tool_rows))
        self.assertIsNotNone(skill.get("tools_hash"))

    def test_mark_content_duplicates_covers_metadata_only(self) -> None:
        a = {"content_hash": "same-hash", "quality_status": "metadata_only", "quality_score": 40}
        b = {"content_hash": "same-hash", "quality_status": "metadata_only", "quality_score": 80}

        scraper.mark_content_duplicates([a, b])

        # pick_canonical prefers the higher quality_score; the loser is marked
        # a duplicate. Both being metadata_only must not exempt them from dedup.
        statuses = {id(a): a.get("quality_status"), id(b): b.get("quality_status")}
        self.assertIn("duplicate", (a.get("quality_status"), b.get("quality_status")))
        self.assertNotEqual(a.get("quality_status"), b.get("quality_status"))

    def test_missing_package_is_informational_not_a_status_override(self) -> None:
        """Reconciliation decision: a GitHub-sourced skill fetched without an
        immutable package snapshot (no commit-pinned provenance) is NOT
        downgraded to a separate quality_status and does NOT lose its
        embedding eligibility -- package_completeness is tracked as an
        honest, separate metadata field. quality.tier_for_ranked_candidates
        (active/metadata_only + score/risk/similarity gates) stays the sole
        tier decision; package completeness is provenance information, not a
        second gate on top of it."""
        skill = {
            "name": "spreadsheet-reporter",
            "description": "Build spreadsheet reports with formulas and charts.",
            "source": "github_skill_file",
            "url": "https://github.com/example/repo/blob/main/SKILL.md",
            "_content": VALID_CONTENT,
        }

        asyncio.run(scan_skill(None, skill))

        self.assertEqual(skill["quality_status"], "active")
        self.assertEqual(skill["package_completeness"], "missing")

    def test_skills_sh_curated_capture_keeps_full_package_separate_from_entrypoint(self) -> None:
        class Response:
            def __init__(self, payload):
                self.status_code = 200
                self._payload = payload

            def json(self):
                return self._payload

        class Client:
            async def get(self, url, **_kwargs):
                if url.endswith("/curated"):
                    return Response(
                        {
                            "data": [
                                {
                                    "skills": [
                                        {
                                            "id": "acme/skills/report",
                                            "name": "report",
                                            "sourceType": "github",
                                            "installUrl": "https://github.com/acme/skills",
                                            "url": "https://skills.sh/acme/skills/report",
                                        }
                                    ]
                                }
                            ]
                        }
                    )
                return Response(
                    {
                        "id": "acme/skills/report",
                        "slug": "report",
                        "hash": "registry-snapshot",
                        "files": [
                            {"path": "SKILL.md", "contents": VALID_CONTENT},
                            {"path": "references/checks.md", "contents": "Verify every total."},
                        ],
                    }
                )

        skills = []
        with patch("scraper.SKILLS_SH_OIDC_TOKEN", "test-oidc"), patch(
            "scraper.ImmutablePackageStore.put", return_value=None
        ):
            asyncio.run(scrape_skills_sh(Client(), skills))

        self.assertEqual(len(skills), 1)
        skill = skills[0]
        self.assertEqual(skill["source"], "skills_sh")
        # The immutable package stores every file (audit/provenance)...
        self.assertEqual(skill["_package_manifest"]["stored_files"], 2)
        self.assertEqual(skill["_package_manifest"]["source"]["registry_snapshot_hash"], "registry-snapshot")
        # ...but the entrypoint content scan_skill will curate/embed from is
        # only SKILL.md itself, not the sibling reference file's contents.
        self.assertNotIn("Verify every total", skill["_content"])


if __name__ == "__main__":
    unittest.main()
