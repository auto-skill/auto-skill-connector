"""Replay the public Terminal-Bench 2 task corpus through skill retrieval.

This is a routing benchmark, not a task-success benchmark.  It records the
ranked candidates and reports concentration/latency statistics so retrieval
experiments can be compared against a frozen set of real task instructions.

Example:
  python backend/bench/tbench_route_replay.py ^
    --task-repo C:\tmp\terminal-bench-2-dataset ^
    --json-out backend/eval-results/tbench-routing-control.json
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import math
import statistics
import subprocess
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

import httpx

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from auto_skill_auth import auth_headers  # noqa: E402
from auto_skill_core import get_autoskill_url  # noqa: E402


class RequestPacer:
    """Serialize request starts without serializing response processing."""

    def __init__(self, interval_seconds: float):
        self.interval_seconds = max(0.0, interval_seconds)
        self._lock = asyncio.Lock()
        self._next_start = 0.0

    async def wait(self) -> None:
        async with self._lock:
            now = time.monotonic()
            delay = self._next_start - now
            if delay > 0:
                await asyncio.sleep(delay)
            self._next_start = time.monotonic() + self.interval_seconds


def _git(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    return result.stdout


def load_tasks(repo: Path, ref: str) -> list[dict]:
    paths = [
        line.strip()
        for line in _git(repo, "ls-tree", "-r", "--name-only", ref).splitlines()
        if line.count("/") == 1 and line.endswith("/instruction.md")
    ]
    tasks = []
    for path in sorted(paths):
        instruction = _git(repo, "show", f"{ref}:{path}").strip()
        tasks.append(
            {
                "id": path.split("/", 1)[0],
                "instruction": instruction,
                "instruction_sha256": hashlib.sha256(instruction.encode("utf-8")).hexdigest(),
            }
        )
    if not tasks:
        raise ValueError(f"no */instruction.md tasks found at {repo} {ref}")
    return tasks


def summarize(rows: list[dict]) -> dict:
    n = len(rows)
    tiers = Counter(str(row.get("tier") or "none") for row in rows)
    top_names = [
        str(row["results"][0].get("name") or row["results"][0].get("id") or "unknown")
        for row in rows
        if row.get("results")
    ]
    top_counts = Counter(top_names)
    top_total = sum(top_counts.values())
    shares = [count / top_total for count in top_counts.values()] if top_total else []
    def duration_summary(field: str) -> dict:
        values = [float(row[field]) for row in rows if row.get(field) is not None]
        return {
            "mean": round(statistics.fmean(values), 3) if values else None,
            "median": round(statistics.median(values), 3) if values else None,
            "p95": round(sorted(values)[max(0, math.ceil(0.95 * len(values)) - 1)], 3)
            if values
            else None,
        }

    return {
        "tasks": n,
        "successful_requests": sum(int(row.get("status_code") == 200) for row in rows),
        "tasks_with_results": top_total,
        "tier_counts": dict(sorted(tiers.items())),
        "unique_top_skills": len(top_counts),
        "most_common_top_skills": top_counts.most_common(15),
        "maximum_top_skill_share": round(max(shares), 6) if shares else 0.0,
        "top_skill_hhi": round(sum(share * share for share in shares), 6),
        "top_skill_entropy_bits": round(-sum(share * math.log2(share) for share in shares), 6)
        if shares
        else 0.0,
        "latency_ms": duration_summary("latency_ms"),
        "queue_wait_ms": duration_summary("queue_wait_ms"),
        "retry_wait_ms": duration_summary("retry_wait_ms"),
    }


async def replay_task(
    client: httpx.AsyncClient,
    semaphore: asyncio.Semaphore,
    base_url: str,
    task: dict,
    limit: int,
    pacer: RequestPacer,
    retries: int,
) -> dict:
    service_seconds = 0.0
    queue_wait_seconds = 0.0
    retry_wait_seconds = 0.0
    try:
        response = None
        for attempt in range(retries + 1):
            queue_started = time.perf_counter()
            await pacer.wait()
            async with semaphore:
                queue_wait_seconds += time.perf_counter() - queue_started
                service_started = time.perf_counter()
                try:
                    response = await client.post(
                        f"{base_url}/find-semantic",
                        json={"q": task["instruction"], "limit": limit, "gate": False},
                        headers=auth_headers(),
                        timeout=90,
                    )
                finally:
                    service_seconds += time.perf_counter() - service_started
            if response.status_code != 429 or attempt == retries:
                break
            retry_after = response.headers.get("retry-after")
            retry_started = time.perf_counter()
            await asyncio.sleep(
                float(retry_after) if retry_after else min(30.0, 2.0 ** (attempt + 1))
            )
            retry_wait_seconds += time.perf_counter() - retry_started
        assert response is not None
        body = response.json() if response.status_code == 200 else {}
        error = None if response.status_code == 200 else response.text[:500]
    except Exception as exc:  # preserve the failed task in the benchmark artifact
        response = None
        body = {}
        error = f"{type(exc).__name__}: {exc}"
    return {
        **task,
        "status_code": response.status_code if response is not None else None,
        "latency_ms": round(service_seconds * 1000, 3),
        "queue_wait_ms": round(queue_wait_seconds * 1000, 3),
        "retry_wait_ms": round(retry_wait_seconds * 1000, 3),
        "tier": body.get("tier", "none"),
        "score_debug": body.get("score_debug") or {},
        "config_version": body.get("config_version"),
        "results": body.get("results") or [],
        "error": error,
    }


async def async_main(args: argparse.Namespace) -> int:
    tasks = load_tasks(args.task_repo.resolve(), args.ref)
    completed: dict[str, dict] = {}
    if args.resume and args.json_out.exists():
        previous = json.loads(args.json_out.read_text(encoding="utf-8"))
        completed = {
            str(row["id"]): row
            for row in previous.get("tasks", [])
            if row.get("status_code") == 200
        }
        print(f"resuming with {len(completed)} successful task(s)")
    semaphore = asyncio.Semaphore(args.concurrency)
    pacer = RequestPacer(args.request_interval)
    async with httpx.AsyncClient() as client:
        pending_rows = await asyncio.gather(
            *(
                replay_task(client, semaphore, args.base_url, task, args.limit, pacer, args.retries)
                for task in tasks
                if task["id"] not in completed
            )
        )
    by_id = {**completed, **{str(row["id"]): row for row in pending_rows}}
    rows = [by_id[task["id"]] for task in tasks]
    artifact = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "task_repo": str(args.task_repo.resolve()),
        "task_ref": args.ref,
        "base_url": args.base_url,
        "candidate_limit": args.limit,
        "summary": summarize(rows),
        "tasks": rows,
    }
    args.json_out.parent.mkdir(parents=True, exist_ok=True)
    args.json_out.write_text(json.dumps(artifact, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps(artifact["summary"], indent=2, sort_keys=True))
    print(f"wrote {args.json_out}")
    return 0 if artifact["summary"]["successful_requests"] == len(tasks) else 1


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--task-repo", type=Path, required=True)
    parser.add_argument("--ref", default="origin/main")
    parser.add_argument("--base-url", default=get_autoskill_url())
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--concurrency", type=int, default=12)
    parser.add_argument("--request-interval", type=float, default=2.1)
    parser.add_argument("--retries", type=int, default=5)
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--json-out", type=Path, required=True)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main(parse_args())))
