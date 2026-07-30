"""Phase 2B: embed-text sampling reflects full-skill substance within the model window."""

from __future__ import annotations

import asyncio
import json
import unittest

from embeddings import (
    MAX_CONTENT_CHARS,
    MAX_EMBED_CHARS,
    build_embed_text,
    generate_capability_summary,
    sample_content_for_embed,
)


class _FakeResponse:
    def __init__(self, status_code: int, json_data=None):
        self.status_code = status_code
        self._json = json_data

    def json(self):
        return self._json


class _FakeClient:
    def __init__(self, response: "_FakeResponse"):
        self._response = response

    async def post(self, url: str, **kwargs):
        return self._response


def _multi_section_skill() -> str:
    return """---
name: workbook-ops
description: Spreadsheet workflow covering discovery through audit.
---

Opening preamble about discovering source ranges and sheet layout.

## Install notes

Ignore these badges and install fluff that often front-loads SKILL.md files.
pip install something-irrelevant

## When to use

Use when the user asks for formula-backed spreadsheet reports with verification.

## Workflow

Inspect source data, create the workbook, add formulas, and verify calculations.

## Reference dump

""" + ("Padding reference material that should not monopolize the embed window. " * 80) + """

## Verification

Check formulas, preserve leading zeroes, and include an audit note.
TAIL_MARKER_UNIQUE_END
"""


class EmbedTextSamplingTests(unittest.TestCase):
    def test_sample_covers_head_priority_and_tail(self) -> None:
        content = _multi_section_skill()
        sampled = sample_content_for_embed(content, budget=700)
        self.assertLessEqual(len(sampled), 700)
        self.assertIn("Opening preamble", sampled)
        self.assertIn("formula-backed spreadsheet", sampled)
        self.assertIn("Inspect source data", sampled)
        self.assertIn("TAIL_MARKER_UNIQUE_END", sampled)
        # Install fluff should not crowd out operational sections.
        self.assertTrue(
            "formula-backed spreadsheet" in sampled or "Inspect source data" in sampled
        )

    def test_sample_is_not_blind_head_clip(self) -> None:
        content = _multi_section_skill()
        head_only = content[:700]
        sampled = sample_content_for_embed(content, budget=700)
        self.assertNotEqual(sampled.replace(" ", ""), head_only.replace(" ", "")[: len(sampled)])
        self.assertIn("Verification", sampled)
        self.assertIn("TAIL_MARKER_UNIQUE_END", sampled)

    def test_build_embed_text_stays_within_model_window(self) -> None:
        skill = {
            "name": "workbook-ops",
            "description": "Spreadsheet workflow covering discovery through audit.",
            "tags": ["excel", "spreadsheet"],
            "capability_summary": "Builds formula-backed workbook reports with verification.",
        }
        text = build_embed_text(skill, _multi_section_skill())
        self.assertLessEqual(len(text), MAX_EMBED_CHARS)
        self.assertIn("workbook-ops", text)
        self.assertIn("TAIL_MARKER_UNIQUE_END", text)
        self.assertLessEqual(MAX_CONTENT_CHARS, MAX_EMBED_CHARS)

    def test_build_embed_text_includes_triggers(self) -> None:
        skill = {
            "name": "workbook-ops",
            "triggers": ["merging two spreadsheets", "auditing formula totals"],
        }
        text = build_embed_text(skill, "")
        self.assertIn("merging two spreadsheets", text)
        self.assertIn("auditing formula totals", text)


class GenerateCapabilitySummaryTests(unittest.TestCase):
    def test_returns_summary_and_triggers_on_success(self) -> None:
        client = _FakeClient(_FakeResponse(200, {
            "message": {"content": json.dumps({
                "summary": "Does the thing.",
                "triggers": ["doing the thing", "doing the thing", "doing another thing"],
            })}
        }))

        result = asyncio.run(generate_capability_summary(client, "thing-tool", "A tool.", "content"))

        self.assertEqual(result["summary"], "Does the thing.")
        # Exact duplicate trigger deduplicated, distinct one kept.
        self.assertEqual(result["triggers"], ["doing the thing", "doing another thing"])

    def test_empty_input_short_circuits_without_a_call(self) -> None:
        class ExplodingClient:
            async def post(self, *a, **k):
                raise AssertionError("should not be called for empty input")

        result = asyncio.run(generate_capability_summary(ExplodingClient(), "", "", ""))

        self.assertEqual(result, {"summary": "", "triggers": []})

    def test_failure_returns_empty_shape_not_a_crash(self) -> None:
        client = _FakeClient(_FakeResponse(500))

        result = asyncio.run(generate_capability_summary(client, "name", "description", "content"))

        self.assertEqual(result, {"summary": "", "triggers": []})


if __name__ == "__main__":
    unittest.main()
