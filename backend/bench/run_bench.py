"""Auto-Skill-Bench: does routing a skill into context actually improve task
outcomes, not just retrieval accuracy?

eval_search.py already proves /route picks the right skill. This proves
whether using that skill changes anything for the agent: each task in
tasks.jsonl is run twice against a real model -- once bare (baseline) and
once with the /route response injected exactly as hooks/skill_suggest.py
would inject it (same wrapper text, only on a full-tier route) -- then both
answers are graded against the same must_include checks and compared.

This costs real LLM API calls and is not wired into CI. Run it manually:
  python run_bench.py
  python run_bench.py --model claude-haiku-4-5-20251001 --limit 5
  python run_bench.py --json-out ../eval-results/bench-latest.json
  python run_bench.py --dry-run   # validates tasks.jsonl and routing only, no LLM calls
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from recommender import SUPABASE_URL

DEFAULT_TASKS_PATH = Path(__file__).parent / "tasks.jsonl"
DEFAULT_MODEL = "claude-haiku-4-5-20251001"

_HOOK_WRAPPER = (
    "Use the following SKILL.md content as active task-specific instructions for this turn. "
    "Apply it immediately unless it is missing, unusable, or unsafe.\n\n"
    "<auto_skill_content>\n{content}\n</auto_skill_content>"
)


def _load_tasks(path: Path, limit: int | None) -> list[dict[str, Any]]:
    tasks: list[dict[str, Any]] = []
    for lineno, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        try:
            raw = json.loads(stripped)
        except json.JSONDecodeError as exc:
            raise ValueError(f"{path}:{lineno}: invalid JSON: {exc}") from exc
        if not raw.get("prompt"):
            raise ValueError(f"{path}:{lineno}: missing prompt")
        tasks.append(raw)
    if not tasks:
        raise ValueError(f"{path}: no bench tasks found")
    return tasks[:limit] if limit else tasks


def _grade(text: str, task: dict[str, Any]) -> bool:
    lowered = text.lower()
    for needle in task.get("must_include_all", []):
        if needle.lower() not in lowered:
            return False
    any_list = task.get("must_include_any")
    if any_list and not any(needle.lower() in lowered for needle in any_list):
        return False
    return True


async def _route(client: httpx.AsyncClient, query: str) -> dict[str, Any]:
    try:
        r = await client.post(
            f"{SUPABASE_URL}/route",
            json={"task": query, "client": "bench", "client_version": "local"},
            timeout=45,
        )
        if r.status_code == 200:
            return r.json()
    except Exception as exc:
        return {"tier": "none", "error": str(exc)}
    return {"tier": "none", "error": f"status_code={r.status_code}"}


async def _call_model(client, model: str, system: str | None, prompt: str) -> dict[str, Any]:
    start = time.monotonic()
    kwargs: dict[str, Any] = {
        "model": model,
        "max_tokens": 1024,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    response = await client.messages.create(**kwargs)
    latency_ms = int((time.monotonic() - start) * 1000)
    text = "".join(block.text for block in response.content if getattr(block, "type", "") == "text")
    return {
        "text": text,
        "latency_ms": latency_ms,
        "input_tokens": response.usage.input_tokens,
        "output_tokens": response.usage.output_tokens,
    }


async def run(args: argparse.Namespace) -> dict[str, Any]:
    tasks = _load_tasks(args.tasks, args.limit)

    summary: dict[str, Any] = {
        "created_at": datetime.now(timezone.utc).isoformat(),
        "base_url": SUPABASE_URL,
        "model": args.model,
        "dry_run": args.dry_run,
        "task_bench": {"cases": [], "baseline": {}, "with_skill": {}, "by_category": {}},
    }

    async with httpx.AsyncClient() as http_client:
        routes = {t["id"]: await _route(http_client, t.get("route_query") or t["prompt"]) for t in tasks}

    if args.dry_run:
        for t in tasks:
            route = routes[t["id"]]
            print(f"  {t['id']:<16} tier={route.get('tier', 'none'):<5} category={t.get('category')}")
        summary["task_bench"]["cases"] = [
            {"id": t["id"], "category": t.get("category"), "tier": routes[t["id"]].get("tier", "none")}
            for t in tasks
        ]
        return summary

    try:
        import anthropic
    except ImportError:
        print(
            "The `anthropic` package is required to run the bench for real (pip install anthropic). "
            "Use --dry-run to validate tasks.jsonl and routing without it.",
            file=sys.stderr,
        )
        raise

    model_client = anthropic.AsyncAnthropic()

    baseline_pass = with_skill_pass = full_tier_count = 0
    by_category: dict[str, dict[str, int]] = {}
    cases: list[dict[str, Any]] = []

    for t in tasks:
        route = routes[t["id"]]
        tier = route.get("tier", "none")
        skill = route.get("skill") or {}
        content = skill.get("content") or ""

        baseline = await _call_model(model_client, args.model, None, t["prompt"])
        baseline_ok = _grade(baseline["text"], t)

        if tier == "full" and content:
            system = _HOOK_WRAPPER.format(content=content)
            with_skill = await _call_model(model_client, args.model, system, t["prompt"])
            full_tier_count += 1
        else:
            with_skill = baseline  # no full route available; same arm, no injection possible
        with_skill_ok = _grade(with_skill["text"], t)

        baseline_pass += baseline_ok
        with_skill_pass += with_skill_ok
        category = t.get("category", "uncategorized")
        cat_stats = by_category.setdefault(category, {"total": 0, "baseline_pass": 0, "with_skill_pass": 0})
        cat_stats["total"] += 1
        cat_stats["baseline_pass"] += baseline_ok
        cat_stats["with_skill_pass"] += with_skill_ok

        print(
            f"  {t['id']:<16} tier={tier:<5} baseline={'OK' if baseline_ok else 'FAIL'} "
            f"with_skill={'OK' if with_skill_ok else 'FAIL'}"
        )
        cases.append(
            {
                "id": t["id"],
                "category": category,
                "tier": tier,
                "skill": skill.get("name") or skill.get("slug"),
                "baseline_pass": baseline_ok,
                "with_skill_pass": with_skill_ok,
                "baseline_latency_ms": baseline["latency_ms"],
                "with_skill_latency_ms": with_skill["latency_ms"],
                "baseline_output_tokens": baseline["output_tokens"],
                "with_skill_output_tokens": with_skill["output_tokens"],
            }
        )

    n = len(tasks)
    summary["task_bench"] = {
        "cases": cases,
        "baseline": {"pass": baseline_pass, "total": n, "pass_rate": round(baseline_pass / n, 4)},
        "with_skill": {
            "pass": with_skill_pass,
            "total": n,
            "pass_rate": round(with_skill_pass / n, 4),
            "full_tier_count": full_tier_count,
        },
        "lift": round((with_skill_pass - baseline_pass) / n, 4),
        "by_category": {
            cat: {
                "total": s["total"],
                "baseline_pass_rate": round(s["baseline_pass"] / s["total"], 4),
                "with_skill_pass_rate": round(s["with_skill_pass"] / s["total"], 4),
            }
            for cat, s in by_category.items()
        },
    }

    print(
        f"\nbaseline pass rate:   {baseline_pass}/{n} ({baseline_pass / n:.0%})\n"
        f"with-skill pass rate: {with_skill_pass}/{n} ({with_skill_pass / n:.0%})  "
        f"[{full_tier_count}/{n} tasks got a full-tier route]\n"
        f"lift: {(with_skill_pass - baseline_pass) / n:+.0%}"
    )
    return summary


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the Auto-Skill task-success benchmark.")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS_PATH)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--json-out", type=Path, default=None)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="validate tasks.jsonl and check /route tiers only; no LLM calls, no cost",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    summary = asyncio.run(run(args))
    if args.json_out:
        args.json_out.parent.mkdir(parents=True, exist_ok=True)
        args.json_out.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote bench summary: {args.json_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
