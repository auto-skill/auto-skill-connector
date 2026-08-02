"""Create and apply constrained, skill-only update packages.

Collector computers export only reviewed public skill rows and their saved
content.  Production validates the complete package before opening SQLite,
takes an online backup, and upserts an explicit column allowlist.  The package
format cannot name tables, columns, SQL, filesystem paths, users, tokens,
route events, or billing records.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import io
import ipaddress
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import quality
from package_store import ImmutablePackageStore
from embeddings import build_embed_text, embed_text_hash


FORMAT = "autoskill-skill-delta"
FORMAT_VERSION = 2
LEGACY_FORMAT_VERSION = 1
SKILLS_MEMBER = "skills.jsonl.gz"
LIBRARY_MEMBER = "library.jsonl.gz"
PACKAGES_MEMBER = "packages.jsonl.gz"
MANIFEST_MEMBER = "manifest.json"
ALLOWED_MEMBERS_V1 = frozenset({MANIFEST_MEMBER, SKILLS_MEMBER, LIBRARY_MEMBER})
ALLOWED_MEMBERS_V2 = frozenset({MANIFEST_MEMBER, SKILLS_MEMBER, LIBRARY_MEMBER, PACKAGES_MEMBER})

EMBEDDING_DIM = 384
EMBEDDING_BYTES = EMBEDDING_DIM * 4
MAX_RECORDS = 250_000
# Headroom over the current corpus (~354MB compressed / ~1.04GiB uncompressed
# library content as of 2026-07) rather than a tight fit -- this keeps growing
# weekly, and both the local collector and the droplet's apply step need to
# actually hold a package this size.
MAX_COMPRESSED_MEMBER_BYTES = 1024 * 1024 * 1024
MAX_UNCOMPRESSED_MEMBER_BYTES = 2 * 1024 * 1024 * 1024
# Keep delta packages aligned with the library ingest ceiling — never silently
# truncate SKILL.md bodies on export/import.
MAX_CONTENT_CHARS = quality.MAX_SKILL_CONTENT_CHARS

JSON_FIELDS = frozenset({"tags", "risk_flags", "quality_reasons", "platforms", "triggers"})
SKILL_FIELDS = (
    "name",
    "description",
    "source",
    "url",
    "tags",
    "discovered_at",
    "risk_score",
    "risk_flags",
    "scanned_at",
    "content_hash",
    "canonical_id",
    "quality_status",
    "quality_reasons",
    "quality_score",
    "prominence_score",
    "provenance_score",
    "meaningfulness_score",
    "platforms",
    "category",
    "capability_summary",
    "triggers",
    "embedding_text_hash",
    "embedded_at",
)
PACKAGE_FIELDS = frozenset((*SKILL_FIELDS, "embedding_b64"))
UPDATE_FIELDS = tuple(field for field in SKILL_FIELDS if field not in {"url", "discovered_at"})
# active + metadata_only (quality.ACTIVE_STATUSES): discovery/indexing-eligible
# skills, not just auto-injectable ones. A metadata_only MCP server now has a
# real capability_summary/embedding computed locally (see scraper.scan_skill)
# and must reach production through this same sync path, or all of that
# ingestion work never becomes recommendable outside the collector machine.
# quality.tier_for_ranked_candidates is the actual injection-safety gate and
# is untouched by this -- packaging a metadata_only skill here still never
# makes it eligible for "full" tier delivery.
QUALITY_STATUSES = quality.ACTIVE_STATUSES


class SkillDeltaError(ValueError):
    pass


@dataclass(frozen=True)
class LoadedPackage:
    manifest: dict
    skills: list[dict]
    library: dict[str, str]
    packages: dict[str, dict]
    package_sha256: str


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _json_line(value: dict) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"


class _GzipLineWriter:
    """Writes JSON lines straight into a gzip buffer one at a time, so the
    caller only ever holds one line's worth of uncompressed data at once
    instead of collecting every line into a list first -- with a corpus of
    hundreds of thousands of skills, that list was the memory cost that
    mattered, not the final compressed bytes."""

    def __init__(self) -> None:
        self._buffer = io.BytesIO()
        self._gz = gzip.GzipFile(fileobj=self._buffer, mode="wb", mtime=0)
        self.count = 0

    def write(self, value: dict) -> None:
        self._gz.write(_json_line(value))
        self.count += 1

    def finish(self) -> bytes:
        self._gz.close()
        return self._buffer.getvalue()


def _gunzip_limited(data: bytes, member: str) -> bytes:
    if len(data) > MAX_COMPRESSED_MEMBER_BYTES:
        raise SkillDeltaError(f"{member} exceeds the compressed size limit")
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as gz:
            raw = gz.read(MAX_UNCOMPRESSED_MEMBER_BYTES + 1)
    except (OSError, EOFError) as exc:
        raise SkillDeltaError(f"{member} is not valid gzip: {exc}") from exc
    if len(raw) > MAX_UNCOMPRESSED_MEMBER_BYTES:
        raise SkillDeltaError(f"{member} exceeds the uncompressed size limit")
    return raw


def _parse_json_lines(data: bytes, member: str) -> list[dict]:
    records: list[dict] = []
    for number, line in enumerate(data.splitlines(), 1):
        if not line.strip():
            continue
        if len(records) >= MAX_RECORDS:
            raise SkillDeltaError(f"{member} exceeds the {MAX_RECORDS} record limit")
        try:
            value = json.loads(line)
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SkillDeltaError(f"{member} line {number} is invalid JSON") from exc
        if not isinstance(value, dict):
            raise SkillDeltaError(f"{member} line {number} must be an object")
        records.append(value)
    return records


def _decode_json_field(value, field: str):
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError as exc:
            raise SkillDeltaError(f"{field} is not valid JSON") from exc
    if not isinstance(value, list):
        raise SkillDeltaError(f"{field} must be a JSON list")
    if len(value) > 100:
        raise SkillDeltaError(f"{field} has too many entries")
    return value


def _validate_text(value, field: str, maximum: int, *, required: bool = False):
    if value is None and not required:
        return None
    if not isinstance(value, str) or (required and not value.strip()):
        raise SkillDeltaError(f"{field} must be a non-empty string")
    if len(value) > maximum:
        raise SkillDeltaError(f"{field} exceeds {maximum} characters")
    if "\x00" in value:
        raise SkillDeltaError(f"{field} contains a NUL byte")
    return value


def _validate_skill(record: dict) -> dict:
    unknown = set(record) - PACKAGE_FIELDS
    if unknown:
        raise SkillDeltaError(f"skill contains forbidden fields: {', '.join(sorted(unknown))}")
    clean = dict(record)
    clean["name"] = _validate_text(clean.get("name"), "name", 500, required=True)
    clean["source"] = _validate_text(clean.get("source"), "source", 200, required=True)
    clean["url"] = _validate_text(clean.get("url"), "url", 2048, required=True)
    parsed = urlparse(clean["url"])
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or parsed.username or parsed.password:
        raise SkillDeltaError(f"url is not a public HTTP(S) URL: {clean['url']!r}")
    hostname = (parsed.hostname or "").rstrip(".").lower()
    if hostname == "localhost" or hostname.endswith(".localhost") or hostname.endswith(".local"):
        raise SkillDeltaError("url may not target a loopback or local hostname")
    try:
        address = ipaddress.ip_address(hostname.strip("[]"))
    except ValueError:
        address = None
    if address is not None and not address.is_global:
        raise SkillDeltaError("url may not target a non-public IP address")
    clean["description"] = _validate_text(clean.get("description"), "description", 20_000)
    clean["capability_summary"] = _validate_text(clean.get("capability_summary"), "capability_summary", 2_000)
    for field in ("discovered_at", "scanned_at", "embedded_at"):
        clean[field] = _validate_text(clean.get(field), field, 100)
    for field in ("content_hash", "embedding_text_hash"):
        value = _validate_text(clean.get(field), field, 128)
        if value is not None and not re.fullmatch(r"[a-f0-9]{32,128}", value):
            raise SkillDeltaError(f"{field} must be a lowercase hexadecimal digest")
        clean[field] = value
    clean["canonical_id"] = _validate_text(clean.get("canonical_id"), "canonical_id", 2048)
    clean["category"] = _validate_text(clean.get("category"), "category", 200)
    if clean.get("quality_status") not in QUALITY_STATUSES:
        raise SkillDeltaError("packages may contain only active or metadata_only skills")
    for field in JSON_FIELDS:
        clean[field] = _decode_json_field(clean.get(field, []), field)
    for field in ("risk_score", "quality_score"):
        value = clean.get(field, 0)
        if not isinstance(value, int) or isinstance(value, bool) or not -10_000 <= value <= 10_000:
            raise SkillDeltaError(f"{field} is outside the allowed integer range")
        clean[field] = value
    for field in ("prominence_score", "provenance_score", "meaningfulness_score"):
        value = clean.get(field, 0.0)
        if not isinstance(value, (int, float)) or isinstance(value, bool) or not math.isfinite(float(value)):
            raise SkillDeltaError(f"{field} must be a finite number")
        clean[field] = float(value)
    encoded = clean.get("embedding_b64")
    if not isinstance(encoded, str):
        raise SkillDeltaError("active skill is missing embedding_b64")
    try:
        blob = base64.b64decode(encoded, validate=True)
    except (ValueError, base64.binascii.Error) as exc:
        raise SkillDeltaError("embedding_b64 is invalid base64") from exc
    if len(blob) != EMBEDDING_BYTES:
        raise SkillDeltaError(f"embedding must contain exactly {EMBEDDING_DIM} float32 values")
    values = memoryview(blob).cast("f")
    if any(not math.isfinite(float(value)) for value in values):
        raise SkillDeltaError("embedding contains a non-finite value")
    norm = math.sqrt(sum(float(value) * float(value) for value in values))
    if not 0.90 <= norm <= 1.10:
        raise SkillDeltaError("embedding is not approximately unit-normalized")
    clean["embedding"] = blob
    clean.pop("embedding_b64", None)
    return clean


def _validate_library(record: dict) -> tuple[str, str]:
    if set(record) != {"url", "content", "content_sha256"}:
        raise SkillDeltaError("library record must contain only url, content, and content_sha256")
    url = _validate_text(record.get("url"), "library url", 2048, required=True)
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SkillDeltaError("library url must be HTTP(S)")
    content = _validate_text(record.get("content"), "library content", MAX_CONTENT_CHARS, required=True)
    digest = _validate_text(record.get("content_sha256"), "content_sha256", 64, required=True)
    if not re.fullmatch(r"[a-f0-9]{64}", digest) or _sha256(content.encode("utf-8")) != digest:
        raise SkillDeltaError(f"library content hash mismatch for {url}")
    return url, content


def _validate_package_record(record: dict) -> tuple[str, dict]:
    """Validate one complete immutable package and its content-addressed bytes."""
    if set(record) != {"skill_urls", "manifest", "objects"}:
        raise SkillDeltaError("package record contains missing or forbidden fields")
    urls = record.get("skill_urls")
    if not isinstance(urls, list) or not urls or any(not isinstance(url, str) for url in urls):
        raise SkillDeltaError("package skill_urls must be a non-empty string list")
    manifest = record.get("manifest")
    if not isinstance(manifest, dict):
        raise SkillDeltaError("package manifest must be an object")
    package_hash = manifest.get("package_hash")
    if not isinstance(package_hash, str) or not re.fullmatch(r"[a-f0-9]{64}", package_hash):
        raise SkillDeltaError("package manifest has an invalid package_hash")
    if manifest.get("completeness_status") != "complete" or manifest.get("entrypoint_truncated"):
        raise SkillDeltaError(f"package is not complete: {package_hash}")
    if not manifest.get("tree_complete", False):
        raise SkillDeltaError(f"package tree is incomplete: {package_hash}")
    objects = record.get("objects")
    if not isinstance(objects, dict):
        raise SkillDeltaError("package objects must be an object")
    expected: set[str] = set()
    decoded: dict[str, bytes] = {}
    for file_info in manifest.get("files") or []:
        if not isinstance(file_info, dict):
            raise SkillDeltaError(f"package file entry is invalid: {package_hash}")
        digest = file_info.get("raw_sha256")
        if not isinstance(digest, str) or not re.fullmatch(r"[a-f0-9]{64}", digest):
            raise SkillDeltaError(f"package file hash is invalid: {package_hash}")
        expected.add(digest)
        encoded = objects.get(digest)
        if not isinstance(encoded, str):
            raise SkillDeltaError(f"package object is missing: {digest}")
        try:
            content = base64.b64decode(encoded, validate=True)
        except (ValueError, base64.binascii.Error) as exc:
            raise SkillDeltaError(f"package object is invalid: {digest}") from exc
        if _sha256(content) != digest or len(content) != int(file_info.get("size") or 0):
            raise SkillDeltaError(f"package object checksum/size mismatch: {digest}")
        decoded[digest] = content
    if set(objects) != expected:
        raise SkillDeltaError(f"package object set does not match manifest: {package_hash}")
    for url in urls:
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise SkillDeltaError(f"package skill URL is not public HTTP(S): {url!r}")
    return package_hash, {"manifest": manifest, "objects": decoded, "skill_urls": urls}


def _read_member(archive: zipfile.ZipFile, name: str) -> bytes:
    infos = [info for info in archive.infolist() if info.filename == name]
    if len(infos) != 1:
        raise SkillDeltaError(f"package must contain exactly one {name}")
    info = infos[0]
    if info.file_size > MAX_COMPRESSED_MEMBER_BYTES:
        raise SkillDeltaError(f"{name} exceeds the package member limit")
    return archive.read(info)


def load_package(path: Path) -> LoadedPackage:
    path = path.resolve()
    if not path.is_file():
        raise SkillDeltaError(f"package does not exist: {path}")
    package_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    try:
        with zipfile.ZipFile(path, "r") as archive:
            names = [info.filename for info in archive.infolist()]
            if len(names) != len(set(names)):
                raise SkillDeltaError("package contains duplicate member names")
            if set(names) not in {ALLOWED_MEMBERS_V1, ALLOWED_MEMBERS_V2}:
                raise SkillDeltaError("package contains missing or forbidden members")
            manifest_raw = _read_member(archive, MANIFEST_MEMBER)
            skills_raw = _read_member(archive, SKILLS_MEMBER)
            library_raw = _read_member(archive, LIBRARY_MEMBER)
            packages_raw = archive.read(PACKAGES_MEMBER) if PACKAGES_MEMBER in names else None
    except zipfile.BadZipFile as exc:
        raise SkillDeltaError("package is not a valid zip archive") from exc
    try:
        manifest = json.loads(manifest_raw)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SkillDeltaError("manifest.json is invalid") from exc
    version = manifest.get("version") if isinstance(manifest, dict) else None
    if not isinstance(manifest, dict) or manifest.get("format") != FORMAT or version not in {LEGACY_FORMAT_VERSION, FORMAT_VERSION}:
        raise SkillDeltaError("unsupported skill delta format or version")
    expected_manifest_keys = {"format", "version", "created_at", "skill_count", "library_count", "files"}
    if set(manifest) != expected_manifest_keys:
        raise SkillDeltaError("manifest contains missing or forbidden fields")
    _validate_text(manifest.get("created_at"), "manifest created_at", 100, required=True)
    if not isinstance(manifest.get("skill_count"), int) or not isinstance(manifest.get("library_count"), int):
        raise SkillDeltaError("manifest counts must be integers")
    expected_files = {SKILLS_MEMBER, LIBRARY_MEMBER}
    if version == FORMAT_VERSION:
        expected_files.add(PACKAGES_MEMBER)
    if set(manifest.get("files") or {}) != expected_files:
        raise SkillDeltaError("manifest file allowlist is invalid")
    members = [(SKILLS_MEMBER, skills_raw), (LIBRARY_MEMBER, library_raw)]
    if version == FORMAT_VERSION:
        if packages_raw is None:
            raise SkillDeltaError("v2 package is missing packages member")
        members.append((PACKAGES_MEMBER, packages_raw))
    for name, raw in members:
        entry = manifest["files"].get(name)
        if (
            not isinstance(entry, dict)
            or set(entry) != {"sha256", "bytes"}
            or entry.get("sha256") != _sha256(raw)
            or entry.get("bytes") != len(raw)
        ):
            raise SkillDeltaError(f"manifest checksum or size mismatch for {name}")
    skills_records = _parse_json_lines(_gunzip_limited(skills_raw, SKILLS_MEMBER), SKILLS_MEMBER)
    library_records = _parse_json_lines(_gunzip_limited(library_raw, LIBRARY_MEMBER), LIBRARY_MEMBER)
    package_records = (
        _parse_json_lines(_gunzip_limited(packages_raw, PACKAGES_MEMBER), PACKAGES_MEMBER)
        if packages_raw is not None
        else []
    )
    if manifest.get("skill_count") != len(skills_records) or manifest.get("library_count") != len(library_records):
        raise SkillDeltaError("manifest record counts do not match package contents")
    if not skills_records:
        raise SkillDeltaError("package contains no active embedded skills")
    skills: list[dict] = []
    seen_urls: set[str] = set()
    for record in skills_records:
        clean = _validate_skill(record)
        if clean["url"] in seen_urls:
            raise SkillDeltaError(f"duplicate skill URL: {clean['url']}")
        seen_urls.add(clean["url"])
        skills.append(clean)
    library: dict[str, str] = {}
    for record in library_records:
        url, content = _validate_library(record)
        if url in library:
            raise SkillDeltaError(f"duplicate library URL: {url}")
        if url not in seen_urls:
            raise SkillDeltaError(f"library content has no packaged skill: {url}")
        library[url] = content
    if set(library) != seen_urls:
        missing = sorted(seen_urls - set(library))
        raise SkillDeltaError(f"active skill is missing reviewed library content: {missing[0]}")

    packages: dict[str, dict] = {}
    package_urls: set[str] = set()
    package_by_url: dict[str, dict] = {}
    for record in package_records:
        package_hash, package = _validate_package_record(record)
        if package_hash in packages:
            raise SkillDeltaError(f"duplicate package hash: {package_hash}")
        if package_urls.intersection(package["skill_urls"]):
            raise SkillDeltaError("a skill URL is linked to multiple packages")
        unknown_urls = set(package["skill_urls"]) - seen_urls
        if unknown_urls:
            raise SkillDeltaError(f"package references an unshipped skill: {sorted(unknown_urls)[0]}")
        package_urls.update(package["skill_urls"])
        for url in package["skill_urls"]:
            package_by_url[url] = package
        packages[package_hash] = package

    for skill in skills:
        content = library[skill["url"]]
        package_manifest = (package_by_url.get(skill["url"]) or {}).get("manifest") or {}
        assessed = quality.evaluate_quality(
            {
                "name": skill["name"],
                "description": skill.get("description"),
                "source": skill["source"] if version == FORMAT_VERSION else "legacy-delta",
                "url": skill["url"],
                "tags": skill.get("tags") or [],
                "package_completeness": package_manifest.get("completeness_status")
                if version == FORMAT_VERSION
                else None,
                "dependency_closure_status": package_manifest.get("dependency_closure_status")
                if version == FORMAT_VERSION
                else None,
                "entrypoint_truncated": package_manifest.get("entrypoint_truncated")
                if version == FORMAT_VERSION
                else None,
            },
            content,
        )
        if assessed.get("quality_status") not in QUALITY_STATUSES:
            raise SkillDeltaError(f"packaged content does not pass the current quality gate: {skill['url']}")
        if skill.get("content_hash") != assessed.get("content_hash"):
            raise SkillDeltaError(f"skill content_hash does not match reviewed content: {skill['url']}")
        expected_embedding_hash = embed_text_hash(build_embed_text(skill, content))
        if skill.get("embedding_text_hash") != expected_embedding_hash:
            raise SkillDeltaError(f"embedding text hash does not match packaged content: {skill['url']}")
        skill.update(assessed)
    if version == FORMAT_VERSION:
        missing = [
            skill["url"]
            for skill in skills
            if skill["source"] in {"github", "github_skill_file", "skillsmp", "awesome_list"}
            and skill["url"] not in package_urls
        ]
        if missing:
            raise SkillDeltaError(f"complete package missing for {missing[0]}")
    return LoadedPackage(
        manifest=manifest,
        skills=skills,
        library=library,
        packages=packages,
        package_sha256=package_sha,
    )


def _library_filenames(library_dir: Path) -> dict[str, str]:
    """Map skill url -> validated on-disk filename, without reading any file
    content. Export only needs to read the (typically much smaller) subset of
    files that actually get exported; see _read_library_file."""
    index_path = library_dir / "index.json"
    if not index_path.exists():
        return {}
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillDeltaError(f"cannot read library index: {exc}") from exc
    if not isinstance(index, dict):
        raise SkillDeltaError("library index must be an object")
    files_root = (library_dir / "files").resolve()
    filenames: dict[str, str] = {}
    for url, metadata in index.items():
        if not isinstance(metadata, dict) or not isinstance(metadata.get("file"), str):
            continue
        candidate = (files_root / metadata["file"]).resolve()
        try:
            candidate.relative_to(files_root)
        except ValueError as exc:
            raise SkillDeltaError("library index contains a path outside files/") from exc
        if candidate.is_file():
            filenames[str(url)] = metadata["file"]
    return filenames


def _read_library_file(files_root: Path, filename: str) -> str:
    # Full body only — truncation here would ship incomplete artifacts while
    # still looking like a successful export.
    text = (files_root / filename).read_text(encoding="utf-8", errors="replace")
    return quality.canonicalize_skill_content(text)


def export_package(db_path: Path, library_dir: Path, output: Path) -> dict:
    # Streams rows and library file content one skill at a time instead of
    # collecting the whole active corpus into memory first -- with a
    # few-hundred-thousand-skill library this was the difference between
    # fitting in the collector's memory limit and OOMing partway through.
    db_path = db_path.resolve()
    if not db_path.is_file():
        raise SkillDeltaError(f"collector database does not exist: {db_path}")
    filenames = _library_filenames(library_dir.resolve())
    files_root = library_dir.resolve() / "files"
    skills_writer = _GzipLineWriter()
    library_writer = _GzipLineWriter()
    packages_writer = _GzipLineWriter()
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(skills)")}
        required = set(SKILL_FIELDS) | {"embedding"}
        missing = required - columns
        if missing:
            raise SkillDeltaError(f"collector database is missing skill columns: {', '.join(sorted(missing))}")
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        package_mode = "package_hash" in columns and {"skill_packages", "skill_package_files"}.issubset(tables)
        package_root = library_dir.resolve() / "packages"
        package_entries: dict[str, dict] = {}
        package_cursor = conn.execute(
            "SELECT package_hash,manifest_json FROM skill_packages"
        ) if package_mode else None
        package_manifests = {
            str(row[0]): json.loads(row[1])
            for row in (package_cursor or [])
        }
        cursor = conn.execute(
            f"SELECT {','.join(SKILL_FIELDS)},embedding"
            + (",package_hash" if package_mode else "")
            + " FROM skills "
            "WHERE quality_status='active' AND embedding IS NOT NULL AND url IS NOT NULL ORDER BY url"
        )
        skipped_invalid = 0
        for row in cursor:
            record = dict(row)
            url = record.get("url")
            package_hash = str(record.pop("package_hash", "") or "")
            filename = filenames.get(url)
            if filename is None:
                continue
            try:
                for field in JSON_FIELDS:
                    record[field] = _decode_json_field(record.get(field, "[]"), field)
                blob = bytes(record.pop("embedding"))
                record["embedding_b64"] = base64.b64encode(blob).decode("ascii")
                _validate_skill(record)  # raises on malformed data; result intentionally unused, as before
                content = _read_library_file(files_root, filename)
                # Matches _validate_library's own check on this same field, so
                # a scraped file with e.g. an embedded NUL byte is skipped here
                # instead of passing the per-row loop and only failing during
                # load_package's whole-package self-verification at the end.
                _validate_text(content, "library content", MAX_CONTENT_CHARS, required=True)
                if package_mode and record.get("source") in {"github", "github_skill_file", "skillsmp", "awesome_list"}:
                    if not re.fullmatch(r"[a-f0-9]{64}", package_hash):
                        raise SkillDeltaError("active GitHub/SkillsMP row has no complete package")
                    package_manifest = package_manifests.get(package_hash)
                    if not isinstance(package_manifest, dict) or package_manifest.get("completeness_status") != "complete":
                        raise SkillDeltaError("active GitHub/SkillsMP row has an incomplete package")
                    object_map: dict[str, str] = {}
                    for file_info in package_manifest.get("files") or []:
                        digest = str(file_info.get("raw_sha256") or "")
                        object_path = package_root / "objects" / digest[:2] / digest
                        if not re.fullmatch(r"[a-f0-9]{64}", digest) or not object_path.is_file():
                            raise SkillDeltaError(f"package object is missing: {digest}")
                        raw = object_path.read_bytes()
                        if _sha256(raw) != digest or len(raw) != int(file_info.get("size") or 0):
                            raise SkillDeltaError(f"package object checksum mismatch: {digest}")
                        object_map[digest] = base64.b64encode(raw).decode("ascii")
                    package_entry = package_entries.setdefault(
                        package_hash,
                        {"skill_urls": [], "manifest": package_manifest, "objects": object_map},
                    )
                    if url not in package_entry["skill_urls"]:
                        package_entry["skill_urls"].append(url)
            except (SkillDeltaError, OSError):
                # The collector's corpus is scraped from noisy sources (e.g.
                # web search results with occasionally malformed URLs); one bad
                # row should not block exporting every other skill for a week.
                skipped_invalid += 1
                continue
            skill_record = {field: record.get(field) for field in SKILL_FIELDS}
            skill_record["embedding_b64"] = record["embedding_b64"]
            skills_writer.write(skill_record)
            library_writer.write({
                "url": url,
                "content": content,
                "content_sha256": _sha256(content.encode("utf-8")),
            })
    finally:
        conn.close()
    if skipped_invalid:
        # Not part of the manifest -- load_package enforces an exact key set
        # on it (a real allowlist boundary), so this stays a side-channel
        # message rather than a package field.
        print(f"skipped {skipped_invalid} invalid skill row(s) during export", file=sys.stderr)
    skills_gz = skills_writer.finish()
    library_gz = library_writer.finish()
    for package_entry in package_entries.values():
        packages_writer.write(package_entry)
    packages_gz = packages_writer.finish()
    version = FORMAT_VERSION if package_mode else LEGACY_FORMAT_VERSION
    manifest = {
        "format": FORMAT,
        "version": version,
        "created_at": _utc_now(),
        "skill_count": skills_writer.count,
        "library_count": library_writer.count,
        "files": {
            SKILLS_MEMBER: {"sha256": _sha256(skills_gz), "bytes": len(skills_gz)},
            LIBRARY_MEMBER: {"sha256": _sha256(library_gz), "bytes": len(library_gz)},
        },
    }
    if version == FORMAT_VERSION:
        manifest["files"][PACKAGES_MEMBER] = {"sha256": _sha256(packages_gz), "bytes": len(packages_gz)}
    output = output.resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=output.name, suffix=".tmp", dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp_name, "w", compression=zipfile.ZIP_STORED) as archive:
            archive.writestr(MANIFEST_MEMBER, json.dumps(manifest, indent=2, sort_keys=True) + "\n")
            archive.writestr(SKILLS_MEMBER, skills_gz)
            archive.writestr(LIBRARY_MEMBER, library_gz)
            if version == FORMAT_VERSION:
                archive.writestr(PACKAGES_MEMBER, packages_gz)
        os.replace(temp_name, output)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)
    load_package(output)
    return manifest


def _db_value(record: dict, field: str):
    value = record.get(field)
    if field in JSON_FIELDS:
        return json.dumps(value or [], sort_keys=True, separators=(",", ":"))
    return value


def _skill_diff(existing: sqlite3.Row, record: dict) -> bool:
    for field in UPDATE_FIELDS:
        if existing[field] != _db_value(record, field):
            return True
    return bytes(existing["embedding"] or b"") != bytes(record["embedding"])


def plan_import(db_path: Path, package: LoadedPackage) -> dict:
    conn = sqlite3.connect(f"file:{db_path.resolve().as_posix()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "skills" not in tables or "admin_audit_log" not in tables:
            raise SkillDeltaError("target is not an initialized Auto-Skill production database")
        inserted = updated = unchanged = 0
        for record in package.skills:
            existing = conn.execute(
                f"SELECT {','.join(UPDATE_FIELDS)},embedding FROM skills WHERE url=?",
                (record["url"],),
            ).fetchone()
            if existing is None:
                inserted += 1
            elif _skill_diff(existing, record):
                updated += 1
            else:
                unchanged += 1
        counts = conn.execute(
            "SELECT COUNT(*) total, SUM(quality_status='active') active, SUM(embedding IS NOT NULL) embedded FROM skills"
        ).fetchone()
        return {
            "inserted": inserted,
            "updated": updated,
            "unchanged": unchanged,
            "library_files": len(package.library),
            "packages": len(package.packages),
            "before": {"total": int(counts[0] or 0), "active": int(counts[1] or 0), "embedded": int(counts[2] or 0)},
        }
    finally:
        conn.close()


def _backup_database(db_path: Path, library_dir: Path, backup_root: Path, package: LoadedPackage) -> Path:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    destination = (backup_root.resolve() / f"pre-skill-import-{stamp}-{package.package_sha256[:12]}")
    destination.mkdir(parents=True, exist_ok=False)
    source = sqlite3.connect(db_path)
    target = sqlite3.connect(destination / "local_skills.db")
    try:
        source.backup(target)
    finally:
        target.close()
        source.close()
    index = library_dir / "index.json"
    if index.is_file():
        shutil.copy2(index, destination / "skills_library.index.json")
    (destination / "import-package.sha256").write_text(package.package_sha256 + "\n", encoding="ascii")
    return destination


def _safe_library_filename(record: dict) -> str:
    name = re.sub(r"[^A-Za-z0-9_-]+", "-", record["name"]).strip("-")[:80] or "skill"
    source = re.sub(r"[^A-Za-z0-9_-]+", "-", record["source"]).strip("-")[:40] or "source"
    url_hash = hashlib.sha1(record["url"].encode("utf-8")).hexdigest()[:10]
    return f"{source}__{name}__{url_hash}.md"


def _write_library(library_dir: Path, package: LoadedPackage) -> None:
    library_dir.mkdir(parents=True, exist_ok=True)
    files_dir = library_dir / "files"
    files_dir.mkdir(parents=True, exist_ok=True)
    index_path = library_dir / "index.json"
    try:
        index = json.loads(index_path.read_text(encoding="utf-8-sig")) if index_path.exists() else {}
    except (OSError, json.JSONDecodeError) as exc:
        raise SkillDeltaError(f"cannot update library index: {exc}") from exc
    if not isinstance(index, dict):
        raise SkillDeltaError("library index is not an object")
    by_url = {record["url"]: record for record in package.skills}
    for url, content in package.library.items():
        record = by_url[url]
        filename = _safe_library_filename(record)
        target = files_dir / filename
        fd, temp_name = tempfile.mkstemp(prefix=filename, suffix=".tmp", dir=files_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(content)
            os.replace(temp_name, target)
        finally:
            if os.path.exists(temp_name):
                os.unlink(temp_name)
        index[url] = {
            "name": record["name"],
            "source": record["source"],
            "url": url,
            "description": record.get("description"),
            "content_hash": record.get("content_hash") or quality.content_hash(content),
            "file": filename,
            "saved_at": _utc_now(),
        }
    fd, temp_name = tempfile.mkstemp(prefix="index.json", suffix=".tmp", dir=library_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(index, handle, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(temp_name, index_path)
    finally:
        if os.path.exists(temp_name):
            os.unlink(temp_name)


def _write_packages(library_dir: Path, package: LoadedPackage) -> None:
    """Install verified package manifests/objects through the immutable CAS."""
    if not package.packages:
        return
    store = ImmutablePackageStore(library_dir.resolve() / "packages")
    for item in package.packages.values():
        manifest = item["manifest"]
        store.put(manifest, item["objects"])


def _persist_package_metadata(conn: sqlite3.Connection, package: LoadedPackage) -> None:
    if not package.packages:
        return
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    required = {"skill_packages", "skill_package_files", "skill_package_sources"}
    if not required.issubset(tables):
        raise SkillDeltaError("target database is missing immutable package tables")
    columns = {row[1] for row in conn.execute("PRAGMA table_info(skills)")}
    if "package_hash" not in columns:
        raise SkillDeltaError("target database is missing skills.package_hash")
    for package_hash, item in package.packages.items():
        manifest = item["manifest"]
        source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
        license_info = manifest.get("license") if isinstance(manifest.get("license"), dict) else {}
        created_at = str(manifest.get("created_at") or _utc_now())
        conn.execute(
            """
            INSERT OR IGNORE INTO skill_packages
            (package_hash, source_url, source_provider, source_commit_sha, root_path,
             entrypoint_path, tree_sha, license_spdx, completeness_status,
             dependency_closure_status, entrypoint_truncated, manifest_json, created_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                package_hash,
                str(manifest.get("source_url") or ""),
                str(source.get("provider") or ""),
                str(source.get("commit_sha") or "") or None,
                str(source.get("root_path") or ""),
                str(manifest.get("entrypoint") or ""),
                str(source.get("tree_sha") or ""),
                license_info.get("spdx_id"),
                str(manifest.get("completeness_status") or "partial"),
                str(manifest.get("dependency_closure_status") or "unknown"),
                int(bool(manifest.get("entrypoint_truncated"))),
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                created_at,
            ),
        )
        source_url = str(manifest.get("source_url") or "")
        if source_url:
            conn.execute(
                """
                INSERT OR IGNORE INTO skill_package_sources
                (package_hash, source_url, source_commit_sha, provenance_json, observed_at)
                VALUES (?,?,?,?,?)
                """,
                (
                    package_hash,
                    source_url,
                    str(source.get("commit_sha") or ""),
                    json.dumps(manifest.get("provenance") or {}, sort_keys=True),
                    created_at,
                ),
            )
        for file_info in manifest.get("files") or []:
            conn.execute(
                """
                INSERT OR IGNORE INTO skill_package_files
                (package_hash, path, raw_sha256, git_blob_sha, size, role, media_type,
                 text_indexable, in_dependency_closure)
                VALUES (?,?,?,?,?,?,?,?,?)
                """,
                (
                    package_hash,
                    str(file_info.get("path") or ""),
                    str(file_info.get("raw_sha256") or ""),
                    str(file_info.get("git_blob_sha") or ""),
                    int(file_info.get("size") or 0),
                    str(file_info.get("role") or "other"),
                    str(file_info.get("media_type") or "application/octet-stream"),
                    int(bool(file_info.get("text_indexable"))),
                    int(bool(file_info.get("in_dependency_closure"))),
                ),
            )
        for url in item["skill_urls"]:
            conn.execute(
                """
                UPDATE skills SET package_hash=?, source_commit_sha=?, license_spdx=?,
                    package_completeness=?, dependency_closure_status=?, entrypoint_truncated=?
                WHERE url=?
                """,
                (
                    package_hash,
                    str(source.get("commit_sha") or "") or None,
                    license_info.get("spdx_id"),
                    str(manifest.get("completeness_status") or "partial"),
                    str(manifest.get("dependency_closure_status") or "unknown"),
                    int(bool(manifest.get("entrypoint_truncated"))),
                    url,
                ),
            )


