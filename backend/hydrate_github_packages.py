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
    MAX_PACKAGE_BYTES,
    MAX_PACKAGE_FILES,
    ImmutablePackageStore,
    PackageFileInput,
    build_package_manifest,
)
from quality import canonicalize_skill_content, content_hash, evaluate_quality


GITHUB_URL_RE = re.compile(
    r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+)"
    r"(?:/(?P<kind>tree|blob)/(?P<ref>[^/]+)(?:/(?P<path>[^?#]+))?)?"
)
PACKAGE_SOURCES = {"github", "github_skill_file", "skillsmp", "awesome_list"}
STATE_NAME = "github_package_hydration_state.json"
MAX_ARCHIVE_BYTES = 100 * 1024 * 1024
_LIBRARY_WRITE_LOCK = threading.Lock()
_THREAD_LOCAL = threading.local()
MAX_THREAD_CACHE_ENTRIES = 16


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


def read_archive(raw: bytes, scope: str = "") -> dict[str, bytes]:
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError(f"archive exceeds {MAX_ARCHIVE_BYTES} byte safety limit")
    files: dict[str, bytes] = {}
    scoped_prefix = scope.strip("/")
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as archive:
        wrapper = ""
        for member in archive:
            name = _safe_member_name(member.name)
            # codeload wraps files in one top-level ``repo-ref`` directory.
            if not wrapper:
                wrapper = name.split("/", 1)[0]
            if not name.startswith(wrapper + "/"):
                continue
            relative = name[len(wrapper) + 1:]
            if scoped_prefix and not (
                relative == scoped_prefix or relative.startswith(scoped_prefix + "/")
            ):
                continue
            if member.issym() or member.islnk() or not (member.isfile() or member.isdir()):
                raise ValueError(f"unsupported archive entry: {member.name}")
            if not member.isfile():
                continue
            if len(files) >= MAX_PACKAGE_FILES:
                raise ValueError("scoped tree exceeds package file limit")
            total_bytes += int(member.size or 0)
            if total_bytes > MAX_PACKAGE_BYTES:
                raise ValueError("scoped tree exceeds package byte limit")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ValueError(f"could not read archive entry: {member.name}")
            files[relative] = extracted.read()
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
    archive_cache: dict[tuple[str, str, str], dict[str, bytes]] | None = None,
    commit_cache: dict[tuple[str, str, str], str] | None = None,
) -> str:
    parsed = parse_github_url(str(row["url"] or ""))
    if not parsed:
        return "not-github"
    cache_key = (parsed["owner"], parsed["repo"], parsed["ref"])
    commit_cache = commit_cache if commit_cache is not None else {}
    archive_cache = archive_cache if archive_cache is not None else {}
    commit_sha = commit_cache.get(cache_key)
    if commit_sha is None:
        commit_sha = resolve_commit(client, *cache_key)
        commit_cache[cache_key] = commit_sha
    files = archive_cache.get(cache_key)
    if files is None:
        archive_url = (
            f"https://codeload.github.com/{parsed['owner']}/{parsed['repo']}/tar.gz/"
            f"{quote(parsed['ref'], safe='')}"
        )
        files = read_archive(download_archive(client, archive_url), parsed["scope"])
        archive_cache[cache_key] = files
    entrypoint, scoped = select_package_files(files, parsed["scope"], parsed["entrypoint"])
    package_files = [
        PackageFileInput(path=path, content=content, expected_size=len(content))
        for path, content in scoped.items()
    ]
    manifest, objects = build_package_manifest(
        source={
            "provider": "github",
            "owner": parsed["owner"],
            "repo": parsed["repo"],
            "requested_ref": parsed["ref"],
            "commit_sha": commit_sha,
            "root_path": parsed["scope"],
        },
        source_url=str(row["url"]),
        entrypoint=entrypoint,
        files=package_files,
        tree_complete=True,
        provenance={"collector": "github-codeload", "immutable_ref": commit_sha},
    )
    if manifest.get("completeness_status") != "complete" or manifest.get("entrypoint_truncated"):
        return f"incomplete:{','.join(manifest.get('completeness_reasons') or [])}"
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
        "--workers",
        type=int,
        default=max(1, int(os.getenv("HYDRATOR_WORKERS", "1"))),
        help="bounded concurrent network workers (default: 1)",
    )
    args = parser.parse_args()
    store.DB_PATH = args.db
    store.init_db()
    state = json.loads(args.state.read_text()) if args.state.exists() else {"done": {}, "failed": {}}
    state.setdefault("done", {})
    state.setdefault("failed", {})
    conn = store.get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM skills WHERE quality_status IN ('active','metadata_only','pending') "
            "AND source IN ('github','github_skill_file','skillsmp','awesome_list') ORDER BY id"
        ).fetchall()
    finally:
        conn.close()
    selected = [
        row for row in rows
        if str(row["url"]) not in state["done"]
        and (args.retry_failed or str(row["url"]) not in state["failed"])
    ][: max(1, args.limit)]
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
    print(json.dumps({"selected": len(selected), "counts": counts, "remaining": len(rows) - len(state["done"])}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
