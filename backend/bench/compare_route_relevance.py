"""Compare two completed blinded route-relevance judgment artifacts.

The comparison is paired by public benchmark task.  Candidate labels within a
task are not treated as independent observations: confidence intervals for
candidate precision resample whole task clusters.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import math
import os
import random
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


LABELS = {"E", "A", "I"}
REQUIRED_JUDGE_METADATA = ("model", "reasoning_effort", "version", "executable_sha256")
HEX_DIGITS = frozenset("0123456789abcdef")
MAX_SOURCE_REPLAY_BYTES = 128 * 1024 * 1024


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_artifact(path: Path) -> tuple[dict[str, Any], dict[str, Any]]:
    raw = path.read_bytes()
    try:
        artifact = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{path}: invalid JSON: {exc}") from exc
    if not isinstance(artifact, dict):
        raise ValueError(f"{path}: artifact must be a JSON object")
    return artifact, {"path": str(path.resolve()), "sha256": _sha256(raw)}


def _validate_artifact(artifact: dict[str, Any], label: str) -> dict[str, Any]:
    if not artifact.get("completed_at"):
        raise ValueError(f"{label}: artifact is not completed")

    evaluation = artifact.get("evaluation")
    if not isinstance(evaluation, dict):
        raise ValueError(f"{label}: evaluation metadata is missing")
    top_k = evaluation.get("top_k")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k < 5:
        raise ValueError(f"{label}: evaluation.top_k must be an integer of at least 5")
    judge = evaluation.get("judge")
    if not isinstance(judge, dict):
        raise ValueError(f"{label}: evaluation.judge metadata is missing")
    missing_metadata = [key for key in REQUIRED_JUDGE_METADATA if not judge.get(key)]
    if missing_metadata:
        raise ValueError(
            f"{label}: judge metadata missing {', '.join(missing_metadata)}"
        )

    tasks = artifact.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise ValueError(f"{label}: tasks must be a non-empty array")
    by_id: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(tasks):
        if not isinstance(task, dict):
            raise ValueError(f"{label}: task {index} is not an object")
        task_id = str(task.get("id") or "").strip()
        if not task_id or task_id in by_id:
            raise ValueError(f"{label}: task {index} has a missing or duplicate id")
        if task.get("status") != "judged":
            raise ValueError(f"{label}: task {task_id} is not successfully judged")
        instruction_hash = task.get("instruction_sha256")
        if (
            not isinstance(instruction_hash, str)
            or len(instruction_hash) != 64
            or not set(instruction_hash.lower()) <= HEX_DIGITS
        ):
            raise ValueError(f"{label}: task {task_id} has no instruction_sha256")
        candidates = task.get("candidates")
        judgment = task.get("judgment")
        labels = judgment.get("labels") if isinstance(judgment, dict) else None
        if not isinstance(candidates, list) or not candidates:
            raise ValueError(f"{label}: task {task_id} has no candidates")
        if len(candidates) > top_k:
            raise ValueError(f"{label}: task {task_id} has more than top_k candidates")
        if not isinstance(labels, list) or len(labels) != len(candidates):
            raise ValueError(
                f"{label}: task {task_id} must have one label per candidate"
            )
        expected_ranks = list(range(1, len(labels) + 1))
        ranks = [item.get("rank") if isinstance(item, dict) else None for item in labels]
        if ranks != expected_ranks:
            raise ValueError(f"{label}: task {task_id} labels are not rank ordered")
        if any(item.get("label") not in LABELS for item in labels):
            raise ValueError(f"{label}: task {task_id} has an invalid relevance label")
        by_id[task_id] = task

    summary = artifact.get("summary")
    if not isinstance(summary, dict):
        raise ValueError(f"{label}: summary is missing")
    if summary.get("tasks_failed") != 0:
        raise ValueError(f"{label}: summary reports failed tasks")
    if summary.get("tasks_total") != len(tasks) or summary.get("tasks_judged") != len(tasks):
        raise ValueError(f"{label}: summary task counts do not match completed tasks")
    return {"top_k": top_k, "judge": judge, "tasks": by_id}


def _validate_pair(control: dict[str, Any], treatment: dict[str, Any]) -> list[str]:
    if control["top_k"] != treatment["top_k"]:
        raise ValueError("artifacts use different top_k values")
    control_judge = tuple(control["judge"][key] for key in REQUIRED_JUDGE_METADATA)
    treatment_judge = tuple(treatment["judge"][key] for key in REQUIRED_JUDGE_METADATA)
    if control_judge != treatment_judge:
        raise ValueError("artifacts use different judge model metadata")
    control_ids = set(control["tasks"])
    treatment_ids = set(treatment["tasks"])
    if control_ids != treatment_ids:
        missing = sorted(control_ids - treatment_ids)
        extra = sorted(treatment_ids - control_ids)
        raise ValueError(
            f"artifacts have different task ids (missing={missing}, extra={extra})"
        )
    task_ids = sorted(control_ids)
    for task_id in task_ids:
        if (
            control["tasks"][task_id]["instruction_sha256"]
            != treatment["tasks"][task_id]["instruction_sha256"]
        ):
            raise ValueError(f"task {task_id} has different instruction_sha256 values")
    return task_ids


def _load_source_replay(
    artifact: dict[str, Any],
    validated: dict[str, Any],
    task_ids: list[str],
    label: str,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Load the exact replay bytes named by a judgment artifact.

    The digest is checked before parsing, and parsing uses those same bytes so a
    changed file cannot be silently paired with the judgments.
    """

    source = artifact.get("source_replay")
    if not isinstance(source, dict):
        raise ValueError(f"{label}: source_replay metadata is missing")
    path_value = source.get("path")
    claimed_sha256 = source.get("sha256")
    if not isinstance(path_value, str) or not path_value.strip():
        raise ValueError(f"{label}: source_replay.path is missing")
    if not Path(path_value).is_absolute():
        raise ValueError(f"{label}: source_replay.path must be absolute")
    if (
        not isinstance(claimed_sha256, str)
        or len(claimed_sha256) != 64
        or not set(claimed_sha256.lower()) <= HEX_DIGITS
    ):
        raise ValueError(f"{label}: source_replay.sha256 is invalid")
    try:
        path = Path(path_value).resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"{label}: source replay cannot be resolved: {exc}") from exc
    if not path.is_file():
        raise ValueError(f"{label}: source replay is not a regular file")
    size = path.stat().st_size
    if size > MAX_SOURCE_REPLAY_BYTES:
        raise ValueError(f"{label}: source replay exceeds the safe size limit")
    raw = path.read_bytes()
    if len(raw) > MAX_SOURCE_REPLAY_BYTES:
        raise ValueError(f"{label}: source replay exceeds the safe size limit")
    actual_sha256 = _sha256(raw)
    if not hmac.compare_digest(actual_sha256, claimed_sha256.lower()):
        raise ValueError(f"{label}: source_replay.sha256 does not match the file")
    try:
        replay = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{label}: source replay is invalid JSON: {exc}") from exc
    if not isinstance(replay, dict) or not isinstance(replay.get("tasks"), list):
        raise ValueError(f"{label}: source replay must contain a tasks array")

    replay_tasks: dict[str, dict[str, Any]] = {}
    for index, task in enumerate(replay["tasks"]):
        if not isinstance(task, dict):
            raise ValueError(f"{label}: source replay task {index} is not an object")
        task_id = str(task.get("id") or "").strip()
        if not task_id or task_id in replay_tasks:
            raise ValueError(
                f"{label}: source replay task {index} has a missing or duplicate id"
            )
        replay_tasks[task_id] = task
    if set(replay_tasks) != set(task_ids):
        raise ValueError(f"{label}: source replay task ids do not match judgments")

    tiers: dict[str, str] = {}
    for task_id in task_ids:
        replay_task = replay_tasks[task_id]
        judged_task = validated["tasks"][task_id]
        instruction = replay_task.get("instruction")
        if not isinstance(instruction, str) or not instruction.strip():
            raise ValueError(f"{label}: source replay task {task_id} has no instruction")
        instruction_sha256 = _sha256(instruction.encode("utf-8"))
        if replay_task.get("instruction_sha256") != instruction_sha256:
            raise ValueError(
                f"{label}: source replay task {task_id} instruction digest is invalid"
            )
        if judged_task["instruction_sha256"] != instruction_sha256:
            raise ValueError(
                f"{label}: source replay task {task_id} does not match its judgment"
            )
        if replay_task.get("status_code") != 200:
            raise ValueError(f"{label}: source replay task {task_id} was not successful")
        tier = str(replay_task.get("tier") or "none")
        if tier not in {"none", "hint", "full"}:
            raise ValueError(f"{label}: source replay task {task_id} has invalid tier {tier!r}")
        results = replay_task.get("results")
        if not isinstance(results, list):
            raise ValueError(f"{label}: source replay task {task_id} has no results array")
        expected_hashes = [candidate.get("content_hash") for candidate in judged_task["candidates"]]
        source_hashes = [
            (str(candidate.get("content_hash")).lower() if candidate.get("content_hash") else None)
            for candidate in results[: validated["top_k"]]
            if isinstance(candidate, dict)
        ]
        if source_hashes != expected_hashes:
            raise ValueError(
                f"{label}: source replay task {task_id} candidates do not match judgments"
            )
        tiers[task_id] = tier
    return tiers, {"path": str(path), "sha256": actual_sha256, "bytes": len(raw)}


