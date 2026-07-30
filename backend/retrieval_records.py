"""Compact retrieval records derived from immutable source packages.

Source packages preserve every byte for integrity.  Retrieval records are a
separate, intentionally lossy view: one small entrypoint-first record per
skill.  This keeps package completeness from turning into all-file embedding
noise, matching the controlled retrieval result retained in the iteration
report.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import re
from typing import Any


RECORD_VERSION = "entrypoint-flat-1500-v1"
MAX_RETRIEVAL_CHARS = 1500

_FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n.*?\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_BADGE_RE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_LINK_RE = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_HTML_RE = re.compile(r"<[^>]{1,300}>")
_CODE_FENCE_RE = re.compile(r"```[^\n]*\n(.*?)```", re.S)
_SPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class RetrievalRecord:
    record_hash: str
    text_hash: str
    text: str
    record_version: str
    package_hash: str | None
    source_commit_sha: str | None
    entrypoint_path: str | None
    dependency_closure_status: str
    file_roles: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _normalise_markdown(text: str) -> str:
    value = _FRONTMATTER_RE.sub(" ", text or "", count=1)
    value = _BADGE_RE.sub(" ", value)
    value = _LINK_RE.sub(r"\1", value)
    value = _HTML_RE.sub(" ", value)
    value = _CODE_FENCE_RE.sub(lambda match: " " + match.group(1)[:300] + " ", value)
    return _SPACE_RE.sub(" ", value).strip()


def build_retrieval_record(
    skill: dict[str, Any],
    entrypoint_content: str,
    package_manifest: dict[str, Any] | None = None,
) -> RetrievalRecord:
    """Create the canonical compact record without mutating the package."""
    manifest = package_manifest or {}
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    tags = skill.get("tags") if isinstance(skill.get("tags"), list) else []
    header_parts = [
        str(skill.get("name") or "").strip(),
        str(skill.get("description") or "").strip(),
        " ".join(str(tag) for tag in tags[:12]),
        str(skill.get("capability_summary") or "").strip(),
    ]
    body = _normalise_markdown(entrypoint_content)
    text = _SPACE_RE.sub(" ", ". ".join(part for part in [*header_parts, body] if part)).strip()
    text = text[:MAX_RETRIEVAL_CHARS].rstrip()
    text_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()

    package_hash = str(manifest.get("package_hash") or "") or None
    entrypoint = str(manifest.get("entrypoint") or "") or None
    commit_sha = str(source.get("commit_sha") or manifest.get("source_commit_sha") or "") or None
    closure = str(manifest.get("dependency_closure_status") or "unknown")
    roles = sorted(
        {
            str(file.get("role"))
            for file in manifest.get("files") or []
            if isinstance(file, dict) and file.get("role")
        }
    )
    identity = json.dumps(
        {
            "version": RECORD_VERSION,
            "text_hash": text_hash,
            "package_hash": package_hash,
            "entrypoint": entrypoint,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    record_hash = hashlib.sha256(identity.encode("utf-8")).hexdigest()
    return RetrievalRecord(
        record_hash=record_hash,
        text_hash=text_hash,
        text=text,
        record_version=RECORD_VERSION,
        package_hash=package_hash,
        source_commit_sha=commit_sha,
        entrypoint_path=entrypoint,
        dependency_closure_status=closure,
        file_roles=tuple(roles),
    )


def embedding_parity(
    rows: list[dict[str, Any]],
    *,
    hash_builder,
) -> dict[str, Any]:
    """Report exact corpus/vector text parity; never infer a production win otherwise."""
    matched = 0
    missing = 0
    mismatched: list[str] = []
    for row in rows:
        stored = str(row.get("embedding_text_hash") or "")
        text = str(row.get("retrieval_text") or "")
        if not stored or not text:
            missing += 1
            continue
        actual = str(hash_builder(text))
        if actual == stored:
            matched += 1
        else:
            mismatched.append(str(row.get("id") or row.get("url") or "unknown"))
    total = len(rows)
    return {
        "record_version": RECORD_VERSION,
        "total": total,
        "matched": matched,
        "mismatched": len(mismatched),
        "missing": missing,
        "parity": bool(total) and matched == total,
        "mismatched_ids": mismatched[:100],
    }
