from __future__ import annotations

import asyncio
import json

from bench.skills_sh_live_eval import _read_cases, main


def test_live_eval_skips_without_oidc_token(tmp_path, monkeypatch, capsys) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"query": "create a spreadsheet report"}) + "\n", encoding="utf-8")
    monkeypatch.delenv("SKILLS_SH_OIDC_TOKEN", raising=False)
    monkeypatch.delenv("VERCEL_OIDC_TOKEN", raising=False)
    args = type("Args", (), {"cases": cases, "limit": 5, "output": None})()
    assert asyncio.run(main(args)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["status"] == "skipped"


def test_live_eval_reads_expected_ids(tmp_path) -> None:
    cases = tmp_path / "cases.jsonl"
    cases.write_text(json.dumps({"query": "make a report", "expected_ids": ["acme/reporting"]}) + "\n", encoding="utf-8")
    assert _read_cases(cases)[0]["expected_ids"] == ["acme/reporting"]
