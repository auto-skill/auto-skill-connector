"""Compare two privacy-safe route_profile.py snapshots.

Route behavior changes are always reported. With --fail-on-regression they
block a performance rollout, as do metrics exceeding the slowdown tolerance.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


BEHAVIOR_KEYS = ("status_code", "tier", "selected_skill")
SUMMARY_KEYS = ("p50", "p95", "p99", "max")
DEFAULT_MINIMUM_SLOWDOWN_MS = 20


def _load(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _row_key(row: dict[str, Any]) -> tuple[str, int]:
    return str(row.get("case_id") or ""), int(row.get("repeat") or 0)


def _behavior_changes(before: dict[str, Any], after: dict[str, Any]) -> list[str]:
    before_rows = {_row_key(row): row for row in before.get("rows", []) if isinstance(row, dict)}
    after_rows = {_row_key(row): row for row in after.get("rows", []) if isinstance(row, dict)}
    changes: list[str] = []
    for key in sorted(set(before_rows) | set(after_rows)):
        before_row = before_rows.get(key)
        after_row = after_rows.get(key)
        if before_row is None or after_row is None:
            changes.append(f"{key[0]} repeat={key[1]} missing from {'after' if after_row is None else 'before'} snapshot")
            continue
        for field in BEHAVIOR_KEYS:
            if before_row.get(field) != after_row.get(field):
                changes.append(
                    f"{key[0]} repeat={key[1]} {field}: "
                    f"{before_row.get(field)!r} -> {after_row.get(field)!r}"
                )
    return changes


def _metric_rows(before: dict[str, Any], after: dict[str, Any]) -> list[dict[str, Any]]:
    before_metrics = before.get("metrics") or {}
    after_metrics = after.get("metrics") or {}
    rows: list[dict[str, Any]] = []
    for metric in sorted(set(before_metrics) & set(after_metrics)):
        before_summary = before_metrics[metric]
        after_summary = after_metrics[metric]
        if not isinstance(before_summary, dict) or not isinstance(after_summary, dict):
            continue
        for summary_key in SUMMARY_KEYS:
            before_value = before_summary.get(summary_key)
            after_value = after_summary.get(summary_key)
            if isinstance(before_value, (int, float)) and isinstance(after_value, (int, float)):
                rows.append(
                    {
                        "name": f"{metric}.{summary_key}",
                        "before": int(before_value),
                        "after": int(after_value),
                    }
                )
    return rows


def _regressions(
    changes: list[str],
    rows: list[dict[str, Any]],
    slowdown_tolerance: float,
    minimum_slowdown_ms: int = DEFAULT_MINIMUM_SLOWDOWN_MS,
) -> list[str]:
    failures = [f"route behavior changed: {change}" for change in changes]
    for row in rows:
        before = int(row["before"])
        after = int(row["after"])
        if (
            before > 0
            and after - before >= minimum_slowdown_ms
            and after > before * (1 + slowdown_tolerance)
        ):
            failures.append(f"{row['name']} increased from {before} to {after}")
    return failures


def _print_rows(rows: list[dict[str, Any]]) -> None:
    print(f"{'metric':<42} {'before':>10} {'after':>10} {'delta':>10}")
    for row in rows:
        before = int(row["before"])
        after = int(row["after"])
        print(f"{row['name']:<42} {before:>10} {after:>10} {after - before:>+10}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Compare sanitized Auto-Skill route profiles.")
    parser.add_argument("before", type=Path)
    parser.add_argument("after", type=Path)
    parser.add_argument("--fail-on-regression", action="store_true")
    parser.add_argument("--slowdown-tolerance", type=float, default=0.25)
    parser.add_argument("--minimum-slowdown-ms", type=int, default=DEFAULT_MINIMUM_SLOWDOWN_MS)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.slowdown_tolerance < 0:
        raise SystemExit("--slowdown-tolerance must be non-negative")
    if args.minimum_slowdown_ms < 0:
        raise SystemExit("--minimum-slowdown-ms must be non-negative")
    before = _load(args.before)
    after = _load(args.after)
    changes = _behavior_changes(before, after)
    rows = _metric_rows(before, after)
    _print_rows(rows)
    if changes:
        print("\nroute behavior changes:")
        for change in changes:
            print(f"  - {change}")
    else:
        print("\nroute behavior: unchanged")

    failures = _regressions(changes, rows, args.slowdown_tolerance, args.minimum_slowdown_ms)
    if args.fail_on_regression and failures:
        print("\nregressions:", file=sys.stderr)
        for failure in failures:
            print(f"  - {failure}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
