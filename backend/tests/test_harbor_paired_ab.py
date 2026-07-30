import hashlib
import json
import threading
from pathlib import Path

import pytest

from backend.bench.harbor_paired_ab import (
    RUN_MANIFEST_NAME,
    aggregate_cells,
    build_job_config,
    create_manifest,
    exact_mcnemar_p,
    execution_batches,
    generate_schedule,
    is_infrastructure_error,
    load_definition,
)
from backend.bench import harbor_paired_ab


def _definition(tmp_path: Path) -> Path:
    tasks = []
    capsule_dir = tmp_path / "capsules"
    capsule_dir.mkdir(parents=True)
    for number in range(6):
        body = f"frozen guidance {number}\n".encode()
        capsule = capsule_dir / f"task-{number}.md"
        capsule.write_bytes(body)
        tasks.append(
            {
                "name": f"terminal-bench/task-{number}",
                "skill_id": f"skill-{number}",
                "skill_name": f"skill name {number}",
                "capsule": f"capsules/task-{number}.md",
                "capsule_sha256": hashlib.sha256(body).hexdigest(),
            }
        )
    value = {
        "schema_version": 1,
        "hypothesis": "relevant procedural guidance improves task success",
        "dataset": {"name": "terminal-bench/example", "ref": "sha256:" + "a" * 64},
        "agent": {
            "name": "codex",
            "model": "gpt-5.6-sol",
            "reasoning_effort": "max",
            "reasoning_summary": "none",
            "web_search": "disabled",
            "version": "0.145.0",
        },
        "replicates": 3,
        "random_seed": 260721,
        "arms": ["control", "treatment"],
        "tasks": tasks,
    }
    path = tmp_path / "definition.json"
    path.write_text(json.dumps(value), encoding="utf-8")
    return path


def test_definition_and_schedule_are_frozen_paired_and_deterministic(tmp_path: Path):
    path = _definition(tmp_path)
    definition = load_definition(path)

    first = generate_schedule(definition)
    second = generate_schedule(definition)

    assert first == second
    assert len(first) == 36
    assert [row["sequence"] for row in first] == list(range(1, 37))
    for replicate in range(1, 4):
        rows = [row for row in first if row["replicate"] == replicate]
        assert len(rows) == 12
        assert {row["task"] for row in rows} == {
            f"terminal-bench/task-{number}" for number in range(6)
        }
        for task in {row["task"] for row in rows}:
            assert {row["arm"] for row in rows if row["task"] == task} == {
                "control",
                "treatment",
            }
    assert any(
        rows[index]["arm"] == "treatment"
        for replicate in range(1, 4)
        for rows in [[row for row in first if row["replicate"] == replicate]]
        for index in range(0, len(rows), 2)
    )
    assert any(
        rows[index]["arm"] == "control"
        for replicate in range(1, 4)
        for rows in [[row for row in first if row["replicate"] == replicate]]
        for index in range(0, len(rows), 2)
    )


def test_manifest_copies_capsules_and_jobs_differ_only_by_instruction(tmp_path: Path):
    definition_path = _definition(tmp_path / "source")
    output = tmp_path / "output"
    manifest = create_manifest(definition_path, output)
    pair = [row for row in manifest["schedule"] if row["task"] == "terminal-bench/task-0"][:2]
    control = next(row for row in pair if row["arm"] == "control")
    treatment = next(row for row in pair if row["arm"] == "treatment")

    control_config = build_job_config(manifest, control, output, 0)
    treatment_config = build_job_config(manifest, treatment, output, 0)

    assert "extra_instruction_paths" not in control_config
    assert treatment_config["extra_instruction_paths"] == [treatment["instruction_path"]]
    assert Path(treatment["instruction_path"]).is_file()
    assert control_config["datasets"] == treatment_config["datasets"]
    assert control_config["datasets"][0]["ref"].startswith("sha256:")
    assert control_config["agents"] == treatment_config["agents"]
    assert control_config["n_attempts"] == treatment_config["n_attempts"] == 1
    assert manifest["environment"]["CODEX_FORCE_AUTH_JSON"] == "1"
    assert manifest["environment"]["PYTHONUTF8"] == "1"
    assert (output / RUN_MANIFEST_NAME).is_file()


