"""Compare two eval_search.py JSON snapshots.

Use this after routing or quality-gate changes to see whether relevance,
route correctness, latency, or token churn moved in the right direction.

Run:
  python eval_compare.py eval-results/before.json eval-results/after.json
  python eval_compare.py --fail-on-regression eval-results/before.json eval-results/after.json

Task-benchmark acceptance is deliberately stricter than comparing point
estimates.  A positive ``task_bench.lift`` is accepted only when the after
snapshot contains a paired evidence block like::

  "evidence": {
    "design": "paired",
    "predeclared_paired_count": 30,
    "observed_paired_count": 30,
    "alpha": 0.05,
    "lift_confidence_interval": {
      "method": "paired_bootstrap",
      "confidence": 0.95,
      "lower": 0.02,
      "upper": 0.18
    }
  }

Binary pass/fail benchmarks must also provide ``hypothesis_test`` with
``method: mcnemar_exact``, ``alternative`` equal to ``greater`` or
``two-sided``, and a p-value no larger than alpha. At least 20 predeclared and
completed pairs are required, and totals/case rows must agree with the reported
observed count and outcomes; exact p-values are recomputed from those outcomes.
A reported bootstrap interval is useful supplemental evidence, but cannot
substitute for the recomputed exact test. Legacy task-benchmark snapshots
without this evidence remain printable, but fail ``--fail-on-regression``.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path
from statistics import mean
from typing import Any


MIN_PAIRED_SAMPLES = 20
_PAIRED_CI_METHODS = {"paired_bootstrap", "paired_bootstrap_percentile"}
_EXACT_PAIRED_TESTS = {"mcnemar_exact", "exact_mcnemar"}


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _rate(passed: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return passed / total


def _percent(value: float) -> str:
    return f"{value * 100:.1f}%"


def _delta(after: float, before: float) -> str:
    diff = after - before
    sign = "+" if diff >= 0 else ""
    return f"{sign}{diff:.4f}"


def _case_values(snapshot: dict[str, Any], key: str) -> list[float]:
    cases = ((snapshot.get("route_benchmark") or {}).get("cases") or [])
    values: list[float] = []
    for case in cases:
        value = case.get(key)
        if isinstance(value, (int, float)):
            values.append(float(value))
    return values


def _mean_case_value(snapshot: dict[str, Any], key: str) -> float:
    values = _case_values(snapshot, key)
    return mean(values) if values else 0.0


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _task_bench_evidence_failure(task_bench: dict[str, Any]) -> str | None:
    """Return why a positive task-bench lift lacks decision-grade evidence."""
    evidence = task_bench.get("evidence")
    if not isinstance(evidence, dict):
        return "task_bench positive lift requires a paired evidence block"
    if evidence.get("design") != "paired":
        return "task_bench evidence.design must be 'paired'"

    planned = evidence.get("predeclared_paired_count")
    observed = evidence.get("observed_paired_count")
    if not isinstance(planned, int) or isinstance(planned, bool) or planned < MIN_PAIRED_SAMPLES:
        return (
            "task_bench evidence.predeclared_paired_count must be an integer "
            f">= {MIN_PAIRED_SAMPLES}"
        )
    if not isinstance(observed, int) or isinstance(observed, bool) or observed < planned:
        return "task_bench evidence.observed_paired_count must meet the predeclared count"

    cases = task_bench.get("cases")
    if not isinstance(cases, list) or len(cases) != observed:
        return "task_bench case count must equal evidence.observed_paired_count"
    if any(
        not isinstance(case, dict)
        or not isinstance(case.get("baseline_pass"), bool)
        or not isinstance(case.get("with_skill_pass"), bool)
        for case in cases
    ):
        return "task_bench paired cases must contain boolean baseline_pass and with_skill_pass"

    baseline_pass = sum(case["baseline_pass"] for case in cases)
    with_skill_pass = sum(case["with_skill_pass"] for case in cases)
    for arm, passed in (("baseline", baseline_pass), ("with_skill", with_skill_pass)):
        arm_summary = task_bench.get(arm) or {}
        total = arm_summary.get("total")
        if total != observed:
            return f"task_bench.{arm}.total must equal evidence.observed_paired_count"
        if arm_summary.get("pass") != passed:
            return f"task_bench.{arm}.pass must agree with paired case outcomes"
        pass_rate = arm_summary.get("pass_rate")
        if not _is_number(pass_rate) or not math.isclose(
            float(pass_rate), passed / observed, abs_tol=0.0001
        ):
            return f"task_bench.{arm}.pass_rate must agree with paired case outcomes"

    observed_lift = (with_skill_pass - baseline_pass) / observed
    reported_lift = task_bench.get("lift")
    if not _is_number(reported_lift) or not math.isclose(
        float(reported_lift), observed_lift, abs_tol=0.0001
    ):
        return "task_bench.lift must agree with paired case outcomes"

    alpha = evidence.get("alpha")
    if not _is_number(alpha) or not 0 < float(alpha) <= 0.05:
        return "task_bench evidence.alpha must be numeric and in (0, 0.05]"
    alpha = float(alpha)

    ci = evidence.get("lift_confidence_interval")
    ci_supported = False
    if isinstance(ci, dict):
        method = ci.get("method")
        confidence = ci.get("confidence")
        lower = ci.get("lower")
        upper = ci.get("upper")
        ci_supported = (
            method in _PAIRED_CI_METHODS
            and _is_number(confidence)
            and float(confidence) >= 1 - alpha
            and _is_number(lower)
            and _is_number(upper)
            and 0 < float(lower) <= float(upper)
        )
    if ci is not None and not ci_supported:
        return (
            "task_bench evidence.lift_confidence_interval must be a supported "
            "paired interval with lower > 0 when provided"
        )

    test = evidence.get("hypothesis_test")
    exact_test_supported = False
    if isinstance(test, dict):
        p_value = test.get("p_value")
        wins = sum(not case["baseline_pass"] and case["with_skill_pass"] for case in cases)
        losses = sum(case["baseline_pass"] and not case["with_skill_pass"] for case in cases)
        discordant = wins + losses
        if test.get("alternative") == "greater":
            exact_p_value = (
                sum(math.comb(discordant, k) for k in range(wins, discordant + 1))
                / (2**discordant)
            )
        else:
            exact_p_value = min(
                1.0,
                2
                * sum(math.comb(discordant, k) for k in range(0, min(wins, losses) + 1))
                / (2**discordant),
            )
        exact_test_supported = (
            test.get("method") in _EXACT_PAIRED_TESTS
            and test.get("alternative") in {"greater", "two-sided"}
            and _is_number(p_value)
            and 0 <= float(p_value) <= alpha
            and math.isclose(float(p_value), exact_p_value, abs_tol=1e-12)
        )

    if not exact_test_supported:
        return (
            "task_bench positive lift requires a significant exact McNemar test "
            "recomputed from the paired case outcomes"
        )
    return None


def _metric_rows(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for engine in sorted(set((before.get("engines") or {}) | (after.get("engines") or {}))):
        before_engine = (before.get("engines") or {}).get(engine) or {}
        after_engine = (after.get("engines") or {}).get(engine) or {}
        for metric in ("hit1_rate", "hit3_rate"):
            rows.append(
                {
                    "name": f"{engine}.{metric}",
                    "before": float(before_engine.get(metric) or 0.0),
                    "after": float(after_engine.get(metric) or 0.0),
                    "higher_is_better": True,
                    "kind": "rate",
                }
            )

    before_gate = before.get("tier_gate") or {}
    after_gate = after.get("tier_gate") or {}
    rows.extend(
        [
            {
                "name": "tier_gate.positive_pass_rate",
                "before": _rate(int(before_gate.get("positive_pass") or 0), int(before_gate.get("positive_total") or 0)),
                "after": _rate(int(after_gate.get("positive_pass") or 0), int(after_gate.get("positive_total") or 0)),
                "higher_is_better": True,
                "kind": "rate",
            },
            {
                "name": "tier_gate.negative_reject_rate",
                "before": _rate(int(before_gate.get("negative_reject") or 0), int(before_gate.get("negative_total") or 0)),
                "after": _rate(int(after_gate.get("negative_reject") or 0), int(after_gate.get("negative_total") or 0)),
                "higher_is_better": True,
                "kind": "rate",
            },
        ]
    )

    before_content = before.get("content_quality") or {}
    after_content = after.get("content_quality") or {}
    rows.append(
        {
            "name": "content_quality.pass_rate",
            "before": _rate(int(before_content.get("passed") or 0), int(before_content.get("total") or 0)),
            "after": _rate(int(after_content.get("passed") or 0), int(after_content.get("total") or 0)),
            "higher_is_better": True,
            "kind": "rate",
        }
    )

    before_route = before.get("route_benchmark") or {}
    after_route = after.get("route_benchmark") or {}
    rows.extend(
        [
            {
                "name": "route_benchmark.pass_rate",
                "before": _rate(int(before_route.get("passed") or 0), int(before_route.get("total") or 0)),
                "after": _rate(int(after_route.get("passed") or 0), int(after_route.get("total") or 0)),
                "higher_is_better": True,
                "kind": "rate",
            },
            {
                "name": "route_benchmark.avg_latency_ms",
                "before": _mean_case_value(before, "latency_ms"),
                "after": _mean_case_value(after, "latency_ms"),
                "higher_is_better": False,
                "kind": "number",
            },
            {
                "name": "route_benchmark.avg_skill_find_ms",
                "before": _mean_case_value(before, "skill_find_ms"),
                "after": _mean_case_value(after, "skill_find_ms"),
                "higher_is_better": False,
                "kind": "number",
            },
            {
                "name": "route_benchmark.avg_injected_tokens",
                "before": _mean_case_value(before, "injected_tokens"),
                "after": _mean_case_value(after, "injected_tokens"),
                "higher_is_better": False,
                "kind": "number",
            },
            {
                "name": "route_benchmark.avg_response_tokens",
                "before": _mean_case_value(before, "response_tokens"),
                "after": _mean_case_value(after, "response_tokens"),
                "higher_is_better": False,
                "kind": "number",
            },
        ]
    )

    before_bench = before.get("task_bench") or {}
    after_bench = after.get("task_bench") or {}
    if before_bench.get("with_skill") or after_bench.get("with_skill"):
        rows.extend(
            [
                {
                    "name": "task_bench.baseline_pass_rate",
                    "before": float((before_bench.get("baseline") or {}).get("pass_rate") or 0.0),
                    "after": float((after_bench.get("baseline") or {}).get("pass_rate") or 0.0),
                    "higher_is_better": True,
                    "kind": "rate",
                },
                {
                    "name": "task_bench.with_skill_pass_rate",
                    "before": float((before_bench.get("with_skill") or {}).get("pass_rate") or 0.0),
                    "after": float((after_bench.get("with_skill") or {}).get("pass_rate") or 0.0),
                    "higher_is_better": True,
                    "kind": "rate",
                },
                {
                    "name": "task_bench.lift",
                    "before": float(before_bench.get("lift") or 0.0),
                    "after": float(after_bench.get("lift") or 0.0),
                    "higher_is_better": True,
                    "kind": "rate",
                    "after_task_bench": after_bench,
                },
            ]
        )
    return rows


def _format_value(value: float, kind: str) -> str:
    if kind == "rate":
        return _percent(value)
    return f"{value:.1f}"


def _print_rows(rows: list[dict[str, Any]]) -> None:
    print(f"{'metric':<42} {'before':>10} {'after':>10} {'delta':>10}")
    for row in rows:
        before = float(row["before"])
        after = float(row["after"])
        print(
            f"{row['name']:<42} "
            f"{_format_value(before, row['kind']):>10} "
            f"{_format_value(after, row['kind']):>10} "
            f"{_delta(after, before):>10}"
        )


def _regressions(rows: list[dict[str, Any]], slowdown_tolerance: float) -> list[str]:
    failures: list[str] = []
    for row in rows:
        before = float(row["before"])
        after = float(row["after"])
        if row["name"] == "task_bench.lift" and after <= 0:
            failures.append(
                f"task_bench.lift must be positive; observed {_format_value(after, row['kind'])}"
            )
            continue
        if row["name"] == "task_bench.lift":
            evidence_failure = _task_bench_evidence_failure(row.get("after_task_bench") or {})
            if evidence_failure:
                failures.append(evidence_failure)
                continue
        if row["higher_is_better"]:
            if after < before:
                failures.append(f"{row['name']} regressed from {_format_value(before, row['kind'])} to {_format_value(after, row['kind'])}")
        elif before > 0 and after > before * (1 + slowdown_tolerance):
            failures.append(
                f"{row['name']} increased from {_format_value(before, row['kind'])} "
                f"to {_format_value(after, row['kind'])}"
            )
    return failures


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare two eval_search.py JSON snapshots.")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument(
        "--slowdown-tolerance",
        type=float,
        default=0.25,
        help="allowed fractional increase for latency/token metrics before they count as regressions",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    before = _load(args.before)
    after = _load(args.after)
    print(f"before: {args.before} ({before.get('created_at', 'unknown')})")
    print(f"after:  {args.after} ({after.get('created_at', 'unknown')})")
    print()

    rows = _metric_rows(before, after)
    _print_rows(rows)

    failures = _regressions(rows, max(0.0, args.slowdown_tolerance))
    if failures:
        print("\nregressions:")
        for failure in failures:
            print(f"- {failure}")
    else:
        print("\nno regressions detected")

    if failures and args.fail_on_regression:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
