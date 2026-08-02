"""Hydrate complete GitHub-backed packages without GitHub API search quota.

This is an operator-side repair path for existing GitHub/SkillsMP/curated rows.
It uses GitHub's public codeload archive plus smart-HTTP ref advertisement for
the immutable commit, then stores every file in the package CAS. A package is
never installed when the archive, entrypoint, dependency closure, or package
limits are incomplete.
"""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import tarfile
import tempfile
import threading
from pathlib import Path, PurePosixPath
from urllib.parse import quote

import httpx

import local_store as store
from capsule_compiler import strip_unsafe_content
from package_store import (
    MAX_FILE_BYTES,
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_FILES,
    ImmutablePackageStore,
    PackageFileInput,
    _is_text,
    _relative_references,
    build_package_manifest,
)
from quality import canonicalize_skill_content, content_hash, evaluate_quality


GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+)"
    r"(?:/(?P<kind>tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>[^?#]+))?)?"
)
PACKAGE_SOURCES = {"github", "github_skill_file", "skillsmp", "awesome_list"}
STATE_NAME = "github_package_hydration_state.json"
# Keep the compressed transfer bounded near the package CAS limit.  A larger
# tarball can expand into a memory spike before the 25 MiB package validator
# gets a chance to reject it, repeatedly killing the isolated worker.
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
# A large repository archive is not evidence that the scoped skill is large.
# When codeload cannot be used, fetch only the immutable commit's requested
# tree through Git's partial-clone protocol.  The fallback still refuses a
# package that exceeds the package limits; it never clips a file or silently
# drops a path.
GIT_FALLBACK_TIMEOUT = 180
MAX_GIT_TREE_LIST_BYTES = 8 * 1024 * 1024
_LIBRARY_WRITE_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()
# URLs are overwhelmingly unique in the catalog; retaining more than the
# current archive only increases memory pressure without meaningful reuse.
MAX_THREAD_CACHE_ENTRIES = 1


def parse_github_url(url: str) -> dict | None:
    match = GITHUB_URL_RE.match(str(url or "").rstrip("/"))
    if not match:
        return None
    owner = match.group("owner")
    repo = match.group("repo").removesuffix(".git")
    ref = match.group("ref") or "HEAD"
    path = (match.group("path") or "").strip("/")
    if match.group("kind") == "blob":
        entrypoint = path
        scope = path.rsplit("/", 1)[0] if "/" in path else ""
    elif match.group("kind") == "tree":
        scope = path
        entrypoint = f"{scope}/SKILL.md" if scope else "SKILL.md"
    else:
        scope = ""
        entrypoint = "SKILL.md"
    return {"owner": owner, "repo": repo, "ref": ref, "scope": scope, "entrypoint": entrypoint}


def resolve_commit(client: httpx.Client, owner: str, repo: str, ref: str) -> str:
    # GitHub's smart-HTTP ref advertisement is not subject to the REST API
    # search quota and is available in the slim API container.
    info_url = f"https://github.com/{owner}/{repo}/info/refs"
    response = client.get(info_url, params={"service": "git-upload-pack"}, timeout=45)
    if response.status_code == 200:
        text = response.content.decode("utf-8", errors="ignore")
        escaped_ref = re.escape(ref)
        match = re.search(rf"([a-f0-9]{{40}})\s+refs/heads/{escaped_ref}(?:\x00|\n|\r)", text)
        if match:
            return match.group(1)
        if ref == "HEAD":
            match = re.search(r"symref=HEAD:refs/heads/[^\x00 ]+", text)
            if match:
                branch = match.group(0).split("refs/heads/", 1)[1]
                branch_match = re.search(rf"([a-f0-9]{{40}})\s+refs/heads/{re.escape(branch)}(?:\x00|\n|\r)", text)
                if branch_match:
                    return branch_match.group(1)
    # Local/offline development fallback; production does not require git.
    result = subprocess.run(
        ["git", "ls-remote", f"https://github.com/{owner}/{repo}.git", ref, "HEAD"],
        check=True,
        capture_output=True,
        text=True,
        timeout=45,
    )
    for line in result.stdout.splitlines():
        sha = line.split()[0] if line.split() else ""
        if re.fullmatch(r"[a-f0-9]{40}", sha):
            return sha
    raise RuntimeError(f"could not resolve immutable commit for {owner}/{repo}@{ref}")


def _safe_member_name(name: str) -> str:
    parts = PurePosixPath(name).parts
    if any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"unsafe archive path: {name}")
    return "/".join(parts)


