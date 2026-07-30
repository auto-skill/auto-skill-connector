"""Phase 2A: accepted SKILL.md bodies must round-trip complete through the library."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from embeddings import LibraryContent
from quality import (
    MAX_SKILL_CONTENT_CHARS,
    canonicalize_skill_content,
    content_hash,
    evaluate_quality,
)
from scraper import (
    _accepted_fetched_text,
    _skill_from_skill_md,
    save_to_library,
    scan_skill,
)


def _large_skill_md(*, sections: int = 40, paragraph_chars: int = 900) -> str:
    """Build a multi-section SKILL.md larger than the old 20k silent cut."""
    parts = [
        "---",
        "name: large-workbook-ops",
        "description: End-to-end workbook operations spanning discovery, formulas, charts, and audits.",
        "---",
        "",
        "# Large Workbook Ops",
        "",
        "Use this skill when the user needs a complete spreadsheet workflow that spans many steps.",
        "",
    ]
    filler = ("Preserve identifiers, verify formulas, and document assumptions. " * 20)[:paragraph_chars]
    for i in range(1, sections + 1):
        parts.extend(
            [
                f"## Section {i}: Workflow Checkpoint",
                "",
                filler,
                "",
                f"- Step {i}.a: inspect the source range and confirm headers.",
                f"- Step {i}.b: write formulas that keep leading zeroes intact.",
                f"- Step {i}.c: validate totals against representative rows.",
                "",
            ]
        )
    body = "\n".join(parts)
    assert len(body) > 20_000, f"fixture must exceed legacy truncate; got {len(body)}"
    assert len(body) < MAX_SKILL_CONTENT_CHARS
    return body


class LibraryContentRoundTripTests(unittest.TestCase):
    def test_accepted_fetched_text_keeps_full_body(self) -> None:
        body = _large_skill_md()
        accepted = _accepted_fetched_text(body.replace("\n", "\r\n"))
        self.assertEqual(accepted, canonicalize_skill_content(body))
        self.assertGreater(len(accepted), 20_000)

    def test_accepted_fetched_text_refuses_oversized_without_truncating(self) -> None:
        huge = "x" * (MAX_SKILL_CONTENT_CHARS + 1)
        self.assertEqual(_accepted_fetched_text(huge), "")

    def test_quality_rejects_oversized_instead_of_accepting_stub(self) -> None:
        skill = {
            "name": "too-big",
            "description": "A skill whose body exceeds the hard library ceiling on purpose.",
            "source": "github_skill_file",
        }
        huge = (
            "---\nname: too-big\n"
            "description: A skill whose body exceeds the hard library ceiling on purpose.\n"
            "---\n\n## Workflow\n\n"
            + ("Use when building reports and validating formulas carefully. " * 20000)
        )
        self.assertGreater(len(huge), MAX_SKILL_CONTENT_CHARS)
        result = evaluate_quality(skill, huge)
        self.assertEqual(result["quality_status"], "rejected")
        self.assertIn("content-too-large", result["quality_reasons"])

    def test_save_hash_reload_equals_full_body(self) -> None:
        body = _large_skill_md()
        skill = {
            "name": "large-workbook-ops",
            "description": "End-to-end workbook operations spanning discovery, formulas, charts, and audits.",
            "source": "github_skill_file",
            "url": "https://github.com/example/repo/tree/HEAD/skills/large-workbook-ops",
            "content_hash": content_hash(body),
            "quality_status": "active",
        }

        with tempfile.TemporaryDirectory() as tmp:
            library_dir = Path(tmp) / "skills_library"
            files_dir = library_dir / "files"
            index_path = library_dir / "index.json"
            with mock.patch("scraper.LIBRARY_DIR", library_dir), mock.patch(
                "scraper.LIBRARY_FILES_DIR", files_dir
            ), mock.patch("scraper.LIBRARY_INDEX_PATH", index_path), mock.patch(
                "scraper._library_index", None
            ), mock.patch("scraper._library_dirty", 0), mock.patch(
                "scraper._library_last_flush", 0.0
            ):
                asyncio.run(save_to_library(skill, body))
                asyncio.run(save_to_library(skill, body))  # idempotent rewrite

            stored_name = json.loads(index_path.read_text(encoding="utf-8"))[skill["url"]]["file"]
            stored = (files_dir / stored_name).read_text(encoding="utf-8")
            self.assertEqual(stored, canonicalize_skill_content(body))
            self.assertEqual(content_hash(stored), skill["content_hash"])

            library = LibraryContent(library_dir)
            by_url = library.get(skill["url"])
            by_hash = library.get_by_hash(skill["content_hash"])
            self.assertEqual(by_url, stored)
            self.assertEqual(by_hash, stored)
            self.assertGreater(len(by_hash), 20_000)

    def test_scan_skill_attaches_complete_library_content(self) -> None:
        body = _large_skill_md()
        skill = _skill_from_skill_md(
            "example",
            "repo",
            "skills/large-workbook-ops/SKILL.md",
            body,
            {"stars": 3, "updated_at": "2026-07-01T00:00:00Z"},
        )
        self.assertGreater(len(skill["_content"]), 20_000)
        # Full-content persistence applies after the source has been pinned
        # into an immutable package. Unpinned GitHub content is intentionally
        # quarantined by the evidence gate.
        skill["_package_manifest"] = {"package_hash": "a" * 64}

        asyncio.run(scan_skill(None, skill))

        self.assertEqual(skill["quality_status"], "active")
        self.assertEqual(skill["_library_content"], canonicalize_skill_content(body))
        self.assertEqual(skill["content_hash"], content_hash(body))


if __name__ == "__main__":
    unittest.main()
