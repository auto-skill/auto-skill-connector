"""Live skills.sh catalog access for task-time discovery.

skills.sh already owns the large public catalog. Auto-Skill should query that
catalog instead of mirroring every skill locally. This module keeps the
integration deliberately small: search the remote index, hydrate only a
bounded shortlist, fetch audit metadata, and shape the result into the
existing deterministic routing schema. The full source package is never
returned by this module as an active instruction payload.
"""

from __future__ import annotations

import asyncio
from contextlib import contextmanager
import hashlib
import json
import os
import re
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlsplit
from pathlib import Path

import httpx

from quality import content_hash, evaluate_quality


DEFAULT_API_URL = "https://skills.sh/api/v1"
DEFAULT_PUBLIC_SEARCH_URL = "https://skills.sh/api/search"
DEFAULT_TIMEOUT_SECONDS = 8.0
DEFAULT_SEARCH_TTL_SECONDS = 45.0
DEFAULT_DETAIL_TTL_SECONDS = 300.0
DEFAULT_AUDIT_TTL_SECONDS = 300.0
DEFAULT_MAX_CONCURRENT_REQUESTS = max(1, int(os.getenv("SKILLS_SH_MAX_CONCURRENT_REQUESTS", "4")))
DEFAULT_MAX_RETRIES = max(0, int(os.getenv("SKILLS_SH_MAX_RETRIES", "3")))
DEFAULT_RETRY_BASE_SECONDS = max(0.0, float(os.getenv("SKILLS_SH_RETRY_BASE_SECONDS", "0.5")))
DEFAULT_MIN_REQUEST_INTERVAL_SECONDS = max(
    0.0, float(os.getenv("SKILLS_SH_MIN_REQUEST_INTERVAL_SECONDS", "0.1"))
)
DEFAULT_MIRROR_STALE_SECONDS = max(
    0.0, float(os.getenv("SKILLS_SH_MIRROR_STALE_SECONDS", "3600"))
)
DEFAULT_SOURCE_RETENTION_SECONDS = max(
    86_400.0, float(os.getenv("SKILLS_SH_SOURCE_RETENTION_SECONDS", str(30 * 86_400)))
)
DEFAULT_LISTING_TTL_SECONDS = max(
    300.0, float(os.getenv("SKILLS_SH_LISTING_TTL_SECONDS", "3600"))
)
MAX_RETRY_DELAY_SECONDS = 30.0
MAX_SEARCH_LIMIT = 50
MAX_DETAIL_CANDIDATES = 12
MAX_RETRIEVAL_CHARS = 1_500
MAX_PUBLIC_DESCRIPTION_CHARS = 800
MAX_PUBLIC_PAGE_METADATA = min(3, max(0, int(os.getenv("SKILLS_SH_PUBLIC_PAGE_METADATA", "1"))))

