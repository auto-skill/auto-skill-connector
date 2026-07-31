from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_compose_mounts_rotating_oidc_file_and_shared_mirror() -> None:
    compose = (ROOT / "backend/deploy/docker-compose.yml").read_text(encoding="utf-8")
    assert "/run/secrets/autoskill-oidc/skills_sh_oidc_token" in compose
    assert "./oidc:/run/secrets/autoskill-oidc:ro" in compose
    assert "SKILLS_SH_MIRROR_DB_PATH:-/data/local_skills.db" in compose


def test_env_example_keeps_mirror_on_shared_data_volume() -> None:
    env_example = (ROOT / "backend/deploy/.env.example").read_text(encoding="utf-8")
    assert "SKILLS_SH_MIRROR_DB_PATH=/data/local_skills.db" in env_example
    assert "SKILLS_SH_MIRROR_DB_PATH=backend/.skills_sh_mirror.db" not in env_example
    assert "SKILLS_SH_OIDC_TOKEN_FILE=/run/secrets/autoskill-oidc/skills_sh_oidc_token" in env_example


def test_refresh_and_verify_scripts_are_present() -> None:
    deploy = ROOT / "backend/deploy"
    refresh = (deploy / "refresh-skills-sh-oidc.sh").read_text(encoding="utf-8")
    verify = (deploy / "verify-skills-sh-mirror.sh").read_text(encoding="utf-8")
    refresh_and_sync = (deploy / "refresh-and-sync-skills-sh.sh").read_text(encoding="utf-8")
    assert 'VERCEL_PROJECT="${VERCEL_PROJECT:-}"' in refresh
    assert 'docker compose -f "$COMPOSE_FILE" run' in verify
    assert "--all-listings" in refresh_and_sync
    assert "refresh-skills-sh-oidc.sh" in refresh_and_sync