def read_archive(
    raw: bytes,
    scope: str = "",
    *,
    entrypoint: str | None = None,
    skill_name: str = "",
    repo: str = "",
) -> dict[str, bytes]:
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} byte safety limit")
    # Scan names first so a root/broad archive can be narrowed to one named
    # skill before any unrelated file bytes are extracted. The raw tarball is
    # already bounded, so a second pass is safe and deterministic.
    members: list[tuple[str, int, bool]] = []
    wrapper = ""
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        for member in archive:
            name = _safe_member_name(member.name)
            if not wrapper:
                wrapper = name.split("/", 1)[0]
            if not name.startswith(wrapper + "/"):
                continue
            relative = name[len(wrapper) + 1:]
            unsupported = member.issym() or member.islnk() or not (
                member.isfile() or member.isdir()
            )
            members.append((relative, int(member.size or 0), unsupported))

    effective_scope = scope.strip("/")
    effective_entrypoint = entrypoint
    candidate_paths = [
        path for path, _size, unsupported in members
        if not unsupported and (path.casefold().endswith("/skill.md") or path.casefold() == "skill.md")
    ]
    scoped_candidates = [
        path for path in candidate_paths
        if not effective_scope
        or path == effective_scope
        or path.startswith(effective_scope.rstrip("/") + "/")
    ]
    if skill_name and scoped_candidates and (
        not effective_scope
        or len(scoped_candidates) != 1
        or len(members) > MAX_PACKAGE_FILES
    ):
        effective_entrypoint = _choose_skill_entrypoint(scoped_candidates, skill_name, repo)
        parent = str(PurePosixPath(effective_entrypoint).parent)
        effective_scope = "" if parent == "." else parent
    if skill_name and effective_entrypoint and not scoped_candidates:
        raise ValueError("package has no SKILL.md entrypoint")
    if not effective_entrypoint and scoped_candidates and skill_name:
        effective_entrypoint = _choose_skill_entrypoint(scoped_candidates, skill_name, repo)
        parent = str(PurePosixPath(effective_entrypoint).parent)
        effective_scope = "" if parent == "." else parent

    def selected(path: str) -> bool:
        if effective_scope:
            return path == effective_scope or path.startswith(effective_scope.rstrip("/") + "/")
        if effective_entrypoint:
            return path == effective_entrypoint
        return not scope or path == scope or path.startswith(scope.rstrip("/") + "/")

    selected_members = [item for item in members if selected(item[0])]
    if len(selected_members) > MAX_PACKAGE_FILES:
        raise ValueError("scoped tree exceeds package file limit")
    total_bytes = sum(size for _path, size, unsupported in selected_members if not unsupported)
    if total_bytes > MAX_PACKAGE_BYTES:
        raise ValueError("scoped tree exceeds package byte limit")

    files: dict[str, bytes] = {}
    selected_paths = {path for path, _size, _unsupported in selected_members}
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r:gz") as archive:
        wrapper = ""
        for member in archive:
            name = _safe_member_name(member.name)
            if not wrapper:
                wrapper = name.split("/", 1)[0]
            if not name.startswith(wrapper + "/"):
                continue
            relative = name[len(wrapper) + 1:]
            if relative not in selected_paths:
                continue
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported archive entry: {member.name}")
            if not member.isfile():
                continue
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"could not read archive entry: {member.name}")
            content = extracted.read()
            if len(content) != int(member.size or 0):
                raise ValueError(f"archive member size mismatch: {member.name}")
            files[relative] = content
    return files


def download_archive(client: httpx.Client, url: str) -> bytes:
    """Download with a hard compressed-size cap before buffering in memory."""
    with client.stream("GET", url, timeout=120) as response:
        response.raise_for_status()
        declared = int(response.headers.get("content-length") or 0)
        if declared > MAX_ARCHIVE_BYTES:
            raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} byte safety limit")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_bytes():
            total += len(chunk)
            if total > MAX_ARCHIVE_BYTES:
                raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} byte safety limit")
            chunks.append(chunk)
    return b"".join(chunks)


def _git_run(args: list[str], *, cwd: Path, timeout: int = GIT_FALLBACK_TIMEOUT) -> subprocess.CompletedProcess:
    """Run a non-interactive Git command for the scoped capture fallback."""
    env = os.environ.copy()
    env["GIT_TERMINAL_PROMPT"] = "0"
    return subprocess.run(
        args,
        cwd=str(cwd),
        check=True,
        capture_output=True,
        timeout=timeout,
        env=env,
    )


