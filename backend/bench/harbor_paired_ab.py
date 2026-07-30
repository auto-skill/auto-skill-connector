"""Run and aggregate a reproducible paired Harbor A/B experiment.

The checked-in experiment definition freezes the dataset, model, task set, and
treatment capsules.  Each task/replicate/arm cell is a separate Harbor job so
an infrastructure retry cannot silently become an additional scored sample.

Examples (planning is read-only with respect to Harbor)::

    python backend/bench/harbor_paired_ab.py plan --output C:\\tmp\\autoskill-ab
    python backend/bench/harbor_paired_ab.py run --output C:\\tmp\\autoskill-ab
    python backend/bench/harbor_paired_ab.py aggregate --output C:\\tmp\\autoskill-ab
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


DEFAULT_DEFINITION = Path(__file__).with_name("harbor_paired_ab.json")
RUN_MANIFEST_NAME = "experiment-manifest.json"
SUMMARY_NAME = "paired-results.json"

# Harbor exception names are not a stable public API.  Keep the allowlist
# intentionally narrow; reward-zero trials and ordinary agent failures are
# experimental outcomes, not retries.
INFRA_EXCEPTION_FRAGMENTS = (
    "DockerCompose",
    "EnvironmentBuild",
    "EnvironmentStart",
    "EnvironmentStop",
    "TaskDownload",
    "TaskNotFound",
    "AgentSetupTimeout",
    "VerifierSetup",
)
TRANSIENT_MESSAGE_FRAGMENTS = (
    "401 Unauthorized",
    "Incorrect API key",
    "429 Too Many Requests",
    "502 Bad Gateway",
    "503 Service Unavailable",
    "504 Gateway Timeout",
    "connection reset",
    "connection refused",
    "connection timed out",
    "temporary failure in name resolution",
)


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _read_json(path: Path) -> dict[str, Any]:
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"expected a JSON object in {path}")
    return value


def _comparison_arm(definition: dict[str, Any]) -> str:
    arms = definition.get("arms") or []
    if (
        not isinstance(arms, list)
        or len(arms) != 2
        or arms[0] != "control"
        or not isinstance(arms[1], str)
        or not arms[1]
        or arms[1] == "control"
    ):
        raise ValueError("arms must contain 'control' followed by one named comparison arm")
    return arms[1]


def _task_instruction_fields(task: dict[str, Any]) -> tuple[Path, str]:
    relative_value = task.get("instruction") or task.get("capsule") or ""
    digest = str(task.get("instruction_sha256") or task.get("capsule_sha256") or "")
    return Path(str(relative_value)), digest


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def load_definition(path: Path) -> dict[str, Any]:
    """Load and strictly validate an experiment definition and its capsules."""

    definition = _read_json(path)
    if definition.get("schema_version") != 1:
        raise ValueError("unsupported experiment schema_version")
    if definition.get("status", "ready") != "ready":
        raise ValueError(
            f"experiment definition is not ready: {definition.get('status')}; "
            "resolve the documented design blocker and set status to 'ready'"
        )
    replicates = definition.get("replicates")
    if not isinstance(replicates, int) or isinstance(replicates, bool) or replicates < 1:
        raise ValueError("replicates must be a positive integer")
    comparison_arm = _comparison_arm(definition)

    dataset = definition.get("dataset") or {}
    if not str(dataset.get("ref", "")).startswith("sha256:"):
        raise ValueError("dataset.ref must be a pinned sha256 digest")
    agent = definition.get("agent") or {}
    if agent.get("name") != "codex" or agent.get("model") != "gpt-5.6-sol":
        raise ValueError("the experiment must use codex with gpt-5.6-sol")
    if agent.get("reasoning_effort") != "max":
        raise ValueError("the experiment must use max reasoning effort")
    if agent.get("reasoning_summary") != "none" or agent.get("web_search") != "disabled":
        raise ValueError("reasoning summaries and web search must be disabled for the experiment")
    if not str(agent.get("version") or ""):
        raise ValueError("the Codex CLI version must be pinned")

    tasks = definition.get("tasks")
    required_task_count = int(definition.get("required_task_count", len(tasks) if isinstance(tasks, list) else 0))
    if not isinstance(tasks, list) or required_task_count < 2 or len(tasks) != required_task_count:
        raise ValueError(f"the controlled design requires exactly {required_task_count} tasks")
    names: set[str] = set()
    for task in tasks:
        name = str(task.get("name", ""))
        if not name.startswith("terminal-bench/") or name in names:
            raise ValueError(f"invalid or duplicate task name: {name!r}")
        names.add(name)
        relative, expected = _task_instruction_fields(task)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"instruction must be relative to the definition: {relative}")
        instruction = (path.parent / relative).resolve()
        if not instruction.is_file():
            raise ValueError(f"missing frozen instruction: {instruction}")
        body = instruction.read_bytes()
        actual = _sha256_bytes(body)
        if actual != expected:
            raise ValueError(f"instruction digest mismatch for {name}: {actual} != {expected}")
        if not body.strip():
            raise ValueError(f"empty comparison instruction for {name}")
        if comparison_arm == "oracle_skill":
            for field in ("skill_id", "skill_name", "source_content_hash", "selection_rank"):
                if not task.get(field):
                    raise ValueError(f"oracle task {name} is missing {field}")
            if len(str(task["source_content_hash"])) != 64:
                raise ValueError(f"oracle task {name} has invalid source_content_hash")
            if int(task["selection_rank"]) not in range(1, 6):
                raise ValueError(f"oracle task {name} has invalid selection_rank")

    return definition


def generate_schedule(definition: dict[str, Any]) -> list[dict[str, Any]]:
    """Generate seeded randomized blocks, preserving pairs within each block."""

    rng = random.Random(int(definition["random_seed"]))
    schedule: list[dict[str, Any]] = []
    sequence = 0
    for replicate in range(1, int(definition["replicates"]) + 1):
        tasks = list(definition["tasks"])
        rng.shuffle(tasks)
        for task in tasks:
            arms = list(definition["arms"])
            rng.shuffle(arms)
            short_name = str(task["name"]).split("/", 1)[1]
            relative, instruction_sha256 = _task_instruction_fields(task)
            for arm in arms:
                sequence += 1
                schedule.append(
                    {
                        "sequence": sequence,
                        "replicate": replicate,
                        "task": task["name"],
                        "task_slug": short_name,
                        "arm": arm,
                        "cell_id": f"r{replicate:02d}-{short_name}-{arm}",
                        "instruction": str(relative) if arm != "control" else None,
                        "instruction_sha256": instruction_sha256 if arm != "control" else None,
                        "instruction_kind": arm if arm != "control" else None,
                    }
                )
    return schedule


def create_manifest(definition_path: Path, output: Path) -> dict[str, Any]:
    definition_path = definition_path.resolve()
    definition = load_definition(definition_path)
    source_bytes = definition_path.read_bytes()
    output = output.resolve()
    instruction_dir = output / "frozen-instructions"
    instruction_dir.mkdir(parents=True, exist_ok=True)

    # Copy exactly the verified inputs used by the run.  The run manifest then
    # points only at these immutable-by-hash copies, never at mutable skill data.
    copied: dict[str, str] = {}
    for task in definition["tasks"]:
        relative, _ = _task_instruction_fields(task)
        source = (definition_path.parent / relative).resolve()
        target = instruction_dir / f"{task['name'].split('/', 1)[1]}.md"
        target.write_bytes(source.read_bytes())
        copied[task["name"]] = str(target)

    schedule = generate_schedule(definition)
    for cell in schedule:
        cell["instruction_path"] = copied[cell["task"]] if cell["arm"] != "control" else None
        cell["status"] = "pending"
        cell["infra_retries"] = []

    manifest = {
        "schema_version": 1,
        "created_at": _utc_now(),
        "definition_path": str(definition_path),
        "definition_sha256": _sha256_bytes(source_bytes),
        "definition": deepcopy(definition),
        "environment": {
            "CODEX_FORCE_AUTH_JSON": "1",
            "PYTHONUTF8": "1",
            "PYTHONIOENCODING": "utf-8",
        },
        "schedule": schedule,
    }
    _write_json(output / RUN_MANIFEST_NAME, manifest)
    return manifest


def build_job_config(manifest: dict[str, Any], cell: dict[str, Any], output: Path, retry: int) -> dict[str, Any]:
    definition = manifest["definition"]
    job_name = f"{cell['sequence']:02d}-{cell['cell_id']}-try{retry + 1:02d}"
    config: dict[str, Any] = {
        "job_name": job_name,
        "jobs_dir": str((output / "jobs").resolve()),
        "n_concurrent_trials": 1,
        "n_attempts": 1,
        "agents": [
            {
                "name": definition["agent"]["name"],
                "model_name": definition["agent"]["model"],
                "kwargs": {
                    "reasoning_effort": definition["agent"]["reasoning_effort"],
                    "reasoning_summary": definition["agent"]["reasoning_summary"],
                    "web_search": definition["agent"]["web_search"],
                    "version": definition["agent"]["version"],
                },
            }
        ],
        "datasets": [
            {
                "name": definition["dataset"]["name"],
                "ref": definition["dataset"]["ref"],
                "task_names": [cell["task"]],
            }
        ],
    }
    if cell["arm"] != "control":
        config["extra_instruction_paths"] = [cell["instruction_path"]]
    return config


def is_infrastructure_error(result: dict[str, Any] | None, returncode: int) -> bool:
    """Return true only for a narrowly recognized infrastructure failure."""

    if result is None:
        return returncode != 0
    exception = result.get("exception_info") or {}
    exception_type = str(exception.get("exception_type") or "")
    message = str(exception.get("exception_message") or "").lower()
    if any(fragment.lower() in exception_type.lower() for fragment in INFRA_EXCEPTION_FRAGMENTS):
        return True
    return any(fragment.lower() in message for fragment in TRANSIENT_MESSAGE_FRAGMENTS)


def _find_trial_result(job_dir: Path) -> tuple[Path | None, dict[str, Any] | None]:
    paths = [path for path in job_dir.glob("*/result.json") if path.parent != job_dir]
    if len(paths) != 1:
        return None, None
    return paths[0], _read_json(paths[0])


def _reward(result: dict[str, Any]) -> float | None:
    rewards = ((result.get("verifier_result") or {}).get("rewards") or {})
    value = rewards.get("reward")
    return float(value) if isinstance(value, (int, float)) else None


def _duration_seconds(phase: dict[str, Any] | None) -> float | None:
    phase = phase or {}
    started = phase.get("started_at")
    finished = phase.get("finished_at")
    if not started or not finished:
        return None
    try:
        start_time = datetime.fromisoformat(str(started).replace("Z", "+00:00"))
        finish_time = datetime.fromisoformat(str(finished).replace("Z", "+00:00"))
    except ValueError:
        return None
    return max(0.0, (finish_time - start_time).total_seconds())


def _measurements(result: dict[str, Any]) -> dict[str, Any]:
    agent = result.get("agent_result") or {}
    return {
        "usage": {
            "input_tokens": agent.get("n_input_tokens"),
            "cache_tokens": agent.get("n_cache_tokens"),
            "output_tokens": agent.get("n_output_tokens"),
            "cost_usd": agent.get("cost_usd"),
        },
        "timing_seconds": {
            "total": _duration_seconds(result),
            "environment_setup": _duration_seconds(result.get("environment_setup")),
            "agent_setup": _duration_seconds(result.get("agent_setup")),
            "agent_execution": _duration_seconds(result.get("agent_execution")),
            "verifier": _duration_seconds(result.get("verifier")),
        },
    }


def execution_batches(cells: Iterable[dict[str, Any]], workers: int) -> list[list[dict[str, Any]]]:
    """Pack cells without ever running two cells for one task together.

    Harbor's task-package cache is shared across jobs.  Concurrent arms (or
    replicates) of the same task can therefore race while unpacking the same
    package on Windows.  Each batch contains at most one cell per task while
    still filling available worker slots with cells from distinct tasks.
    MCMC remains exclusive because of its substantially higher resource use.
    """

    if workers < 1:
        raise ValueError("workers must be positive")
    pending = [cell for cell in cells if cell.get("status") != "completed"]
    batches: list[list[dict[str, Any]]] = []
    while pending:
        first, *rest = pending
        if str(first["task"]).endswith("/mcmc-sampling-stan"):
            batches.append([first])
            pending = rest
            continue

        batch = [first]
        batch_tasks = {str(first["task"])}
        deferred: list[dict[str, Any]] = []
        for cell in rest:
            task = str(cell["task"])
            eligible = (
                len(batch) < workers
                and task not in batch_tasks
                and not task.endswith("/mcmc-sampling-stan")
            )
            if eligible:
                batch.append(cell)
                batch_tasks.add(task)
            else:
                deferred.append(cell)
        batches.append(batch)
        pending = deferred
    return batches


def _execute_cell(
    manifest: dict[str, Any],
    original_cell: dict[str, Any],
    output: Path,
    harbor_executable: str,
    max_infra_retries: int,
) -> dict[str, Any]:
    """Execute one cell and return state; never mutate or write the manifest."""

    cell = deepcopy(original_cell)
    env = os.environ.copy()
    env.update(manifest["environment"])
    retry = len(cell.get("infra_retries") or [])
    while True:
        config = build_job_config(manifest, cell, output, retry)
        config_dir = output / "job-configs"
        config_path = config_dir / f"{config['job_name']}.json"
        _write_json(config_path, config)
        command = [
            harbor_executable,
            "run",
            "--config",
            str(config_path),
            "--max-retries",
            "0",
            "--yes",
            "--quiet",
        ]
        completed = subprocess.run(command, env=env, text=True, encoding="utf-8", errors="replace")
        job_dir = Path(config["jobs_dir"]) / config["job_name"]
        result_path, result = _find_trial_result(job_dir)
        infra = is_infrastructure_error(result, completed.returncode)
        attempt_record = {
            "attempt": retry + 1,
            "job_name": config["job_name"],
            "config_path": str(config_path),
            "returncode": completed.returncode,
            "trial_result_path": str(result_path) if result_path else None,
            "infrastructure_error": infra,
            "exception_type": ((result or {}).get("exception_info") or {}).get("exception_type"),
        }
        if infra and retry < max_infra_retries:
            cell.setdefault("infra_retries", []).append(attempt_record)
            retry += 1
            continue
        cell["final_attempt"] = attempt_record
        cell["status"] = "completed" if result is not None and not infra else "infra_failed"
        cell["reward"] = _reward(result) if result is not None and not infra else None
        cell["passed"] = bool(cell["reward"] is not None and cell["reward"] >= 1.0)
        if result is not None:
            cell.update(_measurements(result))
        cell["finished_at"] = _utc_now()
        return cell


def run_manifest(
    output: Path,
    harbor_executable: str,
    max_infra_retries: int,
    workers: int = 3,
) -> dict[str, Any]:
    output = output.resolve()
    manifest_path = output / RUN_MANIFEST_NAME
    manifest = _read_json(manifest_path)
    # Recheck copied instructions before every resumed run.
    for cell in manifest["schedule"]:
        if cell["arm"] != "control":
            actual = _sha256_bytes(Path(cell["instruction_path"]).read_bytes())
            if actual != cell["instruction_sha256"]:
                raise ValueError(f"frozen instruction changed for {cell['cell_id']}")

    execution_record = {
        "started_at": _utc_now(),
        "workers": workers,
        "max_infra_retries": max_infra_retries,
        "harbor_executable": harbor_executable,
    }
    manifest.setdefault("execution_attempts", []).append(execution_record)
    _write_json(manifest_path, manifest)
    by_id = {str(cell["cell_id"]): cell for cell in manifest["schedule"]}
    for batch in execution_batches(manifest["schedule"], workers):
        if len(batch) == 1:
            completed_cells = [
                _execute_cell(manifest, batch[0], output, harbor_executable, max_infra_retries)
            ]
        else:
            with ThreadPoolExecutor(max_workers=min(workers, len(batch))) as executor:
                completed_cells = list(
                    executor.map(
                        lambda cell: _execute_cell(
                            manifest, cell, output, harbor_executable, max_infra_retries
                        ),
                        batch,
                    )
                )
        # Only this coordinator mutates and atomically writes the manifest.
        for completed_cell in completed_cells:
            target = by_id[str(completed_cell["cell_id"])]
            target.clear()
            target.update(completed_cell)
        _write_json(manifest_path, manifest)
        failed = [cell["cell_id"] for cell in completed_cells if cell["status"] == "infra_failed"]
        if failed:
            execution_record.update({"finished_at": _utc_now(), "status": "infra_failed"})
            _write_json(manifest_path, manifest)
            raise RuntimeError(f"infrastructure failure exhausted retries for {', '.join(failed)}")
    execution_record.update({"finished_at": _utc_now(), "status": "completed"})
    _write_json(manifest_path, manifest)
    return manifest


def exact_mcnemar_p(wins: int, losses: int) -> float:
    """Two-sided exact McNemar p-value (binomial test on discordant pairs)."""

    if wins < 0 or losses < 0:
        raise ValueError("counts must be non-negative")
    discordant = wins + losses
    if discordant == 0:
        return 1.0
    tail = sum(math.comb(discordant, k) for k in range(min(wins, losses) + 1)) / (2**discordant)
    return min(1.0, 2.0 * tail)


def aggregate_cells(cells: Iterable[dict[str, Any]]) -> dict[str, Any]:
    cells = list(cells)
    comparison_arms = sorted({str(cell.get("arm")) for cell in cells if cell.get("arm") != "control"})
    if len(comparison_arms) != 1:
        raise ValueError("cells must contain exactly one non-control comparison arm")
    comparison_arm = comparison_arms[0]
    by_pair: dict[tuple[str, int], dict[str, dict[str, Any]]] = {}
    for cell in cells:
        if cell.get("status") != "completed":
            continue
        key = (str(cell["task"]), int(cell["replicate"]))
        by_pair.setdefault(key, {})[str(cell["arm"])] = cell

    pairs = []
    wins = losses = ties = 0
    for (task, replicate), arms in sorted(by_pair.items()):
        if set(arms) != {"control", comparison_arm}:
            continue
        control = bool(arms["control"]["passed"])
        treatment = bool(arms[comparison_arm]["passed"])
        outcome = "win" if treatment and not control else "loss" if control and not treatment else "tie"
        wins += outcome == "win"
        losses += outcome == "loss"
        ties += outcome == "tie"
        pairs.append(
            {
                "task": task,
                "replicate": replicate,
                "control_pass": control,
                "treatment_pass": treatment,
                "comparison_arm": comparison_arm,
                "control_reward": arms["control"].get("reward"),
                "comparison_reward": arms[comparison_arm].get("reward"),
                "control_cell_id": arms["control"].get("cell_id"),
                "comparison_cell_id": arms[comparison_arm].get("cell_id"),
                "comparison_instruction_sha256": arms[comparison_arm].get("instruction_sha256"),
                "control_result_path": (arms["control"].get("final_attempt") or {}).get("trial_result_path"),
                "comparison_result_path": (arms[comparison_arm].get("final_attempt") or {}).get("trial_result_path"),
                "outcome": outcome,
            }
        )
    total = len(pairs)
    by_task: dict[str, dict[str, Any]] = {}
    for pair in pairs:
        task_summary = by_task.setdefault(
            pair["task"], {"pairs": 0, "wins": 0, "ties": 0, "losses": 0, "control_passes": 0, "treatment_passes": 0}
        )
        task_summary["pairs"] += 1
        task_summary[{"win": "wins", "tie": "ties", "loss": "losses"}[pair["outcome"]]] += 1
        task_summary["control_passes"] += int(pair["control_pass"])
        task_summary["treatment_passes"] += int(pair["treatment_pass"])

    task_wins = sum(item["treatment_passes"] > item["control_passes"] for item in by_task.values())
    task_losses = sum(item["control_passes"] > item["treatment_passes"] for item in by_task.values())
    task_ties = len(by_task) - task_wins - task_losses

    scored_cells = [cell for cell in cells if cell.get("status") == "completed"]
    arm_metrics: dict[str, dict[str, Any]] = {}
    for arm in ("control", comparison_arm):
        arm_cells = [cell for cell in scored_cells if cell.get("arm") == arm]
        timing = [
            float(cell["timing_seconds"]["total"])
            for cell in arm_cells
            if (cell.get("timing_seconds") or {}).get("total") is not None
        ]
        usage_summary: dict[str, Any] = {}
        for key in ("input_tokens", "cache_tokens", "output_tokens", "cost_usd"):
            values = [(cell.get("usage") or {}).get(key) for cell in arm_cells]
            numeric = [float(value) for value in values if isinstance(value, (int, float))]
            usage_summary[key] = sum(numeric) if numeric else None
        arm_metrics[arm] = {
            "completed_cells": len(arm_cells),
            "mean_total_seconds": sum(timing) / len(timing) if timing else None,
            "totals": usage_summary,
        }

    return {
        "comparison_arm": comparison_arm,
        "complete_pairs": total,
        "wins": wins,
        "ties": ties,
        "losses": losses,
        "control_passes": sum(pair["control_pass"] for pair in pairs),
        "treatment_passes": sum(pair["treatment_pass"] for pair in pairs),
        "control_pass_rate": sum(pair["control_pass"] for pair in pairs) / total if total else None,
        "treatment_pass_rate": sum(pair["treatment_pass"] for pair in pairs) / total if total else None,
        "pass_rate_delta": (
            (sum(pair["treatment_pass"] for pair in pairs) - sum(pair["control_pass"] for pair in pairs))
            / total
            if total
            else None
        ),
        "mcnemar_exact_two_sided_p": exact_mcnemar_p(wins, losses),
        "task_level_wins": task_wins,
        "task_level_ties": task_ties,
        "task_level_losses": task_losses,
        # Replicates within one task are clustered, so this exact sign test on
        # within-task pass-count direction is the conservative companion to
        # the requested replicate-level McNemar result.
        "task_clustered_exact_sign_two_sided_p": exact_mcnemar_p(task_wins, task_losses),
        "by_task": by_task,
        "arm_metrics": arm_metrics,
        "pairs": pairs,
    }


def aggregate_manifest(output: Path) -> dict[str, Any]:
    output = output.resolve()
    manifest = _read_json(output / RUN_MANIFEST_NAME)
    summary = {
        "schema_version": 1,
        "created_at": _utc_now(),
        "definition_sha256": manifest["definition_sha256"],
        "dataset": manifest["definition"]["dataset"],
        "agent": manifest["definition"]["agent"],
        "random_seed": manifest["definition"]["random_seed"],
        "execution_attempts": manifest.get("execution_attempts") or [],
        "results": aggregate_cells(manifest["schedule"]),
    }
    _write_json(output / SUMMARY_NAME, summary)
    return summary


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("plan", "run", "aggregate"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--definition", type=Path, default=DEFAULT_DEFINITION)
    parser.add_argument("--harbor-executable", default="harbor")
    parser.add_argument("--max-infra-retries", type=int, default=2)
    parser.add_argument("--workers", type=int, default=3)
    args = parser.parse_args(argv)
    if args.max_infra_retries < 0:
        parser.error("--max-infra-retries must be non-negative")
    if args.workers < 1:
        parser.error("--workers must be positive")
    return args


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "plan":
        manifest = create_manifest(args.definition, args.output)
        print(f"planned {len(manifest['schedule'])} single-attempt jobs in {args.output.resolve()}")
        return 0
    if args.command == "run":
        run_manifest(args.output, args.harbor_executable, args.max_infra_retries, args.workers)
    summary = aggregate_manifest(args.output)
    print(json.dumps(summary["results"], indent=2, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
