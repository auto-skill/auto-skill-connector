from __future__ import annotations

from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


def test_droplet_deploy_establishes_irreversible_privacy_floor() -> None:
    script = (BACKEND / "deploy" / "deploy_droplet.sh").read_text(encoding="utf-8")

    inspector_stop_at = script.index("stop db-inspector")
    stop_at = script.index("stop api admin-local mcp litestream")
    scrub_at = script.index("scrub_route_privacy.py --apply")
    purge_at = script.index("purge-route-db-backups.sh --purge")
    restart_at = script.index("Starting a fresh sanitized Litestream generation")
    verify_at = script.index("--require-litestream")
    marker_at = script.index("touch '${PRIVACY_MARKER}'")

    assert inspector_stop_at < stop_at < scrub_at < purge_at < restart_at < verify_at < marker_at
    assert "up -d db-inspector" not in script
    assert "refusing to restore pre-privacy images" in script
    assert "up -d --force-recreate library-backup" in script
    assert ".local_skills.db-litestream" in script
    assert "PYTHON_BIN=python3" in script
    assert "route privacy -> clean" in script
    assert "127.0.0.1:8002/admin" in script
    assert "admin port 8002 is not exclusively loopback-bound" in script


def test_backup_purge_is_scoped_away_from_skill_library() -> None:
    script = (BACKEND / "deploy" / "purge-route-db-backups.sh").read_text(encoding="utf-8")
    compose = (BACKEND / "deploy" / "docker-compose.yml").read_text(encoding="utf-8")

    assert "LITESTREAM_REPLICA_PREFIX:-local_skills.db" in script
    assert "R2_DB_BACKUP_PREFIX:-alpha-host-backups" in script
    assert '"skills-library"|"skills-library/"' in script
    assert "validate_library_prefix" in script
    assert 'validate_library_prefix "$library_prefix"' in script
    assert "purge-route-db-backups.sh:/scripts/purge-route-db-backups.sh:ro" in compose
