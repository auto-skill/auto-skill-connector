import hashlib
import json
from pathlib import Path


BENCH_DIR = Path(__file__).parents[1] / "bench"


def test_screen4_rejected_result_has_cell_and_capsule_provenance() -> None:
    result = json.loads(
        (BENCH_DIR / "harbor_screen4_results.json").read_text(encoding="utf-8")
    )

    assert result["status"] == "screen-rejected"
    assert result["summary"]["adapted_wins"] == 0
    assert result["summary"]["ties"] == 2
    assert len(result["cells"]) == 4
    assert len({cell["trial_id"] for cell in result["cells"]}) == 4
    assert all(cell["reward"] == 0.0 for cell in result["cells"])

    by_task = {item["task"]: item for item in result["adaptation_artifacts"]}
    for slug in ("dna-insert", "pypi-server"):
        capsule = BENCH_DIR / "harbor_capsules_screen4" / f"{slug}-adapted.md"
        digest = hashlib.sha256(capsule.read_bytes()).hexdigest()
        assert digest == by_task[f"terminal-bench/{slug}"]["capsule_sha256"]

    for cell in result["cells"]:
        assert cell["task_ref"].startswith("sha256:")
        assert len(cell["task_checksum"]) == 64
        if cell["arm"] == "adapted":
            assert len(cell["capsule_sha256"]) == 64
