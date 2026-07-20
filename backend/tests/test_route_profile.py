from route_profile import _metric_summary, _sanitize_route_result


def test_route_profile_omits_task_content_and_unapproved_metrics() -> None:
    payload = {
        "tier": "full",
        "content": "secret skill text",
        "warnings": ["task-specific detail"],
        "skill": {"name": "safe-skill", "description": "not retained"},
        "score_debug": {
            "metrics": {
                "latency_ms": 101,
                "primary_retrieval_ms": 80,
                "unexpected": 999,
            }
        },
    }

    result = _sanitize_route_result("case-1", 1, 200, payload, 150)

    assert result == {
        "case_id": "case-1",
        "repeat": 1,
        "status_code": 200,
        "tier": "full",
        "selected_skill": "safe-skill",
        "client_wall_ms": 150,
        "server_metrics": {"latency_ms": 101, "primary_retrieval_ms": 80},
    }


def test_route_profile_summarizes_percentiles() -> None:
    rows = [
        {"client_wall_ms": 100, "server_metrics": {"latency_ms": 40}},
        {"client_wall_ms": 200, "server_metrics": {"latency_ms": 80}},
        {"client_wall_ms": 300, "server_metrics": {"latency_ms": 120}},
    ]

    summary = _metric_summary(rows)

    assert summary["client_wall_ms"] == {"count": 3, "p50": 200, "p95": 300, "p99": 300, "max": 300}
    assert summary["latency_ms"] == {"count": 3, "p50": 80, "p95": 120, "p99": 120, "max": 120}
