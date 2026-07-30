"""Immutable, provenance-rich skill package snapshots.

Packages retain all source files for integrity and audit.  They are content
addressed and intentionally separate from the compact retrieval records in
``retrieval_records.py``; package files are never implicitly embedded.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import mimetypes
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
from typing import Any, Iterable


PACKAGE_SCHEMA_VERSION = 2
MAX_PACKAGE_FILES = 512
MAX_PACKAGE_BYTES = 25 * 1024 * 1024
MAX_FILE_BYTES = 5 * 1024 * 1024

_MARKDOWN_LINK_RE = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)(?:\s+['\"][^'\"]*['\"])?\)")
_BACKTICK_PATH_RE = re.compile(
    r"`((?:\.{1,2}/)?(?:references|scripts|assets|templates|schemas)/[^`\s]+)`",
    re.I,
)
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.-]*:", re.I)


@dataclass(frozen=True)
class PackageFileInput:
    path: str
    content: bytes
    mode: str = "100644"
    git_blob_sha: str = ""
    expected_size: int | None = None


def normalise_package_path(value: str) -> str:
    path = PurePosixPath(str(value or "").replace("\\", "/"))
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"unsafe package path: {value!r}")
    normalised = str(path)
    if not normalised or normalised == ".":
        raise ValueError("package path must not be empty")
    return normalised


def git_blob_sha(content: bytes) -> str:
    header = f"blob {len(content)}\0".encode("ascii")
    return hashlib.sha1(header + content).hexdigest()


def classify_file_role(path: str, entrypoint: str) -> str:
    lowered = path.casefold()
    name = PurePosixPath(path).name.casefold()
    if path == entrypoint:
        return "entrypoint"
    if name.startswith(("license", "copying", "notice")):
        return "license"
    if name.startswith("readme"):
        return "readme"
    if "/references/" in f"/{lowered}" or lowered.endswith(('.md', '.mdx', '.rst')):
        return "reference"
    if "/scripts/" in f"/{lowered}" or lowered.endswith(('.py', '.js', '.ts', '.sh', '.ps1', '.rb')):
        return "script"
    if "/templates/" in f"/{lowered}" or name.endswith(('.j2', '.jinja', '.tmpl', '.template')):
        return "template"
    if "/schemas/" in f"/{lowered}" or name.endswith(('.schema.json', '.xsd')):
        return "schema"
    if lowered.endswith(('.json', '.yaml', '.yml', '.toml', '.ini', '.cfg')):
        return "config"
    if lowered.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp', '.svg', '.pdf', '.mp3', '.wav')):
        return "asset"
    return "other"


def _is_text(content: bytes) -> bool:
    if b"\0" in content[:4096]:
        return False
    try:
        content[:65536].decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _relative_references(path: str, text: str) -> set[str]:
    parent = PurePosixPath(path).parent
    values = [*(_MARKDOWN_LINK_RE.findall(text)), *(_BACKTICK_PATH_RE.findall(text))]
    refs: set[str] = set()
    for raw in values:
        value = raw.strip(" <>\"'").split("#", 1)[0].split("?", 1)[0]
        if not value or value.startswith("#") or _SCHEME_RE.match(value):
            continue
        try:
            combined = parent / value
            parts: list[str] = []
            for part in combined.parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    if not parts:
                        refs.add(f"UNRESOLVED_OUTSIDE:{value}")
                        parts = []
                        break
                    parts.pop()
                else:
                    parts.append(part)
            if parts:
                refs.add(str(PurePosixPath(*parts)))
        except (TypeError, ValueError):
            continue
    return refs


def _dependency_closure(entrypoint: str, files: dict[str, PackageFileInput]) -> tuple[list[str], list[str]]:
    queue = [entrypoint]
    seen: set[str] = set()
    unresolved: set[str] = set()
    while queue:
        path = queue.pop(0)
        if path in seen:
            continue
        seen.add(path)
        item = files.get(path)
        if item is None or not _is_text(item.content):
            continue
        text = item.content.decode("utf-8", errors="replace")
        for ref in _relative_references(path, text):
            if ref.startswith("UNRESOLVED_OUTSIDE:"):
                unresolved.add(ref.removeprefix("UNRESOLVED_OUTSIDE:"))
            elif ref in files:
                queue.append(ref)
            else:
                unresolved.add(ref)
    return sorted(seen), sorted(unresolved)


def _license_metadata(files: dict[str, PackageFileInput]) -> dict[str, Any]:
    paths = [path for path in files if classify_file_role(path, "") == "license"]
    if not paths:
        return {"status": "missing", "spdx_id": None, "paths": []}
    text = "\n".join(
        files[path].content[:20000].decode("utf-8", errors="ignore") for path in sorted(paths)
    ).casefold()
    spdx = None
    if "mit license" in text:
        spdx = "MIT"
    elif "apache license" in text and "version 2" in text:
        spdx = "Apache-2.0"
    elif "gnu general public license" in text and "version 3" in text:
        spdx = "GPL-3.0"
    elif "bsd 3-clause" in text or ("redistribution and use" in text and "neither the name" in text):
        spdx = "BSD-3-Clause"
    return {"status": "detected" if spdx else "present-unclassified", "spdx_id": spdx, "paths": sorted(paths)}


def build_package_manifest(
    *,
    source: dict[str, Any],
    source_url: str,
    entrypoint: str,
    files: Iterable[PackageFileInput],
    tree_complete: bool,
    provenance: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], dict[str, bytes]]:
    """Validate a package and return its immutable manifest plus CAS objects."""
    entrypoint = normalise_package_path(entrypoint)
    file_map: dict[str, PackageFileInput] = {}
    total_bytes = 0
    errors: list[str] = []
    for supplied in files:
        path = normalise_package_path(supplied.path)
        if path in file_map:
            errors.append(f"duplicate-path:{path}")
            continue
        content = bytes(supplied.content)
        if len(content) > MAX_FILE_BYTES:
            errors.append(f"file-too-large:{path}")
            continue
        total_bytes += len(content)
        file_map[path] = PackageFileInput(
            path=path,
            content=content,
            mode=supplied.mode,
            git_blob_sha=supplied.git_blob_sha,
            expected_size=supplied.expected_size,
        )
    if len(file_map) > MAX_PACKAGE_FILES:
        errors.append("package-file-limit")
    if total_bytes > MAX_PACKAGE_BYTES:
        errors.append("package-byte-limit")

    entry = file_map.get(entrypoint)
    truncated_entrypoint = entry is None or (
        entry.expected_size is not None and entry.expected_size != len(entry.content)
    )
    closure, unresolved = _dependency_closure(entrypoint, file_map)
    closure_status = "complete" if not unresolved and entry is not None else "partial"
    completeness_reasons = list(errors)
    if not tree_complete:
        completeness_reasons.append("source-tree-truncated")
    if truncated_entrypoint:
        completeness_reasons.append("entrypoint-truncated-or-missing")

    objects: dict[str, bytes] = {}
    manifest_files: list[dict[str, Any]] = []
    for path, item in sorted(file_map.items()):
        raw_sha256 = hashlib.sha256(item.content).hexdigest()
        objects[raw_sha256] = item.content
        actual_git_sha = git_blob_sha(item.content)
        if item.git_blob_sha and item.git_blob_sha != actual_git_sha:
            completeness_reasons.append(f"git-blob-mismatch:{path}")
        role = classify_file_role(path, entrypoint)
        manifest_files.append(
            {
                "path": path,
                "raw_sha256": raw_sha256,
                "git_blob_sha": item.git_blob_sha or actual_git_sha,
                "mode": item.mode,
                "size": len(item.content),
                "media_type": mimetypes.guess_type(path)[0] or "application/octet-stream",
                "text_indexable": _is_text(item.content),
                "role": role,
                "in_dependency_closure": path in closure,
            }
        )

    package_identity = json.dumps(
        {
            "entrypoint": entrypoint,
            "files": [
                {"path": item["path"], "sha256": item["raw_sha256"]}
                for item in manifest_files
            ],
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    package_hash = hashlib.sha256(package_identity.encode("utf-8")).hexdigest()
    manifest = {
        "schema_version": PACKAGE_SCHEMA_VERSION,
        "package_hash": package_hash,
        "source_url": source_url,
        "source": dict(source),
        "provenance": dict(provenance or {}),
        "entrypoint": entrypoint,
        "entrypoint_truncated": truncated_entrypoint,
        "tree_complete": bool(tree_complete),
        "completeness_status": "complete" if not completeness_reasons else "partial",
        "completeness_reasons": sorted(set(completeness_reasons)),
        "dependency_closure_status": closure_status,
        "dependency_closure": closure,
        "unresolved_references": unresolved,
        "license": _license_metadata(file_map),
        "files": manifest_files,
        "stored_files": len(manifest_files),
        "stored_bytes": total_bytes,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    return manifest, objects


class ImmutablePackageStore:
    """Filesystem CAS for package bytes and manifests."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self.objects_dir = self.root / "objects"
        self.manifests_dir = self.root / "manifests"

    @staticmethod
    def _atomic_write(path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            if hashlib.sha256(path.read_bytes()).digest() != hashlib.sha256(content).digest():
                raise ValueError(f"immutable object collision: {path.name}")
            return
        descriptor, temp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
        try:
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_name, path)
        except Exception:
            try:
                os.unlink(temp_name)
            except OSError:
                pass
            raise

    def put(self, manifest: dict[str, Any], objects: dict[str, bytes]) -> Path:
        package_hash = str(manifest.get("package_hash") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", package_hash):
            raise ValueError("manifest has no valid package hash")
        for digest, content in objects.items():
            if hashlib.sha256(content).hexdigest() != digest:
                raise ValueError(f"object hash mismatch: {digest}")
            self._atomic_write(self.objects_dir / digest[:2] / digest, content)
        manifest_bytes = json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8") + b"\n"
        manifest_path = self.manifests_dir / f"{package_hash}.json"
        if manifest_path.exists():
            existing = json.loads(manifest_path.read_text(encoding="utf-8"))
            existing_files = [
                (item.get("path"), item.get("raw_sha256"))
                for item in existing.get("files") or []
            ]
            incoming_files = [
                (item.get("path"), item.get("raw_sha256"))
                for item in manifest.get("files") or []
            ]
            if (
                existing.get("package_hash") != package_hash
                or existing.get("entrypoint") != manifest.get("entrypoint")
                or existing_files != incoming_files
            ):
                raise ValueError(f"immutable package collision: {package_hash}")
            # Source aliases/forks are recorded independently in the database.
            # The canonical package manifest remains the first immutable capture.
            return manifest_path
        self._atomic_write(manifest_path, manifest_bytes)
        return manifest_path

    def read_manifest(self, package_hash: str) -> dict[str, Any] | None:
        path = self.manifests_dir / f"{package_hash}.json"
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))
