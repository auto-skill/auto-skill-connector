import hashlib
import importlib.util
import json
from pathlib import Path

import pytest


MODULE_PATH = Path(__file__).resolve().parents[1] / "bench" / "route_relevance_judge.py"
SPEC = importlib.util.spec_from_file_location("route_relevance_judge", MODULE_PATH)
assert SPEC and SPEC.loader
judge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(judge)


def _candidate(name: str, content_hash: str) -> dict:
    return {
        "id": name + "-id",
        "name": name,
        "content_hash": content_hash,
        "similarity": 0.91,
    }


def test_load_replay_uses_only_instruction_and_ranked_results(tmp_path):
    instruction = "Implement the public task."
    replay = {
        "config_version": "test",
        "tasks": [
            {
                "id": "task-one",
                "instruction": instruction,
                "instruction_sha256": hashlib.sha256(instruction.encode()).hexdigest(),
                "verifier": "MUST NEVER ENTER THE PROMPT",
                "results": [_candidate("first", "a" * 64), _candidate("second", "b" * 64)],
            }
        ],
    }
    path = tmp_path / "replay.json"
    path.write_text(json.dumps(replay), encoding="utf-8")

    provenance, tasks = judge.load_replay(path, 1)

    assert provenance["metadata"] == {"config_version": "test"}
    assert tasks[0]["instruction"] == instruction
    assert len(tasks[0]["candidates"]) == 1
    assert "verifier" not in tasks[0]


def test_load_replay_marks_missing_immutable_hash_unavailable(tmp_path):
    path = tmp_path / "replay.json"
    path.write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "task", "instruction": "Do it", "results": [{"name": "bad"}]}
                ]
            }
        ),
        encoding="utf-8",
    )

    _provenance, tasks = judge.load_replay(path, 5)

    assert tasks[0]["candidates"][0]["content_hash"] is None
    assert (
        tasks[0]["candidates"][0]["availability"]
        == "unavailable_no_immutable_content_hash"
    )


def test_fetch_body_uses_content_endpoint_and_records_served_digest(monkeypatch):
    body = b"# Skill\n\nConcrete guidance.\n"
    observed = {}

    class Response:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return body

    def fake_urlopen(request, timeout):
        observed["url"] = request.full_url
        observed["timeout"] = timeout
        return Response()

    monkeypatch.setattr(judge.urllib.request, "urlopen", fake_urlopen)
    result = judge.fetch_body("c" * 64)

    assert observed == {
        "url": "https://skills.autoskill.dev/content/" + "c" * 64,
        "timeout": 60,
    }
    assert result["body"] == body.decode()
    assert result["body_sha256"] == hashlib.sha256(body).hexdigest()
    assert result["source"] == "network"


def test_prompt_batches_candidates_and_excludes_route_scores():
    task = {
        "id": "task",
        "instruction": "Repair an nginx configuration.",
        "candidates": [
            {
                "rank": 1,
                "name": "nginx-debug",
                "skill_id": "one",
                "content_hash": "d" * 64,
                "availability": "immutable_snapshot",
                "route_result": {"similarity": 0.99, "secret_score": "DO NOT SHOW"},
            },
            {
                "rank": 2,
                "name": "generic",
                "skill_id": "two",
                "content_hash": "e" * 64,
                "availability": "immutable_snapshot",
                "route_result": {"similarity": 0.1},
            },
        ],
    }
    bodies = {
        "d" * 64: {"body": "nginx -t then inspect logs", "body_sha256": "1" * 64},
        "e" * 64: {"body": "generic text", "body_sha256": "2" * 64},
    }

    prompt = judge.build_prompt(task, bodies)

    assert prompt.count('"skill_body"') == 2
    assert "nginx -t then inspect logs" in prompt
    assert "DO NOT SHOW" not in prompt
    assert "retrieval score" in prompt
    assert "verifier, solution, tests" in prompt


def test_prompt_and_validator_make_missing_body_explicitly_unusable():
    task = {
        "id": "task",
        "instruction": "Do the task.",
        "candidates": [
            {
                "rank": 1,
                "name": "title-cannot-be-trusted",
                "skill_id": "one",
                "content_hash": None,
                "availability": "unavailable_no_immutable_content_hash",
                "route_result": {},
            }
        ],
    }
    prompt = judge.build_prompt(task, {})
    assert '"skill_body": null' in prompt
    assert "must be I" in prompt
    valid = {"labels": [{"rank": 1, "label": "I", "reason": "No body."}]}
    judge.validate_unavailable_labels(valid, task["candidates"])
    invalid = {"labels": [{"rank": 1, "label": "E", "reason": "Title sounds useful."}]}
    with pytest.raises(ValueError, match="must be labeled I"):
        judge.validate_unavailable_labels(invalid, task["candidates"])


