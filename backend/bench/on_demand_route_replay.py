"""Offline control-vs-on-demand route replay.

The fixture file contains a small, fixed candidate snapshot so this experiment
does not need live network access, a hydrated database, embeddings, or an LLM.
The control arm runs the current deterministic rerank/tier/context gates over
the frozen indexed-style rows.  The treatment arm discovers and fetches the
same task's bounded fixture shortlist through ``OnDemandResolver``.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import statistics
import sys
import time
import tracemalloc
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from context_guard import DEFAULT_CAPSULE_CHARS, build_context_guard, estimate_tokens
from on_demand_resolver import FetchedSkill, OnDemandResolver, SkillSource
from token_budget import FixedTokenCounter
from quality import (
    content_digest,
    content_hash,
    has_valid_skill_frontmatter,
    rerank_candidates,
    skill_capability_flags,
    tier_for_ranked_candidates,
)


DEFAULT_FIXTURE = Path(__file__).with_name("on_demand_resolver_fixtures.json")


def _fixture_commit(value: Any) -> str | None:
    """Turn readable fixture labels into immutable commit-shaped identities."""
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.casefold() in {"head", "main", "master", "latest", "default"}:
        return text
    if len(text) == 40 and all(char in "0123456789abcdefABCDEF" for char in text):
        return text.lower()
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:40]


def _rss_kb() -> int | None:
    try:
        import psutil

        return int(psutil.Process().memory_info().rss / 1024)
    except (ImportError, OSError, RuntimeError):
        return None


class FixtureProvider:
    name = "offline-fixture-provider"

    def __init__(self, rows: list[dict[str, Any]]) -> None:
        def field(row: dict[str, Any], name: str, fallback: Any = None) -> Any:
            return row[name] if name in row else fallback

        self.sources = [
            SkillSource(
                key=str(row["key"]),
                name=str(row["name"]),
                description=str(row.get("description") or ""),
                source_url=str(row.get("source_url") or "https://fixture.invalid/" + str(row["key"])),
                snapshot_hash=(
                    str(field(row, "source_snapshot_hash"))
                    if field(row, "source_snapshot_hash") is not None
                    else str(row.get("snapshot_hash") or "fixture-snapshot")
                ) or None,
                rank=index,
                metadata={
                    "platforms": list(row.get("platforms") or []),
                    "tags": list(row.get("tags") or []),
                },
                repository=row.get("repository"),
                path=str(row.get("path") or "SKILL.md"),
                revision=row.get("revision"),
                commit_sha=row.get("commit_sha"),
                declared_content_hash=row.get("declared_content_hash"),
                declared_content_digest=row.get("declared_content_digest"),
                license=row.get("license"),
            )
            for index, row in enumerate(rows)
        ]
        self.fetched = {
            str(row["key"]): FetchedSkill(
                content=(
                    str(row.get("content") or "")
                    + ("x" * max(0, int(row.get("pad_bytes") or 0)))
                ),
                snapshot_hash=_fixture_commit(
                    row.get("fetched_commit_sha")
                    or row.get("commit_sha")
                    or (
                        field(row, "fetched_snapshot_hash")
                        if field(row, "fetched_snapshot_hash") is not None
                        else row.get("snapshot_hash") or "fixture-snapshot"
                    )
                ),
                source_commit_sha=_fixture_commit(
                    row.get("fetched_commit_sha")
                    or row.get("commit_sha")
                    or (
                        field(row, "fetched_snapshot_hash")
                        if field(row, "fetched_snapshot_hash") is not None
                        else row.get("snapshot_hash") or "fixture-snapshot"
                    )
                ),
                declared_content_hash=row.get("fetched_declared_content_hash"),
                raw_content_digest=row.get("raw_content_digest"),
                declared_content_digest=row.get("fetched_declared_content_digest"),
                source_url=row.get("fetched_source_url"),
                entrypoint_path=str(row.get("fetched_path") or row.get("path") or "SKILL.md"),
                byte_count=int(row.get("byte_count") or 0),
                audit_status=str(row.get("audit_status") or "pass"),
                audit_risk_level=str(row.get("audit_risk_level") or "low"),
                risk_score=int(
                    row["risk_score"]
                    if "risk_score" in row
                    else (1 if str(row.get("audit_status") or "pass").casefold() == "unknown" else 0)
                ),
                entrypoint_truncated=bool(row.get("entrypoint_truncated")),
                rejection_reason=row.get("rejection_reason"),
            )
            for row in rows
        }

    async def discover(
        self, query: str, limit: int, *, deadline: float | None = None
    ) -> list[SkillSource]:
        del query
        if deadline is not None and deadline <= 0:
            return []
        return self.sources[: max(1, int(limit))]

    async def fetch(
        self,
        source: SkillSource,
        max_bytes: int,
        *,
        deadline: float | None = None,
    ) -> FetchedSkill | None:
        del max_bytes
        if deadline is not None and deadline <= 0:
            return None
        return self.fetched.get(source.key)


def _read_fixture(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("tasks"), list):
        raise ValueError("fixture must contain a tasks list")
    return payload


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percentile))))
    return round(float(ordered[index]), 3)


def _match_name(row: dict[str, Any] | None, expected: list[str]) -> bool:
    if not expected:
        return row is None
    if not row:
        return False
    name = str(row.get("name") or "").casefold()
    return any(str(value).casefold() in name for value in expected)


def _forbidden(row: dict[str, Any] | None, forbidden: list[str]) -> bool:
    if not row:
        return False
    name = str(row.get("name") or "").casefold()
    return any(str(value).casefold() in name for value in forbidden)


def _route_rows(task: str, rows: list[dict[str, Any]]) -> dict[str, Any]:
    started = time.perf_counter()
    ranked = rerank_candidates(task, rows)
    tier = tier_for_ranked_candidates(ranked)
    selected = ranked[0] if ranked else None
    capsule = ""
    guard_reason = ""
    guard: dict[str, Any] = {}
    if tier == "full" and selected:
        content = str(selected.get("_content") or "")
        cached_guard = dict(selected.get("_on_demand_context_guard") or {})
        on_demand_guard = bool(selected.get("on_demand_mode") and cached_guard)
        cached = bool(selected.get("_on_demand_cached") and cached_guard)
        verified = cached or (
            bool(content)
            and has_valid_skill_frontmatter(content)
            and content_hash(content) == selected.get("content_hash")
            and not skill_capability_flags(content)
        )
        guard = (
            cached_guard
            if on_demand_guard
            else build_context_guard(
                task=task,
                content=content,
                content_hash=str(selected.get("content_hash") or ""),
                content_digest=content_digest(content),
                max_capsule_chars=DEFAULT_CAPSULE_CHARS,
                force_capsule=True,
            )
            if verified
            else {"delivery": "hint", "reason": "content-verification-failed"}
        )
        guard_reason = str(guard.get("reason") or "")
        if not verified or guard.get("delivery") not in {"capsule", "isolation"}:
            tier = "hint"
        else:
            capsule = str(guard.get("capsule") or "")
    unsafe = bool(selected and skill_capability_flags(str(selected.get("_content") or "")))
    non_portable = guard_reason == "non_portable_project_specific"
    exact_tokens = int(
        (selected or {}).get("on_demand_capsule_token_count")
        or guard.get("estimated_tokens")
        or estimate_tokens(capsule)
    )
    return {
        "latency_ms": (time.perf_counter() - started) * 1000.0,
        "tier": tier,
        "selected": selected,
        "top_candidates": ranked[:3],
        "capsule_chars": len(capsule),
        "capsule_tokens": exact_tokens,
        "tokenizer_id": guard.get("tokenizer_id"),
        "unsafe_content": unsafe,
        "non_portable": non_portable,
        "incomplete_route": bool(
            tier == "full"
            and selected
            and guard.get("complete") is not True
        ),
    }


def _control_rows(task: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for index, item in enumerate(task.get("control") or []):
        row = dict(item)
        content = str(row.get("content") or "")
        row["_content"] = content
        row.setdefault("source", "skills_sh")
        row.setdefault("retrieval_backend", "skills_sh")
        row.setdefault("url", "https://fixture.invalid/control/" + str(row.get("name") or index))
        row.setdefault("rank", 1.0 / (60.0 + index + 1.0))
        row.setdefault("similarity", None)
        row.setdefault("content_hash", content_hash(content))
        row.setdefault("quality_status", "active")
        row.setdefault("quality_score", 90)
        row.setdefault("risk_score", 0)
        row.setdefault("source_snapshot_hash", "control-snapshot")
        row.setdefault("audit_status", "pass")
        rows.append(row)
    return rows


def _experiment_resolver(task: dict[str, Any]) -> OnDemandResolver:
    provider = FixtureProvider(list(task.get("experiment") or []))
    return OnDemandResolver(
        provider,
        max_provider_calls=2,
        max_fetches=2,
        max_bytes=256 * 1024,
        max_entrypoint_bytes=128 * 1024,
        max_wall_ms=450,
        fetch_top_k=2,
        token_counter=FixedTokenCounter(),
    )


async def _experiment_route(
    task: dict[str, Any], resolver: OnDemandResolver | None = None
) -> dict[str, Any]:
    resolver = resolver or _experiment_resolver(task)
    result = await resolver.resolve(str(task["task"]), limit=8)
    route = _route_rows(str(task["task"]), result.candidates)
    route["resolver"] = result.debug()
    return route


def _measure_arm(
    tasks: list[dict[str, Any]], arm: str, repeats: int
) -> tuple[list[dict[str, Any]], int, int | None]:
    rows: list[dict[str, Any]] = []
    rss_before = _rss_kb()
    tracemalloc.start()
    tracemalloc.reset_peak()
    experiment_resolvers = {
        str(task["id"]): _experiment_resolver(task)
        for task in tasks
    } if arm == "experiment" else {}
    for _repeat in range(repeats):
        for task in tasks:
            if arm == "control":
                route = _route_rows(str(task["task"]), _control_rows(task))
            else:
                route = asyncio.run(
                    _experiment_route(task, experiment_resolvers[str(task["id"])])
                )
            expected = [str(value) for value in task.get("expected") or []]
            forbidden = [str(value) for value in task.get("forbidden") or []]
            selected = route.get("selected")
            route["top1_success"] = _match_name(selected, expected)
            route["topk_success"] = (
                selected is None
                if not expected
                else any(_match_name(candidate, expected) for candidate in route.get("top_candidates") or [])
            )
            route["local_db_miss"] = not bool(task.get("control"))
            route["db_miss_eligible"] = route["local_db_miss"] and bool(expected)
            route["db_miss_recovered"] = bool(
                arm == "experiment"
                and route["db_miss_eligible"]
                and route["topk_success"]
                and route["tier"] == "full"
            )
            route["false_positive"] = not expected and selected is not None
            route["irrelevant_or_unsafe"] = _forbidden(selected, forbidden) or (
                route["tier"] == "full" and (route["unsafe_content"] or route["non_portable"])
            )
            route["task_id"] = task["id"]
            route["arm"] = arm
            rows.append(route)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    rss_after = _rss_kb()
    rss_values = [value for value in (rss_before, rss_after) if value is not None]
    return rows, int(peak / 1024), (max(rss_values) if rss_values else None)


def _summarize(
    rows: list[dict[str, Any]],
    peak_memory_kb: int,
    rss_kb: int | None,
    repeats: int,
) -> dict[str, Any]:
    count = len(rows)
    latencies = [float(row["latency_ms"]) for row in rows]
    cold_latencies = [
        float(row["latency_ms"])
        for row in rows
        if not row.get("resolver") or (row.get("resolver") or {}).get("cache") != "hit"
    ]
    capsule_chars = [int(row.get("capsule_chars") or 0) for row in rows]
    capsule_tokens = [int(row.get("capsule_tokens") or 0) for row in rows]
    resolver_rows = [row.get("resolver") for row in rows if row.get("resolver")]
    statuses = [str(item.get("status") or "") for item in resolver_rows]
    budgets = [item.get("budget") or {} for item in resolver_rows]
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row.get("task_id") or ""), []).append(row)
    task_summary: list[dict[str, Any]] = []
    for task_id, task_rows in sorted(grouped.items()):
        selected_names = sorted(
            {
                str((row.get("selected") or {}).get("name") or "")
                for row in task_rows
                if row.get("selected")
            }
        )
        task_statuses = [
            str((row.get("resolver") or {}).get("status") or "")
            for row in task_rows
            if row.get("resolver")
        ]
        task_summary.append(
            {
                "task_id": task_id,
                "samples": len(task_rows),
                "top1_success_rate": round(
                    sum(bool(row["top1_success"]) for row in task_rows) / max(1, len(task_rows)),
                    6,
                ),
                "topk_success_rate": round(
                    sum(bool(row["topk_success"]) for row in task_rows) / max(1, len(task_rows)),
                    6,
                ),
                "tiers": sorted({str(row.get("tier") or "none") for row in task_rows}),
                "selected_names": selected_names,
                "false_positive_rate": round(
                    sum(bool(row["false_positive"]) for row in task_rows) / max(1, len(task_rows)),
                    6,
                ),
                "db_miss_recovery": {
                    "recovered": sum(bool(row["db_miss_recovered"]) for row in task_rows),
                    "eligible": sum(bool(row["db_miss_eligible"]) for row in task_rows),
                },
                "resolver_statuses": {
                    status: task_statuses.count(status)
                    for status in sorted(set(task_statuses))
                    if status
                },
            }
        )
    return {
        "tasks": count // max(1, repeats),
        "samples": count,
        "repeats": repeats,
        "latency_ms": {"p50": _percentile(latencies, 0.50), "p95": _percentile(latencies, 0.95)},
        "cold_latency_ms": {
            "p50": _percentile(cold_latencies, 0.50),
            "p95": _percentile(cold_latencies, 0.95),
        },
        "top1_success_rate": round(sum(bool(row["top1_success"]) for row in rows) / max(1, count), 6),
        "topk_success_rate": round(sum(bool(row["topk_success"]) for row in rows) / max(1, count), 6),
        "irrelevant_or_unsafe_route_rate": round(
            sum(bool(row["irrelevant_or_unsafe"]) for row in rows) / max(1, count), 6
        ),
        "incomplete_route_rate": round(
            sum(bool(row["incomplete_route"]) for row in rows) / max(1, count), 6
        ),
        "false_positive_route_rate": round(
            sum(bool(row["false_positive"]) for row in rows) / max(1, count), 6
        ),
        "full_unsafe_route_count": sum(
            row["tier"] == "full" and (row["unsafe_content"] or row["non_portable"]) for row in rows
        ),
        "db_miss_recovery": {
            "recovered": sum(bool(row["db_miss_recovered"]) for row in rows),
            "eligible": sum(bool(row["db_miss_eligible"]) for row in rows),
            "rate": round(
                sum(bool(row["db_miss_recovered"]) for row in rows)
                / max(1, sum(bool(row["db_miss_eligible"]) for row in rows)),
                6,
            ),
        },
        "capsule_chars": {"p50": _percentile(capsule_chars, 0.50), "p95": _percentile(capsule_chars, 0.95)},
        "capsule_tokens": {"p50": _percentile(capsule_tokens, 0.50), "p95": _percentile(capsule_tokens, 0.95)},
        "process_memory_peak_kb_tracemalloc": peak_memory_kb,
        "process_memory_observed_kb_rss": rss_kb,
        "resolver_cache_hits": sum(1 for item in resolver_rows if item.get("cache") == "hit"),
        "resolver_statuses": {
            status: statuses.count(status)
            for status in sorted(set(statuses))
            if status
        },
        "provider_calls": sum(int(item.get("provider_calls") or 0) for item in budgets),
        "fetches": sum(int(item.get("fetches") or 0) for item in budgets),
        "bytes_read": sum(int(item.get("bytes_read") or 0) for item in budgets),
        "capsule_tokenizer_ids": sorted(
            {
                str(row.get("tokenizer_id"))
                for row in rows
                if row.get("tokenizer_id")
            }
        ),
        "task_summary": task_summary,
    }


def run(fixture: Path, repeats: int) -> dict[str, Any]:
    payload = _read_fixture(fixture)
    tasks = list(payload["tasks"])
    control_rows, control_peak, control_rss = _measure_arm(tasks, "control", repeats)
    experiment_rows, experiment_peak, experiment_rss = _measure_arm(tasks, "experiment", repeats)
    return {
        "schema_version": 2,
        "fixture": fixture.name,
        "network": "disabled",
        "control_definition": "current indexed-style rerank/tier/context gates over frozen rows",
        "experiment_definition": "OnDemandResolver over bounded fixture provider with production budgets",
        "control": _summarize(control_rows, control_peak, control_rss, repeats),
        "experiment": _summarize(experiment_rows, experiment_peak, experiment_rss, repeats),
    }


def _markdown(report: dict[str, Any], command: str) -> str:
    control = report["control"]
    experiment = report["experiment"]
    diagnostics: list[str] = []
    for arm_name, arm in (("control", control), ("experiment", experiment)):
        for item in arm.get("task_summary") or []:
            db_miss = item["db_miss_recovery"]
            if (
                item["top1_success_rate"] < 1.0
                or item["false_positive_rate"] > 0.0
                or db_miss["recovered"] < db_miss["eligible"]
            ):
                selected = ", ".join(item["selected_names"]) or "none"
                diagnostics.append(
                    f"- {arm_name} `{item['task_id']}`: "
                    f"top-1 {item['top1_success_rate']:.3f}, "
                    f"tier {','.join(item['tiers'])}, selected {selected}, "
                    f"DB-miss {db_miss['recovered']}/{db_miss['eligible']}."
                )
    diagnostics_text = "\n".join(diagnostics) or "- none"
    return f"""# On-demand resolver experiment

