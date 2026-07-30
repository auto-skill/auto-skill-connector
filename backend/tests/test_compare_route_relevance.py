import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bench" / "compare_route_relevance.py"
SPEC = importlib.util.spec_from_file_location("compare_route_relevance", MODULE_PATH)
compare_route_relevance = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(compare_route_relevance)


JUDGE = {
    "model": "gpt-5.6-sol",
    "reasoning_effort": "max",
    "version": "codex-cli 0.145.0",
    "executable_sha256": "a" * 64,
}


def artifact(task_labels, source_path, *, tiers=None, usage=True):
    tiers = tiers or ["none"] * len(task_labels)
    tasks = []
    replay_tasks = []
    for index, labels in enumerate(task_labels, start=1):
        instruction = f"Instruction {index}"
        instruction_sha256 = hashlib.sha256(instruction.encode()).hexdigest()
        candidates = [
            {
                "rank": rank,
                "content_hash": hashlib.sha256(
                    f"candidate-{index}-{rank}".encode()
                ).hexdigest(),
            }
            for rank in range(1, len(labels) + 1)
        ]
        task = {
            "id": f"task-{index}",
            "instruction_sha256": instruction_sha256,
            "status": "judged",
            "candidates": candidates,
            "judgment": {
                "labels": [
                    {"rank": rank, "label": label, "reason": "fixture"}
                    for rank, label in enumerate(labels, start=1)
                ]
            },
        }
        if usage:
            task["codex_execution"] = {
                "usage": {
                    "input_tokens": 100 * index,
                    "cached_input_tokens": 10 * index,
                    "output_tokens": 5 * index,
                }
            }
        tasks.append(task)
        replay_tasks.append(
            {
                "id": task["id"],
                "instruction": instruction,
                "instruction_sha256": instruction_sha256,
                "status_code": 200,
                "tier": tiers[index - 1],
                "results": [
                    {"content_hash": candidate["content_hash"]}
                    for candidate in candidates
                ],
            }
        )
    source_bytes = json.dumps({"tasks": replay_tasks}, sort_keys=True).encode()
    source_path.write_bytes(source_bytes)
    return {
        "completed_at": "2026-07-21T00:00:00+00:00",
        "source_replay": {
            "path": str(source_path.resolve()),
            "sha256": hashlib.sha256(source_bytes).hexdigest(),
        },
        "evaluation": {"top_k": 5, "judge": dict(JUDGE)},
        "tasks": tasks,
        "summary": {
            "tasks_total": len(tasks),
            "tasks_judged": len(tasks),
            "tasks_failed": 0,
        },
    }


def test_compare_reports_paired_metrics_cluster_bootstrap_and_usage(tmp_path):
    control = artifact(
        [
            ["E", "I", "I", "I", "I"],
            ["I", "E", "I", "I", "I"],
            ["E", "E", "I", "I", "I"],
            ["I", "I", "I", "I", "I"],
        ],
        tmp_path / "control-replay.json",
    )
    treatment = artifact(
        [
            ["E", "I", "I", "I", "I"],
            ["E", "I", "I", "I", "I"],
            ["I", "I", "I", "I", "I"],
            ["I", "E", "I", "I", "I"],
        ],
        tmp_path / "treatment-replay.json",
    )

    result = compare_route_relevance.compare(
        control, treatment, bootstrap_replicates=200, seed=17
    )

    top1 = result["metrics"]["exact_top1_task_hit"]
    assert top1["control"]["hits"] == 2
    assert top1["treatment"]["hits"] == 2
    assert top1["paired"] == {
        "wins": 1,
        "losses": 1,
        "ties": 2,
        "exact_two_sided_mcnemar_p": 1.0,
        "difference_ci95": top1["paired"]["difference_ci95"],
    }
    assert top1["paired"]["difference_ci95"]["resampling_unit"] == "task_cluster"

    top5 = result["metrics"]["any_exact_top5_task_hit"]
    assert top5["control"]["hits"] == 3
    assert top5["treatment"]["hits"] == 3
    assert top5["paired"]["wins"] == 1
    assert top5["paired"]["losses"] == 1

    candidates = result["metrics"]["exact_candidate_precision"]
    assert candidates["control"] == {"exact": 4, "candidates": 20, "precision": 0.2}
    assert candidates["treatment"] == {"exact": 3, "candidates": 20, "precision": 0.15}
    assert candidates["difference"] == pytest.approx(-0.05)
    assert candidates["paired_task_clusters"]["wins"] == 1
    assert candidates["paired_task_clusters"]["losses"] == 1
    assert candidates["paired_task_clusters"]["ties"] == 2
    assert "not independent" in candidates["inference_note"]

    assert result["token_usage"]["control"]["totals"]["input_tokens"] == 1000
    assert result["token_usage"]["difference"]["output_tokens"] == 0


