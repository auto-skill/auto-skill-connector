from __future__ import annotations

import asyncio
import unittest

from scraper import (
    RunBudget,
    _format_mcp_tools,
    _skill_bundle_sibling_paths,
    fetch_github_raw_content,
    fetch_smithery_tools,
    scan_skill,
)


class _FakeResponse:
    def __init__(self, status_code: int, text: str = "", json_data=None):
        self.status_code = status_code
        self.text = text
        self._json = json_data

    def json(self):
        return self._json


class _FakeClient:
    """Maps exact URLs to canned responses; missing URLs 404."""

    def __init__(self, responses: dict):
        self._responses = responses

    async def get(self, url: str, **kwargs):
        return self._responses.get(url, _FakeResponse(404))


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

    def test_skill_bundle_sibling_paths_picks_md_and_bundle_dirs_only(self) -> None:
        all_paths = [
            "skills/foo/SKILL.md",
            "skills/foo/reference.md",
            "skills/foo/scripts/run.py",
            "skills/foo/resources/template.xlsx",
            "skills/foo/unrelated.bin",
            "skills/bar/SKILL.md",
        ]

        siblings = _skill_bundle_sibling_paths("skills/foo", "skills/foo/SKILL.md", all_paths)

        self.assertIn("skills/foo/reference.md", siblings)
        self.assertIn("skills/foo/scripts/run.py", siblings)
        self.assertIn("skills/foo/resources/template.xlsx", siblings)
        self.assertNotIn("skills/foo/unrelated.bin", siblings)
        self.assertNotIn("skills/foo/SKILL.md", siblings)
        self.assertNotIn("skills/bar/SKILL.md", siblings)

    def test_fetch_github_raw_content_concatenates_manifest_and_readme(self) -> None:
        client = _FakeClient({
            "https://raw.githubusercontent.com/acme/tool/HEAD/SKILL.md": _FakeResponse(200, "SKILL BODY"),
            "https://raw.githubusercontent.com/acme/tool/HEAD/README.md": _FakeResponse(200, "README BODY " * 20),
        })

        content = asyncio.run(fetch_github_raw_content(client, "acme", "tool"))

        self.assertIn("SKILL BODY", content)
        self.assertIn("README BODY", content)

    def test_fetch_github_raw_content_readme_only_when_no_manifest(self) -> None:
        client = _FakeClient({
            "https://raw.githubusercontent.com/acme/tool/HEAD/README.md": _FakeResponse(200, "README BODY " * 20),
        })

        content = asyncio.run(fetch_github_raw_content(client, "acme", "tool"))

        self.assertIn("README BODY", content)

    def test_fetch_smithery_tools_parses_detail_endpoint(self) -> None:
        client = _FakeClient({
            "https://registry.smithery.ai/servers/acme-tool": _FakeResponse(
                200,
                json_data={"tools": [{"name": "search", "description": "Search the web."}]},
            ),
        })

        text = asyncio.run(fetch_smithery_tools(client, "acme-tool"))

        self.assertIn("- search: Search the web.", text)

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
            "https://raw.githubusercontent.com/acme/tool/HEAD/SKILL.md": _FakeResponse(404),
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


if __name__ == "__main__":
    unittest.main()
