import hashlib
import json
from pathlib import Path

from backend.bench.harbor_paired_ab import load_definition


BENCH_DIR = Path(__file__).parents[1] / "bench"
FULL = BENCH_DIR / "harbor_oracle_ab.json"
SMOKE = BENCH_DIR / "harbor_oracle_smoke.json"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_full_oracle_definition_freezes_24_distinct_task_pairs():
    definition = load_definition(FULL)

    assert definition["phase"] == "judge-oracle-all"
    assert definition["arms"] == ["control", "oracle_skill"]
    assert definition["replicates"] == 1
    assert definition["required_task_count"] == len(definition["tasks"]) == 24
    assert len({task["name"] for task in definition["tasks"]}) == 24
    assert len({task["source_content_hash"] for task in definition["tasks"]}) == 23
    assert definition["source_inputs"] == {
        "benchmark_file": "tbench-2.1-full-summary.json",
        "benchmark_sha256": "8c2f30a4fecc9309cf75d98ba83628c5c2301fb884c889dcd1ac1d86c094158c",
        "relevance_file": "route-relevance-union06-provenance-20260721.json",
        "relevance_sha256": "595930ec2adbff2fb6a925e3b980583a0a7972fa6fd6b58a037cf9451abf6a8e",
    }
    assert definition["agent"] == {
        "name": "codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "reasoning_summary": "none",
        "web_search": "disabled",
        "version": "0.145.0",
    }
    for task in definition["tasks"]:
        instruction = BENCH_DIR / task["instruction"]
        assert instruction.is_file()
        assert _sha256(instruction) == task["instruction_sha256"]
        assert task["selection_label"] == "E"
        assert 1 <= task["selection_rank"] <= 5
        assert len(task["served_body_sha256"]) == 64


def test_smoke_definition_is_exactly_the_rank_one_subset():
    full = json.loads(FULL.read_text(encoding="utf-8"))
    smoke = load_definition(SMOKE)
    expected = {
        task["name"] for task in full["tasks"] if task["selection_rank"] == 1
    }

    assert smoke["phase"] == "judge-oracle-smoke"
    assert smoke["replicates"] == 1
    assert smoke["required_task_count"] == len(smoke["tasks"]) == 8
    assert {task["name"] for task in smoke["tasks"]} == expected
    assert all(task["selection_rank"] == 1 for task in smoke["tasks"])