def _git_reference_blob(root: Path, commit_sha: str, path: str) -> bytes | None:
    """Read one immutable blob referenced by a captured file.

    A scoped skill may legitimately link to ``../shared.md`` outside its
    directory.  The first tree listing intentionally avoids the rest of a
    large repository, so resolve those paths lazily through the already
    fetched immutable commit.  Missing/glob-like references remain unresolved
    and are reported by ``build_package_manifest``; they are never replaced by
    a truncated or synthetic file.
    """
    try:
        blob_sha = _git_run(["git", "rev-parse", f"{commit_sha}:{path}"], cwd=root).stdout.decode("ascii").strip()
        if not re.fullmatch(r"[0-9a-f]{40}", blob_sha):
            return None
        kind = _git_run(["git", "cat-file", "-t", blob_sha], cwd=root).stdout.decode("ascii").strip()
        if kind != "blob":
            return None
        size_text = _git_run(["git", "cat-file", "-s", blob_sha], cwd=root).stdout.decode("ascii").strip()
        expected_size = int(size_text)
        if expected_size < 0 or expected_size > MAX_FILE_BYTES:
            raise ValueError(f"referenced file exceeds safety limit: {path}")
        content = _git_run(["git", "cat-file", "blob", blob_sha], cwd=root).stdout
        if len(content) != expected_size:
            raise ValueError(f"Git blob size mismatch: {path}")
        return content
    except (subprocess.CalledProcessError, UnicodeDecodeError, ValueError):
        return None


def _expand_git_dependency_closure(
    root: Path,
    commit_sha: str,
    entrypoint: str,
    files: dict[str, bytes],
) -> None:
    """Add resolvable relative references outside the initial skill scope.

    This mutates ``files`` only with complete, hashable Git blobs.  A missing
    or non-literal reference is intentionally left for the manifest to mark as
    unresolved rather than guessed or partially fetched.
    """
    queue = [entrypoint]
    seen: set[str] = set()
    total_bytes = sum(len(value) for value in files.values())
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        content = files.get(path)
        if content is None or not _is_text(content):
            continue
        text = content.decode("utf-8", errors="replace")
        for reference in _relative_references(path, text):
            if reference.startswith("UNRESOLVED_OUTSIDE:"):
                continue
            if reference not in files:
                fetched = _git_reference_blob(root, commit_sha, reference)
                if fetched is None:
                    continue
                if len(files) >= MAX_PACKAGE_FILES:
                    raise ValueError("dependency closure exceeds package file limit")
                total_bytes += len(fetched)
                if total_bytes > MAX_PACKAGE_BYTES:
                    raise ValueError("dependency closure exceeds package byte limit")
                files[reference] = fetched
            queue.append(reference)


