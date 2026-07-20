"""Regression gates for deployment transport and diagnostic persistence."""

from __future__ import annotations

import unittest
from pathlib import Path


BACKEND = Path(__file__).resolve().parents[1]


class DeployTransportTests(unittest.TestCase):
    def test_droplet_deploy_retries_only_safe_transport_bootstrap(self) -> None:
        script = (BACKEND / "deploy" / "deploy_droplet.sh").read_text(encoding="utf-8")

        connectivity_at = script.index('retry_transport "Checking SSH connectivity')
        archive_at = script.index('retry_transport "Shipping deployment archive"')
        extract_at = script.index('echo "==> Extracting and syncing')

        self.assertIn("AUTOSKILL_DEPLOY_TRANSPORT_ATTEMPTS", script)
        self.assertIn("ConnectTimeout=15", script)
        self.assertIn("ServerAliveInterval=10", script)
        self.assertIn("AUTOSKILL_DEPLOY_REMOTE_SUDO", script)
        self.assertIn("SSH+=(sudo -n)", script)
        self.assertIn('bash -s --', script)
        self.assertLess(connectivity_at, archive_at)
        self.assertLess(archive_at, extract_at)
        self.assertIn("FAILED: ${label} after ${TRANSPORT_ATTEMPTS} attempts", script)

    def test_deploy_workflow_preserves_diagnostics_after_failure(self) -> None:
        workflow = (BACKEND.parent / ".github" / "workflows" / "deploy.yml").read_text(encoding="utf-8")

        self.assertIn("set -o pipefail", workflow)
        self.assertIn('tee "$RUNNER_TEMP/autoskill-deploy/deploy.log"', workflow)
        self.assertIn("if: always()", workflow)
        self.assertIn("continue-on-error: true", workflow)
        self.assertIn("actions/upload-artifact@v4", workflow)
        self.assertIn("retention-days: 30", workflow)