Status: offline replay complete; no live network, deployment, database rewrite, CAS write, or broad hydration was used.
The fixture is deterministic and authored in-repo; it is not yet an external or human-labeled benchmark.

Command:

```text
{command}
```

## Results

| Arm | p50 / p95 latency (ms) | Top-1 / Top-k success | Irrelevant/unsafe | Incomplete | False-positive | Capsule p50 / p95 chars | Python memory (tracemalloc / RSS) |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| Control | {control['latency_ms']['p50']} / {control['latency_ms']['p95']} | {control['top1_success_rate']:.3f} / {control['topk_success_rate']:.3f} | {control['irrelevant_or_unsafe_route_rate']:.3f} | {control['incomplete_route_rate']:.3f} | {control['false_positive_route_rate']:.3f} | {control['capsule_chars']['p50']} / {control['capsule_chars']['p95']} | {control['process_memory_peak_kb_tracemalloc']} / {control['process_memory_observed_kb_rss']} KiB |
| Experiment | {experiment['latency_ms']['p50']} / {experiment['latency_ms']['p95']} | {experiment['top1_success_rate']:.3f} / {experiment['topk_success_rate']:.3f} | {experiment['irrelevant_or_unsafe_route_rate']:.3f} | {experiment['incomplete_route_rate']:.3f} | {experiment['false_positive_route_rate']:.3f} | {experiment['capsule_chars']['p50']} / {experiment['capsule_chars']['p95']} | {experiment['process_memory_peak_kb_tracemalloc']} / {experiment['process_memory_observed_kb_rss']} KiB |