def test_parse_judgment_requires_one_ordered_eai_label_per_rank():
    valid = json.dumps(
        {
            "labels": [
                {"rank": 1, "label": "E", "reason": "Direct procedure."},
                {"rank": 2, "label": "I", "reason": "Wrong task."},
            ]
        }
    )
    assert judge.parse_judgment(valid, 2)["labels"][0]["label"] == "E"

    duplicate = json.dumps(
        {
            "labels": [
                {"rank": 1, "label": "E", "reason": "Direct."},
                {"rank": 1, "label": "A", "reason": "Partial."},
            ]
        }
    )
    with pytest.raises(ValueError, match="rank order"):
        judge.parse_judgment(duplicate, 2)

    invalid = valid.replace('"I"', '"X"')
    with pytest.raises(ValueError, match="E/A/I"):
        judge.parse_judgment(invalid, 2)


def test_summarize_reports_candidate_precision_task_hits_and_wilson_intervals():
    tasks = [
        {
            "status": "judged",
            "judgment": {
                "labels": [
                    {"rank": 1, "label": "E"},
                    {"rank": 2, "label": "I"},
                ]
            },
        },
        {
            "status": "judged",
            "judgment": {
                "labels": [
                    {"rank": 1, "label": "A"},
                    {"rank": 2, "label": "E"},
                ]
            },
        },
        {"status": "failed"},
    ]

    summary = judge.summarize(tasks, 5)

    assert summary["tasks_judged"] == 2
    assert summary["tasks_failed"] == 1
    assert summary["label_counts"] == {"E": 2, "A": 1, "I": 1}
    assert summary["exact_top1_precision_wilson95"]["estimate"] == 0.5
    assert summary["exact_top5_candidate_precision_wilson95"]["estimate"] == 0.5
    assert summary["exact_topk_candidate_precision_wilson95"]["estimate"] == 0.5
    assert summary["exact_top5_task_hit_rate_wilson95"]["estimate"] == 1.0
    assert 0 < summary["exact_top1_precision_wilson95"]["low"] < 0.5
    assert 0.5 < summary["exact_top1_precision_wilson95"]["high"] < 1


def test_dry_run_does_not_fetch_run_codex_or_write_output(tmp_path, monkeypatch, capsys):
    replay = tmp_path / "replay.json"
    output = tmp_path / "judged.json"
    replay.write_text(
        json.dumps(
            {
                "tasks": [
                    {
                        "id": "task",
                        "instruction": "Do the task",
                        "results": [_candidate("candidate", "f" * 64)],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(judge, "fetch_body", lambda *_args: pytest.fail("network called"))
    monkeypatch.setattr(judge, "run_codex_judge", lambda *_args: pytest.fail("Codex called"))

    result = judge.run(judge.parse_args(["--input", str(replay), "--output", str(output), "--dry-run"]))

    assert result == 0
    assert not output.exists()
    report = json.loads(capsys.readouterr().out)
    assert report["network_requests"] == 0
    assert report["codex_runs"] == 0
    assert report["model"] == "gpt-5.6-sol"
    assert report["reasoning_effort"] == "max"


def test_extracts_usage_from_codex_jsonl():
    stdout = "\n".join(
        [
            json.dumps({"type": "item.completed", "item": {"type": "reasoning"}}),
            json.dumps(
                {
                    "type": "turn.completed",
                    "usage": {"input_tokens": 100, "cached_input_tokens": 25, "output_tokens": 8},
                }
            ),
        ]
    )
    assert judge._codex_usage(stdout) == {
        "input_tokens": 100,
        "cached_input_tokens": 25,
        "output_tokens": 8,
    }


def test_resume_reuses_only_matching_completed_tasks(tmp_path):
    task = {
        "id": "same",
        "instruction_sha256": "1" * 64,
        "candidates": [{"content_hash": "a" * 64}],
        "status": "judged",
        "judgment": {"labels": [{"rank": 1, "label": "E", "reason": "direct"}]},
    }
    failed = {
        "id": "retry",
        "instruction_sha256": "2" * 64,
        "candidates": [{"content_hash": "b" * 64}],
        "status": "failed",
    }
    output = tmp_path / "result.json"
    output.write_text(
        json.dumps(
            {
                "created_at": "old-time",
                "source_replay": {"sha256": "f" * 64},
                "evaluation": {
                    "top_k": 5,
                    "judge": {"model": judge.MODEL, "reasoning_effort": judge.REASONING_EFFORT},
                },
                "tasks": [task, failed],
            }
        ),
        encoding="utf-8",
    )

    created_at, reusable = judge._load_resumable_tasks(
        output, "f" * 64, [task, failed], 5
    )

    assert created_at == "old-time"
    assert list(reusable) == ["same"]


def test_resume_rejects_changed_source(tmp_path):
    output = tmp_path / "result.json"
    output.write_text(
        json.dumps(
            {
                "source_replay": {"sha256": "a" * 64},
                "evaluation": {
                    "top_k": 5,
                    "judge": {"model": judge.MODEL, "reasoning_effort": judge.REASONING_EFFORT},
                },
                "tasks": [],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="source replay digest changed"):
        judge._load_resumable_tasks(output, "b" * 64, [], 5)
