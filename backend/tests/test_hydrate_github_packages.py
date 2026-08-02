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