Cold/uncached latency (p50 / p95 ms): control {control['cold_latency_ms']['p50']} / {control['cold_latency_ms']['p95']}; experiment {experiment['cold_latency_ms']['p50']} / {experiment['cold_latency_ms']['p95']}.

Capsule tokens (p50 / p95): control {control['capsule_tokens']['p50']} / {control['capsule_tokens']['p95']}; experiment {experiment['capsule_tokens']['p50']} / {experiment['capsule_tokens']['p95']}.

DB-miss recovery: control {control['db_miss_recovery']}; experiment {experiment['db_miss_recovery']}.

Experiment budgets: provider calls {experiment['provider_calls']}, fetches {experiment['fetches']}, bytes {experiment['bytes_read']}, cache hits {experiment['resolver_cache_hits']}, statuses {experiment['resolver_statuses']}.

Exact capsule tokenizers observed: {experiment['capsule_tokenizer_ids'] or ['none']}.

## Task-level diagnostics

{diagnostics_text}

## Recommendation

No-go for active fallback based on this fixture alone; this is not an improvement claim. Keep the feature flag off until a held-out replay has enough labeled tasks to establish non-inferior top-1/top-k relevance, zero unsafe/incomplete accepted routes, valid DB-miss recovery, and p95 latency within the route budget.
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--json-out", type=Path)
    parser.add_argument("--markdown-out", type=Path)
    args = parser.parse_args()
    repeats = max(1, min(int(args.repeats), 100))
    report = run(args.fixture, repeats)
    command = (
        "python backend/bench/on_demand_route_replay.py "
        f"--fixture {args.fixture.as_posix()} --repeats {repeats}"
    )
    if args.json_out:
        command += f" --json-out {args.json_out.as_posix()}"
    if args.markdown_out:
        command += f" --markdown-out {args.markdown_out.as_posix()}"
    if args.json_out:
        args.json_out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    if args.markdown_out:
        args.markdown_out.write_text(_markdown(report, command), encoding="utf-8")
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
