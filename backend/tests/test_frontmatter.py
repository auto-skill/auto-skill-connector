from __future__ import annotations

import unittest

from scraper import _skill_from_skill_md, parse_frontmatter

FLAT_CONTENT = """---
name: flat-skill
description: A skill with only flat top-level frontmatter fields.
tags: [alpha, beta]
license: MIT
---

Body text.
"""

BLOCK_LIST_CONTENT = """---
name: block-list-skill
description: A skill whose tags are a block list, not a bracketed one.
tags:
  - gamma
  - delta
---

Body text.
"""

NESTED_METADATA_CONTENT = """---
name: nested-skill
description: >
  A skill whose tags/triggers/platforms live under a nested metadata map,
  the way anthropics' own SKILL.md convention does it.
metadata:
  author: someone
  version: "1.0"
  tags: [excel, xlsx]
  platforms: [claude-code, cursor]
  triggers:
    - create an Excel file
    - build a spreadsheet
---

Body text.
"""

OVERLAPPING_TAGS_CONTENT = """---
name: overlap-skill
description: Top-level and nested tags overlap and should be deduped case-insensitively.
tags: [Excel]
metadata:
  tags: [excel, xlsx]
---

Body text.
"""


class ParseFrontmatterTests(unittest.TestCase):
    def test_flat_top_level_fields_still_parse(self) -> None:
        fields, body = parse_frontmatter(FLAT_CONTENT)
        self.assertEqual(fields["name"], "flat-skill")
        self.assertEqual(fields["tags"], ["alpha", "beta"])
        self.assertEqual(fields["license"], "MIT")
        self.assertIn("Body text.", body)

    def test_block_style_list_still_parses(self) -> None:
        fields, _ = parse_frontmatter(BLOCK_LIST_CONTENT)
        self.assertEqual(fields["tags"], ["gamma", "delta"])

    def test_nested_metadata_map_is_captured(self) -> None:
        fields, _ = parse_frontmatter(NESTED_METADATA_CONTENT)
        metadata = fields["metadata"]
        self.assertEqual(metadata["author"], "someone")
        self.assertEqual(metadata["tags"], ["excel", "xlsx"])
        self.assertEqual(metadata["platforms"], ["claude-code", "cursor"])
        self.assertEqual(
            metadata["triggers"],
            ["create an Excel file", "build a spreadsheet"],
        )

    def test_no_frontmatter_returns_empty_fields(self) -> None:
        fields, body = parse_frontmatter("# Just a heading\n\nNo frontmatter here.")
        self.assertEqual(fields, {})
        self.assertIn("Just a heading", body)


class SkillFromSkillMdTagFoldingTests(unittest.TestCase):
    def test_nested_metadata_tags_and_triggers_fold_into_tags(self) -> None:
        skill = _skill_from_skill_md("owner", "repo", "nested-skill/SKILL.md", NESTED_METADATA_CONTENT, {})
        for expected in ("excel", "xlsx", "claude-code", "cursor", "create an Excel file", "build a spreadsheet"):
            self.assertIn(expected, skill["tags"])

    def test_overlapping_tags_are_deduped_case_insensitively(self) -> None:
        skill = _skill_from_skill_md("owner", "repo", "overlap-skill/SKILL.md", OVERLAPPING_TAGS_CONTENT, {})
        lowered = [t.casefold() for t in skill["tags"]]
        self.assertEqual(lowered.count("excel"), 1)
        self.assertIn("xlsx", lowered)

    def test_flat_only_skill_is_unaffected(self) -> None:
        skill = _skill_from_skill_md("owner", "repo", "flat-skill/SKILL.md", FLAT_CONTENT, {})
        self.assertEqual(skill["tags"], ["alpha", "beta"])


if __name__ == "__main__":
    unittest.main()