def _normalise_skill_label(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _choose_skill_entrypoint(paths: list[str], skill_name: str, repo: str) -> str:
    """Choose one unambiguous SKILL.md from a repo/broad scope.

    Root-repository catalog rows often point at a multi-skill repository rather
    than the exact directory.  Capturing that entire repository is both
    wasteful and unsafe (it can exceed package limits).  We narrow only when
    the catalog name gives a unique, explainable match; ties remain rejected.
    """
    candidates = sorted(set(paths))
    if not candidates:
        raise ValueError("package has no SKILL.md entrypoint")
    if len(candidates) == 1:
        return candidates[0]
    targets = {
        label for label in (_normalise_skill_label(skill_name), _normalise_skill_label(repo))
        if label
    }
    scored: list[tuple[int, str]] = []
    for path in candidates:
        parent = PurePosixPath(path).parent
        labels = [_normalise_skill_label(part) for part in parent.parts]
        parent_label = labels[-1] if labels else ""
        score = 0
        for target in targets:
            if parent_label == target:
                score = max(score, 100)
            elif not labels and target == _normalise_skill_label(repo):
                score = max(score, 90)
            elif target and target in labels:
                score = max(score, 70)
            elif target and any(target in label or label in target for label in labels if label):
                score = max(score, 35)
        # Prefer a shallower exact match only after label evidence, never by
        # depth alone; arbitrary lexical selection could capture the wrong skill.
        score = score * 100 - len(parent.parts)
        scored.append((score, path))
    scored.sort(reverse=True)
    if scored[0][0] <= 0 or scored[0][0] == scored[1][0]:
        raise ValueError("package has ambiguous SKILL.md entrypoint")
    return scored[0][1]


def read_git_scope(
    owner: str,
    repo: str,
    commit_sha: str,
    scope: str = "",
    *,
    entrypoint: str | None = None,
    skill_name: str = "",
) -> dict[str, bytes]:
    """Capture a complete scoped tree without downloading an oversized repo.

    GitHub codeload returns an archive for the whole repository.  For large
    repositories that archive can exceed our bounded transfer cap even when a
    selected skill directory is small.  A blob-filtered fetch obtains the
    immutable tree and lazily reads only the requested blobs.  The tree is
    enumerated before any content is stored, and every returned blob is checked
    against Git's declared size.
    """
    with tempfile.TemporaryDirectory(prefix="autoskill-git-") as temp_name:
        root = Path(temp_name)
        _git_run(["git", "init", "--quiet"], cwd=root)
        _git_run(
            ["git", "remote", "add", "origin", f"https://github.com/{owner}/{repo}.git"],
            cwd=root,
        )
        _git_run(
            [
                "git",
                "-c",
                "protocol.version=2",
                "fetch",
                "--quiet",
                "--filter=blob:none",
                "--depth=1",
                "--no-tags",
                "origin",
                commit_sha,
            ],
            cwd=root,
        )
        listing = _git_run(
            ["git", "ls-tree", "-r", "-z", "-l", "FETCH_HEAD", "--", scope] if scope else
            ["git", "ls-tree", "-r", "-z", "-l", "FETCH_HEAD"],
            cwd=root,
        ).stdout
        if len(listing) > MAX_GIT_TREE_LIST_BYTES:
            raise ValueError("scoped Git tree listing exceeds safety limit")

        all_entries: list[tuple[str, str, str, int]] = []
        for item in listing.split(b"\0"):
            if not item:
                continue
            try:
                metadata, raw_path = item.split(b"\t", 1)
                mode_b, kind_b, sha_b, size_b = metadata.split(maxsplit=3)
                path = raw_path.decode("utf-8")
                mode = mode_b.decode("ascii")
                kind = kind_b.decode("ascii")
                sha = sha_b.decode("ascii")
                size = int(size_b)
            except (UnicodeDecodeError, ValueError) as exc:
                raise ValueError("malformed Git tree entry") from exc
            if kind != "blob" or mode not in {"100644", "100755"}:
                raise ValueError(f"unsupported Git tree entry: {mode} {kind} {path}")
            if size < 0 or size > MAX_FILE_BYTES:
                raise ValueError(f"scoped tree file exceeds safety limit: {path}")
            all_entries.append((path, mode, sha, size))

        effective_scope = scope.strip("/")
        effective_entrypoint = entrypoint
        candidate_paths = [
            path for path, _mode, _sha, _size in all_entries
            if path.casefold().endswith("/skill.md") or path.casefold() == "skill.md"
        ]
        scoped_candidates = [
            path for path in candidate_paths
            if not effective_scope
            or path == effective_scope
            or path.startswith(effective_scope.rstrip("/") + "/")
        ]
        if effective_entrypoint and not scoped_candidates:
            raise ValueError("package has no SKILL.md entrypoint")
        # A root URL or broad scope may describe many skills. Select the one
        # matching the catalog row, then capture only its directory. For a
        # root-level SKILL.md, retain the entrypoint and expand references
        # lazily; unrelated repository files are not part of this package.
        if scoped_candidates and (
            not effective_scope
            or len(scoped_candidates) != 1
            or len(all_entries) > MAX_PACKAGE_FILES
        ):
            effective_entrypoint = _choose_skill_entrypoint(scoped_candidates, skill_name, repo)
            parent = str(PurePosixPath(effective_entrypoint).parent)
            effective_scope = "" if parent == "." else parent
        elif effective_entrypoint and effective_entrypoint not in {path for path, *_ in all_entries}:
            effective_entrypoint = None

        if not effective_entrypoint and scoped_candidates:
            effective_entrypoint = _choose_skill_entrypoint(scoped_candidates, skill_name, repo)
            parent = str(PurePosixPath(effective_entrypoint).parent)
            effective_scope = "" if parent == "." else parent

        if effective_scope:
            entries = [
                item for item in all_entries
                if item[0] == effective_scope
                or item[0].startswith(effective_scope.rstrip("/") + "/")
            ]
        elif effective_entrypoint:
            entries = [item for item in all_entries if item[0] == effective_entrypoint]
        else:
            entries = all_entries
        if len(entries) > MAX_PACKAGE_FILES:
            raise ValueError("scoped tree exceeds package file limit")

        files: dict[str, bytes] = {}
        total_bytes = 0
        for path, mode, sha, expected_size in entries:
            content = _git_run(["git", "cat-file", "blob", sha], cwd=root).stdout
            if len(content) != expected_size:
                raise ValueError(f"Git blob size mismatch: {path}")
            total_bytes += len(content)
            if total_bytes > MAX_PACKAGE_BYTES:
                raise ValueError("scoped tree exceeds package byte limit")
            files[path] = content
        if entrypoint:
            _expand_git_dependency_closure(root, commit_sha, effective_entrypoint or entrypoint or "", files)
        return files


def _is_scoped_fallback_failure(value: object) -> bool:
    """Return whether a prior codeload failure is fixed by partial-clone."""
    message = str(value or "").casefold()
    return any(
        marker in message
        for marker in (
            "archive exceeds",
            "unsupported archive entry",
            "scoped tree exceeds package file limit",
            "scoped tree exceeds package byte limit",
        )
    )


def select_package_files(files: dict[str, bytes], scope: str, entrypoint: str) -> tuple[str, dict[str, bytes]]:
    scoped = {
        path: content
        for path, content in files.items()
        if not scope or path == scope or path.startswith(scope.rstrip("/") + "/")
    }
    if entrypoint not in scoped:
        candidates = sorted(path for path in scoped if path.casefold().endswith("/skill.md") or path.casefold() == "skill.md")
        if not candidates:
            raise ValueError("package has no SKILL.md entrypoint")
        if len(candidates) != 1:
            raise ValueError("package has ambiguous SKILL.md entrypoint")
        entrypoint = candidates[0]
    return entrypoint, scoped


def _library_filename(skill: dict) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]+", "-", str(skill.get("name") or "skill")).strip("-")[:80] or "skill"
    digest = hashlib.sha1(str(skill.get("url") or "").encode()).hexdigest()[:10]
    return f"{skill.get('source', 'github')}__{safe}__{digest}.md"


