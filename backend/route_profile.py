"""Capture a privacy-safe, repeatable /route latency snapshot.

The report records only public benchmark case IDs, route tier, selected skill
name, wall time, and numeric server metrics. It never writes task text,
response content, bearer tokens, or response warnings to disk.

Run from the repository root:
  uv run --with-requirements backend/requirements.txt python backend/route_profile.py \
    --json-out backend/eval-results/route-profile-before.json
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from auto_skill_auth import auth_headers
from auto_skill_core import get_autoskill_url


DEFAULT_CASES_PATH = Path(__file__).parent / "evals" / "routes.jsonl"
SAFE_METRIC_KEYS = (
    "latency_ms",
    "skill_find_ms",
    "retrieval_ms",
    "rerank_ms",
    "routing_filters_ms",
    "primary_retrieval_ms",
    "policy_lookup_ms",
    "candidate_rerank_ms",
    "candidate_filter_ms",
    "deliverable_validation_ms",
    "tier_decision_ms",
    "policy_build_ms",
    "content_ms",
    "injected_tokens",
    "response_tokens",
)


def _load_cases(path: Path, limit: int | None) -> list[tuple[str, str]]:
    cases: list[tuple[str, str]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        raw_line = line.strip()
        if not raw_line or raw_line.startswith("#"):
            continue
        try:
            raw = json.loads(raw_line)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        case_id = str(raw.get("id") or f"case-{lineno}").strip()
        task = str(raw.get("prompt") or raw.get("query") or "").strip()
        if not task:
            raise ValueError(f"{path}:{lineno}: missing prompt")
        cases.append((case_id, task))
    if not cases:
        raise ValueError(f"{path}: no route cases found")
    return cases[:limit] if limit else cases


def _percentile(values: list[int], percentile: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((len(ordered) - 1) * percentile)))
    return int(ordered[index])


def _numeric_metrics(payload: dict[str, Any]) -> dict[str, int]:
    raw = ((payload.get("score_debug") or {}).get("metrics") or {})
    if not isinstance(raw, dict):
        return {}
    return {
        key: int(raw[key])
        for key in SAFE_METRIC_KEYS
        if isinstance(raw.get(key), (int, float))
    }


def _sanitize_route_result(
    case_id: str,
    repeat: int,
    status_code: int,
    payload: dict[str, Any],
    client_wall_ms: int,
) -> dict[str, Any]:
    skill = payload.get("skill") if isinstance(payload.get("skill"), dict) else {}
    return {
        "case_id": case_id,
        "repeat": repeat,
        "status_code": status_code,
        "tier": str(payload.get("tier") or "none"),
        "selected_skill": str(skill.get("name") or skill.get("slug") or "") or None,
        "client_wall_ms": client_wall_ms,
        "server_metrics": _numeric_metrics(payload),
    }


def _metric_summary(rows: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    values: dict[str, list[int]] = {"client_wall_ms": []}
    for row in rows:
        values["client_wall_ms"].append(int(row["client_wall_ms"]))
        for key, value in row.get("server_metrics", {}).items():
            values.setdefault(key, []).append(int(value))
    return {
        key: {
            "count": len(metric_values),
            "p50": _percentile(metric_values, 0.50) or 0,
            "p95": _percentile(metric_values, 0.95) or 0,
            "p99": _percentile(metric_values, 0.99) or 0,
            "max": max(metric_values) if metric_values else 0,
        }
        for key, metric_values in values.items()
        if metric_values
    }


async def _capture(args: argparse.Namespace) -> dict[str, Any]:
    cases = _load_cases(args.cases, args.limit)
    headers = auth_headers()
    token = os.getenv(args.token_env)
    if token:
        headers = {"Authorization": f"Bearer {token}"}
    if not headers and not args.allow_unauthenticated:
        raise RuntimeError(
            f"No bearer token found in ${args.token_env} or the local Auto-Skill credentials file. "
            "Use --allow-unauthenticated only for a local development server."
        )

    rows: list[dict[str, Any]] = []
    base_url = args.base_url.rstrip("/")
    async with httpx.AsyncClient(timeout=args.timeout) as client:
        for repeat in range(1, args.repetitions + 1):
            for case_id, task in cases:
                start = time.monotonic()
                response = await client.post(
                    f"{base_url}/route",
                    json={"task": task, "client": "route_profile", "client_version": "v1"},
                    headers=headers,
                )
                try:
                    payload = response.json() if response.status_code == 200 else {}
                except ValueError:
                    payload = {}
                rows.append(
                    _sanitize_route_result(
                        case_id,
                        repeat,
                        response.status_code,
                        payload,
                        int((time.monotonic() - start) * 1000),
                    )
                )

    return {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_url": base_url,
        "case_file": str(args.cases),
        "repetitions": args.repetitions,
        "cases": len(cases),
        "rows": rows,
        "metrics": _metric_summary(rows),
    }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Capture a privacy-safe Auto-Skill route performance snapshot.")
    parser.add_argument("--base-url", default=os.getenv("AUTOSKILL_PROFILE_URL") or get_autoskill_url())
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES_PATH)
    parser.add_argument("--limit", type=int, default=5, help="number of public benchmark cases to run")
    parser.add_argument("--repetitions", type=int, default=3)
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--token-env", default="AUTOSKILL_EVAL_TOKEN")
    parser.add_argument("--allow-unauthenticated", action="store_true")
    parser.add_argument("--json-out", type=Path, default=None)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.repetitions < 1:
        raise SystemExit("--repetitions must be at least 1")
    if args.limit is not None and args.limit < 1:
        raise SystemExit("--limit must be at least 1")
    summary = asyncio.run(_capture(args))
    print(json.dumps({"metrics": summary["metrics"], "cases": summary["cases"], "repetitions": summary["repetitions"]}, indent=2, sort_keys=True))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote sanitized route profile: {args.json_out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
