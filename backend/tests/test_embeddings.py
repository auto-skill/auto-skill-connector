"""Phase 2B: embed-text sampling reflects full-skill substance within the model window."""

from __future__ import annotations

import unittest

from embeddings import (
    MAX_CONTENT_CHARS,
    MAX_EMBED_CHARS,
    build_embed_text,
    sample_content_for_embed,
)


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


if __name__ == "__main__":
    unittest.main()