def apply_import(
    db_path: Path,
    library_dir: Path,
    package: LoadedPackage,
    *,
    backup_root: Path,
    actor_email: str,
    reason: str,
) -> dict:
    actor_email = _validate_text(actor_email, "actor_email", 320, required=True)
    reason = _validate_text(reason, "reason", 500, required=True)
    if "@" not in actor_email:
        raise SkillDeltaError("actor_email must be an email address")
    plan = plan_import(db_path, package)
    backup_dir = _backup_database(db_path, library_dir, backup_root, package)
    _write_packages(library_dir, package)
    _write_library(library_dir, package)
    conn = sqlite3.connect(db_path, timeout=60)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=60000")
        conn.execute("BEGIN IMMEDIATE")
        insert_fields = ("id", *SKILL_FIELDS, "embedding")
        update_fields = (*UPDATE_FIELDS, "embedding")
        placeholders = ",".join("?" for _ in insert_fields)
        set_clause = ",".join(f"{field}=excluded.{field}" for field in update_fields)
        sql = (
            f"INSERT INTO skills ({','.join(insert_fields)}) VALUES ({placeholders}) "
            f"ON CONFLICT(url) DO UPDATE SET {set_clause}"
        )
        for record in package.skills:
            values = [str(uuid.uuid4())]
            for field in SKILL_FIELDS:
                values.append(_db_value(record, field))
            values.append(record["embedding"])
            conn.execute(sql, values)
            stored = conn.execute("SELECT id,content_hash FROM skills WHERE url=?", (record["url"],)).fetchone()
            if stored["content_hash"]:
                conn.execute(
                    "INSERT OR IGNORE INTO skill_versions (id,skill_id,content_hash,seen_at) VALUES (?,?,?,?)",
                    (str(uuid.uuid4()), stored["id"], stored["content_hash"], _utc_now()),
                )
        _persist_package_metadata(conn, package)
        new_value = {
            "package_sha256": package.package_sha256,
            "skills": len(package.skills),
            "inserted": plan["inserted"],
            "updated": plan["updated"],
            "unchanged": plan["unchanged"],
            "library_files": len(package.library),
            "packages": len(package.packages),
        }
        conn.execute(
            "INSERT INTO admin_audit_log "
            "(id,actor_user_id,actor_email,target_user_id,target_email,action,old_value,new_value,reason,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                str(uuid.uuid4()),
                f"offline-import:{actor_email.lower()}",
                actor_email.lower(),
                None,
                None,
                "skill_delta_import",
                json.dumps(plan["before"], sort_keys=True),
                json.dumps(new_value, sort_keys=True),
                reason,
                _utc_now(),
            ),
        )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()
    return {**plan, "backup_dir": str(backup_dir), "package_sha256": package.package_sha256}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Export, validate, or apply an Auto-Skill skill-only delta")
    sub = parser.add_subparsers(dest="command", required=True)
    export = sub.add_parser("export", help="export active embedded skills from a collector database")
    export.add_argument("--db", type=Path, required=True)
    export.add_argument("--library-dir", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    validate = sub.add_parser("validate", help="validate a package without opening a database")
    validate.add_argument("package", type=Path)
    plan = sub.add_parser("plan", help="show inserts/updates without changing production")
    plan.add_argument("package", type=Path)
    plan.add_argument("--db", type=Path, required=True)
    apply = sub.add_parser("apply", help="back up and atomically apply a package")
    apply.add_argument("package", type=Path)
    apply.add_argument("--db", type=Path, required=True)
    apply.add_argument("--library-dir", type=Path, required=True)
    apply.add_argument("--backup-root", type=Path, required=True)
    apply.add_argument("--actor-email", required=True)
    apply.add_argument("--reason", required=True)
    apply.add_argument("--confirm-apply", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.command == "export":
            result = export_package(args.db, args.library_dir, args.output)
        elif args.command == "validate":
            package = load_package(args.package)
            result = {
                "valid": True,
                "package_sha256": package.package_sha256,
                "skills": len(package.skills),
                "library_files": len(package.library),
                "packages": len(package.packages),
            }
        elif args.command == "plan":
            package = load_package(args.package)
            result = plan_import(args.db, package)
        else:
            if not args.confirm_apply:
                raise SkillDeltaError("apply requires --confirm-apply after reviewing the plan")
            package = load_package(args.package)
            result = apply_import(
                args.db,
                args.library_dir,
                package,
                backup_root=args.backup_root,
                actor_email=args.actor_email,
                reason=args.reason,
            )
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, sqlite3.Error, SkillDeltaError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
