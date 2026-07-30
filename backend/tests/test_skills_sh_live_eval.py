from __future__ import annotations

import asyncio
import json

from skills_sh_catalog import SkillsShCatalogError
import bench.skills_sh_live_eval as live_eval
from bench.skills_sh_live_eval import _read_cases, _rrf_union, main


def test_live_eval_uses_public_discovery_without_oidc_token(tmp_path, monkeypatch, capsys) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"query": "create a spreadsheet report"}) + "\n", encoding="utf-8")
    class PublicCatalog:
        configured = False

        async def retrieve(self, query, limit):
            return [{"id": "acme/reporting", "audit_status": "unknown"}]

    monkeypatch.setattr(live_eval, "SkillsShCatalog", PublicCatalog)
    args = type("Args", (), {"cases": cases, "limit": 5, "output": None})()
    assert asyncio.run(main(args)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "insufficient_labels"
    assert result["access_mode"] == "public_search_only"


def test_live_eval_skips_when_public_discovery_is_unavailable(tmp_path, monkeypatch, capsys) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"query": "create a spreadsheet report"}) + "\n", encoding="utf-8")

    class BrokenCatalog:
        configured = False

        async def retrieve(self, query, limit):
            raise SkillsShCatalogError("public search unavailable")

    monkeypatch.setattr(live_eval, "SkillsShCatalog", BrokenCatalog)
    args = type("Args", (), {"cases": cases, "limit": 5, "output": None})()
    assert asyncio.run(main(args)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "skipped"


def test_live_eval_reads_expected_ids(tmp_path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(
        json.dumps({"query": "make a report", "expected_ids": ["acme/reporting"], "label_status": "verified"})
        + "\n",
        encoding="utf-8",
    )
    assert _read_cases(cases)[0]["expected_ids"] == ["acme/reporting"]
    assert _read_cases(cases)[0]["label_status"] == "verified"


def test_rrf_fusion_can_promote_compiled_lane() -> None:
    original = [{"id": "original-top"}, {"id": "shared"}]
    compiled = [{"id": "shared"}, {"id": "compiled-top"}]
    fused = _rrf_union(original, compiled, 3)
    assert [row["id"] for row in fused] == ["shared", "original-top", "compiled-top"]
    assert fused[0]["retrieval_queries"] == ["original", "compiled"]