def test_bootstrap_is_deterministic_for_same_seed(tmp_path):
    control = artifact(
        [["I", "E", "I", "I", "I"], ["E", "I", "I", "I", "I"]],
        tmp_path / "control-replay.json",
    )
    treatment = artifact(
        [["E", "I", "I", "I", "I"], ["I", "I", "I", "I", "I"]],
        tmp_path / "treatment-replay.json",
    )

    first = compare_route_relevance.compare(control, treatment, bootstrap_replicates=50, seed=9)
    second = compare_route_relevance.compare(control, treatment, bootstrap_replicates=50, seed=9)

    assert first == second


def test_exact_mcnemar_uses_two_sided_binomial_tail():
    assert compare_route_relevance.exact_mcnemar_p(4, 0) == 0.125
    assert compare_route_relevance.exact_mcnemar_p(0, 0) == 1.0


def test_gate_aware_metrics_pair_surface_tiers_with_exact_labels(tmp_path):
    control = artifact(
        [
            ["E", "I", "I", "I", "I"],
            ["I", "E", "I", "I", "I"],
            ["I", "I", "I", "I", "I"],
            ["I", "I", "I", "I", "I"],
        ],
        tmp_path / "control-replay.json",
        tiers=["none", "hint", "full", "none"],
    )
    treatment = artifact(
        [
            ["E", "I", "I", "I", "I"],
            ["I", "E", "I", "I", "I"],
            ["I", "I", "I", "I", "I"],
            ["I", "E", "I", "I", "I"],
        ],
        tmp_path / "treatment-replay.json",
        tiers=["hint", "none", "hint", "full"],
    )

    result = compare_route_relevance.compare(
        control, treatment, bootstrap_replicates=100, seed=41
    )
    gate = result["metrics"]["gate_aware_routing"]

    assert gate["control"]["surfaced_task_count"] == {
        "count": 2,
        "total": 4,
        "rate": 0.5,
    }
    assert gate["control"]["routed_exact_top1_hits"]["hits"] == 0
    assert gate["control"]["routed_any_exact_top5_hits"]["hits"] == 1
    assert gate["control"]["route_precision_among_surfaced_tasks"]["estimate"] == 0.5
    assert gate["control"]["recall_of_any_exact_tasks"]["estimate"] == 0.5
    assert gate["control"]["false_positive_surfaced_tasks"]["count"] == 1

    assert gate["treatment"]["surfaced_task_count"]["count"] == 3
    assert gate["treatment"]["routed_exact_top1_hits"]["hits"] == 1
    assert gate["treatment"]["routed_any_exact_top5_hits"]["hits"] == 2
    assert gate["treatment"]["route_precision_among_surfaced_tasks"]["estimate"] == pytest.approx(
        2 / 3
    )
    assert gate["treatment"]["recall_of_any_exact_tasks"]["estimate"] == pytest.approx(
        2 / 3
    )
    assert gate["treatment"]["false_positive_surfaced_tasks"]["count"] == 1

    routed_top1 = gate["paired_routed_hit_outcomes"]["routed_exact_top1_hit"]
    assert (routed_top1["paired"]["wins"], routed_top1["paired"]["losses"]) == (1, 0)
    assert routed_top1["paired"]["difference_ci95"]["resampling_unit"] == "task_cluster"
    routed_top5 = gate["paired_routed_hit_outcomes"]["routed_any_exact_top5_hit"]
    assert (
        routed_top5["paired"]["wins"],
        routed_top5["paired"]["losses"],
        routed_top5["paired"]["ties"],
    ) == (2, 1, 1)
    assert "does not establish prompt injection" in gate["interpretation_note"]