def test_definition_rejects_changed_capsule(tmp_path: Path):
    path = _definition(tmp_path)
    capsule = tmp_path / "capsules" / "task-2.md"
    capsule.write_text("changed", encoding="utf-8")

    with pytest.raises(ValueError, match="instruction digest mismatch"):
        load_definition(path)


def test_oracle_screen_allows_one_replicate_and_reused_body(tmp_path: Path):
    path = _definition(tmp_path)
    definition = json.loads(path.read_text(encoding="utf-8"))
    definition["replicates"] = 1
    definition["arms"] = ["control", "oracle_skill"]
    shared = definition["tasks"][0]["capsule"]
    shared_digest = definition["tasks"][0]["capsule_sha256"]
    for index, task in enumerate(definition["tasks"]):
        task.update(
            {
                "instruction": shared if index < 2 else task["capsule"],
                "instruction_sha256": shared_digest if index < 2 else task["capsule_sha256"],
                "skill_id": f"oracle-{index}",
                "skill_name": f"oracle skill {index}",
                "source_content_hash": f"{index + 1:064x}",
                "selection_rank": (index % 5) + 1,
            }
        )
    path.write_text(json.dumps(definition), encoding="utf-8")

    loaded = load_definition(path)
    schedule = generate_schedule(loaded)

    assert len(schedule) == 12
    assert {cell["arm"] for cell in schedule} == {"control", "oracle_skill"}
    assert all(cell["instruction_kind"] == "oracle_skill" for cell in schedule if cell["arm"] != "control")


@pytest.mark.parametrize(
    ("wins", "losses", "expected"),
    [(0, 0, 1.0), (1, 0, 1.0), (5, 0, 0.0625), (21, 0, 9.5367431640625e-7)],
)
def test_exact_mcnemar(wins: int, losses: int, expected: float):
    assert exact_mcnemar_p(wins, losses) == pytest.approx(expected)


def test_aggregate_reports_paired_win_tie_loss_and_excludes_incomplete():
    cells = []
    outcomes = [(False, True), (True, False), (True, True), (False, False)]
    for replicate, (control, treatment) in enumerate(outcomes, 1):
        for arm, passed in (("control", control), ("treatment", treatment)):
            cells.append(
                {
                    "task": "terminal-bench/task",
                    "replicate": replicate,
                    "arm": arm,
                    "status": "completed",
                    "passed": passed,
                }
            )
    cells.append(
        {
            "task": "terminal-bench/incomplete",
            "replicate": 1,
            "arm": "control",
            "status": "completed",
            "passed": True,
        }
    )

    result = aggregate_cells(cells)

    assert result["complete_pairs"] == 4
    assert result["comparison_arm"] == "treatment"
    assert (result["wins"], result["ties"], result["losses"]) == (1, 2, 1)
    assert result["control_pass_rate"] == result["treatment_pass_rate"] == 0.5
    assert result["pass_rate_delta"] == 0.0
    assert result["mcnemar_exact_two_sided_p"] == 1.0
    assert (result["task_level_wins"], result["task_level_ties"], result["task_level_losses"]) == (0, 1, 0)
    assert result["task_clustered_exact_sign_two_sided_p"] == 1.0
    assert result["by_task"]["terminal-bench/task"] == {
        "pairs": 4,
        "wins": 1,
        "ties": 2,
        "losses": 1,
        "control_passes": 2,
        "treatment_passes": 2,
    }
    assert result["arm_metrics"]["control"]["completed_cells"] == 5


