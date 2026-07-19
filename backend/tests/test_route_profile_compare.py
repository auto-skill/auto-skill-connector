from route_profile_compare import _behavior_changes, _metric_rows, _regressions


def _profile(*, skill: str = "spreadsheet", p95: int = 100) -> dict:
    return {
        "rows": [
            {
                "case_id": "case-1",
                "repeat": 1,
                "status_code": 200,
                "tier": "full",
                "selected_skill": skill,
            }
        ],
        "metrics": {
            "latency_ms": {"p50": 80, "p95": p95, "p99": p95, "max": p95},
        },
    }


def test_route_profile_compare_allows_unchanged_behavior_and_improvement() -> None:
    before = _profile(p95=100)
    after = _profile(p95=80)

    changes = _behavior_changes(before, after)
    rows = _metric_rows(before, after)

    assert changes == []
    assert not _regressions(changes, rows, 0.25)


def test_route_profile_compare_blocks_behavior_drift_and_large_slowdown() -> None:
    before = _profile(p95=100)
    after = _profile(skill="different-skill", p95=130)

    changes = _behavior_changes(before, after)
    failures = _regressions(changes, _metric_rows(before, after), 0.25)

    assert changes == ["case-1 repeat=1 selected_skill: 'spreadsheet' -> 'different-skill'"]
    assert any("route behavior changed" in failure for failure in failures)
    assert any("latency_ms.p95 increased from 100 to 130" in failure for failure in failures)