def exact_mcnemar_p(wins: int, losses: int) -> float:
    """Return the exact two-sided binomial McNemar p-value."""

    if wins < 0 or losses < 0:
        raise ValueError("wins and losses must be non-negative")
    discordant = wins + losses
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(wins, losses) + 1))
    return min(1.0, 2.0 * tail / (2**discordant))


def _percentile(values: list[float], probability: float) -> float:
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    lower = math.floor(position)
    upper = math.ceil(position)
    if lower == upper:
        return ordered[lower]
    fraction = position - lower
    return ordered[lower] * (1.0 - fraction) + ordered[upper] * fraction


def _paired_bootstrap(
    pairs: list[tuple[Any, Any]],
    difference: Callable[[list[tuple[Any, Any]]], float],
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    if replicates < 1:
        raise ValueError("bootstrap_replicates must be positive")
    rng = random.Random(seed)
    count = len(pairs)
    draws = [
        difference([pairs[rng.randrange(count)] for _ in range(count)])
        for _ in range(replicates)
    ]
    return {
        "method": "paired_percentile_bootstrap",
        "resampling_unit": "task_cluster",
        "replicates": replicates,
        "seed": seed,
        "low": round(_percentile(draws, 0.025), 6),
        "high": round(_percentile(draws, 0.975), 6),
    }


def _binary_metric(
    pairs: list[tuple[bool, bool]], replicates: int, seed: int
) -> dict[str, Any]:
    total = len(pairs)
    control_hits = sum(control for control, _ in pairs)
    treatment_hits = sum(treatment for _, treatment in pairs)
    wins = sum(treatment and not control for control, treatment in pairs)
    losses = sum(control and not treatment for control, treatment in pairs)
    ties = total - wins - losses

    def difference(sample: list[tuple[bool, bool]]) -> float:
        return sum(treatment - control for control, treatment in sample) / len(sample)

    return {
        "control": {"hits": control_hits, "total": total, "rate": control_hits / total},
        "treatment": {
            "hits": treatment_hits,
            "total": total,
            "rate": treatment_hits / total,
        },
        "difference": treatment_hits / total - control_hits / total,
        "paired": {
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "exact_two_sided_mcnemar_p": exact_mcnemar_p(wins, losses),
            "difference_ci95": _paired_bootstrap(pairs, difference, replicates, seed),
        },
    }


def _candidate_metric(
    pairs: list[tuple[tuple[int, int], tuple[int, int]]],
    replicates: int,
    seed: int,
) -> dict[str, Any]:
    def aggregate(arm: int, sample: list[tuple[tuple[int, int], tuple[int, int]]]) -> tuple[int, int]:
        return sum(item[arm][0] for item in sample), sum(item[arm][1] for item in sample)

    control_exact, control_total = aggregate(0, pairs)
    treatment_exact, treatment_total = aggregate(1, pairs)

    def difference(sample: list[tuple[tuple[int, int], tuple[int, int]]]) -> float:
        c_exact, c_total = aggregate(0, sample)
        t_exact, t_total = aggregate(1, sample)
        return t_exact / t_total - c_exact / c_total

    task_wins = task_losses = task_ties = 0
    for (c_exact, c_total), (t_exact, t_total) in pairs:
        delta = t_exact / t_total - c_exact / c_total
        task_wins += delta > 0
        task_losses += delta < 0
        task_ties += delta == 0
    return {
        "control": {
            "exact": control_exact,
            "candidates": control_total,
            "precision": control_exact / control_total,
        },
        "treatment": {
            "exact": treatment_exact,
            "candidates": treatment_total,
            "precision": treatment_exact / treatment_total,
        },
        "difference": treatment_exact / treatment_total - control_exact / control_total,
        "paired_task_clusters": {
            "wins": task_wins,
            "losses": task_losses,
            "ties": task_ties,
            "difference_ci95": _paired_bootstrap(pairs, difference, replicates, seed),
        },
        "inference_note": (
            "Candidate labels within a task are not independent; the confidence "
            "interval resamples paired task clusters."
        ),
    }


def _usage(tasks: dict[str, dict[str, Any]], task_ids: list[str]) -> dict[str, Any] | None:
    usages = []
    for task_id in task_ids:
        execution = tasks[task_id].get("codex_execution")
        usage = execution.get("usage") if isinstance(execution, dict) else None
        if isinstance(usage, dict):
            usages.append(usage)
    if not usages:
        return None
    numeric_keys = sorted(
        {key for usage in usages for key, value in usage.items() if isinstance(value, (int, float)) and not isinstance(value, bool)}
    )
    return {
        "tasks_with_usage": len(usages),
        "tasks_total": len(task_ids),
        "totals": {key: sum(usage.get(key, 0) for usage in usages) for key in numeric_keys},
    }


def _ratio(numerator: int, denominator: int) -> dict[str, Any]:
    return {
        "numerator": numerator,
        "denominator": denominator,
        "estimate": numerator / denominator if denominator else None,
    }


def _gate_arm(
    tiers: dict[str, str],
    labels_by_task: dict[str, list[str]],
    task_ids: list[str],
) -> tuple[dict[str, Any], list[bool], list[bool]]:
    surfaced = [tiers[task_id] in {"hint", "full"} for task_id in task_ids]
    exact_top1 = [labels_by_task[task_id][0] == "E" for task_id in task_ids]
    any_exact_top5 = ["E" in labels_by_task[task_id][:5] for task_id in task_ids]
    routed_top1 = [route and exact for route, exact in zip(surfaced, exact_top1, strict=True)]
    routed_top5 = [
        route and exact for route, exact in zip(surfaced, any_exact_top5, strict=True)
    ]
    surfaced_count = sum(surfaced)
    routed_top1_count = sum(routed_top1)
    routed_top5_count = sum(routed_top5)
    any_exact_count = sum(any_exact_top5)
    false_positives = sum(
        route and not exact
        for route, exact in zip(surfaced, any_exact_top5, strict=True)
    )
    return (
        {
            "surfaced_task_count": {
                "count": surfaced_count,
                "total": len(task_ids),
                "rate": surfaced_count / len(task_ids),
            },
            "routed_exact_top1_hits": {
                "hits": routed_top1_count,
                "total": len(task_ids),
                "rate": routed_top1_count / len(task_ids),
            },
            "routed_any_exact_top5_hits": {
                "hits": routed_top5_count,
                "total": len(task_ids),
                "rate": routed_top5_count / len(task_ids),
            },
            "route_precision_among_surfaced_tasks": _ratio(
                routed_top5_count, surfaced_count
            ),
            "recall_of_any_exact_tasks": _ratio(routed_top5_count, any_exact_count),
            "false_positive_surfaced_tasks": {
                "count": false_positives,
                "surfaced_tasks": surfaced_count,
                "rate_among_surfaced": (
                    false_positives / surfaced_count if surfaced_count else None
                ),
            },
        },
        routed_top1,
        routed_top5,
    )


def compare(
    control_artifact: dict[str, Any],
    treatment_artifact: dict[str, Any],
    *,
    bootstrap_replicates: int = 10_000,
    seed: int = 20260721,
) -> dict[str, Any]:
    control = _validate_artifact(control_artifact, "control")
    treatment = _validate_artifact(treatment_artifact, "treatment")
    task_ids = _validate_pair(control, treatment)
    control_tiers, control_source = _load_source_replay(
        control_artifact, control, task_ids, "control"
    )
    treatment_tiers, treatment_source = _load_source_replay(
        treatment_artifact, treatment, task_ids, "treatment"
    )

    def labels(task: dict[str, Any]) -> list[str]:
        return [item["label"] for item in task["judgment"]["labels"]]

    control_labels = {task_id: labels(control["tasks"][task_id]) for task_id in task_ids}
    treatment_labels = {
        task_id: labels(treatment["tasks"][task_id]) for task_id in task_ids
    }
    top1_pairs = [
        (control_labels[task_id][0] == "E", treatment_labels[task_id][0] == "E")
        for task_id in task_ids
    ]
    top5_pairs = [
        (
            "E" in control_labels[task_id][:5],
            "E" in treatment_labels[task_id][:5],
        )
        for task_id in task_ids
    ]
    candidate_pairs = [
        (
            (control_labels[task_id].count("E"), len(control_labels[task_id])),
            (treatment_labels[task_id].count("E"), len(treatment_labels[task_id])),
        )
        for task_id in task_ids
    ]
    control_gate, control_routed_top1, control_routed_top5 = _gate_arm(
        control_tiers, control_labels, task_ids
    )
    treatment_gate, treatment_routed_top1, treatment_routed_top5 = _gate_arm(
        treatment_tiers, treatment_labels, task_ids
    )
    routed_top1_pairs = list(zip(control_routed_top1, treatment_routed_top1, strict=True))
    routed_top5_pairs = list(zip(control_routed_top5, treatment_routed_top5, strict=True))
    control_usage = _usage(control["tasks"], task_ids)
    treatment_usage = _usage(treatment["tasks"], task_ids)
    token_usage: dict[str, Any] | None = None
    if control_usage is not None or treatment_usage is not None:
        token_usage = {"control": control_usage, "treatment": treatment_usage}
        if control_usage is not None and treatment_usage is not None:
            keys = sorted(set(control_usage["totals"]) | set(treatment_usage["totals"]))
            token_usage["difference"] = {
                key: treatment_usage["totals"].get(key, 0)
                - control_usage["totals"].get(key, 0)
                for key in keys
            }

    return {
        "validation": {
            "tasks": len(task_ids),
            "top_k": control["top_k"],
            "judge": {key: control["judge"][key] for key in REQUIRED_JUDGE_METADATA},
            "all_tasks_completed_without_failure": True,
            "task_ids_and_instruction_hashes_match": True,
            "source_replays": {
                "control": control_source,
                "treatment": treatment_source,
            },
        },
        "metrics": {
            "exact_top1_task_hit": _binary_metric(top1_pairs, bootstrap_replicates, seed),
            "any_exact_top5_task_hit": _binary_metric(top5_pairs, bootstrap_replicates, seed + 1),
            "exact_candidate_precision": _candidate_metric(
                candidate_pairs, bootstrap_replicates, seed + 2
            ),
            "gate_aware_routing": {
                "surfaced_definition": "tier is hint or full; tier none is not surfaced",
                "control": control_gate,
                "treatment": treatment_gate,
                "paired_routed_hit_outcomes": {
                    "routed_exact_top1_hit": _binary_metric(
                        routed_top1_pairs, bootstrap_replicates, seed + 3
                    ),
                    "routed_any_exact_top5_hit": _binary_metric(
                        routed_top5_pairs, bootstrap_replicates, seed + 4
                    ),
                },
                "interpretation_note": (
                    "A historical full tier is counted as surfaced for both arms. "
                    "This does not establish prompt injection: current safety policy "
                    "downgrades unvalidated public full routes to hint."
                ),
            },
        },
        "token_usage": token_usage,
    }


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(value, indent=2, sort_keys=True) + "\n"
    fd, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(data)
        Path(temporary).replace(path)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--control", type=Path, required=True)
    parser.add_argument("--treatment", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--bootstrap-replicates", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=20260721)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    control, control_source = _read_artifact(args.control)
    treatment, treatment_source = _read_artifact(args.treatment)
    result = {
        "schema_version": 1,
        "created_at": _utc_now(),
        "sources": {"control": control_source, "treatment": treatment_source},
        "comparison": compare(
            control,
            treatment,
            bootstrap_replicates=args.bootstrap_replicates,
            seed=args.seed,
        ),
    }
    _write_json(args.output, result)
    print(json.dumps(result["comparison"]["metrics"], indent=2, sort_keys=True))
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
