from __future__ import annotations

import json
from pathlib import Path

from bench.on_demand_route_replay import DEFAULT_FIXTURE, run


HELDOUT_FIXTURE = Path(__file__).resolve().parents[1] / "bench" / "on_demand_resolver_heldout.json"


def test_offline_replay_reports_both_arms_and_safety_metrics() -> None:
    report = run(DEFAULT_FIXTURE, repeats=1)

    assert report["network"] == "disabled"
    assert report["control"]["tasks"] == 8
    assert report["experiment"]["tasks"] == 8
    assert "p50" in report["control"]["latency_ms"]
    assert "p95" in report["experiment"]["latency_ms"]
    assert report["experiment"]["full_unsafe_route_count"] == 0
    assert report["experiment"]["irrelevant_or_unsafe_route_rate"] == 0.0
    assert report["experiment"]["incomplete_route_rate"] == 0.0
    assert report["experiment"]["db_miss_recovery"]["eligible"] == 1
    assert report["experiment"]["db_miss_recovery"]["recovered"] == 1
    assert report["control"]["topk_success_rate"] >= report["control"]["top1_success_rate"]
    assert report["experiment"]["topk_success_rate"] >= report["experiment"]["top1_success_rate"]


def test_fixture_is_in_repo_and_network_free() -> None:
    assert DEFAULT_FIXTURE == Path(__file__).resolve().parents[1] / "bench" / "on_demand_resolver_fixtures.json"


def test_heldout_fixture_is_separate_and_covers_real_db_misses() -> None:
    payload = json.loads(HELDOUT_FIXTURE.read_text(encoding="utf-8"))
    tasks = payload["tasks"]

    assert payload["split"] == "heldout"
    assert len(tasks) >= 16
    assert any(not task["control"] and task["expected"] for task in tasks)
    assert any(not task["control"] and not task["expected"] for task in tasks)