def test_only_narrow_infrastructure_failures_are_retryable():
    docker_error = {
        "exception_info": {
            "exception_type": "DockerComposeUpError",
            "exception_message": "compose failed",
        }
    }
    transient_agent_error = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "unexpected status 503 Service Unavailable",
        }
    }
    authentication_error = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "unexpected status 401 Unauthorized: Incorrect API key provided",
        }
    }
    ordinary_agent_error = {
        "exception_info": {
            "exception_type": "NonZeroAgentExitCodeError",
            "exception_message": "agent command exited 1",
        }
    }
    scored_failure = {"verifier_result": {"rewards": {"reward": 0.0}}, "exception_info": None}

    assert is_infrastructure_error(None, returncode=2)
    assert is_infrastructure_error(docker_error, returncode=1)
    assert is_infrastructure_error(transient_agent_error, returncode=1)
    assert is_infrastructure_error(authentication_error, returncode=1)
    assert not is_infrastructure_error(ordinary_agent_error, returncode=1)
    assert not is_infrastructure_error(scored_failure, returncode=0)


def test_execution_batches_serialize_same_task_and_keep_cross_task_parallelism():
    cells = []
    for task in ("light-a", "light-b", "mcmc-sampling-stan", "light-c"):
        for arm in ("control", "treatment"):
            cells.append(
                {
                    "task": f"terminal-bench/{task}",
                    "replicate": 1,
                    "arm": arm,
                    "status": "pending",
                }
            )

    batches = execution_batches(cells, workers=4)

    assert [len(batch) for batch in batches] == [3, 3, 1, 1]
    assert all(len(batch) == 1 for batch in batches if "mcmc" in batch[0]["task"])
    assert all(len({cell["task"] for cell in batch}) == len(batch) for batch in batches)
    for task in ("light-a", "light-b", "light-c"):
        matching = [batch for batch in batches if any(task in cell["task"] for cell in batch)]
        assert len(matching) == 2
        assert all(sum(task in cell["task"] for cell in batch) == 1 for batch in matching)


def test_run_manifest_centralizes_writes_and_never_overlaps_same_task(tmp_path: Path, monkeypatch):
    definition_path = _definition(tmp_path / "source")
    definition = json.loads(definition_path.read_text(encoding="utf-8"))
    definition["tasks"][0]["name"] = "terminal-bench/mcmc-sampling-stan"
    definition_path.write_text(json.dumps(definition), encoding="utf-8")
    output = tmp_path / "output"
    create_manifest(definition_path, output)
    coordinator_thread = threading.get_ident()
    execution_threads = {}
    active_tasks = set()
    overlapping_tasks = set()
    active_lock = threading.Lock()
    manifest_write_threads = []
    real_write = harbor_paired_ab._write_json

    def fake_execute(manifest, cell, output, harbor_executable, max_infra_retries):
        execution_threads[cell["cell_id"]] = threading.get_ident()
        with active_lock:
            if cell["task"] in active_tasks:
                overlapping_tasks.add(cell["task"])
            active_tasks.add(cell["task"])
        completed = dict(cell)
        completed.update({"status": "completed", "reward": 1.0, "passed": True})
        with active_lock:
            active_tasks.remove(cell["task"])
        return completed

    def observed_write(path, value):
        if path.name == RUN_MANIFEST_NAME:
            manifest_write_threads.append(threading.get_ident())
        real_write(path, value)

    monkeypatch.setattr(harbor_paired_ab, "_execute_cell", fake_execute)
    monkeypatch.setattr(harbor_paired_ab, "_write_json", observed_write)

    result = harbor_paired_ab.run_manifest(output, "harbor", max_infra_retries=2, workers=4)

    assert all(cell["status"] == "completed" for cell in result["schedule"])
    assert manifest_write_threads and set(manifest_write_threads) == {coordinator_thread}
    assert not overlapping_tasks
    mcmc_threads = {
        execution_threads[cell["cell_id"]]
        for cell in result["schedule"]
        if "mcmc-sampling-stan" in cell["task"]
    }
    assert mcmc_threads == {coordinator_thread}
    assert any(thread_id != coordinator_thread for thread_id in execution_threads.values())
