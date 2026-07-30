"""Evaluate the skills.sh live gate without pretending a missing token is data.

Input is JSONL with ``query`` and optional ``expected_ids`` (a list of
skills.sh IDs). The evaluator compares original-query retrieval with the
Auto-Skill structured-query union, and reports retrieval quality plus audit
coverage. It never sends benchmark IDs, labels, or solution text to skills.sh.

The authenticated skills.sh API requires a Vercel OIDC bearer token. Without
one, the evaluator uses the public website search endpoint in
``public_search_only`` mode: retrieval can be measured, but detail/audit
coverage is expected to be unavailable. If even public search fails, it emits
``skipped`` rather than fabricating a result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from pathlib import Path
from typing import Any

from query_compiler import compile_intent_query
from skills_sh_catalog import SkillsShCatalog, SkillsShCatalogError


def _read_cases(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        if not isinstance(row, dict) or not str(row.get("query") or "").strip():
            raise ValueError("each JSONL row needs a non-empty query")
        expected = row.get("expected_ids") or []
        if not isinstance(expected, list):
            raise ValueError("expected_ids must be a list when supplied")
        rows.append({"query": " ".join(str(row["query"]).split()), "expected_ids": [str(item) for item in expected]})
    return rows


def _hit(rows: list[dict[str, Any]], expected: set[str], k: int) -> bool | None:
    if not expected:
        return None
    return bool(expected & {str(row.get("id") or "") for row in rows[:k]})


def _rrf_union(original: list[dict[str, Any]], compiled: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    """Fuse independent skills.sh lanes without letting original always win."""
    fused: dict[str, dict[str, Any]] = {}
    for lane, rows in (("original", original), ("compiled", compiled)):
        for rank, row in enumerate(rows[:limit]):
            key = str(row.get("id") or row.get("url") or "")
            if not key:
                continue
            item = fused.setdefault(key, dict(row))
            item["rrf_score"] = float(item.get("rrf_score") or 0.0) + 1.0 / (60 + rank + 1)
            queries = item.setdefault("retrieval_queries", [])
            if lane not in queries:
                queries.append(lane)
    return sorted(
        fused.values(),
        key=lambda row: (-float(row.get("rrf_score") or 0.0), str(row.get("id") or row.get("url") or "")),
    )[:limit]


def _paired_ci_pp(deltas: list[float], *, seed: int = 20260730, replicates: int = 4000) -> list[float] | None:
    if not deltas:
        return None
    rng = random.Random(seed)
    samples = []
    for _ in range(replicates):
        samples.append(100.0 * sum(rng.choice(deltas) for _ in deltas) / len(deltas))
    samples.sort()
    return [round(samples[int(0.025 * (len(samples) - 1))], 3), round(samples[int(0.975 * (len(samples) - 1))], 3)]


async def _evaluate(catalog: SkillsShCatalog, cases: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    started = time.perf_counter()
    details: list[dict[str, Any]] = []
    for case in cases:
        query = case["query"]
        intent = compile_intent_query(query)
        original = await catalog.retrieve(query, limit=limit)
        compiled_rows: list[dict[str, Any]] = []
        for variant in list(intent.query_variants)[1:2]:
            compiled_rows.extend(await catalog.retrieve(variant, limit=limit))
        dual = _rrf_union(original, compiled_rows, limit)
        expected = set(case["expected_ids"])
        details.append(
            {
                "query": query,
                "compiled_query": intent.compressed_query,
                "original_ids": [row.get("id") for row in original[:limit]],
                "dual_ids": [row.get("id") for row in dual[:limit]],
                "original_hit_at_1": _hit(original, expected, 1),
                "dual_hit_at_1": _hit(dual, expected, 1),
                "original_hit_at_5": _hit(original, expected, 5),
                "dual_hit_at_5": _hit(dual, expected, 5),
                "audit_unknown": sum(1 for row in dual if row.get("audit_status") == "unknown"),
                "audit_fail": sum(1 for row in dual if row.get("audit_status") == "fail"),
            }
        )
    labeled = [row for row in details if row["dual_hit_at_1"] is not None]

    def rate(key: str) -> float | None:
        values = [row[key] for row in labeled]
        return sum(values) / len(values) if values else None

    deltas_at_1 = [float(row["dual_hit_at_1"]) - float(row["original_hit_at_1"]) for row in labeled]
    deltas_at_5 = [float(row["dual_hit_at_5"]) - float(row["original_hit_at_5"]) for row in labeled]
    minimum_labeled = 2

    return {
        "status": "complete" if len(labeled) >= minimum_labeled else "insufficient_labels",
        "backend": "skills_sh",
        "access_mode": "authenticated" if catalog.configured else "public_search_only",
        "case_count": len(cases),
        "labeled_case_count": len(labeled),
        "metrics": {
            "original_hit_at_1": rate("original_hit_at_1"),
            "dual_hit_at_1": rate("dual_hit_at_1"),
            "original_hit_at_5": rate("original_hit_at_5"),
            "dual_hit_at_5": rate("dual_hit_at_5"),
            "delta_at_1_pp": round(100.0 * rate("dual_hit_at_1") - 100.0 * rate("original_hit_at_1"), 3) if labeled else None,
            "delta_at_5_pp": round(100.0 * rate("dual_hit_at_5") - 100.0 * rate("original_hit_at_5"), 3) if labeled else None,
            "paired_bootstrap_95ci_at_1_pp": _paired_ci_pp(deltas_at_1),
            "paired_bootstrap_95ci_at_5_pp": _paired_ci_pp(deltas_at_5),
            "paired_wins_at_1": sum(row["original_hit_at_1"] is False and row["dual_hit_at_1"] is True for row in labeled),
            "paired_losses_at_1": sum(row["original_hit_at_1"] is True and row["dual_hit_at_1"] is False for row in labeled),
            "paired_ties_at_1": sum(row["original_hit_at_1"] == row["dual_hit_at_1"] for row in labeled),
            "audit_unknown_rows": sum(row["audit_unknown"] for row in details),
            "audit_fail_rows": sum(row["audit_fail"] for row in details),
        },
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 2),
        "cases": details,
    }


async def main(args: argparse.Namespace) -> int:
    cases = _read_cases(args.cases)
    catalog = SkillsShCatalog()
    try:
        report = await _evaluate(catalog, cases, max(1, min(args.limit, 12)))
    except SkillsShCatalogError as exc:
        print(json.dumps({"status": "skipped", "reason": str(exc), "case_count": len(cases)}, indent=2))
        return 0
    output = json.dumps(report, indent=2)
    if args.output:
        args.output.write_text(output + "\n", encoding="utf-8")
    print(output)
    return 0


def cli() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cases", type=Path, required=True)
    parser.add_argument("--limit", type=int, default=5)
    parser.add_argument("--output", type=Path)
    return asyncio.run(main(parser.parse_args()))


if __name__ == "__main__":
    raise SystemExit(cli())
