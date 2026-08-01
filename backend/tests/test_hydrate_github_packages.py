from __future__ import annotations

import io
import tarfile

import pytest

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
