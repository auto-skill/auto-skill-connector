#!/usr/bin/env python3
"""Paired, reproducible evaluation gates for the evidence-backed router.

The query gate is an inexpensive offline intent-retrieval proxy.  It uses a
pre-existing task file, assigns the held-out split from a hash of public prompt
text, and exposes only prompt text to the compiler.  ``route_query`` is used
solely as the identity-blinded candidate record and post-retrieval target.

The outcome gate consumes replicated task results from real agent runs.  It
compares no-skill, raw-skill, and distilled-capsule conditions only on tasks
where the no-skill control is not already perfect.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import hashlib
import json
import math
from pathlib import Path
import random
import re
import sqlite3
from statistics import mean
import sys
from typing import Any, Iterable


BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from query_compiler import COMPILER_VERSION, compile_intent_query  # noqa: E402
from embeddings import embed_text_hash  # noqa: E402
from retrieval_records import embedding_parity  # noqa: E402


EVAL_VERSION = "evidence-gates-v1"
CONDITIONS = ("no-skill", "raw-skill", "distilled-capsule")
TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9+#.-]*", re.I)


def _tokens(value: str) -> list[str]:
    return [token.casefold() for token in TOKEN_RE.findall(value or "") if len(token) > 1]


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _split(prompt: str) -> str:
    # Stable before compilation and independent of task IDs/labels.
    bucket = int(hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8], 16) % 10
    return "heldout" if bucket < 4 else "development"


def _bm25_rank(query: str, records: list[str]) -> list[int]:
    tokenized = [_tokens(record) for record in records]
    query_tokens = _tokens(query)
    lengths = [len(tokens) for tokens in tokenized]
    average_length = mean(lengths) if lengths else 1.0
    document_frequency: Counter[str] = Counter()
    for tokens in tokenized:
        document_frequency.update(set(tokens))
    count = max(1, len(records))
    scores: list[tuple[float, int]] = []
    for index, tokens in enumerate(tokenized):
        frequencies = Counter(tokens)
        score = 0.0
        for token in query_tokens:
            frequency = frequencies[token]
            if not frequency:
                continue
            idf = math.log(1 + (count - document_frequency[token] + 0.5) / (document_frequency[token] + 0.5))
            denominator = frequency + 1.2 * (1 - 0.75 + 0.75 * len(tokens) / max(1, average_length))
            score += idf * (frequency * 2.2 / denominator)
        scores.append((score, index))
    return [index for _score, index in sorted(scores, key=lambda item: (-item[0], item[1]))]


def _rrf(rankings: Iterable[list[int]], k: int = 60) -> list[int]:
    scores: defaultdict[int, float] = defaultdict(float)
    best_rank: dict[int, int] = {}
    for ranking in rankings:
        for rank, index in enumerate(ranking, 1):
            scores[index] += 1.0 / (k + rank)
            best_rank[index] = min(rank, best_rank.get(index, rank))
    return sorted(scores, key=lambda index: (-scores[index], best_rank[index], index))


def _bootstrap_delta(values: list[float], *, samples: int = 10_000, seed: int = 20260728) -> list[float]:
    if not values:
        return [0.0, 0.0]
    rng = random.Random(seed)
    estimates = sorted(mean(rng.choice(values) for _ in values) for _ in range(samples))
    return [estimates[int(samples * 0.025)], estimates[min(samples - 1, int(samples * 0.975))]]


def _mcnemar_exact(wins: int, losses: int) -> float:
    discordant = wins + losses
    if not discordant:
        return 1.0
    tail = sum(math.comb(discordant, value) for value in range(0, min(wins, losses) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def _wilson(successes: int, total: int, z: float = 1.96) -> list[float]:
    if total <= 0:
        return [0.0, 0.0]
    proportion = successes / total
    denominator = 1 + z * z / total
    center = (proportion + z * z / (2 * total)) / denominator
    radius = z * math.sqrt((proportion * (1 - proportion) + z * z / (4 * total)) / total) / denominator
    return [max(0.0, center - radius), min(1.0, center + radius)]


def evaluate_query_compiler(path: Path) -> dict[str, Any]:
    rows = _load_jsonl(path)
    heldout = [row for row in rows if _split(str(row.get("prompt") or "")) == "heldout"]
    records = [str(row.get("route_query") or "") for row in heldout]
    task_rows: list[dict[str, Any]] = []
    for target, row in enumerate(heldout):
        prompt = str(row.get("prompt") or "")
        intent = compile_intent_query(prompt)
        baseline = _bm25_rank(intent.original_query, records)
        treatment = _rrf(_bm25_rank(query, records) for query in intent.query_variants)
        baseline_rank = baseline.index(target) + 1
        treatment_rank = treatment.index(target) + 1
        task_rows.append(
            {
                "task_key": hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12],
                "baseline_rank": baseline_rank,
                "compiled_rank": treatment_rank,
                "baseline_top1": baseline_rank == 1,
                "compiled_top1": treatment_rank == 1,
                "baseline_top5": baseline_rank <= 5,
                "compiled_top5": treatment_rank <= 5,
                "query_variant_count": len(intent.query_variants),
            }
        )

    def metric(name: str) -> dict[str, Any]:
        baseline = [bool(row[f"baseline_{name}"]) for row in task_rows]
        compiled = [bool(row[f"compiled_{name}"]) for row in task_rows]
        wins = sum(not left and right for left, right in zip(baseline, compiled))
        losses = sum(left and not right for left, right in zip(baseline, compiled))
        deltas = [float(right) - float(left) for left, right in zip(baseline, compiled)]
        return {
            "baseline": sum(baseline),
            "compiled": sum(compiled),
            "total": len(task_rows),
            "delta_pp": 100 * mean(deltas) if deltas else 0.0,
            "paired_wins": wins,
            "paired_losses": losses,
            "paired_ties": len(task_rows) - wins - losses,
            "paired_bootstrap_95ci_pp": [100 * value for value in _bootstrap_delta(deltas)],
            "mcnemar_exact_p": _mcnemar_exact(wins, losses),
        }

    top5 = metric("top5")
    surfaced = top5["compiled"]
    precision = surfaced / len(task_rows) if task_rows else 0.0
    return {
        "eval_version": EVAL_VERSION,
        "evaluation": "heldout-intent-retrieval-proxy",
        "hypothesis": "dual original+structured queries improve paired target retrieval",
        "control": "BM25 over original public prompt only",
        "treatment": "RRF over original and structured compiled public queries",
        "compiler_version": COMPILER_VERSION,
        "dataset": {"path": str(path), "sha256": _sha256(path), "all_rows": len(rows)},
        "split": {
            "rule": "sha256(public_prompt) modulo 10 < 4",
            "heldout_rows": len(heldout),
            "compiler_inputs": ["public prompt text"],
            "compiler_forbidden_inputs": ["task id", "route_query target", "candidate labels"],
        },
        "metrics": {"top1": metric("top1"), "top5": top5},
        "surfaced_precision_proxy": {
            "value": precision,
            "wilson_95ci": _wilson(surfaced, len(task_rows)),
            "note": "Target-record precision in a small offline proxy, not production route precision.",
        },
        "tasks": task_rows,
    }


def evaluate_outcomes(path: Path) -> dict[str, Any]:
    rows = _load_jsonl(path)
    grouped: defaultdict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        condition = str(row.get("condition") or "")
        if condition not in CONDITIONS:
            raise ValueError(f"unknown condition: {condition}")
        grouped[(str(row["task_id"]), condition)].append(row)
    task_ids = sorted({task_id for task_id, _condition in grouped})
    eligible = [
        task_id
        for task_id in task_ids
        if grouped[(task_id, "no-skill")]
        and mean(float(row.get("pass", 0)) for row in grouped[(task_id, "no-skill")]) < 1.0
    ]
    incomplete = [
        task_id
        for task_id in eligible
        if any(len(grouped[(task_id, condition)]) < 2 for condition in CONDITIONS)
    ]
    if incomplete:
        raise ValueError(f"at least two replicates per condition required: {incomplete[:10]}")

    task_means: dict[str, dict[str, float]] = {}
    for task_id in eligible:
        task_means[task_id] = {
            condition: mean(float(row.get("pass", 0)) for row in grouped[(task_id, condition)])
            for condition in CONDITIONS
        }

    def condition_summary(condition: str) -> dict[str, Any]:
        values = [row for task_id in eligible for row in grouped[(task_id, condition)]]
        passes = [float(row.get("pass", 0)) for row in values]
        return {
            "replicates": len(values),
            "pass_rate": mean(passes) if passes else 0.0,
            "pass_rate_bootstrap_95ci": _bootstrap_delta(passes),
            "cost_usd_mean": mean(float(row.get("cost_usd", 0)) for row in values) if values else 0.0,
            "latency_s_mean": mean(float(row.get("latency_s", 0)) for row in values) if values else 0.0,
            "tokens_mean": mean(
                float(row.get("input_tokens", 0)) + float(row.get("output_tokens", 0)) for row in values
            ) if values else 0.0,
            "safety_failures": sum(int(row.get("safety_failures", 0)) for row in values),
            "strategy_displacements": sum(int(row.get("strategy_displacement", 0)) for row in values),
        }

    def paired(condition: str) -> dict[str, Any]:
        deltas = [task_means[task_id][condition] - task_means[task_id]["no-skill"] for task_id in eligible]
        wins = sum(delta > 0 for delta in deltas)
        losses = sum(delta < 0 for delta in deltas)
        return {
            "vs": "no-skill",
            "task_level_wins": wins,
            "task_level_losses": losses,
            "task_level_ties": len(deltas) - wins - losses,
            "mean_pass_delta_pp": 100 * mean(deltas) if deltas else 0.0,
            "paired_bootstrap_95ci_pp": [100 * value for value in _bootstrap_delta(deltas)],
            "mcnemar_sign_exact_p": _mcnemar_exact(wins, losses),
        }

    return {
        "eval_version": EVAL_VERSION,
        "evaluation": "replicated-agent-outcomes",
        "hypothesis": "distilled capsules improve pass rate without raw-skill safety or displacement cost",
        "control": "no-skill",
        "dataset": {"path": str(path), "sha256": _sha256(path), "rows": len(rows)},
        "eligible_tasks": len(eligible),
        "excluded_control_perfect_tasks": len(task_ids) - len(eligible),
        "conditions": {condition: condition_summary(condition) for condition in CONDITIONS},
        "paired": {condition: paired(condition) for condition in ("raw-skill", "distilled-capsule")},
        "task_means": task_means,
    }


def evaluate_corpus_vector_parity(path: Path) -> dict[str, Any]:
    connection = sqlite3.connect(f"file:{path.resolve().as_posix()}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    try:
        rows = [
            dict(row)
            for row in connection.execute(
                """
                SELECT id, url, retrieval_text, embedding_text_hash
                FROM skills
                WHERE quality_status='active'
                ORDER BY id
                """
            ).fetchall()
        ]
    finally:
        connection.close()
    result = embedding_parity(rows, hash_builder=embed_text_hash)
    return {
        "eval_version": EVAL_VERSION,
        "evaluation": "v6-corpus-vector-parity",
        "hypothesis": "all active v6 retrieval records match the text used for their vectors",
        "control": "exact per-row embedding_text_hash equality; missing rows fail",
        "database": {"path": str(path), "sha256": _sha256(path)},
        **result,
        "production_attribution_allowed": result["parity"],
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command", required=True)
    query = subparsers.add_parser("query")
    query.add_argument("--tasks", type=Path, default=Path(__file__).with_name("tasks.jsonl"))
    query.add_argument("--output", type=Path)
    outcomes = subparsers.add_parser("outcomes")
    outcomes.add_argument("results", type=Path)
    outcomes.add_argument("--output", type=Path)
    parity = subparsers.add_parser("parity")
    parity.add_argument("database", type=Path)
    parity.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.command == "query":
        result = evaluate_query_compiler(args.tasks)
    elif args.command == "outcomes":
        result = evaluate_outcomes(args.results)
    else:
        result = evaluate_corpus_vector_parity(args.database)
    rendered = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
