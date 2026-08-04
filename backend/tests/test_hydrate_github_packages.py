from __future__ import annotations

import io
import subprocess
import tarfile

import pytest

import hydrate_github_packages as hydrator
from hydrate_github_packages import parse_github_url, read_archive, select_package_files


def _archive(files: dict[str, bytes]) -> bytes:
    out = io.BytesIO()
    with tarfile.open(fileobj=out, mode="w:gz") as tar:
        for path, content in files.items():
            info = tarfile.TarInfo(path)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return out.getvalue()


def test_parse_tree_url_keeps_skill_boundary_and_entrypoint() -> None:
    parsed = parse_github_url("https://github.com/acme/repo/tree/main/skills/report")
    assert parsed == {
        "owner": "acme",
        "repo": "repo",
        "ref": "main",
        "scope": "skills/report",
        "entrypoint": "skills/report/SKILL.md",
    }


def test_read_archive_strips_codeload_wrapper() -> None:
    files = read_archive(_archive({"repo-main/SKILL.md": b"body", "repo-main/ref.md": b"ref"}))
    assert files == {"SKILL.md": b"body", "ref.md": b"ref"}


def test_read_archive_narrows_broad_repository_to_named_skill() -> None:
    files = read_archive(
        _archive(
            {
                "repo-main/skills/foo/SKILL.md": b"foo",
                "repo-main/skills/foo/ref.md": b"ref",
                "repo-main/skills/bar/SKILL.md": b"bar",
            }
        ),
        entrypoint="SKILL.md",
        skill_name="foo",
        repo="repo",
    )
    assert files == {"skills/foo/SKILL.md": b"foo", "skills/foo/ref.md": b"ref"}


def test_select_package_files_rejects_missing_entrypoint() -> None:
    with pytest.raises(ValueError, match="no SKILL.md"):
        select_package_files({"skills/report/README.md": b"readme"}, "skills/report", "skills/report/SKILL.md")


def test_select_package_files_rejects_ambiguous_entrypoint() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        select_package_files(
            {"one/SKILL.md": b"one", "two/SKILL.md": b"two"},
            "",
            "SKILL.md",
        )


def test_git_run_terminates_process_tree_on_timeout(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    class FakeProcess:
        pid = 1234
        returncode = None

        def __init__(self) -> None:
            self.communicate_calls: list[int | None] = []
            self.killed = False

        def communicate(self, timeout=None):
            self.communicate_calls.append(timeout)
            if len(self.communicate_calls) == 1:
                raise subprocess.TimeoutExpired(["git"], timeout)
            self.returncode = -9
            return b"", b""

        def kill(self) -> None:
            self.killed = True

    process = FakeProcess()
    monkeypatch.setattr(hydrator.subprocess, "Popen", lambda *args, **kwargs: process)
    terminated: list[object] = []
    monkeypatch.setattr(hydrator, "_terminate_process_tree", terminated.append)

    with pytest.raises(subprocess.TimeoutExpired):
        hydrator._git_run(["git", "fetch"], cwd=tmp_path, timeout=1)

    assert terminated == [process]
    assert process.communicate_calls == [1, 5]


def test_choose_skill_entrypoint_narrows_broad_repository_by_catalog_name() -> None:
    assert hydrator._choose_skill_entrypoint(
        ["skills/report/SKILL.md", "skills/browser/SKILL.md"],
        "report",
        "multi-skill-repo",
    ) == "skills/report/SKILL.md"


def test_choose_skill_entrypoint_refuses_unmatched_ambiguity() -> None:
    with pytest.raises(ValueError, match="ambiguous"):
        hydrator._choose_skill_entrypoint(
            ["skills/one/SKILL.md", "skills/two/SKILL.md"],
            "unrelated",
            "multi-skill-repo",
        )


def test_read_git_scope_returns_each_declared_blob(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_git_run(args: list[str], *, cwd, timeout=hydrator.GIT_FALLBACK_TIMEOUT):
        del cwd, timeout
        if "ls-tree" in args:
            output = b"100644 blob deadbeef 4\tSKILL.md\0"
        elif "cat-file" in args:
            output = b"body"
        else:
            output = b""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(hydrator, "_git_run", fake_git_run)
    assert hydrator.read_git_scope("acme", "repo", "0" * 40) == {"SKILL.md": b"body"}


def test_read_git_scope_narrows_root_repository_to_named_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entries = {
        "skills/foo/SKILL.md": (b"foo", "a" * 40),
        "skills/foo/ref.md": (b"ref", "b" * 40),
        "skills/bar/SKILL.md": (b"bar", "c" * 40),
    }
    by_sha = {sha: body for body, sha in entries.values()}

    def fake_git_run(args: list[str], *, cwd, timeout=hydrator.GIT_FALLBACK_TIMEOUT):
        del cwd, timeout
        if "ls-tree" in args:
            output = b"".join(
                f"100644 blob {sha} {len(body)}\t{path}\0".encode()
                for path, (body, sha) in entries.items()
            )
        elif args[:3] == ["git", "cat-file", "blob"]:
            output = by_sha[args[-1]]
        else:
            output = b""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(hydrator, "_git_run", fake_git_run)
    assert hydrator.read_git_scope(
        "acme", "repo", "0" * 40, entrypoint="SKILL.md", skill_name="foo"
    ) == {"skills/foo/SKILL.md": b"foo", "skills/foo/ref.md": b"ref"}


def test_read_git_scope_expands_relative_reference_outside_scope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    entrypoint = "skills/foo/SKILL.md"
    commit = "0" * 40
    entry_content = b"---\nname: foo\ndescription: foo skill\n---\n[shared](../shared.md)"

    def fake_git_run(args: list[str], *, cwd, timeout=hydrator.GIT_FALLBACK_TIMEOUT):
        del cwd, timeout
        if "ls-tree" in args:
            output = (
                b"100644 blob " + b"a" * 40 + f" {len(entry_content)}\t".encode()
                + entrypoint.encode() + b"\0"
            )
        elif args[:3] == ["git", "cat-file", "blob"] and args[-1] == "a" * 40:
            output = entry_content
        elif args[:2] == ["git", "rev-parse"]:
            output = b"b" * 40 + b"\n"
        elif args[:3] == ["git", "cat-file", "-t"]:
            output = b"blob\n"
        elif args[:3] == ["git", "cat-file", "-s"]:
            output = b"6\n"
        elif "cat-file" in args:
            output = b"shared"
        else:
            output = b""
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr=b"")

    monkeypatch.setattr(hydrator, "_git_run", fake_git_run)
    files = hydrator.read_git_scope("acme", "repo", commit, "skills/foo", entrypoint=entrypoint)
    assert files[entrypoint].endswith(b"../shared.md)")
    assert files["skills/shared.md"] == b"shared"