def _write_curated_body(library_dir: Path, skill: dict, body: str) -> None:
    files_dir = library_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    index_path = library_dir / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8")) if index_path.exists() else {}
    except (OSError, json.JSONDecodeError):
        index = {}
    filename = _library_filename(skill)
    target = files_dir / filename
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=files_dir, delete=False) as handle:
        handle.write(body)
        temp_name = handle.name
    Path(temp_name).replace(target)
    index[str(skill["url"])] = {
        "name": skill.get("name"),
        "source": skill.get("source"),
        "url": skill.get("url"),
        "description": skill.get("description"),
        "content_hash": content_hash(body),
        "file": filename,
    }
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", newline="\n", dir=library_dir, delete=False) as handle:
        handle.write(json.dumps(index, indent=2, sort_keys=True) + "\n")
        index_temp = handle.name
    Path(index_temp).replace(index_path)


def hydrate_row(
    client: httpx.Client,
    row: sqlite3.Row,
    library_dir: Path,
    archive_cache: dict[tuple[str, str, str, str, str], dict[str, bytes]] | None = None,
    commit_cache: dict[tuple[str, str, str], str] | None = None,
) -> str:
    parsed = parse_github_url(str(row["url"] or ""))
    if not parsed:
        return "not-github"
    commit_key = (parsed["owner"], parsed["repo"], parsed["ref"])
    # The same repository can expose multiple skills. Include the requested
    # scope/name so a narrowed root capture is never reused for another row.
    archive_key = (*commit_key, parsed["scope"], str(row["name"] or ""))
    commit_cache = commit_cache if commit_cache is not None else {}
    archive_cache = archive_cache if archive_cache is not None else {}
    commit_sha = commit_cache.get(commit_key)
    if commit_sha is None:
        commit_sha = resolve_commit(client, *commit_key)
        commit_cache[commit_key] = commit_sha
    files = archive_cache.get(archive_key)
    # Root URLs are commonly repositories containing many skills. Start with
    # the immutable partial clone so we can select the matching entrypoint
    # without downloading or storing unrelated repository files.
    capture_method = "partial-clone" if not parsed["scope"] else "codeload"
    if files is None:
        if not parsed["scope"]:
            try:
                files = read_git_scope(
                    parsed["owner"], parsed["repo"], commit_sha, parsed["scope"],
                    entrypoint=parsed["entrypoint"], skill_name=str(row["name"] or ""),
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                archive_url = (
                    f"https://codeload.github.com/{parsed['owner']}/{parsed['repo']}/tar.gz/"
                    f"{quote(parsed['ref'], safe='')}"
                )
                files = read_archive(
                    download_archive(client, archive_url),
                    parsed["scope"],
                    entrypoint=parsed["entrypoint"],
                    skill_name=str(row["name"] or ""),
                    repo=parsed["repo"],
                )
                capture_method = "codeload-narrowed"
        else:
            archive_url = (
                f"https://codeload.github.com/{parsed['owner']}/{parsed['repo']}/tar.gz/"
                f"{quote(parsed['ref'], safe='')}"
            )
            try:
                files = read_archive(
                    download_archive(client, archive_url),
                    parsed["scope"],
                    entrypoint=parsed["entrypoint"],
                    skill_name=str(row["name"] or ""),
                    repo=parsed["repo"],
                )
            except ValueError as archive_error:
                # A broad scoped archive can exceed package limits because it
                # contains unrelated skills. Re-enumerate the immutable Git
                # tree and narrow to the row's matching entrypoint.
                archive_message = str(archive_error).casefold()
                if not any(
                    marker in archive_message
                    for marker in (
                        "archive exceeds",
                        "unsupported archive entry",
                        "scoped tree exceeds package file limit",
                        "scoped tree exceeds package byte limit",
                    )
                ):
                    raise
                files = read_git_scope(
                    parsed["owner"], parsed["repo"], commit_sha, parsed["scope"],
                    entrypoint=parsed["entrypoint"], skill_name=str(row["name"] or ""),
                )
                capture_method = "partial-clone"
            except httpx.HTTPError:
                files = read_git_scope(
                    parsed["owner"], parsed["repo"], commit_sha, parsed["scope"],
                    entrypoint=parsed["entrypoint"], skill_name=str(row["name"] or ""),
                )
                capture_method = "partial-clone"
        archive_cache[archive_key] = files
        archive_cache[archive_key] = files
    try:
        entrypoint, scoped = select_package_files(files, parsed["scope"], parsed["entrypoint"])
    except ValueError as selection_error:
        if "ambiguous" not in str(selection_error).casefold() or parsed["scope"]:
            raise
        files = read_git_scope(
            parsed["owner"], parsed["repo"], commit_sha, parsed["scope"],
            entrypoint=parsed["entrypoint"], skill_name=str(row["name"] or ""),
        )
        capture_method = "partial-clone"
        archive_cache[archive_key] = files
        entrypoint, scoped = select_package_files(files, parsed["scope"], parsed["entrypoint"])
    captured_root = str(PurePosixPath(entrypoint).parent)
    if captured_root == ".":
        captured_root = ""
    def build_manifest() -> tuple[dict, dict[str, bytes]]:
        package_files = [
            PackageFileInput(path=path, content=content, expected_size=len(content))
            for path, content in scoped.items()
        ]
        return build_package_manifest(
            source={
                "provider": "github",
                "owner": parsed["owner"],
                "repo": parsed["repo"],
                "requested_ref": parsed["ref"],
                "commit_sha": commit_sha,
                "root_path": captured_root,
            },
            source_url=str(row["url"]),
            entrypoint=entrypoint,
            files=package_files,
            tree_complete=True,
            provenance={"collector": f"github-{capture_method}", "immutable_ref": commit_sha},
        )

    manifest, objects = build_manifest()
    # Codeload's scoped archive deliberately omits files outside the skill
    # directory. If the entrypoint references one, use the immutable Git
    # transport to fetch only the missing closure paths and rebuild the
    # manifest. Never claim a package is complete while references remain
    # unresolved.
    if manifest.get("dependency_closure_status") != "complete":
        expanded = read_git_scope(
            parsed["owner"], parsed["repo"], commit_sha, parsed["scope"],
            entrypoint=entrypoint, skill_name=str(row["name"] or ""),
        )
        for path, content in expanded.items():
            scoped.setdefault(path, content)
        manifest, objects = build_manifest()
    if (
        manifest.get("completeness_status") != "complete"
        or manifest.get("entrypoint_truncated")
        or manifest.get("dependency_closure_status") != "complete"
    ):
        reasons = list(manifest.get("completeness_reasons") or [])
        if manifest.get("dependency_closure_status") != "complete":
            reasons.append("dependency-closure-unresolved")
        return f"incomplete:{','.join(sorted(set(reasons)))}"
    ImmutablePackageStore(library_dir / "packages").put(manifest, objects)
    body = canonicalize_skill_content(scoped[entrypoint].decode("utf-8", errors="replace"))
    stripped = strip_unsafe_content(body)
    body = canonicalize_skill_content(stripped.text) or body
    skill = {key: row[key] for key in row.keys()}
    skill.update(
        {
            "package_completeness": "complete",
            "source_commit_sha": commit_sha,
            "package_hash": manifest["package_hash"],
            "license_spdx": (manifest.get("license") or {}).get("spdx_id"),
            "dependency_closure_status": manifest.get("dependency_closure_status"),
            "entrypoint_truncated": 0,
        }
    )
    assessed = evaluate_quality(skill, body)
    # The content-addressed package store is atomic, but the legacy curated
    # index is a single JSON file. Serialize only that compatibility write so
    # bounded concurrent fetchers cannot lose each other's index entry.
    with _LIBRARY_WRITE_LOCK:
        _write_curated_body(library_dir, skill, body)
    conn = store.get_conn()
    try:
        conn.execute(
            """UPDATE skills SET package_hash=?,source_commit_sha=?,license_spdx=?,
               package_completeness=?,dependency_closure_status=?,entrypoint_truncated=?,
               content_hash=?,quality_status=?,quality_reasons=?,quality_score=?,
               prominence_score=?,provenance_score=?,meaningfulness_score=?,platforms=?,category=?,
               embedding=NULL,embedding_text_hash=NULL,embedded_at=NULL,capability_summary=NULL,triggers='[]'
               WHERE id=?""",
            (
                manifest["package_hash"], commit_sha, skill["license_spdx"], "complete",
                manifest.get("dependency_closure_status"), 0, assessed["content_hash"],
                assessed["quality_status"], json.dumps(assessed["quality_reasons"]), assessed["quality_score"],
                assessed["prominence_score"], assessed["provenance_score"], assessed["meaningfulness_score"],
                json.dumps(assessed["platforms"]), assessed["category"], row["id"],
            ),
        )
        conn.commit()
    finally:
        conn.close()
    store.upsert_skill_package(manifest, skill_id=str(row["id"]))
    return "hydrated"


def _hydrate_in_worker(row: sqlite3.Row, library_dir: Path) -> str:
    """Hydrate one row with a thread-local client/cache pair.

    Codeload and ref resolution are network-bound. A small worker pool cuts
    wall-clock time without sharing httpx clients or mutable archive caches
    across threads; package writes and per-row SQLite transactions remain
    independently atomic.
    """
    client = getattr(_THREAD_LOCAL, "client", None)
    if client is None:
        client = httpx.Client(
            follow_redirects=True,
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        )
        _THREAD_LOCAL.client = client
        _THREAD_LOCAL.archive_cache = {}
        _THREAD_LOCAL.commit_cache = {}
    try:
        return hydrate_row(
            client,
            row,
            library_dir,
            _THREAD_LOCAL.archive_cache,
            _THREAD_LOCAL.commit_cache,
        )
    finally:
        # A batch can contain hundreds of distinct repositories. Keep only a
        # small LRU-like tail per thread so archive bytes cannot exhaust the
        # hydrator cgroup; package objects are already persisted atomically.
        for cache in (_THREAD_LOCAL.archive_cache, _THREAD_LOCAL.commit_cache):
            while len(cache) > MAX_THREAD_CACHE_ENTRIES:
                cache.pop(next(iter(cache)))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path, default=store.DB_PATH)
    parser.add_argument("--library-dir", type=Path, default=Path(__file__).parent / "skills_library")
    parser.add_argument("--state", type=Path, default=Path(__file__).parent / STATE_NAME)
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--retry-failed", action="store_true")
    parser.add_argument(
        "--only-pending",
        action="store_true",
        help="select only rows currently marked pending (for transient retry lanes)",
    )
    parser.add_argument(
        "--retry-closure",
        action="store_true",
        help="rehydrate active packages whose stored dependency closure is partial",
    )
    parser.add_argument(
        "--retry-fallback",
        action="store_true",
        help="retry only archive/unsupported-entry failures handled by scoped Git capture",
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=max(1, int(os.getenv("HYDRATOR_WORKERS", "1"))),
        help="bounded concurrent network workers (default: 1)",
    )
    parser.add_argument(
        "--sources",
        default="",
        help="comma-separated source partitions (default: all package-backed sources)",
    )
    args = parser.parse_args()
    store.DB_PATH = args.db
    store.init_db()
    state = json.loads(args.state.read_text()) if args.state.exists() else {"done": {}, "failed": {}}
    state.setdefault("done", {})
    state.setdefault("failed", {})
    # Stream the catalog cursor instead of materializing every large raw row;
    # this keeps memory bounded even when the mirror contains hundreds of
    # thousands of source observations.
    requested_sources = tuple(
        item.strip() for item in str(args.sources or "").split(",") if item.strip()
    )
    sources = tuple(item for item in requested_sources if item in PACKAGE_SOURCES) or tuple(sorted(PACKAGE_SOURCES))
    statuses = ["active", "metadata_only", "pending"]
    if args.retry_fallback:
        # Scoped partial-clone capture can recover repositories previously
        # rejected only because codeload saw an unrelated oversized tree.
        statuses.append("rejected")
    status_placeholders = ",".join("?" for _ in statuses)
    placeholders = ",".join("?" for _ in sources)
    conn = store.get_conn()
    selected = []
    try:
        rows = conn.execute(
            f"SELECT * FROM skills WHERE quality_status IN ({status_placeholders}) "
            f"AND source IN ({placeholders}) "
            "ORDER BY CASE quality_status WHEN 'active' THEN 0 WHEN 'pending' THEN 1 ELSE 2 END, url, id",
            (*statuses, *sources),
        )
        for row in rows:
            if args.only_pending and str(row["quality_status"] or "") != "pending":
                continue
            url = str(row["url"])
            retry_closure = (
                args.retry_closure
                and str(row["quality_status"] or "") == "active"
                and str(row["dependency_closure_status"] or "").casefold() != "complete"
                and bool(row["package_hash"])
            )
            if url in state["done"] and not retry_closure:
                continue
            if (
                str(row["quality_status"] or "") == "rejected"
                and not args.retry_fallback
            ):
                continue
            if (
                str(row["quality_status"] or "") == "rejected"
                and (
                    url not in state["failed"]
                    or not _is_scoped_fallback_failure(state["failed"].get(url))
                )
            ):
                continue
            if url in state["failed"] and not args.retry_failed and not retry_closure:
                parsed_url = parse_github_url(url)
                if (
                    not args.retry_fallback
                    or not _is_scoped_fallback_failure(state["failed"][url])
                    or not parsed_url
                ):
                    continue
            # Hydration only needs identity/quality fields. Do not retain
            # large raw manifests, embeddings, or retrieval text for every
            # queued row; those remain authoritative in SQLite.
            compact = {
                key: row[key]
                for key in row.keys()
                if key not in {"raw", "embedding", "retrieval_text"}
            }
            compact["raw"] = "{}"
            selected.append(compact)
            if len(selected) >= max(1, args.limit):
                break
        rows.close()
    finally:
        conn.close()
    counts = {"hydrated": 0, "incomplete": 0, "failed": 0, "not-github": 0}
    worker_count = max(1, min(int(args.workers), 8))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = {
            executor.submit(_hydrate_in_worker, row, args.library_dir): row
            for row in selected
        }
        for future in as_completed(futures):
            row = futures[future]
            url = str(row["url"])
            try:
                result = future.result()
                if result == "hydrated":
                    state["done"][url] = result
                    counts[result] += 1
                elif result.startswith("incomplete:"):
                    state["failed"][url] = result
                    conn = store.get_conn()
                    try:
                        conn.execute(
                            "UPDATE skills SET quality_status='rejected', package_completeness='incomplete', "
                            "quality_reasons=? WHERE id=?",
                            (json.dumps(["package-incomplete", result], sort_keys=True), row["id"]),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    counts["incomplete"] += 1
                elif result == "not-github":
                    # The source gate selected this row because its registry
                    # claims GitHub-backed instructions, but the URL is not a
                    # verifiable GitHub tree.  Leaving it active would make
                    # the package audit impossible to bring to zero forever.
                    state["failed"][url] = result
                    conn = store.get_conn()
                    try:
                        conn.execute(
                            "UPDATE skills SET quality_status='rejected', package_completeness='incomplete', "
                            "quality_reasons=? WHERE id=?",
                            (json.dumps(["package-incomplete", "not-github-source"], sort_keys=True), row["id"]),
                        )
                        conn.commit()
                    finally:
                        conn.close()
                    counts["not-github"] += 1
                else:
                    state["failed"][url] = result
                    counts[result] += 1
            except Exception as exc:  # one public repository must not stop the resumable run
                reason = f"{type(exc).__name__}:{exc}"[:500]
                state["failed"][url] = reason
                # Invalid/malformed packages are permanently ineligible for
                # routing. Network/rate-limit failures remain pending so a
                # later --retry-failed pass can retry them without surfacing
                # an incomplete source package.
                terminal = isinstance(exc, ValueError)
                if isinstance(exc, httpx.HTTPStatusError):
                    terminal = exc.response.status_code in {400, 404, 410, 422}
                conn = store.get_conn()
                try:
                    conn.execute(
                        "UPDATE skills SET quality_status=?, package_completeness=?, "
                        "quality_reasons=? WHERE id=?",
                        (
                            "rejected" if terminal else "pending",
                            "incomplete",
                            json.dumps(["package-incomplete", reason], sort_keys=True),
                            row["id"],
                        ),
                    )
                    conn.commit()
                finally:
                    conn.close()
                counts["failed"] += 1
            args.state.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
            print(url, counts, flush=True)
    print(json.dumps({"selected": len(selected), "counts": counts}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