def test_source_replay_digest_and_candidate_linkage_are_enforced(tmp_path):
    control = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "control-replay.json"
    )
    treatment_path = tmp_path / "treatment-replay.json"
    treatment = artifact(
        [["E", "I", "I", "I", "I"]], treatment_path
    )
    treatment_path.write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="sha256 does not match"):
        compare_route_relevance.compare(control, treatment)

    treatment = artifact(
        [["E", "I", "I", "I", "I"]], treatment_path
    )
    replay = json.loads(treatment_path.read_text(encoding="utf-8"))
    replay["tasks"][0]["results"][0]["content_hash"] = "f" * 64
    changed = json.dumps(replay, sort_keys=True).encode()
    treatment_path.write_bytes(changed)
    treatment["source_replay"]["sha256"] = hashlib.sha256(changed).hexdigest()

    with pytest.raises(ValueError, match="candidates do not match"):
        compare_route_relevance.compare(control, treatment)


@pytest.mark.parametrize(
    ("mutation", "message"),
    [
        (lambda value: value.update(completed_at=None), "not completed"),
        (
            lambda value: value["summary"].update(tasks_failed=1),
            "reports failed tasks",
        ),
        (
            lambda value: value["tasks"][0].update(status="failed"),
            "not successfully judged",
        ),
        (
            lambda value: value["evaluation"]["judge"].pop("version"),
            "judge metadata missing version",
        ),
    ],
)
def test_rejects_incomplete_or_failed_artifacts(tmp_path, mutation, message):
    control = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "control-replay.json"
    )
    treatment = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "treatment-replay.json"
    )
    mutation(treatment)

    with pytest.raises(ValueError, match=message):
        compare_route_relevance.compare(control, treatment)


def test_rejects_task_instruction_topk_and_judge_mismatches(tmp_path):
    control = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "control-replay.json"
    )

    treatment = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "treatment-replay.json"
    )
    treatment["tasks"][0]["id"] = "other"
    with pytest.raises(ValueError, match="different task ids"):
        compare_route_relevance.compare(control, treatment)

    treatment = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "treatment-replay.json"
    )
    treatment["tasks"][0]["instruction_sha256"] = "f" * 64
    with pytest.raises(ValueError, match="different instruction_sha256"):
        compare_route_relevance.compare(control, treatment)

    treatment = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "treatment-replay.json"
    )
    treatment["evaluation"]["top_k"] = 6
    with pytest.raises(ValueError, match="different top_k"):
        compare_route_relevance.compare(control, treatment)

    treatment = artifact(
        [["I", "I", "I", "I", "I"]], tmp_path / "treatment-replay.json"
    )
    treatment["evaluation"]["judge"]["model"] = "different"
    with pytest.raises(ValueError, match="different judge model metadata"):
        compare_route_relevance.compare(control, treatment)


def test_cli_writes_sources_and_optional_usage(tmp_path):
    control = artifact(
        [["I", "I", "I", "I", "I"]],
        tmp_path / "control-replay.json",
        usage=False,
    )
    treatment = artifact(
        [["E", "I", "I", "I", "I"]],
        tmp_path / "treatment-replay.json",
        usage=False,
    )
    control_path = tmp_path / "control.json"
    treatment_path = tmp_path / "treatment.json"
    output = tmp_path / "comparison.json"
    control_path.write_text(json.dumps(control), encoding="utf-8")
    treatment_path.write_text(json.dumps(treatment), encoding="utf-8")

    assert (
        compare_route_relevance.main(
            [
                "--control",
                str(control_path),
                "--treatment",
                str(treatment_path),
                "--output",
                str(output),
                "--bootstrap-replicates",
                "20",
                "--seed",
                "3",
            ]
        )
        == 0
    )
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["sources"]["control"]["sha256"]
    assert written["comparison"]["token_usage"] is None
    assert written["comparison"]["metrics"]["exact_top1_task_hit"]["difference"] == 1.0
