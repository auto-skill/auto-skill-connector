import hashlib
import json
from pathlib import Path

from backend.bench.harbor_paired_ab import load_definition


DEFINITION = Path(__file__).parents[1] / "bench" / "harbor_screen2.json"


def _sha256(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def test_screen2_definition_pins_instructions_sources_and_capsules():
    definition = load_definition(DEFINITION)
    instruction_path = DEFINITION.parent / definition["dataset"]["instruction_snapshot"]
    instructions = json.loads(instruction_path.read_text(encoding="utf-8"))
    wrapper = definition["capsule_generation"]["wrapper"]

    assert definition["phase"] == "independent-screen-2"
    assert definition["required_task_count"] == len(definition["tasks"]) == 2
    assert definition["dataset"]["name"] == "terminal-bench/terminal-bench-2-1"
    assert definition["dataset"]["ref"].startswith("sha256:")
    assert definition["agent"] == {
        "name": "codex",
        "model": "gpt-5.6-sol",
        "reasoning_effort": "max",
        "reasoning_summary": "none",
        "web_search": "disabled",
        "version": "0.145.0",
    }

    for task in definition["tasks"]:
        instruction = instructions[task["task_instruction_key"]]
        assert _sha256(instruction.encode("utf-8")) == task["task_instruction_sha256"]
        assert task["source_content_hash_verified"] is True
        assert len(task["source_content_hash"]) == 64
        assert len(task["served_body_sha256"]) == 64

        capsule_path = DEFINITION.parent / task["capsule"]
        frozen = capsule_path.read_bytes()
        assert _sha256(frozen) == task["capsule_sha256"]

        text = frozen.decode("utf-8")
        prefix = wrapper + "\n\n"
        assert text.startswith(prefix)
        unwrapped = text[len(prefix) :].removesuffix("\n")
        assert len(unwrapped) == task["capsule_chars_unwrapped"]
        assert _sha256(unwrapped.encode("utf-8")) == task["capsule_unwrapped_sha256"]
        assert unwrapped.startswith("[Auto-Skill capsule v1]\n")


def test_screen2_results_reject_ceiling_screen_with_cell_provenance():
    results_path = DEFINITION.with_name("harbor_screen2_results.json")
    results = json.loads(results_path.read_text(encoding="utf-8"))

    assert results["status"] == "screen-rejected"
    assert results["summary"]["treatment_wins"] == 0
    assert results["summary"]["treatment_losses"] == 0
    assert results["summary"]["ties"] == 2
    assert len(results["cells"]) == 4
    assert len({cell["trial_id"] for cell in results["cells"]}) == 4
    assert all(cell["reward"] == 1.0 for cell in results["cells"])
    assert all(cell["task_ref"].startswith("sha256:") for cell in results["cells"])
    treatment = [cell for cell in results["cells"] if cell["arm"] == "treatment"]
    assert all(len(cell["capsule_sha256"]) == 64 for cell in treatment)
    assert results["excluded_runs"][0]["disposition"].startswith("excluded")
