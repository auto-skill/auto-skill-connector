from __future__ import annotations

import json
from pathlib import Path

import pytest

import auto_skill_personalize as personalize


def _isolate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTOSKILL_WEIGHTS_PATH", str(tmp_path / "weights.json"))
    monkeypatch.setenv("AUTOSKILL_ROUTE_HISTORY_PATH", str(tmp_path / "route_history.json"))
    monkeypatch.delenv("AUTOSKILL_PERSONALIZATION", raising=False)


def test_estimated_weight_is_neutral_until_min_observations(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    assert personalize.estimated_weight("some-skill") == 0.5

    personalize.record_route("route-1", "some-skill", tags=[])
    personalize.record_outcome("route-1", "used")
    # A single observation must not swing the estimate to a confident 1.0.
    assert personalize.estimated_weight("some-skill") == 0.5


def test_outcome_updates_increase_estimated_weight(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    for i in range(6):
        route_id = f"route-{i}"
        personalize.record_route(route_id, "some-skill", tags=["rust"])
        personalize.record_outcome(route_id, "used")

    weight = personalize.estimated_weight("some-skill")
    assert weight > 0.5

    summary = personalize.weights_summary()
    keys = {arm["key"] for arm in summary["arms"]}
    assert "skill:some-skill" in keys
    assert "tag:rust" in keys
    learned = next(arm for arm in summary["arms"] if arm["key"] == "skill:some-skill")
    assert learned["learned"] is True


def test_failure_outcomes_decrease_estimated_weight(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    for i in range(6):
        route_id = f"route-{i}"
        personalize.record_route(route_id, "flaky-skill", tags=[])
        personalize.record_outcome(route_id, "skipped")

    assert personalize.estimated_weight("flaky-skill") < 0.5


def test_record_outcome_is_noop_without_matching_route(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    personalize.record_outcome("unknown-route", "used")
    assert personalize.weights_summary()["arms"] == []


def test_record_outcome_never_stores_prompt_text(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    personalize.record_route("route-1", "some-skill", tags=[])
    raw = json.loads(personalize.get_route_history_path().read_text(encoding="utf-8"))
    dumped = json.dumps(raw)
    assert "prompt" not in dumped.lower()


def test_apply_personalization_reorders_without_changing_safety_fields(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    for i in range(6):
        route_id = f"route-{i}"
        personalize.record_route(route_id, "underdog", tags=[])
        personalize.record_outcome(route_id, "used")

    candidates = [
        {"name": "favorite", "routing_score": 0.9, "risk_score": 0, "routing_tier": "hint"},
        {"name": "underdog", "routing_score": 0.85, "risk_score": 0, "routing_tier": "hint"},
    ]
    reordered = personalize.apply_personalization(candidates)

    assert {c["name"] for c in reordered} == {"favorite", "underdog"}
    for candidate in reordered:
        assert candidate["risk_score"] == 0
        assert candidate["routing_tier"] == "hint"


def test_personalization_can_be_disabled(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("AUTOSKILL_PERSONALIZATION", "0")
    personalize.record_route("route-1", "some-skill", tags=[])
    personalize.record_outcome("route-1", "used")
    assert not personalize.get_weights_path().exists()

    candidates = [{"name": "a", "routing_score": 0.5}, {"name": "b", "routing_score": 0.4}]
    assert personalize.apply_personalization(candidates) == candidates


def test_reset_weights_clears_state(tmp_path, monkeypatch) -> None:
    _isolate(tmp_path, monkeypatch)
    personalize.record_route("route-1", "some-skill", tags=[])
    personalize.record_outcome("route-1", "used")
    assert personalize.get_weights_path().exists()

    personalize.reset_weights()
    assert not personalize.get_weights_path().exists()
    assert personalize.weights_summary()["arms"] == []
