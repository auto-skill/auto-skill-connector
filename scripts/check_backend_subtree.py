from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MARKER = ROOT / ".autoskill-backend-subtree.json"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def fail(message: str) -> int:
    print(f"[FAIL] {message}")
    return 1


def pass_(message: str) -> None:
    print(f"[PASS] {message}")


def load_marker() -> dict:
    if not MARKER.exists():
        raise FileNotFoundError(f"missing {MARKER}")
    return json.loads(MARKER.read_text(encoding="utf-8"))


def git_ls_remote(url: str, branch: str) -> str | None:
    proc = subprocess.run(
        ["git", "ls-remote", url, f"refs/heads/{branch}"],
        cwd=ROOT,
        text=True,
        capture_output=True,
        timeout=30,
    )
    if proc.returncode != 0:
        print(f"[WARN] could not query upstream: {(proc.stderr or proc.stdout).strip()}")
        return None
    first = (proc.stdout or "").strip().split()
    return first[0] if first else None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Check the combined repo backend subtree metadata.")
    parser.add_argument("--check-remote", action="store_true", help="warn if upstream main has moved beyond the marker")
    args = parser.parse_args(argv)

    marker = load_marker()
    failures: list[str] = []
    upstream_commit = str(marker.get("upstream_commit") or "")
    subtree_path = str(marker.get("subtree_path") or "")
    upstream_url = str(marker.get("upstream_url") or "")
    upstream_branch = str(marker.get("upstream_branch") or "")

    if not SHA_RE.match(upstream_commit):
        failures.append("marker upstream_commit must be a 40-character git SHA")
    if subtree_path != "backend":
        failures.append("marker subtree_path must be backend")
    if not upstream_url.startswith("https://github.com/auto-skill/auto-skill"):
        failures.append("marker upstream_url does not point at the standalone backend repo")
    if upstream_branch != "main":
        failures.append("marker upstream_branch must be main")

    required_paths = [
        ROOT / "backend" / "launch_check.py",
        ROOT / "backend" / "deploy" / "compose_preflight.py",
        ROOT / "backend" / "deploy" / ".env.ci",
        ROOT / "backend" / "tests" / "test_compose_preflight.py",
        ROOT / ".github" / "workflows" / "backend-ci.yml",
    ]
    for path in required_paths:
        if not path.exists():
            failures.append(f"missing required backend subtree file: {path.relative_to(ROOT)}")

    nested_workflow = ROOT / "backend" / ".github" / "workflows" / "ci.yml"
    if nested_workflow.exists():
        failures.append("backend/.github/workflows/ci.yml should stay removed; use top-level backend-ci.yml")

    workflow_path = ROOT / ".github" / "workflows" / "backend-ci.yml"
    workflow = workflow_path.read_text(encoding="utf-8") if workflow_path.exists() else ""
    workflow_needles = [
        'branches: ["**"]',
        "working-directory: backend",
        "python deploy/compose_preflight.py --env-file deploy/.env.ci --skip-seed-checks",
        "python launch_check.py --env-file deploy/.env.ci --skip-local --skip-http --skip-docker",
    ]
    for needle in workflow_needles:
        if needle not in workflow:
            failures.append(f"backend-ci.yml missing {needle!r}")

    if failures:
        for item in failures:
            print(f"[FAIL] {item}")
        return 1

    pass_(f"backend subtree marker points to {upstream_commit[:7]} on {upstream_branch}")
    pass_("combined repo backend CI layout is consistent")

    if args.check_remote:
        remote_sha = git_ls_remote(upstream_url, upstream_branch)
        if remote_sha and remote_sha != upstream_commit:
            print(f"[WARN] upstream {upstream_branch} is {remote_sha[:7]}, marker is {upstream_commit[:7]}")
        elif remote_sha:
            pass_("marker matches upstream branch head")

    return 0


if __name__ == "__main__":
    sys.exit(main())
