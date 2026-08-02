from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEPLOY = ROOT / "deploy"


def test_hydrator_compose_service_is_bounded_and_one_shot() -> None:
    compose = (DEPLOY / "docker-compose.yml").read_text(encoding="utf-8")
    block = compose.split("  api:", 1)[0]
    assert 'profiles: ["hydrator"]' in block
    assert 'mem_limit: ${HYDRATOR_MEMORY_LIMIT:-512m}' in block
    assert 'cpus: "${HYDRATOR_CPU_LIMIT:-0.50}"' in block
    assert "pids_limit: ${HYDRATOR_PIDS_LIMIT:-96}" in block
    assert 'restart: "no"' in block
    assert "init: true" in block


def test_every_hydration_lane_uses_shared_lock_and_api_guard() -> None:
    for name in (
        "hydrate-source-packages.sh",
        "run-closure-repair.sh",
        "run-transient-retry.sh",
    ):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        assert "AUTOSKILL_HYDRATOR_LOCK" in text
        assert "flock -n" in text
        assert "AUTOSKILL_API_HEALTH_URL" in text
        assert "curl -fsS --max-time 5" in text
