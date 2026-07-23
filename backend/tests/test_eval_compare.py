from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eval_compare import _load, _metric_rows, _regressions


def _snapshot(
    *,
    hit1: float = 0.8,
    positive_pass: int = 10,
    latency_ms: int = 100,
    skill_find_ms: int = 80,
    injected_tokens: int = 700,
    response_tokens: int = 1000,
    task_bench_lift: float | None = None,
    task_bench_evidence: dict | None = None,
    task_bench_pairs: int = 20,
) -> dict:
    snapshot = {
        "engines": {"hybrid": {"hit1_rate": hit1, "hit3_rate": 0.9}},
        "tier_gate": {
            "positive_pass": positive_pass,
            "positive_total": 10,
            "negative_reject": 7,
            "negative_total": 7,
        },
        "content_quality": {"passed": 4, "total": 4},
        "route_benchmark": {
            "passed": 3,
            "total": 3,
            "cases": [
                {
                    "latency_ms": latency_ms,
                    "skill_find_ms": skill_find_ms,
                    "injected_tokens": injected_tokens,
                    "response_tokens": response_tokens,
                },
                {
                    "latency_ms": latency_ms,
                    "skill_find_ms": skill_find_ms,
                    "injected_tokens": injected_tokens,
                    "response_tokens": response_tokens,
                },
            ],
        },
    }
    if task_bench_lift is not None:
        baseline_pass = task_bench_pairs // 2
        with_skill_pass = baseline_pass + round(task_bench_lift * task_bench_pairs)
        snapshot["task_bench"] = {
            "cases": [
                {
                    "id": f"pair-{index}",
                    "baseline_pass": index < baseline_pass,
                    "with_skill_pass": index < with_skill_pass,
                }
                for index in range(task_bench_pairs)
            ],
            "baseline": {
                "pass": baseline_pass,
                "pass_rate": baseline_pass / task_bench_pairs,
                "total": task_bench_pairs,
            },
            "with_skill": {
                "pass": with_skill_pass,
                "pass_rate": with_skill_pass / task_bench_pairs,
                "total": task_bench_pairs,
            },
            "lift": task_bench_lift,
        }
        if task_bench_evidence is not None:
            snapshot["task_bench"]["evidence"] = task_bench_evidence
    return snapshot


def _paired_evidence(**overrides) -> dict:
    evidence = {
        "design": "paired",
        "predeclared_paired_count": 20,
        "observed_paired_count": 20,
        "alpha": 0.05,
        "lift_confidence_interval": {
            "method": "paired_bootstrap",
            "confidence": 0.95,
            "lower": 0.01,
            "upper": 0.19,
        },
    }
    evidence.update(overrides)
    return evidence


class EvalCompareTests(unittest.TestCase):
    def test_load_accepts_bom_prefixed_json(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "snapshot.json"
            path.write_text(json.dumps(_snapshot()), encoding="utf-8-sig")

            self.assertEqual(_load(path)["engines"]["hybrid"]["hit1_rate"], 0.8)

    def test_metric_rows_include_quality_and_cost_signals(self) -> None:
        rows = _metric_rows(_snapshot(), _snapshot(hit1=0.9, positive_pass=9, latency_ms=120))
        by_name = {row["name"]: row for row in rows}

        self.assertEqual(by_name["hybrid.hit1_rate"]["after"], 0.9)
        self.assertEqual(by_name["tier_gate.positive_pass_rate"]["after"], 0.9)
        self.assertEqual(by_name["route_benchmark.avg_latency_ms"]["after"], 120)
        self.assertEqual(by_name["route_benchmark.avg_skill_find_ms"]["after"], 80)
        self.assertEqual(by_name["route_benchmark.avg_injected_tokens"]["after"], 700)
        self.assertFalse(by_name["route_benchmark.avg_latency_ms"]["higher_is_better"])

    def test_regressions_catch_relevance_drop(self) -> None:
        rows = _metric_rows(_snapshot(hit1=0.8), _snapshot(hit1=0.7))

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("hybrid.hit1_rate regressed" in failure for failure in failures))

    def test_regressions_honor_slowdown_tolerance(self) -> None:
        within_tolerance = _metric_rows(_snapshot(latency_ms=100), _snapshot(latency_ms=120))
        over_tolerance = _metric_rows(_snapshot(latency_ms=100), _snapshot(latency_ms=140))

        self.assertFalse(_regressions(within_tolerance, slowdown_tolerance=0.25))
        self.assertTrue(any("avg_latency_ms increased" in failure for failure in _regressions(over_tolerance, 0.25)))

    def test_nonpositive_blinded_task_lift_is_a_quality_failure(self) -> None:
        rows = _metric_rows(
            _snapshot(task_bench_lift=-0.075),
            _snapshot(task_bench_lift=-0.075),
        )

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("task_bench.lift must be positive" in failure for failure in failures))

    def test_positive_task_lift_without_paired_evidence_is_a_quality_failure(self) -> None:
        rows = _metric_rows(
            _snapshot(task_bench_lift=0.05),
            _snapshot(task_bench_lift=0.10),
        )

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("requires a paired evidence block" in failure for failure in failures))

    def test_underpowered_task_bench_is_rejected_even_with_positive_ci(self) -> None:
        rows = _metric_rows(
            _snapshot(task_bench_lift=0.05),
            _snapshot(
                task_bench_lift=0.10,
                task_bench_pairs=6,
                task_bench_evidence=_paired_evidence(
                    predeclared_paired_count=6,
                    observed_paired_count=6,
                ),
            ),
        )

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("predeclared_paired_count" in failure for failure in failures))

    def test_positive_ci_without_recomputed_exact_test_is_rejected(self) -> None:
        rows = _metric_rows(
            _snapshot(task_bench_lift=0.05),
            _snapshot(task_bench_lift=0.10, task_bench_evidence=_paired_evidence()),
        )

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("exact McNemar" in failure for failure in failures))

    def test_significant_exact_mcnemar_is_accepted_without_ci(self) -> None:
        evidence = _paired_evidence(
            lift_confidence_interval=None,
            hypothesis_test={
                "method": "mcnemar_exact",
                "alternative": "two-sided",
                "p_value": 0.03125,
            },
        )
        rows = _metric_rows(
            _snapshot(task_bench_lift=0.05),
            _snapshot(task_bench_lift=0.30, task_bench_evidence=evidence),
        )

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertFalse(any(failure.startswith("task_bench") for failure in failures))

    def test_unsupported_or_nonsignificant_test_does_not_substitute_for_ci(self) -> None:
        evidence = _paired_evidence(
            lift_confidence_interval=None,
            hypothesis_test={
                "method": "independent_t_test",
                "alternative": "greater",
                "p_value": 0.001,
            },
        )
        rows = _metric_rows(
            _snapshot(task_bench_lift=0.05),
            _snapshot(task_bench_lift=0.10, task_bench_evidence=evidence),
        )

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("exact McNemar" in failure for failure in failures))

    def test_reported_pair_count_must_match_cases_and_arm_totals(self) -> None:
        evidence = _paired_evidence(observed_paired_count=21)
        rows = _metric_rows(
            _snapshot(task_bench_lift=0.05),
            _snapshot(task_bench_lift=0.10, task_bench_evidence=evidence),
        )

        failures = _regressions(rows, slowdown_tolerance=0.25)

        self.assertTrue(any("case count must equal" in failure for failure in failures))


if __name__ == "__main__":
    unittest.main()