_FRONTMATTER_RE = re.compile(r"\A\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_FIELD_RE = re.compile(r"^(name|description):[ \t]*(.*)$", re.I)
_TOKEN_RE = re.compile(r"[a-z0-9]+", re.I)
_REFERENCE_RE = re.compile(r"\]\(([^)#\s]+)|(?<![\w])((?:\.{0,2}/)[^\s)`>]+)", re.I)


class SkillsShCatalogError(RuntimeError):
    """Raised when the live catalog cannot be queried safely."""


@dataclass(frozen=True)
class _CacheEntry:
    expires_at: float
    value: Any


class _PersistentMirror:
    """Small shared SQLite mirror for already-hydrated skills.sh records.

    The mirror is deliberately separate from the publisher corpus: every row
    must carry the skills.sh ID and snapshot hash, and callers can distinguish
    a cached upstream record from an arbitrary local skill.  A mounted SQLite
    path (or a shared database volume) makes this useful across restarts and
    replicas; the request path never needs to query skills.sh for a warm row.
    """

    _SCHEMA = """
    CREATE TABLE IF NOT EXISTS skills_sh_mirror (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '',
        retrieval_text TEXT NOT NULL DEFAULT '',
        row_json TEXT NOT NULL,
        snapshot_hash TEXT,
        updated_at REAL NOT NULL,
        expires_at REAL NOT NULL,
        stale_until REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS skills_sh_mirror_expiry_idx
        ON skills_sh_mirror(expires_at, stale_until);
    CREATE TABLE IF NOT EXISTS skills_sh_sources (
        content_hash TEXT PRIMARY KEY,
        canonical_skill_id TEXT NOT NULL DEFAULT '',
        snapshot_hash TEXT,
        entrypoint_path TEXT,
        content TEXT NOT NULL,
        files_json TEXT NOT NULL DEFAULT '[]',
        byte_count INTEGER NOT NULL DEFAULT 0,
        created_at REAL NOT NULL,
        last_seen_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS skills_sh_sources_snapshot_idx
        ON skills_sh_sources(snapshot_hash);
    CREATE TABLE IF NOT EXISTS skills_sh_ingestion_attempts (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        skill_id TEXT NOT NULL,
        snapshot_hash TEXT,
        status TEXT NOT NULL,
        reason TEXT NOT NULL DEFAULT '',
        retryable INTEGER NOT NULL DEFAULT 0,
        content_hash TEXT,
        byte_count INTEGER NOT NULL DEFAULT 0,
        error TEXT,
        attempted_at REAL NOT NULL
    );
    CREATE INDEX IF NOT EXISTS skills_sh_ingestion_attempts_lookup_idx
        ON skills_sh_ingestion_attempts(skill_id, snapshot_hash, attempted_at DESC);
    CREATE VIRTUAL TABLE IF NOT EXISTS skills_sh_mirror_fts USING fts5(
        id UNINDEXED, name, description, retrieval_text,
        content='skills_sh_mirror', content_rowid='rowid'
    );
    CREATE TRIGGER IF NOT EXISTS skills_sh_mirror_ai AFTER INSERT ON skills_sh_mirror BEGIN
        INSERT INTO skills_sh_mirror_fts(rowid, id, name, description, retrieval_text)
        VALUES (new.rowid, new.id, new.name, new.description, new.retrieval_text);
    END;
    CREATE TRIGGER IF NOT EXISTS skills_sh_mirror_ad AFTER DELETE ON skills_sh_mirror BEGIN
        INSERT INTO skills_sh_mirror_fts(skills_sh_mirror_fts, rowid, id, name, description, retrieval_text)
        VALUES ('delete', old.rowid, old.id, old.name, old.description, old.retrieval_text);
    END;
    CREATE TRIGGER IF NOT EXISTS skills_sh_mirror_au AFTER UPDATE ON skills_sh_mirror BEGIN
        INSERT INTO skills_sh_mirror_fts(skills_sh_mirror_fts, rowid, id, name, description, retrieval_text)
        VALUES ('delete', old.rowid, old.id, old.name, old.description, old.retrieval_text);
        INSERT INTO skills_sh_mirror_fts(rowid, id, name, description, retrieval_text)
        VALUES (new.rowid, new.id, new.name, new.description, new.retrieval_text);
    END;
    """

    def __init__(self, path: str) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._connection() as conn:
            conn.executescript(self._SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        return conn

    @contextmanager
    def _connection(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _fts_query(query: str) -> str:
        tokens = re.findall(r"[a-z0-9]+", query.casefold())
        return " OR ".join(f'"{token}"' for token in dict.fromkeys(tokens[:24]))

    def search(self, query: str, limit: int, now: float) -> list[dict[str, Any]]:
        fts_query = self._fts_query(query)
        if not fts_query:
            return []
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                """
                SELECT m.row_json, m.expires_at
                FROM skills_sh_mirror_fts f
                JOIN skills_sh_mirror m ON m.rowid = f.rowid
                WHERE skills_sh_mirror_fts MATCH ? AND m.stale_until >= ?
                ORDER BY CASE WHEN m.expires_at >= ? THEN 0 ELSE 1 END,
                         bm25(skills_sh_mirror_fts), m.updated_at DESC
                LIMIT ?
                """,
                (fts_query, now, now, min(MAX_SEARCH_LIMIT, max(1, int(limit)) * 3)),
            ).fetchall()
        values: list[dict[str, Any]] = []
        for row in rows:
            try:
                value = json.loads(row["row_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                self._attach_source(conn=None, value=value)
                value["retrieval_backend"] = "skills_sh_mirror"
                fresh = float(row["expires_at"]) >= now
                value["mirror_fresh"] = fresh
                if not fresh:
                    value["quality_status"] = "metadata_only"
                    value["quality_reasons"] = sorted(
                        set([*(value.get("quality_reasons") or []), "skills-sh-mirror-stale"])
                    )
                values.append(value)
        return self._dedupe_rows(values)[: max(1, int(limit))]

    def get_ids(self, ids: list[str], now: float) -> list[dict[str, Any]]:
        if not ids:
            return []
        marks = ",".join("?" for _ in ids)
        with self._lock, self._connection() as conn:
            rows = conn.execute(
                f"SELECT id, row_json, expires_at FROM skills_sh_mirror "
                f"WHERE id IN ({marks}) AND stale_until >= ?",
                (*ids, now),
            ).fetchall()
        by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                value = json.loads(row["row_json"])
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict):
                self._attach_source(None, value)
                value["retrieval_backend"] = "skills_sh_mirror"
                fresh = float(row["expires_at"]) >= now
                value["mirror_fresh"] = fresh
                if not fresh:
                    value["quality_status"] = "metadata_only"
                    value["quality_reasons"] = sorted(
                        set([*(value.get("quality_reasons") or []), "skills-sh-mirror-stale"])
                    )
                by_id[str(row["id"])] = value
        return [by_id[item] for item in ids if item in by_id]

    @staticmethod
    def _dedupe_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
        seen: set[str] = set()
        result: list[dict[str, Any]] = []
        for row in rows:
            key = str(row.get("content_hash") or row.get("skills_sh_id") or row.get("id") or "")
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            result.append(row)
        return result

    def _attach_source(self, conn: sqlite3.Connection | None, value: dict[str, Any]) -> None:
        """Join compact mirror metadata to its immutable source blob.

        Legacy rows may still carry ``_content`` inline. They remain readable
        until the migration command rewrites them into ``skills_sh_sources``.
        """
        if value.get("_content") or not value.get("content_hash"):
            return
        owned = conn
        close = False
        if owned is None:
            owned = sqlite3.connect(self.path, timeout=30)
            owned.row_factory = sqlite3.Row
            close = True
        try:
            source = owned.execute(
                "SELECT content, files_json FROM skills_sh_sources WHERE content_hash=?",
                (str(value.get("content_hash")),),
            ).fetchone()
            if source:
                value["_content"] = str(source["content"] or "")
                try:
                    value["_source_files"] = json.loads(source["files_json"] or "[]")
                except (TypeError, ValueError):
                    value["_source_files"] = []
        finally:
            if close:
                owned.close()

    def get_content_by_hash(self, target_hash: str) -> str:
        """Return the complete hydrated entrypoint for an exact content hash."""
        with self._lock, self._connection() as conn:
            source = conn.execute(
                "SELECT content FROM skills_sh_sources WHERE content_hash=?", (target_hash,)
            ).fetchone()
            if source:
                return str(source["content"] or "")
            # Compatibility for pre-migration rows.
            row = conn.execute(
                "SELECT row_json FROM skills_sh_mirror WHERE json_extract(row_json, '$.content_hash')=? LIMIT 1",
                (target_hash,),
            ).fetchone()
        if not row:
            return ""
        try:
            value = json.loads(row["row_json"])
        except (TypeError, ValueError):
            return ""
        return str(value.get("_content") or "") if isinstance(value, dict) else ""

    def record_attempt(
        self,
        *,
        skill_id: str,
        snapshot_hash: str | None,
        status: str,
        reason: str = "",
        retryable: bool = False,
        content_hash_value: str | None = None,
        byte_count: int = 0,
        error: str | None = None,
    ) -> None:
        with self._lock, self._connection() as conn:
            conn.execute(
                """
                INSERT INTO skills_sh_ingestion_attempts
                    (skill_id, snapshot_hash, status, reason, retryable,
                     content_hash, byte_count, error, attempted_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    skill_id,
                    snapshot_hash,
                    status,
                    reason,
                    int(retryable),
                    content_hash_value,
                    int(byte_count or 0),
                    error,
                    time.time(),
                ),
            )

    def latest_attempt(self, skill_id: str, snapshot_hash: str | None) -> dict[str, Any] | None:
        with self._lock, self._connection() as conn:
            row = conn.execute(
                """
                SELECT skill_id, snapshot_hash, status, reason, retryable,
                       content_hash, byte_count, error, attempted_at
                FROM skills_sh_ingestion_attempts
                WHERE skill_id=? AND (snapshot_hash=? OR (snapshot_hash IS NULL AND ? IS NULL))
                ORDER BY attempted_at DESC LIMIT 1
                """,
                (skill_id, snapshot_hash, snapshot_hash),
            ).fetchone()
        return dict(row) if row else None

    def migrate_legacy_rows(self, *, retention_seconds: float = DEFAULT_SOURCE_RETENTION_SECONDS) -> dict[str, int]:
        """Move inline legacy bodies into the immutable source table safely."""
        migrated = 0
        retained = 0
        now = time.time()
        with self._lock, self._connection() as conn:
            rows = conn.execute("SELECT id, row_json, stale_until FROM skills_sh_mirror").fetchall()
            for row in rows:
                try:
                    value = json.loads(row["row_json"])
                except (TypeError, ValueError):
                    continue
                if not isinstance(value, dict):
                    continue
                content = str(value.get("_content") or "")
                content_hash_value = str(value.get("content_hash") or "")
                if content and content_hash_value:
                    files = value.get("_source_files") or []
                    try:
                        files_json = json.dumps(files, separators=(",", ":"), ensure_ascii=False)
                    except (TypeError, ValueError):
                        files_json = "[]"
                    conn.execute(
                        """
                        INSERT INTO skills_sh_sources
                            (content_hash, canonical_skill_id, snapshot_hash, entrypoint_path,
                             content, files_json, byte_count, created_at, last_seen_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        ON CONFLICT(content_hash) DO UPDATE SET
                            last_seen_at=excluded.last_seen_at
                        """,
                        (
                            content_hash_value,
                            str(value.get("skills_sh_id") or value.get("id") or ""),
                            str(value.get("source_snapshot_hash") or "") or None,
                            str((value.get("raw") or {}).get("entrypoint_path") or "")
                            if isinstance(value.get("raw"), dict)
                            else "",
                            content,
                            files_json,
                            len(content.encode("utf-8")),
                            now,
                            now,
                        ),
                    )
                    value.pop("_content", None)
                    value.pop("_source_files", None)
                    conn.execute(
                        "UPDATE skills_sh_mirror SET row_json=? WHERE id=?",
                        (json.dumps(value, separators=(",", ":"), ensure_ascii=False), row["id"]),
                    )
                    migrated += 1
                conn.execute(
                    "UPDATE skills_sh_mirror SET stale_until=? WHERE id=?",
                    (max(float(row["stale_until"] or 0), now + retention_seconds), row["id"]),
                )
                retained += 1
        return {"migrated": migrated, "retained": retained}

    def put(
        self,
        rows: list[dict[str, Any]],
        *,
        expires_at: float,
        stale_until: float,
        source_retention_until: float | None = None,
    ) -> None:
        values = []
        for row in rows:
            skill_id = str(row.get("skills_sh_id") or row.get("id") or "").strip()
            if not skill_id:
                continue
            try:
                row_for_json = dict(row)
                row_for_json.pop("_content", None)
                row_for_json.pop("_source_files", None)
                row_json = json.dumps(row_for_json, separators=(",", ":"), ensure_ascii=False)
            except (TypeError, ValueError):
                continue
            values.append(
                (
                    skill_id,
                    str(row.get("name") or ""),
                    str(row.get("description") or ""),
                    str(row.get("retrieval_text") or ""),
                    row_json,
                    str(row.get("source_snapshot_hash") or "") or None,
                    time.time(),
                    expires_at,
                    max(stale_until, float(source_retention_until or 0.0)),
                )
            )
        if not values:
            return
        now = time.time()
        with self._lock, self._connection() as conn:
            for row in rows:
                content = str(row.get("_content") or "")
                content_hash_value = str(row.get("content_hash") or "")
                if not content or not content_hash_value:
                    continue
                try:
                    files_json = json.dumps(row.get("_source_files") or [], separators=(",", ":"), ensure_ascii=False)
                except (TypeError, ValueError):
                    files_json = "[]"
                conn.execute(
                    """
                    INSERT INTO skills_sh_sources
                        (content_hash, canonical_skill_id, snapshot_hash, entrypoint_path,
                         content, files_json, byte_count, created_at, last_seen_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(content_hash) DO UPDATE SET
                        last_seen_at=excluded.last_seen_at
                    """,
                    (
                        content_hash_value,
                        str(row.get("skills_sh_id") or row.get("id") or ""),
                        str(row.get("source_snapshot_hash") or "") or None,
                        str((row.get("raw") or {}).get("entrypoint_path") or "")
                        if isinstance(row.get("raw"), dict)
                        else "",
                        content,
                        files_json,
                        len(content.encode("utf-8")),
                        now,
                        now,
                    ),
                )
            conn.executemany(
                """
                INSERT INTO skills_sh_mirror
                    (id, name, description, retrieval_text, row_json, snapshot_hash,
                     updated_at, expires_at, stale_until)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    name=excluded.name,
                    description=excluded.description,
                    retrieval_text=excluded.retrieval_text,
                    row_json=excluded.row_json,
                    snapshot_hash=excluded.snapshot_hash,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at,
                    stale_until=excluded.stale_until
                """,
                values,
            )


def _frontmatter_fields(text: str) -> dict[str, str]:
    match = _FRONTMATTER_RE.match(text or "")
    if not match:
        return {}
    fields: dict[str, str] = {}
    for line in match.group(1).splitlines():
        field = _FIELD_RE.match(line.strip())
        if field:
            fields[field.group(1).casefold()] = field.group(2).strip().strip('"\'')
    return fields


def _entrypoint(files: list[dict[str, Any]]) -> tuple[str, str]:
    candidates: list[tuple[str, str]] = []
    for file in files:
        if not isinstance(file, dict):
            continue
        path = str(file.get("path") or "").replace("\\", "/").strip("/")
        contents = file.get("contents")
        if not path or contents is None:
            continue
        if path.casefold() == "skill.md" or path.casefold().endswith("/skill.md"):
            candidates.append((path, str(contents)))
    return sorted(candidates, key=lambda item: (item[0].count("/"), item[0].casefold()))[0] if candidates else ("", "")


def _retrieval_text(name: str, description: str, content: str) -> str:
    body = _FRONTMATTER_RE.sub(" ", content or "", count=1)
    body = re.sub(r"[`*_>#]", " ", body)
    body = re.sub(r"\s+", " ", body).strip()
    value = " ".join(part for part in (name, description, body) if part).strip()
    return value[:MAX_RETRIEVAL_CHARS]


def _reference_closure(files: list[dict[str, Any]], entrypoint: str) -> tuple[list[str], list[str]]:
    """Resolve local markdown/path references against the captured package."""
    available = {
        str(item.get("path") or "").replace("\\", "/").strip("/")
        for item in files
        if isinstance(item, dict) and item.get("path")
    }
    refs: set[str] = set()
    for item in files:
        if not isinstance(item, dict) or item.get("contents") is None:
            continue
        text = str(item.get("contents"))
        for match in _REFERENCE_RE.finditer(text):
            reference = (match.group(1) or match.group(2) or "").strip().strip("`'\"")
            if not reference or reference.startswith(("http://", "https://", "mailto:")):
                continue
            base = Path(str(item.get("path") or "").replace("\\", "/")).parent
            normalized = (base / reference).as_posix().lstrip("./")
            refs.add(normalized)
    unresolved = sorted(reference for reference in refs if reference not in available)
    return sorted(refs), unresolved


def _audit_summary(audits: list[dict[str, Any]] | None) -> tuple[str, str, int, list[str]]:
    """Collapse partner audits without treating missing audits as safe."""
    if not audits:
        return "unknown", "unknown", 1, ["audit-unavailable"]
    statuses = {str(item.get("status") or "").casefold() for item in audits if isinstance(item, dict)}
    risks = {str(item.get("riskLevel") or "").casefold() for item in audits if isinstance(item, dict)}
    if "fail" in statuses or risks & {"critical", "high"}:
        return "fail", "critical" if "critical" in risks else "high", 3, ["audit-fail"]
    if "warn" in statuses or risks & {"medium", "high"}:
        return "warn", "medium" if "medium" in risks else "warn", 1, ["audit-warn"]
    if statuses and statuses <= {"pass"}:
        return "pass", "low" if "low" in risks else "none", 0, []
    return "unknown", "unknown", 1, ["audit-incomplete"]


def _stable_skill_id(item: dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


class SkillsShCatalog:
    """skills.sh client with a persistent mirror and bounded upstream access."""

    def __init__(
        self,
        *,
        api_url: str | None = None,
        public_search_url: str | None = None,
        public_page_base_url: str | None = None,
        oidc_token: str | None = None,
        timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
        search_ttl_seconds: float = DEFAULT_SEARCH_TTL_SECONDS,
        detail_ttl_seconds: float = DEFAULT_DETAIL_TTL_SECONDS,
        audit_ttl_seconds: float = DEFAULT_AUDIT_TTL_SECONDS,
        max_concurrent_requests: int = DEFAULT_MAX_CONCURRENT_REQUESTS,
        max_retries: int = DEFAULT_MAX_RETRIES,
        retry_base_seconds: float = DEFAULT_RETRY_BASE_SECONDS,
        min_request_interval_seconds: float = DEFAULT_MIN_REQUEST_INTERVAL_SECONDS,
        mirror_db_path: str | None = None,
        mirror_enabled: bool | None = None,
        mirror_stale_seconds: float = DEFAULT_MIRROR_STALE_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.api_url = (api_url or os.getenv("SKILLS_SH_API_URL", DEFAULT_API_URL)).rstrip("/")
        self.public_search_url = public_search_url or os.getenv(
            "SKILLS_SH_PUBLIC_SEARCH_URL", DEFAULT_PUBLIC_SEARCH_URL
        )
        self.public_page_base_url = public_page_base_url or (
            f"{urlsplit(self.public_search_url).scheme}://{urlsplit(self.public_search_url).netloc}"
        )
        self._explicit_oidc_token = oidc_token
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.search_ttl_seconds = max(0.0, float(search_ttl_seconds))
        self.detail_ttl_seconds = max(0.0, float(detail_ttl_seconds))
        self.audit_ttl_seconds = max(0.0, float(audit_ttl_seconds))
        self.max_concurrent_requests = max(1, int(max_concurrent_requests))
        self.max_retries = max(0, int(max_retries))
        self.retry_base_seconds = max(0.0, float(retry_base_seconds))
        self.min_request_interval_seconds = max(0.0, float(min_request_interval_seconds))
        self.mirror_stale_seconds = max(0.0, float(mirror_stale_seconds))
        self.transport = transport
        self._cache: dict[tuple[str, str], _CacheEntry] = {}
        # Tests and custom transports stay isolated unless they explicitly
        # opt into persistence. The production singleton uses the configured
        # shared path by default.
        if mirror_enabled is None:
            mirror_enabled = transport is None or bool(os.getenv("SKILLS_SH_MIRROR_DB_PATH"))
        self.mirror_enabled = bool(mirror_enabled)
        configured_path = mirror_db_path or os.getenv("SKILLS_SH_MIRROR_DB_PATH", "")
        if not configured_path:
            configured_path = os.getenv(
                "LOCAL_DB_PATH", str(Path(__file__).with_name(".skills_sh_mirror.db"))
            )
        if "://" in str(configured_path):
            # LOCAL_DB_PATH may be repurposed as a remote URL by deployments;
            # never attempt to create a filesystem path from that value.
            configured_path = str(Path(__file__).with_name(".skills_sh_mirror.db"))
        self._mirror = _PersistentMirror(configured_path) if self.mirror_enabled else None
        self._inflight: dict[tuple[str, str], asyncio.Task[Any]] = {}
        # A catalog can be used by tests across multiple asyncio.run calls;
        # bind the semaphore lazily to the loop that owns the request.
        self._request_semaphore: asyncio.Semaphore | None = None
        self._request_loop: asyncio.AbstractEventLoop | None = None
        self._pacing_lock: asyncio.Lock | None = None
        self._pacing_loop: asyncio.AbstractEventLoop | None = None
        self._last_request_at = 0.0

    async def _singleflight(self, key: tuple[str, str], factory: Any) -> Any:
        """Share one in-flight refresh across concurrent user requests."""
        task = self._inflight.get(key)
        if task is None:
            task = asyncio.create_task(factory())
            self._inflight[key] = task
        try:
            return await task
        finally:
            if self._inflight.get(key) is task:
                self._inflight.pop(key, None)

    async def _mirror_search(self, query: str, limit: int) -> list[dict[str, Any]]:
        if self._mirror is None:
            return []
        return await asyncio.to_thread(self._mirror.search, query, limit, time.time())

    async def _mirror_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        if self._mirror is None:
            return []
        return await asyncio.to_thread(self._mirror.get_ids, ids, time.time())

    async def content_by_hash(self, target_hash: str) -> str:
        if self._mirror is None or not target_hash:
            return ""
        return await asyncio.to_thread(self._mirror.get_content_by_hash, target_hash)

    async def cached_ids(self, ids: list[str]) -> list[dict[str, Any]]:
        """Return mirror rows for a sync worker without an upstream call."""
        return await self._mirror_ids(ids)

    async def _mirror_put(self, rows: list[dict[str, Any]], *, ttl_seconds: float | None = None) -> None:
        if self._mirror is None or not rows:
            return
        now = time.time()
        ttl = max(
            self.detail_ttl_seconds,
            self.audit_ttl_seconds,
            self.search_ttl_seconds,
            float(ttl_seconds or 0.0),
        )
        expires_at = now + ttl
        source_retention_until = now + DEFAULT_SOURCE_RETENTION_SECONDS
        await asyncio.to_thread(
            self._mirror.put,
            rows,
            expires_at=expires_at,
            stale_until=expires_at + self.mirror_stale_seconds,
            source_retention_until=source_retention_until,
        )

    async def migrate_mirror(self) -> dict[str, int]:
        if self._mirror is None:
            return {"migrated": 0, "retained": 0}
        return await asyncio.to_thread(
            self._mirror.migrate_legacy_rows,
            retention_seconds=DEFAULT_SOURCE_RETENTION_SECONDS,
        )

    async def record_ingestion_attempt(self, **kwargs: Any) -> None:
        if self._mirror is None:
            return
        await asyncio.to_thread(self._mirror.record_attempt, **kwargs)

    async def latest_ingestion_attempt(
        self, skill_id: str, snapshot_hash: str | None
    ) -> dict[str, Any] | None:
        if self._mirror is None:
            return None
        return await asyncio.to_thread(self._mirror.latest_attempt, skill_id, snapshot_hash)

    async def hydrate_listings(
        self,
        listings: list[dict[str, Any]],
        limit: int = MAX_DETAIL_CANDIDATES,
    ) -> list[dict[str, Any]]:
        """Hydrate and persist a bounded listing batch for the sync worker."""
        shortlist = [dict(item) for item in listings[: max(1, int(limit))]]
        rows = await self._materialize(shortlist)
        await self._mirror_put(rows)
        return rows

    async def index_listings(self, listings: list[dict[str, Any]]) -> int:
        """Persist listing metadata without fetching package bodies.

        The leaderboard is the complete discovery prior, not a trust grant.
        Listing-only rows remain ``metadata_only`` hints until a bounded
        shortlist is hydrated through the detail and audit endpoints.
        """
        rows: list[dict[str, Any]] = []
        for item in listings:
            skill_id = _stable_skill_id(item)
            if not skill_id or bool(item.get("isDuplicate")):
                continue
            name = str(item.get("name") or item.get("slug") or skill_id.rsplit("/", 1)[-1])
            description = str(item.get("description") or "")
            source = str(item.get("source") or "skills_sh")
            slug = str(item.get("slug") or "")
            install_url = str(item.get("installUrl") or "")
            page_url = str(item.get("url") or f"https://skills.sh/{skill_id}")
            retrieval_text = _retrieval_text(name, description, "")
            retrieval_text = " ".join(part for part in (retrieval_text, source, slug) if part)[:MAX_RETRIEVAL_CHARS]
            rows.append(
                {
                    "id": skill_id,
                    "name": name,
                    "description": description,
                    "source": source,
                    "registry": "skills_sh",
                    "slug": slug,
                    "url": install_url or page_url,
                    "skills_sh_url": page_url,
                    "install_url": install_url,
                    "skills_sh_id": skill_id,
                    "installs": item.get("installs"),
                    "source_type": item.get("sourceType"),
                    "is_duplicate": False,
                    "source_snapshot_hash": None,
                    "content_hash": None,
                    "retrieval_text": retrieval_text,
                    "retrieval_text_hash": hashlib.sha256(retrieval_text.encode()).hexdigest(),
                    "quality_status": "metadata_only",
                    "quality_score": 0,
                    "quality_reasons": ["skills-sh-listing-only"],
                    "audit_status": "unknown",
                    "audit_risk_level": "unknown",
                    "audit_count": 0,
                    "risk_score": 1,
                    "risk_flags": ["audit-unavailable"],
                    "package_completeness": "unknown",
                    "dependency_closure_status": "unresolved",
                    "entrypoint_truncated": 0,
                }
            )
        await self._mirror_put(rows, ttl_seconds=DEFAULT_LISTING_TTL_SECONDS)
        return len(rows)

    @property
    def configured(self) -> bool:
        """The documented API requires a current Vercel OIDC bearer token."""
        return bool(self._current_oidc_token())

    def _current_oidc_token(self) -> str:
        if self._explicit_oidc_token is not None:
            return str(self._explicit_oidc_token).strip()
        token = os.getenv("SKILLS_SH_OIDC_TOKEN", "") or os.getenv("VERCEL_OIDC_TOKEN", "")
        token_file = os.getenv("SKILLS_SH_OIDC_TOKEN_FILE", "").strip()
        if not token and token_file:
            try:
                # Windows PowerShell may write UTF-8 files with a BOM. It is
                # valid file metadata but cannot appear in an HTTP header.
                token = Path(token_file).read_text(encoding="utf-8").lstrip("\ufeff").strip()
            except OSError:
                token = ""
        return token.lstrip("\ufeff").strip()

    def _cached(self, kind: str, key: str) -> Any | None:
        entry = self._cache.get((kind, key))
        if entry and entry.expires_at > time.monotonic():
            return entry.value
        if entry:
            self._cache.pop((kind, key), None)
        return None

    def _put(self, kind: str, key: str, value: Any, ttl: float) -> Any:
        if ttl > 0:
            self._cache[(kind, key)] = _CacheEntry(time.monotonic() + ttl, value)
        return value

    def _semaphore(self) -> asyncio.Semaphore:
        loop = asyncio.get_running_loop()
        if self._request_semaphore is None or self._request_loop is not loop:
            self._request_loop = loop
            self._request_semaphore = asyncio.Semaphore(self.max_concurrent_requests)
        return self._request_semaphore

    def _pacer(self) -> asyncio.Lock:
        loop = asyncio.get_running_loop()
        if self._pacing_lock is None or self._pacing_loop is not loop:
            self._pacing_loop = loop
            self._pacing_lock = asyncio.Lock()
            self._last_request_at = 0.0
        return self._pacing_lock

    async def _wait_for_request_slot(self) -> None:
        if self.min_request_interval_seconds <= 0:
            return
        async with self._pacer():
            elapsed = time.monotonic() - self._last_request_at
            delay = self.min_request_interval_seconds - elapsed
            if delay > 0:
                await asyncio.sleep(delay)
            self._last_request_at = time.monotonic()

    def _retry_delay(self, response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After", "").strip()
        try:
            delay = float(retry_after) if retry_after else self.retry_base_seconds * (2**attempt)
        except ValueError:
            delay = self.retry_base_seconds * (2**attempt)
        return min(MAX_RETRY_DELAY_SECONDS, max(0.0, delay))

    async def _request_with_retry(
        self,
        client: httpx.AsyncClient,
        method: str,
        url: str,
        **kwargs: Any,
    ) -> httpx.Response:
        retryable_statuses = {429, 502, 503, 504}
        for attempt in range(self.max_retries + 1):
            try:
                await self._wait_for_request_slot()
                async with self._semaphore():
                    response = await client.request(method, url, **kwargs)
            except (httpx.HTTPError, OSError) as exc:
                if attempt >= self.max_retries:
                    raise SkillsShCatalogError(f"skills.sh request failed: {type(exc).__name__}") from exc
                await asyncio.sleep(min(MAX_RETRY_DELAY_SECONDS, self.retry_base_seconds * (2**attempt)))
                continue
            if response.status_code not in retryable_statuses or attempt >= self.max_retries:
                return response
            await asyncio.sleep(self._retry_delay(response, attempt))
        raise SkillsShCatalogError("skills.sh request retry budget exhausted")

    async def _get(self, path: str, *, params: dict[str, str] | None = None) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        token = self._current_oidc_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=self.timeout_seconds,
            follow_redirects=True,
        ) as client:
            response = await self._request_with_retry(
                client,
                "GET",
                f"{self.api_url}/{path.lstrip('/')}",
                params=params,
                headers=headers,
            )
        if response.status_code == 404:
            raise SkillsShCatalogError("skills.sh resource not found")
        if response.status_code in {401, 403}:
            raise SkillsShCatalogError("skills.sh authentication rejected")
        if response.status_code >= 400:
            raise SkillsShCatalogError(f"skills.sh returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillsShCatalogError("skills.sh returned invalid JSON") from exc
        return payload if isinstance(payload, dict) else {}

    async def search(self, query: str, limit: int = 10) -> list[dict[str, Any]]:
        query = " ".join(str(query or "").split())
        if len(query) < 2:
            return []
        limit = max(1, min(int(limit), MAX_SEARCH_LIMIT))
        cache_key = f"{query.casefold()}::{limit}::{'auth' if self.configured else 'public'}"
        cached = self._cached("search", cache_key)
        if cached is not None:
            return [dict(item) for item in cached]
        # Search the persistent skills.sh mirror before spending upstream
        # quota. ``search`` returns listing rows for API compatibility; the
        # hydrated ``retrieve`` path below is what normally populates it.
        mirrored = await self._mirror_search(query, limit)
        if mirrored:
            return [dict(item) for item in self._put("search", cache_key, mirrored, self.search_ttl_seconds)]
        return await self._singleflight(
            ("search", cache_key), lambda: self._search_remote(query, limit, cache_key)
        )

    async def _search_remote(self, query: str, limit: int, cache_key: str) -> list[dict[str, Any]]:
        try:
            payload = await self._get("skills/search", params={"q": query, "limit": str(limit)}) if self.configured else {}
            data = payload.get("data")
        except SkillsShCatalogError as exc:
            if "authentication rejected" not in str(exc):
                raise
            data = None
        if data is None:
            payload = await self._public_search(query, limit)
            data = payload.get("skills")
        rows = [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else []
        if payload.get("skills") is not None:
            rows = [self._public_listing_row(item) for item in rows]
        # Keep the last listing metadata available for a follow-up selection.
        # This is intentionally a bounded cache; it is not a second catalog.
        for item in rows:
            skill_id = _stable_skill_id(item)
            if skill_id:
                self._put("listing", skill_id, dict(item), self.search_ttl_seconds)
        return [dict(item) for item in self._put("search", cache_key, rows, self.search_ttl_seconds)]

    async def _public_search(self, query: str, limit: int) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(
                transport=self.transport,
                timeout=self.timeout_seconds,
                follow_redirects=True,
            ) as client:
                response = await self._request_with_retry(
                    client,
                    "GET",
                    self.public_search_url,
                    params={"q": query, "limit": str(limit)},
                    headers={"Accept": "application/json"},
                )
        except SkillsShCatalogError as exc:
            raise SkillsShCatalogError(f"skills.sh public search failed: {exc}") from exc
        if response.status_code >= 400:
            raise SkillsShCatalogError(f"skills.sh public search returned HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SkillsShCatalogError("skills.sh public search returned invalid JSON") from exc
        return payload if isinstance(payload, dict) else {}

    async def leaderboard(
        self,
        *,
        view: str = "trending",
        page: int = 0,
        per_page: int = 100,
    ) -> list[dict[str, Any]]:
        """Read one bounded page from the authenticated skills.sh catalog."""
        payload = await self.leaderboard_page(view=view, page=page, per_page=per_page)
        return payload["data"]

    async def leaderboard_page(
        self,
        *,
        view: str = "trending",
        page: int = 0,
        per_page: int = 100,
    ) -> dict[str, Any]:
        """Read a page and its pagination metadata from the authenticated catalog."""
        if not self.configured:
            raise SkillsShCatalogError("skills.sh sync requires an OIDC token")
        payload = await self._get(
            "skills",
            params={
                "view": str(view or "trending"),
                "page": str(max(0, int(page))),
                "per_page": str(max(1, min(int(per_page), 500))),
            },
        )
        data = payload.get("data")
        pagination = payload.get("pagination")
        return {
            "data": [dict(item) for item in data if isinstance(item, dict)] if isinstance(data, list) else [],
            "pagination": dict(pagination) if isinstance(pagination, dict) else {},
        }

    async def curated(self) -> list[dict[str, Any]]:
        """Flatten the official curated owners into the common listing shape."""
        if not self.configured:
            raise SkillsShCatalogError("skills.sh sync requires an OIDC token")
        payload = await self._get("skills/curated")
        owners = payload.get("data")
        rows: list[dict[str, Any]] = []
        if isinstance(owners, list):
            for owner in owners:
                if not isinstance(owner, dict) or not isinstance(owner.get("skills"), list):
                    continue
                rows.extend(dict(item) for item in owner["skills"] if isinstance(item, dict))
        return rows

    async def _public_page_metadata(self, skill_id: str) -> dict[str, Any]:
        values = await self._public_page_metadata_many([skill_id])
        return values[0] if values else {}

    @staticmethod
    def _parse_public_page_metadata(text: str) -> dict[str, Any]:
        matches = re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            text,
            flags=re.I | re.S,
        )
        for raw in matches:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            if isinstance(value, dict) and value.get("@type") == "SoftwareApplication":
                return {
                    "description": str(value.get("description") or "")[:MAX_PUBLIC_DESCRIPTION_CHARS],
                    "installs": (value.get("interactionStatistic") or {}).get("userInteractionCount"),
                }
        return {}

    async def _public_page_metadata_many(self, skill_ids: list[str]) -> list[dict[str, Any]]:
        if not self.public_page_base_url:
            return [{} for _ in skill_ids]
        pending: list[str] = []
        values: dict[str, dict[str, Any]] = {}
        for skill_id in dict.fromkeys(skill_ids):
            if not skill_id:
                continue
            cached = self._cached("page", skill_id)
            if cached is not None:
                values[skill_id] = dict(cached)
            else:
                pending.append(skill_id)
        if pending:
            try:
                async with httpx.AsyncClient(
                    transport=self.transport,
                    timeout=self.timeout_seconds,
                    follow_redirects=True,
                ) as client:
                    responses = await asyncio.gather(
                        *(
                            self._request_with_retry(
                                client,
                                "GET",
                                f"{self.public_page_base_url.rstrip('/')}/{quote(skill_id, safe='/')}",
                                headers={"Accept": "text/html"},
                            )
                            for skill_id in pending
                        ),
                        return_exceptions=True,
                    )
            except (httpx.HTTPError, OSError):
                responses = []
            for skill_id, response in zip(pending, responses):
                metadata = (
                    self._parse_public_page_metadata(response.text)
                    if isinstance(response, httpx.Response) and response.status_code < 400
                    else {}
                )
                values[skill_id] = dict(
                    self._put("page", skill_id, metadata, self.detail_ttl_seconds)
                ) if metadata else {}
        return [values.get(skill_id, {}) for skill_id in skill_ids]

    @staticmethod
    def _public_listing_row(item: dict[str, Any]) -> dict[str, Any]:
        skill_id = str(item.get("id") or "").strip()
        source = str(item.get("source") or "").strip()
        skill_name = str(item.get("name") or item.get("skillId") or skill_id.rsplit("/", 1)[-1])
        install_url = source if (source.startswith(("http://", "https://")) or "/" in source) else ""
        return {
            "id": skill_id,
            "slug": str(item.get("skillId") or skill_name),
            "name": skill_name,
            "source": source,
            "installs": item.get("installs"),
            "sourceType": "github" if "/" in source else "well-known",
            "installUrl": install_url,
            "url": f"https://skills.sh/{skill_id}" if skill_id else "",
            "description": str(item.get("description") or ""),
            "_public_search_only": True,
        }

    async def detail(self, skill_id: str) -> dict[str, Any] | None:
        skill_id = _stable_skill_id({"id": skill_id})
        if not skill_id:
            return None
        cached = self._cached("detail", skill_id)
        if cached is not None:
            return dict(cached)
        encoded = quote(skill_id, safe="/")
        try:
            payload = await self._get(f"skills/{encoded}")
        except SkillsShCatalogError:
            return None
        return dict(self._put("detail", skill_id, payload, self.detail_ttl_seconds))

    async def audit(self, skill_id: str) -> list[dict[str, Any]] | None:
        skill_id = _stable_skill_id({"id": skill_id})
        if not skill_id:
            return None
        cached = self._cached("audit", skill_id)
        if cached is not None:
            return [dict(item) for item in cached]
        encoded = quote(skill_id, safe="/")
        try:
            payload = await self._get(f"skills/audit/{encoded}")
        except SkillsShCatalogError:
            return None
        audits = payload.get("audits")
        rows = [dict(item) for item in audits if isinstance(item, dict)] if isinstance(audits, list) else []
        return [dict(item) for item in self._put("audit", skill_id, rows, self.audit_ttl_seconds)]

    async def _materialize(
        self,
        shortlist: list[dict[str, Any]],
        *,
        details: list[dict[str, Any] | None] | None = None,
        audits: list[list[dict[str, Any]] | None] | None = None,
        page_metadata: list[dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Turn catalog listings into the bounded internal retrieval record."""
        shortlist = shortlist[:MAX_DETAIL_CANDIDATES]
        detail_jobs = [
            self.detail(_stable_skill_id(item)) if not item.get("_public_search_only") else asyncio.sleep(0, result=None)
            for item in shortlist
        ]
        audit_jobs = [
            self.audit(_stable_skill_id(item)) if not item.get("_public_search_only") else asyncio.sleep(0, result=None)
            for item in shortlist
        ]
        if details is None:
            details = list(await asyncio.gather(*detail_jobs))
        if audits is None:
            audits = list(await asyncio.gather(*audit_jobs))
        if page_metadata is None:
            page_ids = [
                _stable_skill_id(item) if item.get("_public_search_only") and index < MAX_PUBLIC_PAGE_METADATA else ""
                for index, item in enumerate(shortlist)
            ]
            page_metadata = await self._public_page_metadata_many(page_ids)
        rows: list[dict[str, Any]] = []
        for rank, (listing_item, detail, partner_audits, page_meta) in enumerate(
            zip(shortlist, details, audits, page_metadata)
        ):
            detail = detail or {}
            files = detail.get("files") if isinstance(detail.get("files"), list) else []
            entrypoint, content = _entrypoint(files)
            # Preserve the complete upstream entrypoint. Retrieval uses the
            # compact normalized record below; delivery decides whether these
            # verified bytes are inline or isolated.
            fields = _frontmatter_fields(content)
            source_files: list[dict[str, Any]] = []
            for file in files:
                if not isinstance(file, dict):
                    continue
                path = str(file.get("path") or "").replace("\\", "/").strip("/")
                if not path or file.get("contents") is None:
                    continue
                body = str(file.get("contents"))
                source_files.append(
                    {
                        "path": path,
                        "contents": body,
                        "sha256": hashlib.sha256(body.encode("utf-8")).hexdigest(),
                        "bytes": len(body.encode("utf-8")),
                    }
                )
            detail_snapshot = str(detail.get("hash") or "") or None
            source_commit_sha = str(
                detail.get("commitSha")
                or detail.get("commit_sha")
                or detail.get("commit")
                or detail.get("sha")
                or ""
            ) or None
            license_spdx = str(
                detail.get("licenseSpdx")
                or detail.get("license_spdx")
                or detail.get("license")
                or ""
            ) or None
            entrypoint_record = next(
                (item for item in files if isinstance(item, dict) and str(item.get("path") or "").replace("\\", "/").strip("/") == entrypoint),
                {},
            )
            entrypoint_truncated = int(
                bool(
                    entrypoint_record.get("truncated")
                    or entrypoint_record.get("isTruncated")
                    or detail.get("truncated")
                    or detail.get("filesTruncated")
                )
            )
            references, unresolved_references = _reference_closure(files, entrypoint)
            skill_id = _stable_skill_id(listing_item) or _stable_skill_id(detail)
            name = str(listing_item.get("name") or fields.get("name") or detail.get("slug") or skill_id)
            description = str(
                listing_item.get("description")
                or fields.get("description")
                or page_meta.get("description")
                or ""
            )
            install_url = str(listing_item.get("installUrl") or "")
            page_url = str(listing_item.get("url") or f"https://skills.sh/{skill_id}")
            row: dict[str, Any] = {
                "id": skill_id,
                "name": name,
                "description": description,
                # Preserve the publisher/source from skills.sh. The registry
                # itself is recorded separately so provenance never collapses
                # every publisher into one synthetic source name.
                "source": str(listing_item.get("source") or detail.get("source") or "skills_sh"),
                "registry": "skills_sh",
                "slug": str(listing_item.get("slug") or detail.get("slug") or ""),
                "url": install_url or page_url,
                "skills_sh_url": page_url,
                "install_url": install_url,
                "skills_sh_id": skill_id,
                "source_snapshot_hash": detail_snapshot,
                "is_duplicate": bool(listing_item.get("isDuplicate")),
                "stars": 0,
                "installs": listing_item.get("installs") or page_meta.get("installs"),
                "source_type": listing_item.get("sourceType"),
                "rank": 1.0 / (60.0 + rank + 1.0),
                "similarity": None,
                "tags": [],
                "_content": content,
                "retrieval_text": _retrieval_text(name, description, content),
                "retrieval_text_hash": hashlib.sha256(_retrieval_text(name, description, content).encode()).hexdigest(),
                "source_commit_sha": source_commit_sha,
                "license_spdx": license_spdx,
                "source_file_count": len(source_files),
                "source_total_bytes": sum(int(item.get("bytes") or 0) for item in source_files),
                "package_completeness": "complete" if source_files and detail_snapshot else "unknown",
                "dependency_closure_status": (
                    "resolved" if source_files and not unresolved_references else
                    "captured_unresolved" if source_files else "unresolved"
                ),
                "entrypoint_truncated": entrypoint_truncated,
                "_source_files": source_files,
                "raw": {
                    "skills_sh_id": skill_id,
                    "skills_sh_url": page_url,
                    "install_url": install_url,
                    "installs": listing_item.get("installs"),
                    "source_type": listing_item.get("sourceType"),
                    "snapshot_hash": detail.get("hash"),
                    "entrypoint_path": entrypoint,
                    "references": references,
                    "unresolved_references": unresolved_references,
                    "file_manifest": [
                        {key: item[key] for key in ("path", "sha256", "bytes")}
                        for item in source_files
                    ],
                    "audits": partner_audits or [],
                },
            }
            status, risk_level, risk_score, risk_flags = _audit_summary(partner_audits)
            row["audit_status"] = status
            row["audit_risk_level"] = risk_level
            row["audit_count"] = len(partner_audits or [])
            row["risk_score"] = risk_score
            row["risk_flags"] = risk_flags
            if content:
                row["content_hash"] = content_hash(content)
            quality = evaluate_quality(row, content)
            if not content:
                # Public website search is a discovery-only lane. Keep the
                # record visible as a hint even when detail/audit endpoints
                # require OIDC, but never let missing bytes appear trusted.
                quality["content_hash"] = None
                quality["quality_status"] = "metadata_only"
                quality["quality_reasons"] = sorted(
                    set([*quality.get("quality_reasons", []), "detail-unavailable"])
                )
            row.update(quality)
            # An audit failure is a hard reject even if the static quality
            # scorer considers the markdown well-formed.
            if status == "fail":
                row["quality_status"] = "rejected"
                row["quality_reasons"] = sorted(set([*row.get("quality_reasons", []), "skills-sh-audit-fail"]))
            rows.append(row)
        return rows

    async def retrieve(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        """Search, hydrate, and audit a bounded skills.sh shortlist.

        Warm queries are served entirely from the persistent mirror. A cold
        query is single-flighted, so a burst of identical users creates one
        skills.sh search/detail/audit sequence rather than N sequences.
        """
        bounded_limit = min(max(1, int(limit)), MAX_DETAIL_CANDIDATES)
        mirrored = await self._mirror_search(query, bounded_limit)
        if mirrored:
            # A complete listing index is intentionally metadata-only. It is
            # useful as a hint while a cold authenticated query hydrates the
            # shortlist, but it must never suppress detail/audit retrieval.
            fresh = [
                row
                for row in mirrored
                if row.get("mirror_fresh") is not False
                and row.get("quality_status") == "active"
                and row.get("content_hash")
            ]
            if fresh:
                return fresh[:bounded_limit]
        key = f"{query.casefold()}::{bounded_limit}"

        async def refresh_retrieve() -> list[dict[str, Any]]:
            # Bypass the stale mirror while refreshing; the single-flight key
            # ensures only one replica-local refresh is in flight.
            search_limit = max(bounded_limit, 10)
            cache_key = f"{query.casefold()}::{search_limit}::{'auth' if self.configured else 'public'}"
            listing = await self._singleflight(
                ("search-refresh", cache_key),
                lambda: self._search_remote(query, search_limit, cache_key),
            )
            shortlist = listing[:bounded_limit]
            rows = await self._materialize(shortlist)
            await self._mirror_put(rows)
            return rows

        if mirrored:
            if self.configured:
                # Authenticated callers need authoritative detail/audit state
                # before ranking. A metadata-only hit can remain a fallback if
                # the refresh fails, but it must not race the refresh and
                # displace the hydrated primary candidate.
                try:
                    return await self._singleflight(("retrieve", key), refresh_retrieve)
                except SkillsShCatalogError:
                    return mirrored[:bounded_limit]
            # Serve a bounded, explicitly hint-only stale result while a
            # background refresh repairs the mirror. This keeps the user path
            # available during a brief upstream outage without treating old
            # audit state as current safety evidence.
            refresh = asyncio.create_task(self._singleflight(("retrieve", key), refresh_retrieve))
            refresh.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
            return mirrored[:bounded_limit]
        return await self._singleflight(("retrieve", key), refresh_retrieve)

    async def retrieve_ids(self, skill_ids: list[str], limit: int = 10) -> list[dict[str, Any]]:
        """Rehydrate previously offered skills without consulting local storage.

        Follow-up requests may carry a stable skills.sh ID instead of a fresh
        natural-language query.  Authenticated callers get the authoritative
        detail/audit records.  Tokenless callers can only reuse a short-lived
        listing cache populated by public search, and therefore remain
        metadata-only hints.
        """
        ids = list(dict.fromkeys(str(value or "").strip() for value in skill_ids if str(value or "").strip()))
        ids = ids[: min(max(1, int(limit)), MAX_DETAIL_CANDIDATES)]
        if not ids:
            return []
        mirrored = await self._mirror_ids(ids)
        if len(mirrored) == len(ids):
            return mirrored
        listings: list[dict[str, Any]] = []
        for skill_id in ids:
            listing = self._cached("listing", skill_id)
            if listing is not None:
                listings.append(dict(listing))
            else:
                listings.append({
                    "id": skill_id,
                    "name": skill_id.rsplit("/", 1)[-1],
                    "source": "/".join(skill_id.split("/")[:-1]),
                    "url": f"https://skills.sh/{skill_id}",
                    "_public_search_only": not self.configured,
                })
        if not self.configured:
            # A public page is not an authoritative detail/audit source.  Do
            # not turn an arbitrary user-provided ID into a trusted row.
            listings = [item for item in listings if self._cached("listing", _stable_skill_id(item)) is not None]
            if not listings:
                return []
        else:
            for item in listings:
                item.pop("_public_search_only", None)
        rows = await self._materialize(listings)
        await self._mirror_put(rows)
        return rows


_DEFAULT_CATALOG: SkillsShCatalog | None = None


def default_catalog() -> SkillsShCatalog:
    global _DEFAULT_CATALOG
    if _DEFAULT_CATALOG is None:
        _DEFAULT_CATALOG = SkillsShCatalog()
    return _DEFAULT_CATALOG
