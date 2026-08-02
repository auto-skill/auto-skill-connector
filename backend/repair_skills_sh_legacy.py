"""Repair legacy skills.sh rows whose manifest exists but files were not retained.

The detail endpoint is not required for these rows: the mirror already stores
the public GitHub install URL and an immutable expected file manifest.  This
operator path resolves the repository commit, finds the matching package root
in a codeload archive, stores every expected file in the shared package CAS,
and rewrites the mirror's source metadata transactionally.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import re
import sqlite3
import tarfile
from pathlib import Path
from urllib.parse import quote

import httpx

import local_store as store
from hydrate_github_packages import resolve_commit
from package_store import ImmutablePackageStore, PackageFileInput, build_package_manifest
from skills_sh_catalog import _PersistentMirror


GITHUB_RE = re.compile(r"^https?://github\.com/(?P<owner>[^/]+)/(?P<repo>[^/#?]+)")
MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_EXTRACTED_BYTES = 25 * 1024 * 1024
MAX_MEMBER_BYTES = 5 * 1024 * 1024
MAX_ARCHIVE_FILES = 20_000


def read_full_archive(raw: bytes) -> dict[str, bytes]:
    if len(raw) > MAX_ARCHIVE_BYTES:
        raise ValueError("archive exceeds safety limit")
    files: dict[str, bytes] = {}
    total_bytes = 0
    with tarfile.open(fileobj=io.BytesIO(raw), mode="r|gz") as archive:
        wrapper = ""
        for member in archive:
            name = str(member.name).replace("\\", "/")
            parts = Path(name).parts
            if not parts or any(part in {"", ".", ".."} for part in parts):
                raise ValueError(f"unsafe archive path: {name}")
            if not wrapper:
                wrapper = parts[0]
            if not name.startswith(wrapper + "/") or not member.isfile():
                continue
            relative = name[len(wrapper) + 1 :]
            if len(files) >= MAX_ARCHIVE_FILES:
                raise ValueError("archive file limit exceeded")
            member_size = int(member.size or 0)
            if member_size > MAX_MEMBER_BYTES or total_bytes + member_size > MAX_EXTRACTED_BYTES:
                raise ValueError("archive extracted content exceeds safety limit")
            handle = archive.extractfile(member)
            if handle is None:
                raise ValueError(f"could not read {relative}")
            content = handle.read()
            if len(content) != member_size:
                raise ValueError(f"short read for {relative}")
            total_bytes += len(content)
            files[relative] = content
    return files


def _expected_files(value: dict) -> list[dict]:
    raw = value.get("raw") if isinstance(value.get("raw"), dict) else {}
    manifest = raw.get("file_manifest")
    return [item for item in manifest if isinstance(item, dict) and item.get("path")] if isinstance(manifest, list) else []


def _find_prefix(files: dict[str, bytes], expected: list[dict]) -> str:
    first = str(expected[0]["path"]).strip("/")
    candidates = [path[: -len(first)].rstrip("/") for path in files if path == first or path.endswith("/" + first)]
    for prefix in sorted(set(candidates), key=lambda item: (item.count("/"), item)):
        if all(
            (prefix + "/" if prefix else "") + str(item["path"]).strip("/") in files
            and len(files[(prefix + "/" if prefix else "") + str(item["path"]).strip("/")]) == int(item.get("bytes") or 0)
            and hashlib.sha256(files[(prefix + "/" if prefix else "") + str(item["path"]).strip("/")]).hexdigest() == str(item.get("sha256") or "")
            for item in expected
        ):
            return prefix
    raise ValueError("manifest does not match any GitHub package root")


def _package_materialized(package_root: Path, package_hash: str) -> bool:
    """Return true only when the CAS contains every byte in the manifest.

    Older skills.sh rows used ``files_json`` as a metadata-only manifest.  A
    matching path/hash list is not enough for delivery: every referenced CAS
    object must exist, have the declared byte count, and hash to its digest.
    """
    if not re.fullmatch(r"[a-f0-9]{64}", str(package_hash or "")):
        return False
    manifest_path = package_root / "manifests" / f"{package_hash}.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, TypeError, ValueError):
        return False
    if manifest.get("package_hash") != package_hash or manifest.get("completeness_status") != "complete":
        return False
    files = manifest.get("files") or []
    if not files:
        return False
    for item in files:
        digest = str(item.get("raw_sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", digest):
            return False
        object_path = package_root / "objects" / digest[:2] / digest
        try:
            content = object_path.read_bytes()
        except OSError:
            return False
        if len(content) != int(item.get("size") or -1) or hashlib.sha256(content).hexdigest() != digest:
            return False
    return True


def repair_row(client: httpx.Client, conn: sqlite3.Connection, row: sqlite3.Row, package_root: Path) -> str:
    value = json.loads(row["row_json"])
    expected = _expected_files(value)
    expected_bytes = sum(int(item.get("bytes") or 0) for item in expected)
    if len(expected) > MAX_ARCHIVE_FILES or expected_bytes > MAX_EXTRACTED_BYTES:
        raise ValueError("manifest exceeds repair safety limit")
    url = str(value.get("url") or "")
    match = GITHUB_RE.match(url)
    if not expected or not match:
        raise ValueError("missing GitHub URL or file manifest")
    owner, repo = match.group("owner"), match.group("repo").removesuffix(".git")
    commit_sha = resolve_commit(client, owner, repo, "HEAD")
    response = client.get(
        f"https://codeload.github.com/{owner}/{repo}/tar.gz/{quote(commit_sha, safe='')}",
        timeout=45,
    )
    response.raise_for_status()
    files = read_full_archive(response.content)
    prefix = _find_prefix(files, expected)
    package_inputs = []
    for item in expected:
        path = str(item["path"]).strip("/")
        package_path = f"{prefix}/{path}" if prefix else path
        package_inputs.append(
            PackageFileInput(path=path, content=files[package_path], expected_size=int(item.get("bytes") or 0))
        )
    raw = value.get("raw") if isinstance(value.get("raw"), dict) else {}
    entrypoint = str(raw.get("entrypoint_path") or "SKILL.md").strip("/")
    if not any(item.path == entrypoint for item in package_inputs):
        raise ValueError("manifest entrypoint is absent")
    manifest, objects = build_package_manifest(
        source={
            "provider": "github",
            "owner": owner,
            "repo": repo,
            "requested_ref": "HEAD",
            "commit_sha": commit_sha,
            "root_path": prefix,
        },
        source_url=url,
        entrypoint=entrypoint,
        files=package_inputs,
        tree_complete=True,
        provenance={"collector": "skills-sh-legacy-repair", "immutable_ref": commit_sha},
    )
    if manifest.get("completeness_status") != "complete":
        raise ValueError("reconstructed package is incomplete")
    ImmutablePackageStore(package_root).put(manifest, objects)
    store.upsert_skill_package(manifest, skill_id=f"skills_sh:{value.get('skills_sh_id') or row['id']}")
    actual_files = [
        {"path": item["path"], "sha256": item["raw_sha256"], "bytes": item["size"]}
        for item in manifest["files"]
    ]
    raw.update(
        {
            "package_hash": manifest["package_hash"],
            "source_commit_sha": commit_sha,
            "package_root_path": prefix,
            "legacy_package_repaired": True,
            "file_manifest": actual_files,
        }
    )
    value["raw"] = raw
    value["package_completeness"] = "complete"
    row_json = json.dumps(value, separators=(",", ":"), ensure_ascii=False)
    conn.execute(
        "UPDATE skills_sh_sources SET files_json=?, snapshot_hash=?, entrypoint_path=? WHERE content_hash=?",
        (json.dumps(actual_files, separators=(",", ":")), manifest["package_hash"], entrypoint, value.get("content_hash")),
    )
    conn.execute(
        "UPDATE skills_sh_mirror SET row_json=?, snapshot_hash=? WHERE id=?",
        (row_json, manifest["package_hash"], row["id"]),
    )
    return manifest["package_hash"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", type=Path, default=Path("/data/local_skills.db"))
    parser.add_argument("--package-root", type=Path, default=Path("/app/skills_library/packages"))
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()
    store.DB_PATH = args.db
    conn = sqlite3.connect(args.db, timeout=60)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id,row_json FROM skills_sh_mirror WHERE json_extract(row_json,'$.quality_status')='active'")
    selected = []
    for row in rows:
        try:
            value = json.loads(row["row_json"])
            package_hash = str((value.get("raw") or {}).get("package_hash") or "")
        except (TypeError, ValueError):
            package_hash = ""
        if _package_materialized(args.package_root, package_hash):
            continue
        selected.append(row)
        if len(selected) >= max(1, args.limit):
            break
    # Release the selector's read transaction before upsert_skill_package()
    # opens its own IMMEDIATE write connection.
    rows.close()
    repaired = failed = 0
    with httpx.Client(follow_redirects=True) as client:
        for row in selected:
            value = json.loads(row["row_json"])
            source = conn.execute(
                "SELECT content,files_json FROM skills_sh_sources WHERE content_hash=?",
                (value.get("content_hash"),),
            ).fetchone()
            files = []
            try:
                files = json.loads(source[1] or "[]") if source else []
            except (TypeError, json.JSONDecodeError):
                pass
            package_hash = str((value.get("raw") or {}).get("package_hash") or "")
            if (
                source
                and _PersistentMirror._source_files_complete(value, files, str(source[0] or ""))
                and _package_materialized(args.package_root, package_hash)
            ):
                continue
            try:
                repair_row(client, conn, row, args.package_root)
                repaired += 1
            except Exception as exc:
                failed += 1
                value["quality_status"] = "metadata_only"
                value["package_completeness"] = "incomplete"
                value["quality_reasons"] = sorted(set([*(value.get("quality_reasons") or []), f"legacy-repair-failed:{type(exc).__name__}"]))
                conn.execute("UPDATE skills_sh_mirror SET row_json=? WHERE id=?", (json.dumps(value, separators=(",", ":")), row["id"]))
            conn.commit()
    conn.close()
    print(json.dumps({"selected": len(selected), "repaired": repaired, "failed": failed}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
