"""SQLite-backed replacement for the Supabase skills DB, used once the
scraper stops writing to Supabase (running out of free-tier space). Exposes
just enough of the PostgREST REST + RPC surface that scraper.py and
recommender.py already speak, so those files only need a base-URL swap.

Schema mirrors the Supabase `skills` / `scrape_runs` tables closely enough
that skill_to_row() output drops in unchanged. Embeddings are stored as
packed float32 BLOBs; vector search is brute-force numpy (fine at the scale
a single scraper accumulates going forward).
"""
import ipaddress
import json
import heapq
import os
import re
import sqlite3
import struct
import threading
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np

import quality

DB_PATH = Path(os.getenv("LOCAL_DB_PATH", str(Path(__file__).parent / "local_skills.db")))

_SCHEMA = """
CREATE TABLE IF NOT EXISTS skills (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    description TEXT,
    source TEXT NOT NULL,
    url TEXT UNIQUE,
    tags TEXT DEFAULT '[]',
    raw TEXT DEFAULT '{}',
    discovered_at TEXT,
    risk_score INTEGER DEFAULT 0,
    risk_flags TEXT DEFAULT '[]',
    scanned_at TEXT,
    content_hash TEXT,
    canonical_id TEXT,
    quality_status TEXT DEFAULT 'pending',
    readiness TEXT DEFAULT 'catalog-ready',
    quality_reasons TEXT DEFAULT '[]',
    quality_score INTEGER DEFAULT 0,
    prominence_score REAL DEFAULT 0,
    provenance_score REAL DEFAULT 0.25,
    meaningfulness_score REAL DEFAULT 0,
    platforms TEXT DEFAULT '[]',
    category TEXT,
    embedding BLOB,
    embedding_text_hash TEXT,
    embedded_at TEXT,
    feedback_score REAL,
    capability_summary TEXT,
    triggers TEXT DEFAULT '[]',
    retrieval_text TEXT,
    retrieval_text_hash TEXT,
    retrieval_record_hash TEXT,
    package_hash TEXT,
    source_commit_sha TEXT,
    license_spdx TEXT,
    package_completeness TEXT,
    dependency_closure_status TEXT,
    entrypoint_truncated INTEGER DEFAULT 0
);

CREATE VIRTUAL TABLE IF NOT EXISTS skills_fts USING fts5(
    name, description, tags, capability_summary, triggers, retrieval_text,
    content='skills', content_rowid='rowid', tokenize='porter unicode61'
);

CREATE TRIGGER IF NOT EXISTS skills_ai AFTER INSERT ON skills BEGIN
    INSERT INTO skills_fts(rowid, name, description, tags, capability_summary, triggers, retrieval_text)
    VALUES (new.rowid, new.name, new.description, new.tags, new.capability_summary, new.triggers, new.retrieval_text);
END;

CREATE TRIGGER IF NOT EXISTS skills_ad AFTER DELETE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, tags, capability_summary, triggers, retrieval_text)
    VALUES ('delete', old.rowid, old.name, old.description, old.tags, old.capability_summary, old.triggers, old.retrieval_text);
END;

CREATE TRIGGER IF NOT EXISTS skills_au AFTER UPDATE ON skills BEGIN
    INSERT INTO skills_fts(skills_fts, rowid, name, description, tags, capability_summary, triggers, retrieval_text)
    VALUES ('delete', old.rowid, old.name, old.description, old.tags, old.capability_summary, old.triggers, old.retrieval_text);
    INSERT INTO skills_fts(rowid, name, description, tags, capability_summary, triggers, retrieval_text)
    VALUES (new.rowid, new.name, new.description, new.tags, new.capability_summary, new.triggers, new.retrieval_text);
END;

-- These are deliberately partial indexes: readiness and semantic retrieval
-- need small metadata indexes, not a second copy of every embedding BLOB.
CREATE INDEX IF NOT EXISTS skills_active_idx ON skills(id) WHERE quality_status = 'active';
CREATE INDEX IF NOT EXISTS skills_embedded_idx ON skills(id) WHERE embedding IS NOT NULL;
DROP INDEX IF EXISTS skills_active_embedded_idx;
CREATE INDEX IF NOT EXISTS skills_route_embedded_idx ON skills(id)
    WHERE quality_status IN ('active', 'metadata_only') AND embedding IS NOT NULL;
CREATE TABLE IF NOT EXISTS skill_packages (
    package_hash TEXT PRIMARY KEY,
    source_url TEXT,
    source_provider TEXT,
    source_commit_sha TEXT,
    root_path TEXT,
    entrypoint_path TEXT,
    tree_sha TEXT,
    license_spdx TEXT,
    completeness_status TEXT NOT NULL,
    dependency_closure_status TEXT NOT NULL,
    entrypoint_truncated INTEGER DEFAULT 0,
    manifest_json TEXT NOT NULL,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS skill_package_files (
    package_hash TEXT NOT NULL,
    path TEXT NOT NULL,
    raw_sha256 TEXT NOT NULL,
    git_blob_sha TEXT,
    size INTEGER NOT NULL,
    role TEXT NOT NULL,
    media_type TEXT,
    text_indexable INTEGER DEFAULT 0,
    in_dependency_closure INTEGER DEFAULT 0,
    PRIMARY KEY (package_hash, path),
    FOREIGN KEY (package_hash) REFERENCES skill_packages(package_hash)
);
CREATE INDEX IF NOT EXISTS skill_package_files_hash_idx ON skill_package_files(raw_sha256);

CREATE TABLE IF NOT EXISTS skill_package_sources (
    package_hash TEXT NOT NULL,
    source_url TEXT NOT NULL,
    source_commit_sha TEXT,
    provenance_json TEXT NOT NULL DEFAULT '{}',
    observed_at TEXT NOT NULL,
    PRIMARY KEY (package_hash, source_url, source_commit_sha),
    FOREIGN KEY (package_hash) REFERENCES skill_packages(package_hash)
);

CREATE TABLE IF NOT EXISTS skill_retrieval_records (
    record_hash TEXT PRIMARY KEY,
    skill_id TEXT,
    package_hash TEXT,
    record_version TEXT NOT NULL,
    text TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'candidate',
    dependency_closure_status TEXT,
    source_commit_sha TEXT,
    entrypoint_path TEXT,
    active INTEGER DEFAULT 1,
    created_at TEXT NOT NULL,
    FOREIGN KEY (package_hash) REFERENCES skill_packages(package_hash)
);
CREATE INDEX IF NOT EXISTS skill_retrieval_records_skill_idx ON skill_retrieval_records(skill_id, active);

-- One row per MCP tool, embedded separately from the parent skill. A
-- multi-tool server's single blended skill-level embedding dilutes a query
-- that matches one specific tool among many; matching per-tool and rolling
-- up to the parent skill (see vector_search_tools) fixes that without
-- touching the skill-level embedding path at all.
-- Keyed by the skill's URL, not its id: upsert_rows() regenerates a fresh
-- uuid4 for "id" on every re-upsert of an already-known url (scraper.py
-- never sends "id" back, and the generic ON CONFLICT UPDATE clause includes
-- id=excluded.id) -- a skill's id is NOT stable across scrape runs today.
-- url is the one identifier that actually is stable throughout this
-- pipeline (it's the upsert conflict key), so that's what this FKs to.
CREATE TABLE IF NOT EXISTS skill_tools (
    id TEXT PRIMARY KEY,
    skill_url TEXT NOT NULL,
    tool_name TEXT NOT NULL,
    tool_description TEXT,
    embedding BLOB,
    embedding_text_hash TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS skill_tools_url_idx ON skill_tools(skill_url);
CREATE INDEX IF NOT EXISTS skill_tools_embedded_idx ON skill_tools(id) WHERE embedding IS NOT NULL;

CREATE TABLE IF NOT EXISTS scrape_runs (
    id TEXT PRIMARY KEY,
    started_at TEXT,
    finished_at TEXT,
    status TEXT DEFAULT 'running',
    skills_found INTEGER DEFAULT 0,
    error TEXT,
    new_skills_found INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS route_events (
    id TEXT PRIMARY KEY,
    created_at TEXT,
    client TEXT,
    client_version TEXT,
    query_chars INTEGER,
    tier TEXT,
    skill_id TEXT,
    skill_name TEXT,
    skill_url TEXT,
    latency_ms INTEGER,
    skill_find_ms INTEGER,
    retrieval_ms INTEGER,
    rerank_ms INTEGER,
    content_ms INTEGER,
    result_count INTEGER,
    input_tokens INTEGER,
    hint_tokens INTEGER,
    candidate_tokens INTEGER,
    content_tokens INTEGER,
    capsule_tokens INTEGER,
    injected_tokens INTEGER,
    response_tokens INTEGER,
    guard_delivery TEXT,
    capsule_chars INTEGER,
    meaningfulness_score REAL,
    config_version TEXT,
    outcome TEXT,
    outcome_at TEXT,
    feedback_source TEXT,
    anonymous_id_hash TEXT,
    warnings TEXT DEFAULT '[]'
);

CREATE INDEX IF NOT EXISTS route_events_created_at_idx ON route_events(created_at);
CREATE INDEX IF NOT EXISTS route_events_tier_idx ON route_events(tier);

CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE NOT NULL,
    name TEXT,
    avatar_url TEXT,
    created_at TEXT,
    plan TEXT DEFAULT 'free'
);

CREATE TABLE IF NOT EXISTS complimentary_entitlements (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    reason TEXT NOT NULL,
    granted_by_user_id TEXT NOT NULL,
    granted_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    revoked_at TEXT,
    revoked_by_user_id TEXT,
    revoke_reason TEXT
);
CREATE INDEX IF NOT EXISTS complimentary_entitlements_user_idx
    ON complimentary_entitlements(user_id, expires_at);

CREATE TABLE IF NOT EXISTS admin_audit_log (
    id TEXT PRIMARY KEY,
    actor_user_id TEXT NOT NULL,
    actor_email TEXT NOT NULL,
    target_user_id TEXT,
    target_email TEXT,
    action TEXT NOT NULL,
    old_value TEXT,
    new_value TEXT,
    reason TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS admin_audit_log_created_idx ON admin_audit_log(created_at);
CREATE TRIGGER IF NOT EXISTS admin_audit_log_no_update
BEFORE UPDATE ON admin_audit_log BEGIN
    SELECT RAISE(ABORT, 'admin audit log is append-only');
END;
CREATE TRIGGER IF NOT EXISTS admin_audit_log_no_delete
BEFORE DELETE ON admin_audit_log BEGIN
    SELECT RAISE(ABORT, 'admin audit log is append-only');
END;

CREATE TABLE IF NOT EXISTS stripe_webhook_events (
    event_id TEXT PRIMARY KEY,
    event_type TEXT NOT NULL,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stripe_subscriptions (
    subscription_id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    customer_id TEXT NOT NULL,
    plan TEXT NOT NULL,
    status TEXT NOT NULL,
    org_id TEXT,
    seat_limit INTEGER,
    synced_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS stripe_subscriptions_user_idx ON stripe_subscriptions(user_id, status);

CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS route_usage (
    user_id TEXT NOT NULL,
    month TEXT NOT NULL,
    count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, month)
);

CREATE TABLE IF NOT EXISTS oauth_identities (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    provider TEXT NOT NULL,
    provider_user_id TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(provider, provider_user_id)
);
CREATE INDEX IF NOT EXISTS oauth_identities_user_idx ON oauth_identities(user_id);

CREATE TABLE IF NOT EXISTS cli_tokens (
    id TEXT PRIMARY KEY,
    token_hash TEXT UNIQUE NOT NULL,
    user_id TEXT NOT NULL,
    created_at TEXT,
    last_used_at TEXT,
    revoked_at TEXT
);
CREATE INDEX IF NOT EXISTS cli_tokens_user_idx ON cli_tokens(user_id);

CREATE TABLE IF NOT EXISTS favorites (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(user_id, skill_id)
);
CREATE INDEX IF NOT EXISTS favorites_user_idx ON favorites(user_id);

CREATE TABLE IF NOT EXISTS installs (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    skill_id TEXT,
    skill_url TEXT,
    target TEXT,
    installed_at TEXT
);
CREATE INDEX IF NOT EXISTS installs_user_idx ON installs(user_id);

CREATE TABLE IF NOT EXISTS private_skills (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    name TEXT NOT NULL,
    description TEXT,
    content TEXT NOT NULL,
    created_at TEXT,
    org_id TEXT
);
CREATE INDEX IF NOT EXISTS private_skills_owner_idx ON private_skills(owner_user_id);

CREATE TABLE IF NOT EXISTS orgs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    owner_user_id TEXT NOT NULL,
    created_at TEXT,
    seat_limit INTEGER
);

CREATE TABLE IF NOT EXISTS org_members (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL DEFAULT 'member',
    created_at TEXT,
    UNIQUE(org_id, user_id)
);
CREATE INDEX IF NOT EXISTS org_members_user_idx ON org_members(user_id);

CREATE TABLE IF NOT EXISTS skill_versions (
    id TEXT PRIMARY KEY,
    skill_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    seen_at TEXT,
    UNIQUE(skill_id, content_hash)
);
CREATE INDEX IF NOT EXISTS skill_versions_skill_idx ON skill_versions(skill_id);

CREATE TABLE IF NOT EXISTS skill_pins (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(user_id, skill_id)
);
CREATE INDEX IF NOT EXISTS skill_pins_user_idx ON skill_pins(user_id);

CREATE TABLE IF NOT EXISTS skill_watches (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    last_seen_hash TEXT,
    created_at TEXT,
    UNIQUE(user_id, skill_id)
);
CREATE INDEX IF NOT EXISTS skill_watches_user_idx ON skill_watches(user_id);

CREATE TABLE IF NOT EXISTS collections (
    id TEXT PRIMARY KEY,
    owner_user_id TEXT NOT NULL,
    org_id TEXT,
    name TEXT NOT NULL,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS collections_owner_idx ON collections(owner_user_id);
CREATE INDEX IF NOT EXISTS collections_org_idx ON collections(org_id);

CREATE TABLE IF NOT EXISTS collection_skills (
    id TEXT PRIMARY KEY,
    collection_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(collection_id, skill_id)
);

CREATE TABLE IF NOT EXISTS routing_preferences (
    user_id TEXT PRIMARY KEY,
    excluded_skill_ids TEXT DEFAULT '[]',
    excluded_sources TEXT DEFAULT '[]',
    updated_at TEXT
);

CREATE TABLE IF NOT EXISTS org_skill_policies (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    skill_id TEXT NOT NULL,
    policy TEXT NOT NULL,
    created_at TEXT,
    UNIQUE(org_id, skill_id)
);
CREATE INDEX IF NOT EXISTS org_skill_policies_org_idx ON org_skill_policies(org_id);

CREATE TABLE IF NOT EXISTS org_audit_log (
    id TEXT PRIMARY KEY,
    org_id TEXT NOT NULL,
    actor_user_id TEXT,
    action TEXT NOT NULL,
    subject TEXT,
    created_at TEXT
);
CREATE INDEX IF NOT EXISTS org_audit_log_org_idx ON org_audit_log(org_id);

CREATE TABLE IF NOT EXISTS oauth_clients (
    client_id TEXT PRIMARY KEY,
    client_info TEXT NOT NULL,
    created_at TEXT
);

CREATE TABLE IF NOT EXISTS mcp_auth_codes (
    code TEXT PRIMARY KEY,
    client_id TEXT NOT NULL,
    code_challenge TEXT NOT NULL,
    redirect_uri TEXT NOT NULL,
    scopes TEXT DEFAULT '[]',
    user_id TEXT NOT NULL,
    created_at TEXT,
    expires_at TEXT,
    consumed_at TEXT
);
"""

TABLES = {
    "skills": {"unique": "url", "json_cols": {"tags", "raw", "risk_flags", "quality_reasons", "platforms", "triggers"}},
    "scrape_runs": {"unique": None, "json_cols": set()},
    "route_events": {"unique": None, "json_cols": {"warnings"}},
    "users": {"unique": "email", "json_cols": set()},
    "oauth_identities": {"unique": None, "json_cols": set()},
    "cli_tokens": {"unique": "token_hash", "json_cols": set()},
    "route_usage": {"unique": None, "json_cols": set()},
    "favorites": {"unique": None, "json_cols": set()},
    "installs": {"unique": None, "json_cols": set()},
    "private_skills": {"unique": None, "json_cols": set()},
    "orgs": {"unique": None, "json_cols": set()},
    "org_members": {"unique": None, "json_cols": set()},
    "oauth_clients": {"unique": None, "json_cols": {"client_info"}},
    "mcp_auth_codes": {"unique": None, "json_cols": {"scopes"}},
    "skill_tools": {"unique": None, "json_cols": set()},
}

SKILL_COLUMN_DEFAULTS = {
    "content_hash": "TEXT",
    "canonical_id": "TEXT",
    "quality_status": "TEXT DEFAULT 'pending'",
    "readiness": "TEXT DEFAULT 'catalog-ready'",
    "quality_reasons": "TEXT DEFAULT '[]'",
    "quality_score": "INTEGER DEFAULT 0",
    "prominence_score": "REAL DEFAULT 0",
    "provenance_score": "REAL DEFAULT 0.25",
    "meaningfulness_score": "REAL DEFAULT 0",
    "platforms": "TEXT DEFAULT '[]'",
    "category": "TEXT",
    "feedback_score": "REAL",
    "capability_summary": "TEXT",
    "triggers": "TEXT DEFAULT '[]'",
    "tools_hash": "TEXT",
    "retrieval_text": "TEXT",
    "retrieval_text_hash": "TEXT",
    "retrieval_record_hash": "TEXT",
    "package_hash": "TEXT",
    "source_commit_sha": "TEXT",
    "license_spdx": "TEXT",
    "package_completeness": "TEXT",
    "dependency_closure_status": "TEXT",
    "entrypoint_truncated": "INTEGER DEFAULT 0",
}

# Router retrieval needs metadata for quality/ranking and raw publisher
# metadata, but never the packed embedding BLOB. Keep this projection shared
# across lexical and vector result fetches so those BLOBs stay in the matrix
# cache instead of being copied into every candidate row.
SKILL_RETRIEVAL_COLUMNS = (
    "id",
    "name",
    "description",
    "source",
    "url",
    "tags",
    "raw",
    "discovered_at",
    "risk_score",
    "risk_flags",
    "scanned_at",
    "content_hash",
    "canonical_id",
    "quality_status",
    "readiness",
    "quality_reasons",
    "quality_score",
    "prominence_score",
    "provenance_score",
    "meaningfulness_score",
    "platforms",
    "category",
    "embedding_text_hash",
    "embedded_at",
    "feedback_score",
    "capability_summary",
    "triggers",
    "retrieval_text",
    "retrieval_text_hash",
    "retrieval_record_hash",
    "package_hash",
    "source_commit_sha",
    "license_spdx",
    "package_completeness",
    "dependency_closure_status",
    "entrypoint_truncated",
)
SKILL_RETRIEVAL_SQL = ", ".join(SKILL_RETRIEVAL_COLUMNS)

ROUTE_EVENT_COLUMN_DEFAULTS = {
    "outcome": "TEXT",
    "outcome_at": "TEXT",
    "feedback_source": "TEXT",
    "skill_find_ms": "INTEGER",
    "rerank_ms": "INTEGER",
    "candidate_tokens": "INTEGER",
    "capsule_tokens": "INTEGER",
    "injected_tokens": "INTEGER",
    "guard_delivery": "TEXT",
    "capsule_chars": "INTEGER",
    "meaningfulness_score": "REAL",
    "anonymous_id_hash": "TEXT",
    "user_id": "TEXT",
    "skip_reason": "TEXT",
    "ip_address": "TEXT",
}

# Route analytics are deliberately metadata-only. Keep this allowlist at the
# storage boundary rather than trusting every HTTP/MCP caller to remember not
# to pass a raw task. Legacy callers may still include prompt_text/query_hash/
# feedback_note; those keys are silently discarded before SQL is constructed.
ROUTE_EVENT_WRITE_COLUMNS = frozenset(
    {
        "id",
        "created_at",
        "client",
        "client_version",
        "query_chars",
        "tier",
        "skill_id",
        "skill_name",
        "skill_url",
        "latency_ms",
        "skill_find_ms",
        "retrieval_ms",
        "rerank_ms",
        "content_ms",
        "result_count",
        "input_tokens",
        "hint_tokens",
        "candidate_tokens",
        "content_tokens",
        "capsule_tokens",
        "injected_tokens",
        "response_tokens",
        "guard_delivery",
        "capsule_chars",
        "meaningfulness_score",
        "anonymous_id_hash",
        "config_version",
        "outcome",
        "outcome_at",
        "feedback_source",
        "warnings",
        "user_id",
        "skip_reason",
        "ip_address",
    }
)

ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS = ("prompt_text", "query_hash", "feedback_note")
ROUTE_EVENT_OUTCOMES = frozenset(
    {"used", "skipped", "installed", "failed", "dismissed", "shown", "injected"}
)
ROUTE_EVENT_TIERS = frozenset({"full", "hint", "none", "skipped"})
ROUTE_GUARD_DELIVERIES = frozenset({"full", "capsule", "isolation", "hint", "none"})
SAFE_ROUTE_SKIP_REASONS = frozenset(
    {
        "empty prompt",
        "command prompt",
        "too short",
        "too long",
        "too long; likely pasted context",
        "acknowledgement",
        "acknowledgement or continuation",
        "meta prompt",
        "meta or status prompt",
    }
)
_SAFE_ROUTE_IDENTIFIER_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/+@-]*\Z")
_ANONYMOUS_HASH_RE = re.compile(r"^[a-f0-9]{64}$")

CLI_TOKEN_TTL_SECONDS = 30 * 24 * 60 * 60  # 30 days, sliding forward on each use
ANONYMOUS_ID_RETENTION_DAYS = max(1, int(os.getenv("AUTOSKILL_ANONYMOUS_ID_RETENTION_DAYS", "90")))

CLI_TOKEN_COLUMN_DEFAULTS = {
    "expires_at": "TEXT",
}

USER_COLUMN_DEFAULTS = {
    "plan": "TEXT DEFAULT 'free'",
    # Stripe is the source of truth for subscription state; this is the only
    # billing identifier we persist (never card or invoice data).
    "stripe_customer_id": "TEXT",
    "stripe_subscription_id": "TEXT",
    "stripe_subscription_status": "TEXT",
    "stripe_plan_updated_at": "TEXT",
}

# NULL org_id = personal submission; a real org_id shares the skill with
# every member of that org. NULL status = approved (personal skills and
# owner-published org skills); 'pending' = a member submission awaiting the
# org owner's approval, invisible to routing until approved.
PRIVATE_SKILL_COLUMN_DEFAULTS = {
    "org_id": "TEXT",
    "status": "TEXT",
}

ORG_SKILL_POLICIES = ("allow", "block")

ORG_AUDIT_ACTIONS = frozenset(
    {
        "member_added",
        "member_removed",
        "org_skill_added",
        "org_skill_submitted",
        "org_skill_approved",
        "org_skill_removed",
        "policy_set",
        "policy_removed",
        "seats_changed",
        "skill_installed",
    }
)

# NULL seat_limit = the plan's included member count (TEAM_INCLUDED_MEMBERS);
# a real value is mirrored from Stripe's paid extra-seat quantity.
ORG_COLUMN_DEFAULTS = {
    "seat_limit": "INTEGER",
}

ORG_ROLES = ("owner", "member")

USER_PLANS = ("free", "pro", "team")
COMPLIMENTARY_PLANS = ("pro", "team")
_PLAN_RANK = {"free": 0, "pro": 1, "team": 2}

# 0 disables metering entirely (self-hosted deployments).
FREE_ROUTES_PER_MONTH = int(os.getenv("AUTOSKILL_FREE_ROUTES_PER_MONTH", "100"))

# Pro is marketed as unlimited fair-use routing; this is the internal abuse
# cap behind that promise, never shown on the pricing page. 0 disables.
PRO_ROUTES_PER_MONTH = int(os.getenv("AUTOSKILL_PRO_ROUTES_PER_MONTH", "15000"))

# Free plan includes up to this many personal private skills; pro/team are
# unlimited. 0 disables the cap.
FREE_PRIVATE_SKILLS = int(os.getenv("AUTOSKILL_FREE_PRIVATE_SKILLS", "10"))

# Team workspaces include this many members; extra seats flow through Stripe.
TEAM_INCLUDED_MEMBERS = int(os.getenv("AUTOSKILL_TEAM_INCLUDED_MEMBERS", "5"))


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=30)  # ride out concurrent write bursts (migration, embed loop)
    conn.row_factory = sqlite3.Row
    return conn


def _stale_duplicate_running_scrapes(conn: sqlite3.Connection) -> None:
    rows = conn.execute(
        "SELECT id FROM scrape_runs WHERE status='running' "
        "ORDER BY COALESCE(started_at, '') DESC, id DESC"
    ).fetchall()
    if len(rows) <= 1:
        return
    now = _now()
    for row in rows[1:]:
        conn.execute(
            "UPDATE scrape_runs SET status=?, finished_at=?, error=? WHERE id=?",
            (
                "stale",
                now,
                "Marked stale by init_db before creating the single-running scrape guard.",
                row["id"],
            ),
        )


def _route_event_columns(conn: sqlite3.Connection) -> set[str]:
    return {
        str(row["name"] if isinstance(row, sqlite3.Row) else row[1])
        for row in conn.execute("PRAGMA table_info(route_events)").fetchall()
    }


def _safe_route_identifier(value: object, max_length: int = 80) -> str:
    """Return a compact identifier or empty string, never arbitrary prose."""
    text = str(value or "").strip()[:max_length]
    return text if _SAFE_ROUTE_IDENTIFIER_RE.fullmatch(text) else ""


def _safe_route_ip(value: object) -> str | None:
    """Return a normalized IPv4/IPv6 address or None -- proxy headers are
    caller-controlled, so anything that doesn't parse as an address is
    dropped rather than stored as free text."""
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return str(ipaddress.ip_address(text))
    except ValueError:
        return None


def _scrub_route_event_privacy(conn: sqlite3.Connection) -> None:
    """Logically clear legacy free-text route data on an existing connection.

    ``secure_delete`` is enabled before the UPDATE so SQLite overwrites deleted
    cell content instead of merely releasing it for reuse. The standalone scrub
    utility additionally checkpoints and vacuums with the services stopped.
    """
    conn.execute("PRAGMA secure_delete=ON")
    columns = _route_event_columns(conn)
    assignments = [
        f"{column}=NULL" for column in ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS if column in columns
    ]
    if assignments:
        where = " OR ".join(
            f"{column} IS NOT NULL"
            for column in ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS
            if column in columns
        )
        conn.execute(f"UPDATE route_events SET {', '.join(assignments)} WHERE {where}")
    if "skip_reason" in columns:
        placeholders = ",".join("?" for _ in SAFE_ROUTE_SKIP_REASONS)
        conn.execute(
            f"UPDATE route_events SET skip_reason=NULL "
            f"WHERE skip_reason IS NOT NULL AND skip_reason NOT IN ({placeholders})",
            tuple(sorted(SAFE_ROUTE_SKIP_REASONS)),
        )
    if "warnings" in columns:
        # Warning prose is operationally useful in the HTTP response but is
        # not needed in retained analytics. Reset legacy rows and keep the
        # storage boundary free of arbitrary caller-controlled text.
        conn.execute("UPDATE route_events SET warnings='[]' WHERE warnings IS NOT NULL")


def route_event_privacy_status(conn: sqlite3.Connection | None = None) -> dict:
    """Count legacy privacy violations without loading or returning their text."""
    owned = conn is None
    if conn is None:
        conn = get_conn()
    try:
        columns = _route_event_columns(conn)
        forbidden_counts = {}
        for column in ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS:
            forbidden_counts[column] = (
                int(conn.execute(f"SELECT COUNT(*) FROM route_events WHERE {column} IS NOT NULL").fetchone()[0])
                if column in columns
                else 0
            )
        unsafe_skip_reason = 0
        if "skip_reason" in columns:
            placeholders = ",".join("?" for _ in SAFE_ROUTE_SKIP_REASONS)
            unsafe_skip_reason = int(
                conn.execute(
                    f"SELECT COUNT(*) FROM route_events "
                    f"WHERE skip_reason IS NOT NULL AND skip_reason NOT IN ({placeholders})",
                    tuple(sorted(SAFE_ROUTE_SKIP_REASONS)),
                ).fetchone()[0]
            )
        violations = sum(forbidden_counts.values()) + unsafe_skip_reason
        return {
            "ok": violations == 0,
            "violations": violations,
            "forbidden_non_null": forbidden_counts,
            "unsafe_skip_reason": unsafe_skip_reason,
        }
    finally:
        if owned:
            conn.close()


def _migrate_legacy_manual_plans(conn: sqlite3.Connection) -> None:
    """Preserve pre-separation manual plans as reviewable 30-day grants."""
    migration = "2026-07-separate-complimentary-plans-v1"
    if conn.execute("SELECT 1 FROM schema_migrations WHERE name=?", (migration,)).fetchone():
        return
    now = _now()
    expires = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()
    rows = conn.execute(
        "SELECT id, email, plan FROM users WHERE plan IN ('pro', 'team') AND stripe_customer_id IS NULL"
    ).fetchall()
    for row in rows:
        entitlement_id = str(uuid.uuid4())
        reason = "Legacy manual plan migrated for founder review"
        conn.execute(
            "INSERT INTO complimentary_entitlements "
            "(id, user_id, plan, reason, granted_by_user_id, granted_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entitlement_id, row["id"], row["plan"], reason, "system-migration", now, expires),
        )
        conn.execute(
            "INSERT INTO admin_audit_log "
            "(id, actor_user_id, actor_email, target_user_id, target_email, action, old_value, new_value, reason, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), "system-migration", "system@local", row["id"], row["email"],
                "legacy_plan_migrated", json.dumps({"plan": row["plan"]}, sort_keys=True),
                json.dumps({"entitlement_id": entitlement_id, "plan": row["plan"], "expires_at": expires}, sort_keys=True),
                reason, now,
            ),
        )
        conn.execute("UPDATE users SET plan='free' WHERE id=?", (row["id"],))
    conn.execute("INSERT INTO schema_migrations (name, applied_at) VALUES (?, ?)", (migration, now))


def init_db() -> None:
    conn = get_conn()
    try:
        # journal_mode is persistent per database. Setting it at startup keeps
        # route connections from taking the SQLite mode-change path repeatedly.
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(_SCHEMA)
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(skills)").fetchall()}
        for col, spec in SKILL_COLUMN_DEFAULTS.items():
            if col not in existing:
                conn.execute(f"ALTER TABLE skills ADD COLUMN {col} {spec}")
        conn.execute("CREATE INDEX IF NOT EXISTS skills_package_hash_idx ON skills(package_hash)")
        # Existing databases may still have a narrow FTS table. Rebuild it
        # once so capability summaries, author triggers, and the compact
        # retrieval record participate in lexical discovery.
        fts_row = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='skills_fts'"
        ).fetchone()
        fts_sql = (fts_row["sql"] or "") if fts_row else ""
        if any(column not in fts_sql for column in ("capability_summary", "triggers", "retrieval_text")):
            conn.execute("DROP TRIGGER IF EXISTS skills_ai")
            conn.execute("DROP TRIGGER IF EXISTS skills_ad")
            conn.execute("DROP TRIGGER IF EXISTS skills_au")
            conn.execute("DROP TABLE IF EXISTS skills_fts")
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO skills_fts(rowid, name, description, tags, capability_summary, triggers, retrieval_text) "
                "SELECT rowid, name, description, tags, capability_summary, triggers, retrieval_text FROM skills"
            )
        route_existing = {row["name"] for row in conn.execute("PRAGMA table_info(route_events)").fetchall()}
        for col, spec in ROUTE_EVENT_COLUMN_DEFAULTS.items():
            if col not in route_existing:
                conn.execute(f"ALTER TABLE route_events ADD COLUMN {col} {spec}")
        conn.execute("CREATE INDEX IF NOT EXISTS route_events_anonymous_id_idx ON route_events(anonymous_id_hash)")
        _scrub_route_event_privacy(conn)
        cli_token_existing = {row["name"] for row in conn.execute("PRAGMA table_info(cli_tokens)").fetchall()}
        for col, spec in CLI_TOKEN_COLUMN_DEFAULTS.items():
            if col not in cli_token_existing:
                conn.execute(f"ALTER TABLE cli_tokens ADD COLUMN {col} {spec}")
        user_existing = {row["name"] for row in conn.execute("PRAGMA table_info(users)").fetchall()}
        for col, spec in USER_COLUMN_DEFAULTS.items():
            if col not in user_existing:
                conn.execute(f"ALTER TABLE users ADD COLUMN {col} {spec}")
        _migrate_legacy_manual_plans(conn)
        private_skill_existing = {row["name"] for row in conn.execute("PRAGMA table_info(private_skills)").fetchall()}
        for col, spec in PRIVATE_SKILL_COLUMN_DEFAULTS.items():
            if col not in private_skill_existing:
                conn.execute(f"ALTER TABLE private_skills ADD COLUMN {col} {spec}")
        org_existing = {row["name"] for row in conn.execute("PRAGMA table_info(orgs)").fetchall()}
        for col, spec in ORG_COLUMN_DEFAULTS.items():
            if col not in org_existing:
                conn.execute(f"ALTER TABLE orgs ADD COLUMN {col} {spec}")
        conn.execute("CREATE INDEX IF NOT EXISTS private_skills_org_idx ON private_skills(org_id)")
        # A missing status must never become silently routable because an old
        # SQLite table still has the historical DEFAULT 'active'.
        conn.execute("UPDATE skills SET quality_status='pending' WHERE quality_status IS NULL")
        # Backfill the explicit delivery state for rows written before the
        # readiness column existed.  This is metadata-only and does not touch
        # source bodies or embeddings.
        conn.execute(
            """
            UPDATE skills
            SET readiness = CASE
                WHEN quality_status IN ('rejected', 'duplicate') OR entrypoint_truncated = 1
                    THEN 'rejected'
                WHEN quality_status = 'active'
                 AND content_hash IS NOT NULL
                 AND (
                     source NOT IN ('github', 'github_skill_file', 'skillsmp', 'awesome_list')
                     OR (
                         package_completeness = 'complete'
                         AND dependency_closure_status = 'complete'
                     )
                 ) THEN 'full-ready'
                WHEN quality_status IN ('active', 'metadata_only')
                 AND (name IS NOT NULL OR description IS NOT NULL) THEN 'hint-ready'
                ELSE 'catalog-ready'
            END
            WHERE readiness IS NULL OR readiness = 'catalog-ready'
            """
        )
        _stale_duplicate_running_scrapes(conn)
        conn.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS scrape_runs_one_running_idx "
            "ON scrape_runs(status) WHERE status='running'"
        )
        conn.commit()
    finally:
        conn.close()


def upsert_skill_package(
    manifest: dict,
    retrieval_record: dict | None = None,
    *,
    skill_id: str | None = None,
) -> None:
    """Persist immutable package metadata and its separate retrieval record.

    Repeated package bytes from forks share one package row while every source
    observation is retained in ``skill_package_sources``.
    """
    package_hash = str(manifest.get("package_hash") or "")
    if not re.fullmatch(r"[a-f0-9]{64}", package_hash):
        raise ValueError("invalid package hash")
    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    source_url = str(manifest.get("source_url") or "")
    source_commit_sha = str(source.get("commit_sha") or "") or None
    now = str(manifest.get("created_at") or _now())
    license_info = manifest.get("license") if isinstance(manifest.get("license"), dict) else {}
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            """
            INSERT OR IGNORE INTO skill_packages
            (package_hash, source_url, source_provider, source_commit_sha, root_path,
             entrypoint_path, tree_sha, license_spdx, completeness_status,
             dependency_closure_status, entrypoint_truncated, manifest_json, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                package_hash,
                source_url,
                str(source.get("provider") or ""),
                source_commit_sha,
                str(source.get("root_path") or ""),
                str(manifest.get("entrypoint") or ""),
                str(source.get("tree_sha") or ""),
                license_info.get("spdx_id"),
                str(manifest.get("completeness_status") or "partial"),
                str(manifest.get("dependency_closure_status") or "unknown"),
                int(bool(manifest.get("entrypoint_truncated"))),
                json.dumps(manifest, sort_keys=True, separators=(",", ":")),
                now,
            ),
        )
        if source_url:
            conn.execute(
                """
                INSERT OR IGNORE INTO skill_package_sources
                (package_hash, source_url, source_commit_sha, provenance_json, observed_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    package_hash,
                    source_url,
                    source_commit_sha or "",
                    json.dumps(manifest.get("provenance") or {}, sort_keys=True),
                    now,
                ),
            )
        for file_info in manifest.get("files") or []:
            if not isinstance(file_info, dict):
                continue
            conn.execute(
                """
                INSERT OR IGNORE INTO skill_package_files
                (package_hash, path, raw_sha256, git_blob_sha, size, role, media_type,
                 text_indexable, in_dependency_closure)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        if retrieval_record:
            record_hash = str(retrieval_record.get("record_hash") or "")
            if not re.fullmatch(r"[a-f0-9]{64}", record_hash):
                raise ValueError("invalid retrieval record hash")
            if skill_id:
                conn.execute(
                    "UPDATE skill_retrieval_records SET active=0 WHERE skill_id=?",
                    (skill_id,),
                )
            conn.execute(
                """
                INSERT OR REPLACE INTO skill_retrieval_records
                (record_hash, skill_id, package_hash, record_version, text, text_hash,
                 role, dependency_closure_status, source_commit_sha, entrypoint_path,
                 active, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
                """,
                (
                    record_hash,
                    skill_id,
                    package_hash,
                    str(retrieval_record.get("record_version") or ""),
                    str(retrieval_record.get("text") or ""),
                    str(retrieval_record.get("text_hash") or ""),
                    str(retrieval_record.get("role") or "candidate"),
                    str(retrieval_record.get("dependency_closure_status") or "unknown"),
                    retrieval_record.get("source_commit_sha"),
                    retrieval_record.get("entrypoint_path"),
                    now,
                ),
            )
        if skill_id:
            conn.execute(
                """
                UPDATE skills SET package_hash=?, source_commit_sha=?, license_spdx=?,
                    package_completeness=?, dependency_closure_status=?,
                    entrypoint_truncated=?, retrieval_text=COALESCE(?, retrieval_text),
                    retrieval_text_hash=COALESCE(?, retrieval_text_hash),
                    retrieval_record_hash=COALESCE(?, retrieval_record_hash)
                WHERE id=?
                """,
                (
                    package_hash,
                    source_commit_sha,
                    license_info.get("spdx_id"),
                    str(manifest.get("completeness_status") or "partial"),
                    str(manifest.get("dependency_closure_status") or "unknown"),
                    int(bool(manifest.get("entrypoint_truncated"))),
                    retrieval_record.get("text") if retrieval_record else None,
                    retrieval_record.get("text_hash") if retrieval_record else None,
                    retrieval_record.get("record_hash") if retrieval_record else None,
                    skill_id,
                ),
            )
            updated = conn.execute("SELECT * FROM skills WHERE id=?", (skill_id,)).fetchone()
            if updated:
                conn.execute(
                    "UPDATE skills SET readiness=? WHERE id=?",
                    (quality.readiness_for_skill(dict(updated)), skill_id),
                )
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def get_skill_package_manifest(package_hash: str) -> dict | None:
    if not re.fullmatch(r"[a-f0-9]{64}", str(package_hash or "")):
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT manifest_json FROM skill_packages WHERE package_hash=?", (package_hash,)
        ).fetchone()
        return json.loads(row["manifest_json"]) if row else None
    finally:
        conn.close()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def pack_embedding(vec: list[float]) -> bytes:
    return struct.pack(f"{len(vec)}f", *vec)


def unpack_embedding(blob: bytes) -> np.ndarray:
    n = len(blob) // 4
    return np.array(struct.unpack(f"{n}f", blob), dtype=np.float32)


def _row_to_dict(row: sqlite3.Row, table: str, select_cols: list[str] | None) -> dict:
    d = dict(row)
    d.pop("embedding", None)
    for col in TABLES[table]["json_cols"]:
        if col in d and isinstance(d[col], str):
            try:
                d[col] = json.loads(d[col])
            except Exception:
                pass
    if select_cols:
        d = {k: d[k] for k in select_cols if k in d}
    return d


def _record_skill_version(cur, skill_id: str, content_hash: str) -> None:
    """Append-only version history: every content hash a skill has ever been
    seen with, so pro users can pin or roll back to a prior version and
    watchers can be alerted when the hash moves."""
    cur.execute(
        "INSERT OR IGNORE INTO skill_versions (id, skill_id, content_hash, seen_at) VALUES (?, ?, ?, ?)",
        (str(uuid.uuid4()), skill_id, content_hash, _now()),
    )


def upsert_rows(table: str, rows: list[dict], on_conflict: str | None) -> list[dict]:
    conn = get_conn()
    try:
        cur = conn.cursor()
        out = []
        for row in rows:
            row = dict(row)
            if "id" not in row:
                row["id"] = str(uuid.uuid4())
            if table == "skills" and "discovered_at" not in row:
                row["discovered_at"] = _now()
            if table == "skills" and "quality_status" not in row:
                # Routine discovery upserts old, already-scanned URLs without
                # their quality fields. Preserve those rows, but make a truly
                # new unscanned URL pending rather than accidentally active.
                url = row.get("url")
                existing = cur.execute("SELECT 1 FROM skills WHERE url=?", (url,)).fetchone() if url else None
                if existing is None:
                    row["quality_status"] = "pending"
            if table == "scrape_runs" and "started_at" not in row:
                row["started_at"] = _now()
            for col in TABLES[table]["json_cols"]:
                if col in row and not isinstance(row[col], str):
                    row[col] = json.dumps(row[col])
            if table in ("skills", "skill_tools") and isinstance(row.get("embedding"), list):
                row["embedding"] = pack_embedding(row["embedding"])

            cols = list(row.keys())
            placeholders = ",".join("?" for _ in cols)
            col_list = ",".join(cols)

            unique_col = TABLES[table]["unique"]
            if on_conflict and unique_col:
                update_cols = [c for c in cols if c != unique_col]
                set_clause = ",".join(f"{c}=excluded.{c}" for c in update_cols)
                sql = (
                    f"INSERT INTO {table} ({col_list}) VALUES ({placeholders}) "
                    f"ON CONFLICT({unique_col}) DO UPDATE SET {set_clause}"
                )
            else:
                sql = f"INSERT INTO {table} ({col_list}) VALUES ({placeholders})"
            cur.execute(sql, list(row.values()))

            if unique_col and unique_col in row:
                r = cur.execute(f"SELECT * FROM {table} WHERE {unique_col}=?", (row[unique_col],)).fetchone()
            else:
                r = cur.execute(f"SELECT * FROM {table} WHERE id=?", (row["id"],)).fetchone()
            stored = _row_to_dict(r, table, None)
            if table == "skills" and stored.get("content_hash"):
                _record_skill_version(cur, stored["id"], stored["content_hash"])
            out.append(stored)
        conn.commit()
        if table in ("skills", "skill_tools"):
            invalidate_vector_cache()
        return out
    finally:
        conn.close()


_FILTER_RE = re.compile(r"^(not\.)?([a-z]+)\.(.*)$")


def _apply_filter(col: str, raw_value: str) -> tuple[str, list]:
    m = _FILTER_RE.match(raw_value)
    if not m:
        return f"{col} = ?", [raw_value]
    negate, op, val = m.groups()
    if op == "is" and val == "null":
        clause = f"{col} IS NULL"
        return (f"NOT ({clause})", []) if negate else (clause, [])
    if op == "eq":
        return f"{col} {'!=' if negate else '='} ?", [val]
    if op == "gt":
        return f"{col} {'<=' if negate else '>'} ?", [val]
    if op == "lt":
        return f"{col} {'>=' if negate else '<'} ?", [val]
    if op == "in":
        items = [v.strip().strip('"') for v in val.strip("()").split(",") if v.strip()]
        placeholders = ",".join("?" for _ in items)
        clause = f"{col} IN ({placeholders})"
        return (f"NOT ({clause})", items) if negate else (clause, items)
    return "1=1", []


def select_rows(
    table: str,
    select: str | None = None,
    filters: dict | None = None,
    order: str | None = None,
    limit: int | None = None,
    range_start: int | None = None,
    range_end: int | None = None,
    count_exact: bool = False,
) -> tuple[list[dict], int | None]:
    conn = get_conn()
    try:
        where_sql = []
        params = []
        for col, val in (filters or {}).items():
            clause, p = _apply_filter(col, val)
            where_sql.append(clause)
            params.extend(p)
        where = f"WHERE {' AND '.join(where_sql)}" if where_sql else ""

        total = None
        if count_exact:
            total = conn.execute(f"SELECT COUNT(*) FROM {table} {where}", params).fetchone()[0]

        order_sql = ""
        if order:
            col, _, direction = order.partition(".")
            direction = "DESC" if direction.lower() == "desc" else "ASC"
            order_sql = f"ORDER BY {col} {direction}"

        limit_sql, offset_sql = "", ""
        if range_start is not None and range_end is not None:
            offset_sql = f"OFFSET {range_start}"
            limit_sql = f"LIMIT {range_end - range_start + 1}"
        elif limit is not None:
            limit_sql = f"LIMIT {limit}"

        sql = f"SELECT * FROM {table} {where} {order_sql} {limit_sql} {offset_sql}"
        rows = conn.execute(sql, params).fetchall()
        select_cols = [c.strip() for c in select.split(",")] if select else None
        return [_row_to_dict(r, table, select_cols) for r in rows], total
    finally:
        conn.close()


def update_rows(table: str, filters: dict, data: dict) -> int:
    conn = get_conn()
    try:
        for col in TABLES[table]["json_cols"]:
            if col in data and not isinstance(data[col], str):
                data[col] = json.dumps(data[col])
        if table == "skills" and isinstance(data.get("embedding"), list):
            data["embedding"] = pack_embedding(data["embedding"])
        where_sql, params = [], []
        for col, val in filters.items():
            clause, p = _apply_filter(col, val)
            where_sql.append(clause)
            params.extend(p)
        set_clause = ",".join(f"{k}=?" for k in data.keys())
        sql = f"UPDATE {table} SET {set_clause} WHERE {' AND '.join(where_sql)}"
        cur = conn.execute(sql, list(data.values()) + params)
        if table == "skills" and "content_hash" in data and cur.rowcount:
            for row in conn.execute(
                f"SELECT id, content_hash FROM skills WHERE {' AND '.join(where_sql)}", params
            ).fetchall():
                if row["content_hash"]:
                    _record_skill_version(conn, row["id"], row["content_hash"])
        conn.commit()
        if table == "skills" and cur.rowcount:
            invalidate_vector_cache()
        return cur.rowcount
    finally:
        conn.close()


def delete_rows(table: str, filters: dict) -> int:
    conn = get_conn()
    try:
        where_sql, params = [], []
        for col, val in filters.items():
            clause, p = _apply_filter(col, val)
            where_sql.append(clause)
            params.extend(p)
        sql = f"DELETE FROM {table} WHERE {' AND '.join(where_sql)}"
        cur = conn.execute(sql, params)
        conn.commit()
        if table in ("skills", "skill_tools") and cur.rowcount:
            invalidate_vector_cache()
        return cur.rowcount
    finally:
        conn.close()


def insert_route_event(event: dict) -> None:
    """Append privacy-safe route metadata, discarding every unknown field."""
    conn = get_conn()
    try:
        row = {key: value for key, value in event.items() if key in ROUTE_EVENT_WRITE_COLUMNS}
        row.setdefault("id", str(uuid.uuid4()))
        row.setdefault("created_at", _now())
        for key in ("client", "client_version", "config_version", "feedback_source"):
            if key in row:
                row[key] = _safe_route_identifier(row[key]) or None
        if row.get("anonymous_id_hash") and not _ANONYMOUS_HASH_RE.fullmatch(str(row["anonymous_id_hash"])):
            row["anonymous_id_hash"] = None
        if "ip_address" in row:
            row["ip_address"] = _safe_route_ip(row["ip_address"])
        if row.get("anonymous_id_hash") or row.get("ip_address"):
            # IPs get the same rolling retention as the anonymous installation
            # hash -- both identify a caller, neither is needed beyond the
            # abuse/attribution window.
            cutoff = (datetime.now(timezone.utc) - timedelta(days=ANONYMOUS_ID_RETENTION_DAYS)).isoformat()
            conn.execute(
                "UPDATE route_events SET anonymous_id_hash=NULL "
                "WHERE anonymous_id_hash IS NOT NULL AND created_at < ?",
                (cutoff,),
            )
            conn.execute(
                "UPDATE route_events SET ip_address=NULL "
                "WHERE ip_address IS NOT NULL AND created_at < ?",
                (cutoff,),
            )
        if row.get("tier") not in ROUTE_EVENT_TIERS:
            row["tier"] = None
        if row.get("guard_delivery") not in ROUTE_GUARD_DELIVERIES:
            row["guard_delivery"] = None
        if row.get("outcome") not in ROUTE_EVENT_OUTCOMES:
            row["outcome"] = None
        if row.get("skip_reason") not in SAFE_ROUTE_SKIP_REASONS:
            row["skip_reason"] = None
        row["warnings"] = "[]"
        cols = list(row.keys())
        placeholders = ",".join("?" for _ in cols)
        conn.execute(
            f"INSERT INTO route_events ({','.join(cols)}) VALUES ({placeholders})",
            [row[col] for col in cols],
        )
        conn.commit()
    finally:
        conn.close()


def _percentile(values: list[int], percentile: float) -> int:
    if not values:
        return 0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int(round((len(ordered) - 1) * percentile))))
    return int(ordered[index] or 0)


def route_event_summary(
    hours: int = 24,
    *,
    config_version: str | None = None,
    max_latency_ms: int = 750,
    max_skill_find_ms: int = 500,
    max_injected_tokens: int = 1000,
    max_response_tokens: int = 3500,
) -> dict:
    """Aggregate recent route events for local ops/product checks."""
    conn = get_conn()
    try:
        cutoff = time.time() - (max(1, hours) * 3600)
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        where = "created_at >= ?"
        params: list[str] = [cutoff_iso]
        if config_version:
            where += " AND config_version = ?"
            params.append(config_version)
        total = conn.execute(f"SELECT COUNT(*) FROM route_events WHERE {where}", params).fetchone()[0]
        identity_counts = conn.execute(
            f"""
            SELECT
              SUM(CASE WHEN user_id IS NOT NULL THEN 1 ELSE 0 END) AS authenticated,
              SUM(CASE WHEN user_id IS NULL AND anonymous_id_hash IS NOT NULL THEN 1 ELSE 0 END) AS anonymous,
              COUNT(DISTINCT CASE WHEN user_id IS NULL THEN anonymous_id_hash END) AS anonymous_installations
            FROM route_events
            WHERE {where}
            """,
            params,
        ).fetchone()
        tiers = {
            row["tier"]: row["count"]
            for row in conn.execute(
                f"SELECT tier, COUNT(*) AS count FROM route_events WHERE {where} GROUP BY tier",
                params,
            ).fetchall()
        }
        outcomes = {
            row["outcome"] or "pending": row["count"]
            for row in conn.execute(
                f"SELECT outcome, COUNT(*) AS count FROM route_events WHERE {where} GROUP BY outcome",
                params,
            ).fetchall()
        }
        guard_deliveries = {
            row["guard_delivery"] or "none": row["count"]
            for row in conn.execute(
                f"SELECT guard_delivery, COUNT(*) AS count FROM route_events WHERE {where} GROUP BY guard_delivery",
                params,
            ).fetchall()
        }
        row = conn.execute(
            """
            SELECT
              AVG(latency_ms) AS avg_latency_ms,
              AVG(skill_find_ms) AS avg_skill_find_ms,
              AVG(retrieval_ms) AS avg_retrieval_ms,
              AVG(rerank_ms) AS avg_rerank_ms,
              AVG(content_ms) AS avg_content_ms,
              AVG(injected_tokens) AS avg_injected_tokens,
              AVG(response_tokens) AS avg_response_tokens,
              MAX(skill_find_ms) AS max_skill_find_ms,
              MAX(latency_ms) AS max_latency_ms,
              MAX(injected_tokens) AS max_injected_tokens,
              MAX(response_tokens) AS max_response_tokens
            FROM route_events
            WHERE {where}
            """.format(where=where),
            params,
        ).fetchone()
        metric_rows = [
            dict(r)
            for r in conn.execute(
                """
                SELECT latency_ms, skill_find_ms, injected_tokens, response_tokens
                FROM route_events
                WHERE {where}
                """.format(where=where),
                params,
            ).fetchall()
        ]
        latency_values = [int(r["latency_ms"] or 0) for r in metric_rows]
        skill_find_values = [int(r["skill_find_ms"] or 0) for r in metric_rows]
        injected_values = [int(r["injected_tokens"] or 0) for r in metric_rows]
        response_values = [int(r["response_tokens"] or 0) for r in metric_rows]
        budget_breaches = {
            "latency_ms": sum(1 for value in latency_values if value > max_latency_ms),
            "skill_find_ms": sum(1 for value in skill_find_values if value > max_skill_find_ms),
            "injected_tokens": sum(1 for value in injected_values if value > max_injected_tokens),
            "response_tokens": sum(1 for value in response_values if value > max_response_tokens),
            "any": sum(
                1
                for r in metric_rows
                if int(r["latency_ms"] or 0) > max_latency_ms
                or int(r["skill_find_ms"] or 0) > max_skill_find_ms
                or int(r["injected_tokens"] or 0) > max_injected_tokens
                or int(r["response_tokens"] or 0) > max_response_tokens
            ),
        }
        slowest = [
            dict(r)
            for r in conn.execute(
                """
                SELECT created_at, client, tier, skill_name, latency_ms, skill_find_ms,
                       retrieval_ms, rerank_ms, content_ms, injected_tokens,
                       response_tokens, guard_delivery, capsule_chars,
                       meaningfulness_score, warnings
                FROM route_events
                WHERE {where}
                ORDER BY latency_ms DESC
                LIMIT 5
                """.format(where=where),
                params,
            ).fetchall()
        ]
        for event in slowest:
            try:
                event["warnings"] = json.loads(event.get("warnings") or "[]")
            except Exception:
                event["warnings"] = []
        top_skills = [
            dict(r)
            for r in conn.execute(
                """
                SELECT
                  skill_name,
                  skill_url,
                  COUNT(*) AS count,
                  SUM(CASE WHEN tier='full' THEN 1 ELSE 0 END) AS full_count,
                  SUM(CASE WHEN tier='hint' THEN 1 ELSE 0 END) AS hint_count,
                  SUM(CASE WHEN outcome IS NOT NULL THEN 1 ELSE 0 END) AS feedback_count,
                  SUM(CASE WHEN outcome IN ('used', 'installed') THEN 1 ELSE 0 END) AS positive_count,
                  AVG(latency_ms) AS avg_latency_ms,
                  AVG(skill_find_ms) AS avg_skill_find_ms,
                  AVG(injected_tokens) AS avg_injected_tokens,
                  AVG(response_tokens) AS avg_response_tokens
                FROM route_events
                WHERE {where}
                  AND skill_name IS NOT NULL
                  AND skill_name != ''
                GROUP BY skill_name, skill_url
                ORDER BY count DESC, positive_count DESC, skill_name ASC
                LIMIT 10
                """.format(where=where),
                params,
            ).fetchall()
        ]
        for skill in top_skills:
            for key in ("count", "full_count", "hint_count", "feedback_count", "positive_count"):
                skill[key] = int(skill[key] or 0)
            skill["avg_latency_ms"] = int(skill["avg_latency_ms"] or 0)
            skill["avg_skill_find_ms"] = int(skill["avg_skill_find_ms"] or 0)
            skill["avg_injected_tokens"] = int(skill["avg_injected_tokens"] or 0)
            skill["avg_response_tokens"] = int(skill["avg_response_tokens"] or 0)
        top_used_skills = [skill for skill in top_skills if skill["positive_count"] > 0]
        top_used_skills.sort(key=lambda s: (-s["positive_count"], -s["count"], s["skill_name"] or ""))
        return {
            "window_hours": hours,
            "metrics_config_version": config_version or "all",
            "total": total,
            "authenticated_events": int(identity_counts["authenticated"] or 0),
            "anonymous_events": int(identity_counts["anonymous"] or 0),
            "anonymous_installations": int(identity_counts["anonymous_installations"] or 0),
            "tiers": tiers,
            "outcomes": outcomes,
            "guard_deliveries": guard_deliveries,
            "top_skills": top_skills,
            "top_used_skills": top_used_skills[:10],
            "vector_index": vector_index_stats(),
            "avg_latency_ms": int(row["avg_latency_ms"] or 0),
            "avg_skill_find_ms": int(row["avg_skill_find_ms"] or 0),
            "avg_retrieval_ms": int(row["avg_retrieval_ms"] or 0),
            "avg_rerank_ms": int(row["avg_rerank_ms"] or 0),
            "avg_content_ms": int(row["avg_content_ms"] or 0),
            "avg_injected_tokens": int(row["avg_injected_tokens"] or 0),
            "avg_response_tokens": int(row["avg_response_tokens"] or 0),
            "p95_latency_ms": _percentile(latency_values, 0.95),
            "p95_skill_find_ms": _percentile(skill_find_values, 0.95),
            "p95_injected_tokens": _percentile(injected_values, 0.95),
            "p95_response_tokens": _percentile(response_values, 0.95),
            "max_skill_find_ms": int(row["max_skill_find_ms"] or 0),
            "max_latency_ms": int(row["max_latency_ms"] or 0),
            "max_injected_tokens": int(row["max_injected_tokens"] or 0),
            "max_response_tokens": int(row["max_response_tokens"] or 0),
            "budgets": {
                "latency_ms": int(max_latency_ms),
                "skill_find_ms": int(max_skill_find_ms),
                "injected_tokens": int(max_injected_tokens),
                "response_tokens": int(max_response_tokens),
            },
            "budget_breaches": budget_breaches,
            "slowest": slowest,
        }
    finally:
        conn.close()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def scrape_run_summary(stale_after_seconds: int = 7200, limit: int = 5) -> dict:
    """Return recent scraper bookkeeping for readiness and launch checks."""
    stale_after_seconds = max(1, int(stale_after_seconds or 7200))
    limit = max(1, min(int(limit or 5), 20))
    now = datetime.now(timezone.utc)
    conn = get_conn()
    try:
        recent = [
            dict(r)
            for r in conn.execute(
                """
                SELECT id, started_at, finished_at, status, skills_found,
                       new_skills_found, error
                FROM scrape_runs
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        ]
        row = conn.execute(
            """
            SELECT MAX(finished_at) AS last_success_at
            FROM scrape_runs
            WHERE status='done'
            """
        ).fetchone()
    finally:
        conn.close()

    running_recent = 0
    running_stale = 0
    for run in recent:
        started_at = _parse_iso(run.get("started_at"))
        age_seconds = int((now - started_at).total_seconds()) if started_at else None
        run["age_seconds"] = age_seconds
        if run.get("status") == "running":
            if age_seconds is not None and age_seconds > stale_after_seconds:
                running_stale += 1
            else:
                running_recent += 1
    return {
        "stale_after_seconds": stale_after_seconds,
        "running_recent": running_recent,
        "running_stale": running_stale,
        "last_success_at": row["last_success_at"] if row else None,
        "recent_runs": recent,
    }


def update_route_event_feedback(route_id: str, outcome: str, source: str = "", note: str = "") -> bool:
    """Attach enum outcome/source metadata; free-form ``note`` is ignored."""
    del note
    outcome = (outcome or "").strip().lower()
    if outcome not in ROUTE_EVENT_OUTCOMES:
        return False
    safe_source = _safe_route_identifier(source)
    conn = get_conn()
    try:
        columns = _route_event_columns(conn)
        clear_legacy_note = ", feedback_note=NULL" if "feedback_note" in columns else ""
        cur = conn.execute(
            f"""
            UPDATE route_events
            SET outcome=?, outcome_at=?, feedback_source=?{clear_legacy_note}
            WHERE id=?
            """,
            (
                outcome,
                _now(),
                safe_source or None,
                route_id,
            ),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


_WORD_RE = re.compile(r"[A-Za-z0-9]+")
_FTS_STOPWORDS = frozenset(
    set(quality.NAME_STOPWORDS)
    | {
        "about", "after", "also", "and", "are", "can", "could", "create", "does", "for",
        "from", "have", "help", "into", "make", "need", "please", "should", "that", "the",
        "this", "using", "want", "with", "would", "you", "your",
    }
)


def _fts_query(text: str) -> str:
    words = [word.lower() for word in _WORD_RE.findall(text) if len(word) >= 3]
    meaningful = [word for word in words if word not in _FTS_STOPWORDS]
    selected = meaningful or words
    # OR is intentionally used here. An all-terms AND query turns ordinary
    # task phrasing into an empty fallback search; routing still applies the
    # stricter deterministic quality/similarity gate afterwards.
    return " OR ".join(f'"{word}"' for word in selected[:12]) if selected else ""


_lex_cache_lock = threading.RLock()
_lex_cache: dict = {
    "generation": -1,
    "db_path": "",
    "postings": {},
    "ids_desc": [],
}


def _lexical_tokens(text: str) -> list[str]:
    tokens: set[str] = set()
    for raw_word in _WORD_RE.findall(text or ""):
        word = raw_word.lower()
        if len(word) < 3 or word in _FTS_STOPWORDS:
            continue
        tokens.add(word)
        if len(word) > 4 and word.endswith("s"):
            tokens.add(word[:-1])
    return sorted(tokens)


def warm_lexical_index() -> dict:
    """Build a compact in-memory token->skill-id index for fast route probes."""
    conn = get_conn()
    try:
        postings: dict[str, list[str]] = defaultdict(list)
        rows = conn.execute(
            """
            SELECT id, name, description, tags, capability_summary, triggers, retrieval_text
            FROM skills
            WHERE risk_score < 3
              AND COALESCE(quality_status, 'pending') IN ('active', 'metadata_only')
            """
        ).fetchall()
        ids_desc: list[str] = []
        for row in rows:
            skill_id = str(row["id"])
            ids_desc.append(skill_id)
            text = " ".join(
                [
                    str(row["name"] or ""),
                    str(row["description"] or ""),
                    str(row["tags"] or ""),
                    str(row["capability_summary"] or ""),
                    str(row["triggers"] or ""),
                    str(row["retrieval_text"] or ""),
                ]
            )
            for token in set(_lexical_tokens(text)):
                postings[token].append(skill_id)
        generation = int(_emb_cache.get("generation") or 0)
        with _lex_cache_lock:
            _lex_cache.update(
                {
                    "generation": generation,
                    "db_path": str(DB_PATH),
                    "postings": dict(postings),
                    "ids_desc": sorted(ids_desc, reverse=True),
                }
            )
        return {"tokens": len(postings), "skills": len(rows)}
    finally:
        conn.close()
        warm_fast_path_index()


# Separate from the general lexical postings above: this indexes *only*
# triggers[] phrases and skill_tools.tool_name, kept apart from the general
# name/description/tags/capability_summary token soup so a match here can be
# recognized as "the query is near-exactly one of this skill's own declared
# triggers or tool names" -- strong, specific evidence -- rather than an
# ordinary keyword overlap. hybrid_search_skills uses this to force a skill
# into the candidate set even when FTS/vector both missed it.
_fast_path_cache_lock = threading.RLock()
_fast_path_cache: dict = {
    "db_path": "",
    "phrases": [],  # list of (tokens: frozenset[str], skill_id: str, phrase: str)
    "token_index": {},  # token -> set[int] (indices into "phrases")
}
FAST_PATH_OVERLAP_THRESHOLD = 0.7


def warm_fast_path_index() -> dict:
    conn = get_conn()
    try:
        # Keyed by skill url, not id -- see the skill_tools schema comment;
        # url is the one identifier guaranteed stable across scrape runs.
        phrases: list[tuple[frozenset, str, str]] = []
        trigger_rows = conn.execute(
            "SELECT url, triggers FROM skills WHERE risk_score < 3 "
            "AND COALESCE(quality_status, 'pending') IN ('active', 'metadata_only') "
            "AND triggers IS NOT NULL AND triggers != '[]'"
        ).fetchall()
        for row in trigger_rows:
            try:
                triggers = json.loads(row["triggers"] or "[]")
            except (TypeError, json.JSONDecodeError):
                triggers = []
            if not isinstance(triggers, list):
                continue
            for phrase in triggers:
                tokens = frozenset(_lexical_tokens(str(phrase)))
                if tokens:
                    phrases.append((tokens, str(row["url"]), str(phrase)))

        tool_rows = conn.execute("SELECT skill_url, tool_name FROM skill_tools").fetchall()
        for row in tool_rows:
            readable = re.sub(r"[_\-]+", " ", str(row["tool_name"] or ""))
            tokens = frozenset(_lexical_tokens(readable))
            if tokens:
                phrases.append((tokens, str(row["skill_url"]), str(row["tool_name"])))

        token_index: dict[str, set[int]] = defaultdict(set)
        for i, (tokens, _skill_url, _phrase) in enumerate(phrases):
            for token in tokens:
                token_index[token].add(i)

        with _fast_path_cache_lock:
            _fast_path_cache.update(
                {"db_path": str(DB_PATH), "phrases": phrases, "token_index": dict(token_index)}
            )
        return {"phrases": len(phrases)}
    finally:
        conn.close()


def _fast_path_matches(query_text: str) -> dict[str, float]:
    """skill url -> match strength (fraction of the matched phrase's tokens
    present in the query), for triggers/tool-names close enough to the query
    to count as a near-exact match. Bounded to phrases sharing at least one
    token with the query via the inverted index, not a full scan."""
    with _fast_path_cache_lock:
        if _fast_path_cache.get("db_path") != str(DB_PATH):
            return {}
        phrases = _fast_path_cache["phrases"]
        token_index = _fast_path_cache["token_index"]

    query_tokens = set(_lexical_tokens(query_text))
    if not query_tokens:
        return {}
    candidate_indices: set[int] = set()
    for token in query_tokens:
        candidate_indices |= token_index.get(token, set())

    matches: dict[str, float] = {}
    for i in candidate_indices:
        tokens, skill_url, _phrase = phrases[i]
        overlap = len(tokens & query_tokens) / len(tokens)
        if overlap >= FAST_PATH_OVERLAP_THRESHOLD and overlap > matches.get(skill_url, 0.0):
            matches[skill_url] = overlap
    return matches


def _cached_lexical_postings() -> dict[str, list[str]] | None:
    with _lex_cache_lock:
        postings = _lex_cache.get("postings")
        if not postings or _lex_cache.get("db_path") != str(DB_PATH):
            return None
        return postings


def _cached_lexical_ids_desc() -> list[str] | None:
    with _lex_cache_lock:
        ids_desc = _lex_cache.get("ids_desc")
        if not ids_desc or _lex_cache.get("db_path") != str(DB_PATH):
            return None
        return ids_desc if isinstance(ids_desc, list) else None


def _top_scored_skill_ids(
    scores: dict[str, float],
    max_results: int,
    ids_desc: list[str] | None = None,
) -> list[str]:
    """Return the legacy score-then-ID order without tuple-key heap churn.

    Lexical scores are sums of 1.0 per query token, so a route usually has a
    few score buckets even when a generic word matches most of the catalog.
    Selecting IDs within each bucket directly retains the historical
    ``heapq.nlargest(..., key=(score, id))`` ordering while avoiding one Python
    tuple-key call per matching skill. For a dense bucket, scanning the cached
    descending ID order stops as soon as the small route limit is satisfied;
    sparse buckets retain the bounded heap path.
    """
    if max_results < 1:
        return []
    score_counts = Counter(scores.values())
    sparse_scores = {
        score
        for score, count in score_counts.items()
        if ids_desc is None or count <= max(1024, max_results * 16)
    }
    sparse_buckets: dict[float, list[str]] = {score: [] for score in sparse_scores}
    if sparse_buckets:
        for skill_id, score in scores.items():
            bucket = sparse_buckets.get(score)
            if bucket is not None:
                bucket.append(skill_id)

    top_ids: list[str] = []
    for score in sorted(score_counts, reverse=True):
        remaining = max_results - len(top_ids)
        if remaining < 1:
            break
        if ids_desc is not None and score not in sparse_buckets:
            selected = [skill_id for skill_id in ids_desc if scores.get(skill_id) == score][:remaining]
            if len(selected) == remaining:
                top_ids.extend(selected)
                continue
            # A cache from a different corpus must not silently drop a match.
            selected_set = set(selected)
            bucket = [
                skill_id
                for skill_id, candidate_score in scores.items()
                if candidate_score == score and skill_id not in selected_set
            ]
            remaining -= len(selected)
            top_ids.extend(selected)
        else:
            bucket = sparse_buckets[score]
        top_ids.extend(heapq.nlargest(remaining, bucket))
    return top_ids


def _search_skills_lexical_memory(query: str, max_results: int) -> list[dict]:
    postings = _cached_lexical_postings()
    if postings is None:
        return []
    tokens = _lexical_tokens(query)[:12]
    if not tokens:
        return []
    scores: Counter[str] = Counter()
    for token in tokens:
        scores.update(postings.get(token, ()))
    if not scores:
        return []
    # Keep the in-memory path bounded: near-duplicate comparison is quadratic
    # in candidate count, and the router only needs a small recall set before
    # deterministic reranking trims it to the requested limit.
    top_count = max(1, max_results)
    top_ids = _top_scored_skill_ids(scores, top_count, _cached_lexical_ids_desc())
    placeholders = ",".join("?" for _ in top_ids)
    conn = get_conn()
    try:
        rows = conn.execute(
            f"SELECT {SKILL_RETRIEVAL_SQL} FROM skills WHERE id IN ({placeholders})", top_ids
        ).fetchall()
        by_id = {}
        for row in rows:
            item = _row_to_dict(row, "skills", None)
            item["stars"] = _stars(dict(row).get("raw", "{}"))
            item["rank"] = float(scores.get(item["id"], 0))
            by_id[item["id"]] = item
        ordered = [by_id[skill_id] for skill_id in top_ids if skill_id in by_id]
        return quality.dedupe_by_content_hash(ordered)[:max_results]
    finally:
        conn.close()


def _stars(raw_json: str) -> int:
    try:
        return int(json.loads(raw_json or "{}").get("stars") or 0)
    except Exception:
        return 0


def search_skills_fts(query: str, max_results: int = 10) -> list[dict]:
    try:
        limit = max(1, min(int(max_results), 100))
    except (TypeError, ValueError):
        limit = 10
    fast = _search_skills_lexical_memory(query, limit)
    if fast:
        return fast
    conn = get_conn()
    try:
        fts_q = _fts_query(query)
        if not fts_q:
            return []
        skill_columns = ", ".join(f"s.{column}" for column in SKILL_RETRIEVAL_COLUMNS)
        rows = conn.execute(
            f"""
            SELECT {skill_columns}, bm25(skills_fts) AS bm25
            FROM skills_fts
            JOIN skills s ON s.rowid = skills_fts.rowid
            WHERE skills_fts MATCH ?
              AND s.risk_score < 3
              AND COALESCE(s.quality_status, 'pending') IN ('active', 'metadata_only')
            ORDER BY bm25(skills_fts) ASC
            LIMIT ?
            """,
            (fts_q, max(limit * 3, limit)),
        ).fetchall()
        out = []
        for r in rows:
            d = _row_to_dict(r, "skills", None)
            d["stars"] = _stars(dict(r).get("raw", "{}"))
            d["rank"] = -r["bm25"]
            out.append(d)
        return quality.dedupe_by_content_hash(out)[:limit]
    except sqlite3.OperationalError:
        return []
    finally:
        conn.close()


# In-memory embedding-matrix cache. Rebuilding the matrix from blobs is
# O(corpus) per query and dominates latency once the corpus is large; with the
# cache a search is a single matvec.
#
# Writes mark this cache stale but retain the last complete matrix. The API
# rebuilds in the background and atomically swaps the replacement, so a route
# request never waits behind a multi-second blob scan during a scraper burst.
# Set a positive TTL only when another process writes SQLite directly.
EMBEDDING_DIM = 384
_EMB_DIM = EMBEDDING_DIM
_EMB_BLOB_LEN = _EMB_DIM * 4
MAX_ROUTING_DESCRIPTION_CHARS = 2000
_EMB_CACHE_TTL_SECONDS = float(os.getenv("EMBEDDING_MATRIX_CACHE_TTL_SECONDS", "0"))
_emb_cache: dict = {
    "at": 0.0,
    "ids": [],
    "mat": None,
    "db_path": "",
    "generation": 0,
    "built_generation": -1,
}
_emb_cache_lock = threading.RLock()
_emb_rebuild_lock = threading.Lock()


def invalidate_vector_cache() -> None:
    """Mark the vector matrix stale after skills writes without dropping it."""
    with _emb_cache_lock:
        _emb_cache["generation"] = int(_emb_cache["generation"] or 0) + 1
    with _tool_emb_cache_lock:
        _tool_emb_cache["generation"] = int(_tool_emb_cache["generation"] or 0) + 1
    with _lex_cache_lock:
        _lex_cache["generation"] = -1


def _index_counts(conn: sqlite3.Connection) -> dict[str, int]:
    """Use metadata indexes only; never scan all embedding BLOB payloads."""
    row = conn.execute(
        """
        SELECT
          (SELECT COUNT(*) FROM skills) AS total,
          (SELECT COUNT(*) FROM skills WHERE quality_status = 'active') AS active,
          (SELECT COUNT(*) FROM skills WHERE embedding IS NOT NULL) AS embedded,
          (SELECT COUNT(*) FROM skills
             WHERE quality_status = 'active' AND embedding IS NOT NULL) AS active_embedded
        """
    ).fetchone()
    return {
        "total_skills": int(row["total"] or 0),
        "active_skills": int(row["active"] or 0),
        "embedded_skills": int(row["active_embedded"] or 0),
        "all_embedded_skills": int(row["embedded"] or 0),
        # Embeddings are written only through the 384-dimension local embedder;
        # checking BLOB lengths here was the expensive readiness regression.
        "valid_vectors": int(row["active_embedded"] or 0),
    }


def vector_index_stats() -> dict:
    """Return cheap search-index stats for latency/debug dashboards."""
    conn = get_conn()
    try:
        counts = _index_counts(conn)
    finally:
        conn.close()

    with _emb_cache_lock:
        mat = _emb_cache.get("mat")
        cache_matches_db = _emb_cache.get("db_path") == str(DB_PATH)
        cache_at = float(_emb_cache.get("at") or 0.0)
        cache_vectors = len(_emb_cache.get("ids") or []) if cache_matches_db else 0
        generation = int(_emb_cache.get("generation") or 0)
        built_raw = _emb_cache.get("built_generation")
        built_generation = -1 if built_raw is None else int(built_raw)
    cache_ready = mat is not None and cache_matches_db
    age_ms = int((time.monotonic() - cache_at) * 1000) if cache_ready and cache_at else None
    return {
        **counts,
        "cache_ready": cache_ready,
        "cache_current": cache_ready and built_generation == generation,
        "cache_rebuild_in_progress": _emb_rebuild_lock.locked(),
        "cache_vectors": cache_vectors,
        "cache_age_ms": age_ms,
        "cache_ttl_seconds": int(_EMB_CACHE_TTL_SECONDS),
        "matrix_bytes": int(mat.nbytes) if cache_ready else 0,
        "vector_dim": _EMB_DIM,
    }


def readiness_stats() -> dict:
    """Cheap readiness payload used by /readyz on every deploy and monitor."""
    conn = get_conn()
    try:
        counts = _index_counts(conn)
        readiness_rows = conn.execute(
            """
            SELECT COALESCE(readiness, 'catalog-ready') AS readiness, COUNT(*) AS count
            FROM skills
            GROUP BY COALESCE(readiness, 'catalog-ready')
            """
        ).fetchall()
    finally:
        conn.close()
    readiness = {str(row["readiness"]): int(row["count"] or 0) for row in readiness_rows}
    return {
        "total_skills": counts["total_skills"],
        "active_skills": counts["active_skills"],
        "embedded_skills": counts["embedded_skills"],
        "readiness": readiness,
        "vector_index": vector_index_stats(),
    }


def warm_vector_index() -> dict:
    """Build the in-process matrix ahead of a user-facing route request.

    This is intentionally separate from ``vector_index_stats``: stats stay
    metadata-only, while readiness and the write-side debouncer can opt into
    the one-time matrix construction after a restart or invalidation.
    """
    conn = get_conn()
    try:
        _embedding_matrix(conn, refresh=True)
    finally:
        conn.close()
    return vector_index_stats()


def _cached_embedding_matrix(*, refresh: bool) -> tuple[list[str], np.ndarray] | None:
    """Read a usable matrix without making routing wait on a rebuild."""
    with _emb_cache_lock:
        cached = _emb_cache["mat"]
        if cached is None or _emb_cache.get("db_path") != str(DB_PATH):
            return None
        now = time.monotonic()
        ttl_fresh = _EMB_CACHE_TTL_SECONDS <= 0 or now - _emb_cache["at"] < _EMB_CACHE_TTL_SECONDS
        cache_current = _emb_cache["built_generation"] == _emb_cache["generation"]
        if (not refresh and ttl_fresh) or (refresh and cache_current and ttl_fresh):
            return _emb_cache["ids"], cached
    return None


def _embedding_matrix(conn: sqlite3.Connection, *, refresh: bool = False) -> tuple[list[str], np.ndarray]:
    cached = _cached_embedding_matrix(refresh=refresh)
    if cached is not None:
        return cached

    # Do the costly SQLite scan and BLOB copy outside the cache lock. Existing
    # routes can keep using the previous matrix while a debounced write-side
    # refresh is in progress.
    with _emb_rebuild_lock:
        while True:
            cached = _cached_embedding_matrix(refresh=refresh)
            if cached is not None:
                return cached

            with _emb_cache_lock:
                target_generation = int(_emb_cache["generation"] or 0)

            rows = conn.execute(
                "SELECT id, embedding FROM skills "
                "WHERE embedding IS NOT NULL "
                "AND risk_score < 3 "
                "AND COALESCE(quality_status, 'pending') IN ('active', 'metadata_only')"
            ).fetchall()
            ids = [r["id"] for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
            blobs = [bytes(r["embedding"]) for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
            if blobs:
                mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), _EMB_DIM)
            else:
                mat = np.zeros((0, _EMB_DIM), dtype=np.float32)
            mat.setflags(write=False)

            with _emb_cache_lock:
                if int(_emb_cache["generation"] or 0) == target_generation:
                    _emb_cache.update(
                        at=time.monotonic(),
                        ids=ids,
                        mat=mat,
                        db_path=str(DB_PATH),
                        built_generation=target_generation,
                    )
                    return ids, mat

                # A write arrived while the scan was running. Keep the last
                # complete matrix serving and rebuild once more against the new
                # generation before publishing anything.


def vector_search_skills(
    query_embedding: list[float],
    match_count: int = 10,
    candidate_ids: list[str] | None = None,
) -> list[dict]:
    conn = get_conn()
    try:
        ids, mat = _embedding_matrix(conn)
        if not ids:
            return []
        q = np.array(query_embedding, dtype=np.float32)
        similarity_by_position: dict[int, float] = {}
        if candidate_ids:
            candidate_set = set(candidate_ids)
            positions = [index for index, skill_id in enumerate(ids) if skill_id in candidate_set]
            if positions:
                candidate_mat = mat[positions]
                sims = candidate_mat @ q
                similarity_by_position = {position: float(sim) for position, sim in zip(positions, sims)}
                k = min(match_count, len(positions))
                local_top = np.argpartition(-sims, k - 1)[:k]
                top = np.array([positions[index] for index in local_top], dtype=np.int64)
                top = top[np.argsort(-sims[local_top])]
            else:
                return []
        else:
            sims = mat @ q  # both L2-normalized -> cosine similarity
            k = min(match_count, len(ids))
            top = np.argpartition(-sims, k - 1)[:k]
            top = top[np.argsort(-sims[top])]
            similarity_by_position = {int(index): float(sims[index]) for index in top}
        top_ids = [ids[i] for i in top]
        placeholders = ",".join("?" for _ in top_ids)
        fetched = conn.execute(
            f"SELECT {SKILL_RETRIEVAL_SQL} FROM skills WHERE id IN ({placeholders})", top_ids
        ).fetchall()
        by_id: dict[str, dict] = {}
        for r in fetched:
            d = _row_to_dict(r, "skills", None)
            d["stars"] = _stars(dict(r).get("raw", "{}"))
            by_id[d["id"]] = d
        out = []
        for i in top:
            d = by_id.get(ids[i])
            if d is not None:
                d["rank"] = similarity_by_position.get(int(i), 0.0)
                out.append(d)
        return out
    finally:
        conn.close()


# Second, smaller matrix: one vector per MCP tool rather than per skill (see
# skill_tools in _SCHEMA). Mirrors the skill-level cache above -- same
# generation-counter invalidation (invalidate_vector_cache bumps both), same
# "keep serving the last complete matrix during a background rebuild" shape --
# but the tool corpus is a fraction of the skill corpus (most skills have no
# MCP tools at all), so this stays a separate, lighter cache rather than
# complicating the skill-level one with a second row shape.
_tool_emb_cache: dict = {
    "at": 0.0,
    "ids": [],
    "skill_urls": [],
    "tool_names": [],
    "mat": None,
    "db_path": "",
    "generation": 0,
    "built_generation": -1,
}
_tool_emb_cache_lock = threading.RLock()
_tool_emb_rebuild_lock = threading.Lock()


def _cached_tool_embedding_matrix(*, refresh: bool):
    with _tool_emb_cache_lock:
        cached = _tool_emb_cache["mat"]
        if cached is None or _tool_emb_cache.get("db_path") != str(DB_PATH):
            return None
        cache_current = _tool_emb_cache["built_generation"] == _tool_emb_cache["generation"]
        if not refresh or cache_current:
            return _tool_emb_cache["ids"], _tool_emb_cache["skill_urls"], _tool_emb_cache["tool_names"], cached
    return None


def _tool_embedding_matrix(conn: sqlite3.Connection, *, refresh: bool = False):
    cached = _cached_tool_embedding_matrix(refresh=refresh)
    if cached is not None:
        return cached

    with _tool_emb_rebuild_lock:
        while True:
            cached = _cached_tool_embedding_matrix(refresh=refresh)
            if cached is not None:
                return cached

            with _tool_emb_cache_lock:
                target_generation = int(_tool_emb_cache["generation"] or 0)

            rows = conn.execute(
                "SELECT t.id, t.skill_url, t.tool_name, t.embedding FROM skill_tools t "
                "JOIN skills s ON s.url = t.skill_url "
                "WHERE t.embedding IS NOT NULL "
                "AND s.risk_score < 3 "
                "AND COALESCE(s.quality_status, 'pending') IN ('active', 'metadata_only')"
            ).fetchall()
            ids = [r["id"] for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
            skill_urls = [r["skill_url"] for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
            tool_names = [r["tool_name"] for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
            blobs = [bytes(r["embedding"]) for r in rows if len(r["embedding"]) == _EMB_BLOB_LEN]
            if blobs:
                mat = np.frombuffer(b"".join(blobs), dtype=np.float32).reshape(len(blobs), _EMB_DIM)
            else:
                mat = np.zeros((0, _EMB_DIM), dtype=np.float32)
            mat.setflags(write=False)

            with _tool_emb_cache_lock:
                if int(_tool_emb_cache["generation"] or 0) == target_generation:
                    _tool_emb_cache.update(
                        at=time.monotonic(),
                        ids=ids,
                        skill_urls=skill_urls,
                        tool_names=tool_names,
                        mat=mat,
                        db_path=str(DB_PATH),
                        built_generation=target_generation,
                    )
                    return ids, skill_urls, tool_names, mat


def vector_search_tools(query_embedding: list[float], match_count: int = 10) -> list[dict]:
    """Match the query against individual tool embeddings and roll up to one
    row per parent skill (by URL -- see the skill_tools schema comment on why
    not id), keeping the best-matching tool's similarity and name -- a skill
    with many tools should be found by its best tool, not diluted by all of
    them averaged into one skill-level vector."""
    conn = get_conn()
    try:
        _, skill_urls, tool_names, mat = _tool_embedding_matrix(conn)
        if not skill_urls:
            return []
        q = np.array(query_embedding, dtype=np.float32)
        sims = mat @ q
        best_by_url: dict[str, tuple[float, str]] = {}
        for skill_url, tool_name, sim in zip(skill_urls, tool_names, sims):
            sim = float(sim)
            current = best_by_url.get(skill_url)
            if current is None or sim > current[0]:
                best_by_url[skill_url] = (sim, tool_name)
        top_urls = sorted(best_by_url, key=lambda u: best_by_url[u][0], reverse=True)[:match_count]
        if not top_urls:
            return []
        placeholders = ",".join("?" for _ in top_urls)
        fetched = conn.execute(
            f"SELECT {SKILL_RETRIEVAL_SQL} FROM skills WHERE url IN ({placeholders})", top_urls
        ).fetchall()
        by_url: dict[str, dict] = {}
        for r in fetched:
            d = _row_to_dict(r, "skills", None)
            d["stars"] = _stars(dict(r).get("raw", "{}"))
            by_url[d["url"]] = d
        out = []
        for skill_url in top_urls:
            d = by_url.get(skill_url)
            if d is not None:
                sim, tool_name = best_by_url[skill_url]
                d["rank"] = sim
                d["matched_tool"] = tool_name
                out.append(d)
        return out
    finally:
        conn.close()


FAST_PATH_BOOST_WEIGHT = 0.15  # a strength=1.0 (near-exact trigger/tool-name) hit
# outweighs a typical single-channel top-rank RRF contribution (~0.05-0.08),
# without being large enough to bypass rerank_candidates/tier_for_ranked_candidates
# -- it forces the candidate INTO the fused set and biases its position, nothing more.


def hybrid_search_skills(
    query_text: str,
    query_embedding: list[float] | None,
    match_count: int = 10,
    fts_weight: float = 1.0,
    vec_weight: float = 0.6,
    rrf_k: int = 20,
) -> list[dict]:
    fts = search_skills_fts(query_text, 60)
    # Always rank the query vector against the full embedding matrix, not just
    # rows FTS already found by keyword. Restricting to FTS candidates whenever
    # FTS found >=10 hits (the previous behavior) defeated the point of hybrid
    # search: a semantically on-target skill sharing no keywords with the query
    # could never surface through the vector channel if FTS was "confident" on
    # an unrelated set of >=10 keyword matches. Measured cost of a full scan
    # against the real ~150k-row matrix: ~4ms (numpy matvec, BLAS-backed) --
    # the "scanning the entire matrix" concern this restriction was written
    # for does not hold up at this corpus size. (Independently confirmed:
    # origin/master fixed this exact restriction the same way, same root
    # cause, different wording -- "the first twelve query tokens [became] a
    # hard recall boundary.")
    vec = vector_search_skills(query_embedding, 60) if query_embedding else []
    # Per-tool channel: a multi-tool MCP server's blended skill-level vector
    # (above) dilutes a query that matches one specific tool -- this ranks
    # against individual tool embeddings instead and rolls up to the parent
    # skill (vector_search_tools), so that tool can still win on its own.
    tool_vec = vector_search_tools(query_embedding, 30) if query_embedding else []
    # Trigger/tool-name fast path: a near-exact match against a skill's own
    # declared triggers[] or a tool's name, independent of both FTS and
    # vector similarity -- see _fast_path_matches.
    fast_path = _fast_path_matches(query_text)

    by_id: dict[str, dict] = {}
    scores: dict[str, float] = {}
    for ix, row in enumerate(fts, start=1):
        by_id[row["id"]] = row
        scores[row["id"]] = scores.get(row["id"], 0.0) + fts_weight / (rrf_k + ix)
    for ix, row in enumerate(vec, start=1):
        row["similarity"] = row["rank"]  # cosine, before rank is overwritten with the fused score
        existing = by_id.get(row["id"])
        if existing is not None:
            existing["similarity"] = row["similarity"]
        else:
            by_id[row["id"]] = row
        scores[row["id"]] = scores.get(row["id"], 0.0) + vec_weight / (rrf_k + ix)
    for ix, row in enumerate(tool_vec, start=1):
        existing = by_id.get(row["id"])
        if existing is not None:
            existing.setdefault("matched_tool", row.get("matched_tool"))
            if row["rank"] > existing.get("similarity", 0.0):
                existing["similarity"] = row["rank"]
        else:
            row["similarity"] = row["rank"]
            by_id[row["id"]] = row
        scores[row["id"]] = scores.get(row["id"], 0.0) + vec_weight / (rrf_k + ix)

    if fast_path:
        # fast_path is keyed by skill url (see _fast_path_matches); by_id/scores
        # are keyed by the skills row's own "id". Reconcile through url rather
        # than assuming any id stability across the fetches above.
        url_to_id = {row.get("url"): row.get("id") for row in by_id.values() if row.get("url")}
        missing_urls = [url for url in fast_path if url not in url_to_id]
        if missing_urls:
            conn = get_conn()
            try:
                placeholders = ",".join("?" for _ in missing_urls)
                fetched = conn.execute(
                    f"SELECT {SKILL_RETRIEVAL_SQL} FROM skills WHERE url IN ({placeholders})", missing_urls
                ).fetchall()
                for r in fetched:
                    d = _row_to_dict(r, "skills", None)
                    d["stars"] = _stars(dict(r).get("raw", "{}"))
                    by_id[d["id"]] = d
                    url_to_id[d.get("url")] = d["id"]
            finally:
                conn.close()
        for url, strength in fast_path.items():
            skill_id = url_to_id.get(url)
            if skill_id is None or skill_id not in by_id:
                continue  # deleted/ineligible between warm and query; nothing to boost
            scores[skill_id] = scores.get(skill_id, 0.0) + FAST_PATH_BOOST_WEIGHT * strength

    # Dedup near-identical forks (same content_hash) before truncating to
    # match_count -- otherwise a duplicated cluster can crowd out distinct
    # results. Sort by fusion score first so pick_canonical only decides
    # which duplicate SURVIVES; existing RRF/star/quality ordering still
    # decides position via each survivor's own fusion score.
    fused_rows = []
    for skill_id, score in scores.items():
        row = dict(by_id[skill_id])
        # active + metadata_only: same discovery-eligibility set used
        # throughout this session's ingestion work (quality.ACTIVE_STATUSES).
        # Auto-injection safety is a separate, unchanged gate
        # (quality.tier_for_ranked_candidates, FULL_ROUTE_STATUS = 'active'
        # only) -- being a fusion candidate here does not make a
        # metadata_only skill (most MCP servers) eligible for full delivery.
        if (row.get("quality_status") or "pending") not in quality.ACTIVE_STATUSES:
            continue
        if len(str(row.get("description") or "")) > MAX_ROUTING_DESCRIPTION_CHARS:
            continue
        # Stable schema for newly activated rows that have not reached the
        # background embedder yet. Downstream confidence gates already prevent
        # a null-similarity row from becoming a silent full route.
        row.setdefault("similarity", None)
        row["_fuse_score"] = score
        fused_rows.append(row)
    fused_rows.sort(key=lambda r: r["_fuse_score"], reverse=True)
    fused_rows = quality.dedupe_by_content_hash(fused_rows)

    # Preserve the semantic recall budget before truncation.  If pending-
    # embedding lexical rows share the fused sort, enough exact-token matches
    # can otherwise consume every slot before the recommender has a chance to
    # put them in its review-only hint lane.  Reserve at most the final slot
    # for one pending row; all earlier slots must have real query similarity.
    if query_embedding:
        semantic_rows = [row for row in fused_rows if row.get("similarity") is not None]
        pending_rows = [row for row in fused_rows if row.get("similarity") is None]
        if pending_rows and match_count >= 2:
            selected_rows = semantic_rows[: match_count - 1] + pending_rows[:1]
        else:
            selected_rows = semantic_rows[:match_count]
    else:
        selected_rows = fused_rows[:match_count]

    out = []
    for row in selected_rows:
        score = row.pop("_fuse_score")
        risk = row.get("risk_score") or 0
        components = quality.meaningfulness_components(row)
        row["prominence_score"] = components["prominence"]
        row["provenance_score"] = components["provenance"]
        row["meaningfulness_score"] = components["meaningfulness"]
        risk_penalty = 0.01 * min(risk, 2)
        row["rank"] = float(score + (0.006 * float(components["meaningfulness"])) - risk_penalty)
        out.append(row)
    out.sort(
        key=lambda r: (
            bool(query_embedding) and r.get("similarity") is None,
            -float(r["rank"]),
        )
    )
    return out


def recompute_feedback_scores(min_samples: int = 8, prior_strength: float = 8.0, prior_mean: float = 0.5) -> int:
    """Aggregate route_events outcomes per content_hash, apply Bayesian
    shrinkage toward a neutral prior, and write feedback_score onto every
    skills row sharing that content_hash (so ranking reads it with no join).
    Positive: used/installed. Negative: failed/dismissed. skipped/NULL are
    ignored (no signal either way). Hashes with fewer than min_samples
    total events are pinned to prior_mean outright -- with route_events
    still sparse in practice, this keeps cold-start skills mathematically
    neutral instead of trusting noise from one or two events."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT s.content_hash AS content_hash,
                    SUM(CASE WHEN re.outcome IN ('used','installed') THEN 1 ELSE 0 END) AS positive,
                    SUM(CASE WHEN re.outcome IN ('failed','dismissed') THEN 1 ELSE 0 END) AS negative
            FROM route_events re
            JOIN skills s ON s.id = re.skill_id
            WHERE s.content_hash IS NOT NULL AND s.content_hash != ''
              AND COALESCE(re.feedback_source, '') != 'auto-skill-hook'
            GROUP BY s.content_hash
            """
        ).fetchall()
        updated = 0
        for row in rows:
            chash = row["content_hash"]
            positive = int(row["positive"] or 0)
            negative = int(row["negative"] or 0)
            total = positive + negative
            if total < min_samples:
                score = prior_mean
            else:
                score = (positive + prior_strength * prior_mean) / (total + prior_strength)
            cur = conn.execute(
                "UPDATE skills SET feedback_score=? WHERE content_hash=?",
                (score, chash),
            )
            updated += cur.rowcount
        conn.commit()
        return updated
    finally:
        conn.close()


# --- Accounts: users, OAuth identities, CLI tokens, and per-user data ------

def get_or_create_user(email: str, name: str | None, avatar_url: str | None) -> dict:
    """Find a user by email, or create one. Existing name/avatar_url are
    refreshed from the identity provider's latest profile on every login."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email,)).fetchone()
        if row is None:
            user_id = str(uuid.uuid4())
            conn.execute(
                "INSERT INTO users (id, email, name, avatar_url, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, email, name, avatar_url, _now()),
            )
        else:
            user_id = row["id"]
            conn.execute(
                "UPDATE users SET name=?, avatar_url=? WHERE id=?",
                (name, avatar_url, user_id),
            )
        conn.commit()
        return dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    finally:
        conn.close()


def link_oauth_identity(user_id: str, provider: str, provider_user_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO oauth_identities (id, user_id, provider, provider_user_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, provider, provider_user_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def _cli_token_expiry() -> str:
    return (datetime.now(timezone.utc) + timedelta(seconds=CLI_TOKEN_TTL_SECONDS)).isoformat()


def create_cli_token(user_id: str, token_hash: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO cli_tokens (id, token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), token_hash, user_id, _now(), _cli_token_expiry()),
        )
        conn.commit()
    finally:
        conn.close()


def get_user_by_token_hash(token_hash: str) -> dict | None:
    """Resolve a live (non-revoked, non-expired) CLI token to its user. Each
    successful use bumps last_used_at and slides expires_at forward another
    CLI_TOKEN_TTL_SECONDS, so an actively-used token never expires underneath
    a user, while an abandoned/leaked one ages out on its own."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT u.* FROM cli_tokens t JOIN users u ON u.id = t.user_id "
            "WHERE t.token_hash=? AND t.revoked_at IS NULL "
            "AND (t.expires_at IS NULL OR t.expires_at > ?)",
            (token_hash, _now()),
        ).fetchone()
        if row is None:
            return None
        conn.execute(
            "UPDATE cli_tokens SET last_used_at=?, expires_at=? WHERE token_hash=?",
            (_now(), _cli_token_expiry(), token_hash),
        )
        conn.commit()
        return dict(row)
    finally:
        conn.close()


def revoke_cli_token(token_hash: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE cli_tokens SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
            (_now(), token_hash),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _usage_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def set_user_plan(email: str, plan: str) -> bool:
    if plan not in USER_PLANS:
        raise ValueError(f"unknown plan {plan!r}; choose one of {', '.join(USER_PLANS)}")
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE users SET plan=? WHERE email=?", (plan, email))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_user_plan_by_id(user_id: str, plan: str) -> bool:
    if plan not in USER_PLANS:
        raise ValueError(f"unknown plan {plan!r}; choose one of {', '.join(USER_PLANS)}")
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE users SET plan=? WHERE id=?", (plan, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def _active_complimentary_entitlement_for_conn(
    conn: sqlite3.Connection, user_id: str, now: str | None = None
) -> dict | None:
    row = conn.execute(
        "SELECT * FROM complimentary_entitlements "
        "WHERE user_id=? AND revoked_at IS NULL AND expires_at > ? "
        "ORDER BY CASE plan WHEN 'team' THEN 2 WHEN 'pro' THEN 1 ELSE 0 END DESC, "
        "expires_at DESC, granted_at DESC LIMIT 1",
        (user_id, now or _now()),
    ).fetchone()
    return dict(row) if row else None


def access_details_for_user(user: dict) -> dict:
    """Return paid, complimentary, and effective access without mutating DB state.

    ``users.plan`` remains the Stripe-owned paid-plan mirror. Every caller
    that authorizes product features should use the returned ``plan`` (the
    effective plan), while billing code can use ``paid_plan`` explicitly.
    Expired grants stop applying immediately without a cleanup job.
    """
    paid_plan = user.get("plan") if user.get("plan") in USER_PLANS else "free"
    conn = get_conn()
    try:
        comp = _active_complimentary_entitlement_for_conn(conn, user["id"])
        inherited_org = None
        inherited_rows = conn.execute(
            "SELECT o.id AS org_id, owner.id AS owner_user_id, COALESCE(owner.plan, 'free') AS paid_plan "
            "FROM org_members m JOIN orgs o ON o.id=m.org_id JOIN users owner ON owner.id=o.owner_user_id "
            "WHERE m.user_id=? ORDER BY o.created_at ASC",
            (user["id"],),
        ).fetchall()
        for row in inherited_rows:
            owner_comp = _active_complimentary_entitlement_for_conn(conn, row["owner_user_id"])
            owner_direct_plan = max(
                (row["paid_plan"], owner_comp["plan"] if owner_comp else "free"),
                key=lambda plan: _PLAN_RANK.get(plan, 0),
            )
            if owner_direct_plan == "team":
                inherited_org = row["org_id"]
                break
    finally:
        conn.close()
    complimentary_plan = comp["plan"] if comp else None
    if complimentary_plan and _PLAN_RANK[complimentary_plan] > _PLAN_RANK[paid_plan]:
        effective_plan = complimentary_plan
        source = "complimentary"
    elif paid_plan != "free":
        effective_plan = paid_plan
        source = "stripe"
    elif complimentary_plan:
        effective_plan = complimentary_plan
        source = "complimentary"
    else:
        effective_plan = "free"
        source = "free"
    if inherited_org and _PLAN_RANK[effective_plan] < _PLAN_RANK["team"]:
        effective_plan = "team"
        source = "team_org"
    return {
        **user,
        "paid_plan": paid_plan,
        "plan": effective_plan,
        "plan_source": source,
        "complimentary_plan": complimentary_plan,
        "complimentary_expires_at": comp["expires_at"] if comp else None,
        "team_org_id": inherited_org,
    }


def grant_complimentary_entitlement(
    user_id: str,
    plan: str,
    expires_at: str,
    reason: str,
    actor: dict,
) -> dict | None:
    if plan not in COMPLIMENTARY_PLANS:
        raise ValueError("complimentary plan must be pro or team")
    now = _now()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        user = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        if user is None:
            conn.rollback()
            return None
        old = _active_complimentary_entitlement_for_conn(conn, user["id"], now)
        conn.execute(
            "UPDATE complimentary_entitlements SET revoked_at=?, revoked_by_user_id=?, revoke_reason=? "
            "WHERE user_id=? AND revoked_at IS NULL AND expires_at > ?",
            (now, actor["id"], "Superseded by a newer complimentary grant", user["id"], now),
        )
        entitlement_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO complimentary_entitlements "
            "(id, user_id, plan, reason, granted_by_user_id, granted_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (entitlement_id, user["id"], plan, reason, actor["id"], now, expires_at),
        )
        new_value = {"entitlement_id": entitlement_id, "plan": plan, "expires_at": expires_at}
        old_value = (
            {"entitlement_id": old["id"], "plan": old["plan"], "expires_at": old["expires_at"]}
            if old
            else None
        )
        conn.execute(
            "INSERT INTO admin_audit_log "
            "(id, actor_user_id, actor_email, target_user_id, target_email, action, "
            "old_value, new_value, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), actor["id"], actor["email"], user["id"], user["email"],
                "complimentary_entitlement_granted", json.dumps(old_value, sort_keys=True),
                json.dumps(new_value, sort_keys=True), reason, now,
            ),
        )
        conn.commit()
        return {**new_value, "user_id": user["id"], "email": user["email"], "reason": reason}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def revoke_complimentary_entitlement(entitlement_id: str, reason: str, actor: dict) -> dict | None:
    now = _now()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT e.*, u.email FROM complimentary_entitlements e "
            "JOIN users u ON u.id=e.user_id WHERE e.id=? AND e.revoked_at IS NULL",
            (entitlement_id,),
        ).fetchone()
        if row is None:
            conn.rollback()
            return None
        conn.execute(
            "UPDATE complimentary_entitlements SET revoked_at=?, revoked_by_user_id=?, revoke_reason=? "
            "WHERE id=? AND revoked_at IS NULL",
            (now, actor["id"], reason, entitlement_id),
        )
        old_value = {"entitlement_id": row["id"], "plan": row["plan"], "expires_at": row["expires_at"]}
        new_value = {**old_value, "revoked_at": now}
        conn.execute(
            "INSERT INTO admin_audit_log "
            "(id, actor_user_id, actor_email, target_user_id, target_email, action, "
            "old_value, new_value, reason, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                str(uuid.uuid4()), actor["id"], actor["email"], row["user_id"], row["email"],
                "complimentary_entitlement_revoked", json.dumps(old_value, sort_keys=True),
                json.dumps(new_value, sort_keys=True), reason, now,
            ),
        )
        conn.commit()
        return {**new_value, "user_id": row["user_id"], "email": row["email"], "reason": reason}
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_complimentary_entitlements(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, plan, reason, granted_at, expires_at, revoked_at, revoke_reason "
            "FROM complimentary_entitlements WHERE user_id=? ORDER BY granted_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def list_admin_audit(limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit), 500))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, actor_email, target_email, action, old_value, new_value, reason, created_at "
            "FROM admin_audit_log ORDER BY created_at DESC, id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        out = []
        for row in rows:
            entry = dict(row)
            for key in ("old_value", "new_value"):
                entry[key] = json.loads(entry[key]) if entry[key] else None
            out.append(entry)
        return out
    finally:
        conn.close()


def org_has_active_team_access(org_id: str) -> bool:
    conn = get_conn()
    try:
        owner = conn.execute(
            "SELECT u.* FROM orgs o JOIN users u ON u.id=o.owner_user_id WHERE o.id=?",
            (org_id,),
        ).fetchone()
        if owner is None:
            return False
        if (owner["plan"] or "free") == "team":
            return True
        comp = _active_complimentary_entitlement_for_conn(conn, owner["id"])
        return bool(comp and comp["plan"] == "team")
    finally:
        conn.close()


def get_user_by_id(user_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def set_stripe_customer_id(user_id: str, customer_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("UPDATE users SET stripe_customer_id=? WHERE id=?", (customer_id, user_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def set_stripe_subscription_state(
    user_id: str, plan: str, subscription_id: str | None, status: str | None
) -> bool:
    if plan not in USER_PLANS:
        raise ValueError(f"unknown plan {plan!r}")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE users SET plan=?, stripe_subscription_id=?, stripe_subscription_status=?, "
            "stripe_plan_updated_at=? WHERE id=?",
            (plan, subscription_id, status, _now(), user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def replace_stripe_customer_subscriptions(
    user_id: str, customer_id: str, subscriptions: list[dict]
) -> str:
    """Atomically replace Stripe's subscription snapshot for one customer.

    The webhook obtains this snapshot from Stripe's API rather than trusting
    an individual (possibly delayed) event payload. That makes duplicate and
    out-of-order lifecycle deliveries converge on Stripe's current state and
    correctly handles more than one subscription for the same customer.
    """
    now = _now()
    conn = get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        old_org_ids = {
            row["org_id"]
            for row in conn.execute(
                "SELECT org_id FROM stripe_subscriptions WHERE user_id=? AND org_id IS NOT NULL",
                (user_id,),
            ).fetchall()
        }
        conn.execute("DELETE FROM stripe_subscriptions WHERE user_id=?", (user_id,))
        for sub in subscriptions:
            conn.execute(
                "INSERT INTO stripe_subscriptions "
                "(subscription_id, user_id, customer_id, plan, status, org_id, seat_limit, synced_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    sub["subscription_id"], user_id, customer_id, sub["plan"], sub["status"],
                    sub.get("org_id"), sub.get("seat_limit"), now,
                ),
            )
        active = [s for s in subscriptions if s["status"] in {"active", "trialing", "past_due"}]
        paid_plan = max((s["plan"] for s in active), key=lambda p: _PLAN_RANK[p], default="free")
        chosen = next((s for s in active if s["plan"] == paid_plan), None)
        conn.execute(
            "UPDATE users SET plan=?, stripe_customer_id=?, stripe_subscription_id=?, "
            "stripe_subscription_status=?, stripe_plan_updated_at=? WHERE id=?",
            (
                paid_plan,
                customer_id,
                chosen["subscription_id"] if chosen else None,
                chosen["status"] if chosen else None,
                now,
                user_id,
            ),
        )
        active_org_ids = {s.get("org_id") for s in active if s.get("org_id")}
        for org_id in old_org_ids - active_org_ids:
            conn.execute("UPDATE orgs SET seat_limit=NULL WHERE id=?", (org_id,))
        for sub in active:
            if sub.get("org_id") and sub.get("seat_limit") is not None:
                conn.execute("UPDATE orgs SET seat_limit=? WHERE id=?", (sub["seat_limit"], sub["org_id"]))
        conn.commit()
        return paid_plan
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def stripe_subscription_org_id(subscription_id: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT org_id FROM stripe_subscriptions WHERE subscription_id=?", (subscription_id,)
        ).fetchone()
        return row["org_id"] if row and row["org_id"] else None
    finally:
        conn.close()


def bind_stripe_subscription_org(subscription_id: str, user_id: str, org_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE stripe_subscriptions SET org_id=? WHERE subscription_id=? AND user_id=? AND plan='team' "
            "AND (org_id IS NULL OR org_id=?)",
            (org_id, subscription_id, user_id, org_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def stripe_event_processed(event_id: str) -> bool:
    conn = get_conn()
    try:
        return conn.execute(
            "SELECT 1 FROM stripe_webhook_events WHERE event_id=?", (event_id,)
        ).fetchone() is not None
    finally:
        conn.close()


def record_stripe_event(event_id: str, event_type: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO stripe_webhook_events (event_id, event_type, processed_at) VALUES (?, ?, ?)",
            (event_id, event_type, _now()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def get_user_by_stripe_customer(customer_id: str) -> dict | None:
    if not customer_id:
        return None
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE stripe_customer_id=?", (customer_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_users_with_stripe_customer() -> list[dict]:
    conn = get_conn()
    try:
        return [
            dict(row)
            for row in conn.execute(
                "SELECT * FROM users WHERE stripe_customer_id IS NOT NULL ORDER BY created_at ASC"
            ).fetchall()
        ]
    finally:
        conn.close()


def increment_route_usage(user_id: str) -> int:
    """Count one route against the user's current calendar month and return
    the new total. Kept in a bare (user_id, month, count) table rather than on
    route_events so per-user billing state never links back to route history."""
    month = _usage_month()
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO route_usage (user_id, month, count) VALUES (?, ?, 1) "
            "ON CONFLICT(user_id, month) DO UPDATE SET count = count + 1",
            (user_id, month),
        )
        row = conn.execute(
            "SELECT count FROM route_usage WHERE user_id=? AND month=?", (user_id, month)
        ).fetchone()
        conn.commit()
        return int(row["count"])
    finally:
        conn.close()


def get_route_usage(user_id: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT count FROM route_usage WHERE user_id=? AND month=?",
            (user_id, _usage_month()),
        ).fetchone()
        return int(row["count"]) if row else 0
    finally:
        conn.close()


def create_oauth_client(client_id: str, client_info: dict) -> None:
    """Store a dynamically-registered MCP OAuth client verbatim (client_secret,
    token_endpoint_auth_method, redirect_uris, etc. -- whatever the `mcp` SDK's
    registration handler assigned) so get_oauth_client can hand it back
    unchanged for the SDK's own client authentication checks."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO oauth_clients (client_id, client_info, created_at) VALUES (?, ?, ?)",
            (client_id, json.dumps(client_info), _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_oauth_client(client_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT client_info FROM oauth_clients WHERE client_id=?", (client_id,)).fetchone()
        return json.loads(row["client_info"]) if row else None
    finally:
        conn.close()


def create_mcp_auth_code(
    code: str, client_id: str, code_challenge: str, redirect_uri: str, scopes: list[str], user_id: str, expires_at: str
) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO mcp_auth_codes "
            "(code, client_id, code_challenge, redirect_uri, scopes, user_id, created_at, expires_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (code, client_id, code_challenge, redirect_uri, json.dumps(scopes), user_id, _now(), expires_at),
        )
        conn.commit()
    finally:
        conn.close()


def _mcp_auth_code_row(row: sqlite3.Row) -> dict:
    entry = dict(row)
    entry["scopes"] = json.loads(entry["scopes"])
    return entry


def peek_mcp_auth_code(code: str) -> dict | None:
    """Non-destructive lookup -- the `mcp` SDK validates expiry/redirect_uri/
    PKCE itself against this before ever calling exchange, so this must not
    consume the code (see mcp_oauth.py's /codes/{code} and /token split)."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM mcp_auth_codes WHERE code=?", (code,)).fetchone()
        return _mcp_auth_code_row(row) if row else None
    finally:
        conn.close()


def consume_mcp_auth_code(code: str, client_id: str) -> dict | None:
    """Atomically load and consume a not-yet-used, not-expired auth code
    belonging to `client_id`, guarding against replay."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT * FROM mcp_auth_codes WHERE code=? AND client_id=? AND consumed_at IS NULL AND expires_at > ?",
            (code, client_id, _now()),
        ).fetchone()
        if row is None:
            return None
        conn.execute("UPDATE mcp_auth_codes SET consumed_at=? WHERE code=?", (_now(), code))
        conn.commit()
        return _mcp_auth_code_row(row)
    finally:
        conn.close()


def list_route_events_for_user(user_id: str, limit: int = 100) -> list[dict]:
    """Return the current user's privacy-safe route metadata projection."""
    limit = max(1, min(int(limit), 200))
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT id, created_at, client, client_version, query_chars, tier,
                   skill_id, skill_name, skill_url, latency_ms, skill_find_ms,
                   retrieval_ms, rerank_ms, content_ms, result_count,
                   input_tokens, hint_tokens, candidate_tokens, content_tokens, capsule_tokens,
                   injected_tokens, response_tokens, guard_delivery, capsule_chars,
                   meaningfulness_score, config_version, outcome,
                   outcome_at, feedback_source, warnings, skip_reason
            FROM route_events
            WHERE user_id=?
            ORDER BY created_at DESC
            LIMIT ?
            """,
            (user_id, limit),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["warnings"] = json.loads(event["warnings"]) if event.get("warnings") else []
            events.append(event)
        return events
    finally:
        conn.close()


def _stripe_customer_url(customer_id: str | None) -> str | None:
    if not customer_id or not re.fullmatch(r"cus_[A-Za-z0-9]+", customer_id):
        return None
    prefix = "test/" if os.getenv("STRIPE_DASHBOARD_TEST_MODE", "").lower() in {"1", "true", "yes"} else ""
    return f"https://dashboard.stripe.com/{prefix}customers/{customer_id}"


def admin_user_stats(q: str = "", limit: int = 50, offset: int = 0) -> list[dict]:
    """Per-user rollup for the admin dashboard: signup info, login provider,
    plan/quota/billing state, tier/outcome breakdown, average latency/tokens,
    and favorites/installs/private-skill/pro-feature counts. One row per user,
    newest signup first."""
    month = _usage_month()
    conn = get_conn()
    try:
        limit = max(1, min(int(limit), 200))
        offset = max(0, int(offset))
        if q.strip():
            needle = f"%{q.strip()}%"
            users = conn.execute(
                "SELECT * FROM users WHERE email LIKE ? COLLATE NOCASE OR name LIKE ? COLLATE NOCASE "
                "ORDER BY created_at DESC LIMIT ? OFFSET ?",
                (needle, needle, limit, offset),
            ).fetchall()
        else:
            users = conn.execute("SELECT * FROM users ORDER BY created_at DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        stats = []
        for user in users:
            user_id = user["id"]
            provider = conn.execute(
                "SELECT provider FROM oauth_identities WHERE user_id=? ORDER BY created_at ASC LIMIT 1", (user_id,)
            ).fetchone()
            totals = conn.execute(
                """
                SELECT
                    COUNT(*) AS run_count,
                    MAX(created_at) AS last_run,
                    AVG(latency_ms) AS avg_latency_ms,
                    AVG(skill_find_ms) AS avg_skill_find_ms,
                    AVG(injected_tokens) AS avg_injected_tokens,
                    AVG(response_tokens) AS avg_response_tokens
                FROM route_events WHERE user_id=?
                """,
                (user_id,),
            ).fetchone()
            tiers = conn.execute(
                "SELECT tier, COUNT(*) AS c FROM route_events WHERE user_id=? GROUP BY tier", (user_id,)
            ).fetchall()
            outcomes = conn.execute(
                "SELECT outcome, COUNT(*) AS c FROM route_events WHERE user_id=? AND outcome IS NOT NULL GROUP BY outcome",
                (user_id,),
            ).fetchall()
            favorites_count = conn.execute("SELECT COUNT(*) FROM favorites WHERE user_id=?", (user_id,)).fetchone()[0]
            installs_count = conn.execute("SELECT COUNT(*) FROM installs WHERE user_id=?", (user_id,)).fetchone()[0]
            private_skills_count = conn.execute(
                "SELECT COUNT(*) FROM private_skills WHERE owner_user_id=?", (user_id,)
            ).fetchone()[0]
            pins_count = conn.execute("SELECT COUNT(*) FROM skill_pins WHERE user_id=?", (user_id,)).fetchone()[0]
            watches_count = conn.execute("SELECT COUNT(*) FROM skill_watches WHERE user_id=?", (user_id,)).fetchone()[0]
            collections_count = conn.execute(
                "SELECT COUNT(*) FROM collections WHERE owner_user_id=?", (user_id,)
            ).fetchone()[0]
            usage_row = conn.execute(
                "SELECT count FROM route_usage WHERE user_id=? AND month=?", (user_id, month)
            ).fetchone()
            org_rows = conn.execute(
                "SELECT o.id AS org_id, o.name, m.role FROM org_members m "
                "JOIN orgs o ON o.id = m.org_id WHERE m.user_id=? ORDER BY m.created_at ASC",
                (user_id,),
            ).fetchall()
            access = access_details_for_user(dict(user))
            plan = access["plan"]
            plan_cap = FREE_ROUTES_PER_MONTH if plan == "free" else PRO_ROUTES_PER_MONTH
            routes_used = int(usage_row["count"]) if usage_row else 0
            if plan == "team":
                team_pool = team_route_pool(user_id)
                if team_pool is not None:
                    routes_used, plan_cap = team_pool
            stats.append(
                {
                    "id": user_id,
                    "email": user["email"],
                    "name": user["name"],
                    "created_at": user["created_at"],
                    "login_provider": provider["provider"] if provider else None,
                    "paid_plan": access["paid_plan"],
                    "effective_plan": plan,
                    "plan_source": access["plan_source"],
                    "complimentary_plan": access["complimentary_plan"],
                    "complimentary_expires_at": access["complimentary_expires_at"],
                    "team_org_id": access["team_org_id"],
                    "billing_linked": bool(user["stripe_customer_id"]),
                    "stripe_customer_url": _stripe_customer_url(user["stripe_customer_id"]),
                    "stripe_subscription_status": user["stripe_subscription_status"],
                    "stripe_plan_updated_at": user["stripe_plan_updated_at"],
                    "complimentary_entitlements": list_complimentary_entitlements(user_id),
                    "routes_this_month": routes_used,
                    "routes_limit": plan_cap or None,
                    "orgs": [{"org_id": row["org_id"], "name": row["name"], "role": row["role"]} for row in org_rows],
                    "run_count": totals["run_count"] or 0,
                    "last_run": totals["last_run"],
                    "avg_latency_ms": round(totals["avg_latency_ms"]) if totals["avg_latency_ms"] is not None else None,
                    "avg_skill_find_ms": round(totals["avg_skill_find_ms"])
                    if totals["avg_skill_find_ms"] is not None
                    else None,
                    "avg_injected_tokens": round(totals["avg_injected_tokens"])
                    if totals["avg_injected_tokens"] is not None
                    else None,
                    "avg_response_tokens": round(totals["avg_response_tokens"])
                    if totals["avg_response_tokens"] is not None
                    else None,
                    "tiers": {row["tier"] or "unknown": row["c"] for row in tiers},
                    "outcomes": {row["outcome"]: row["c"] for row in outcomes},
                    "favorites_count": favorites_count,
                    "installs_count": installs_count,
                    "private_skills_count": private_skills_count,
                    "pins_count": pins_count,
                    "watches_count": watches_count,
                    "collections_count": collections_count,
                }
            )
        return stats
    finally:
        conn.close()


def admin_plan_summary() -> dict:
    """Business rollup for the admin dashboard: plan mix, Stripe billing
    linkage, org/seat totals, and current-month route usage. Aggregate counts
    only -- same metadata-only stance as the rest of the admin surface."""
    month = _usage_month()
    conn = get_conn()
    try:
        plans = {plan: 0 for plan in USER_PLANS}
        for row in conn.execute("SELECT COALESCE(plan, 'free') AS plan, COUNT(*) AS c FROM users GROUP BY 1"):
            plans[row["plan"]] = row["c"]
        effective_plans = {plan: 0 for plan in USER_PLANS}
        for user in conn.execute("SELECT * FROM users"):
            effective_plans[access_details_for_user(dict(user))["plan"]] += 1
        active_comps = conn.execute(
            "SELECT COUNT(*) FROM complimentary_entitlements WHERE revoked_at IS NULL AND expires_at > ?",
            (_now(),),
        ).fetchone()[0]
        billing_linked = conn.execute(
            "SELECT COUNT(*) FROM users WHERE stripe_customer_id IS NOT NULL"
        ).fetchone()[0]
        orgs_row = conn.execute(
            "SELECT COUNT(*) AS orgs, COALESCE(SUM(COALESCE(seat_limit, ?)), 0) AS seats FROM orgs",
            (TEAM_INCLUDED_MEMBERS,),
        ).fetchone()
        org_members = conn.execute("SELECT COUNT(*) FROM org_members").fetchone()[0]
        usage = conn.execute(
            "SELECT COALESCE(SUM(count), 0) AS routes, COUNT(*) AS users FROM route_usage WHERE month=?",
            (month,),
        ).fetchone()
        free_users_at_quota = 0
        if FREE_ROUTES_PER_MONTH > 0:
            for row in conn.execute(
                "SELECT u.*, ru.count AS route_count FROM route_usage ru JOIN users u ON u.id=ru.user_id "
                "WHERE ru.month=? AND ru.count >= ?",
                (month, FREE_ROUTES_PER_MONTH),
            ):
                if access_details_for_user(dict(row))["plan"] == "free":
                    free_users_at_quota += 1
        return {
            "month": month,
            "paid_plans": plans,
            "effective_plans": effective_plans,
            "paying_users": plans.get("pro", 0) + plans.get("team", 0),
            "active_complimentary_entitlements": active_comps,
            "billing_linked_users": billing_linked,
            "orgs": orgs_row["orgs"],
            "org_seats": int(orgs_row["seats"]),
            "org_members": org_members,
            "routes_this_month": int(usage["routes"]),
            "users_routing_this_month": usage["users"],
            "free_users_at_quota": free_users_at_quota,
        }
    finally:
        conn.close()


def admin_recent_events(limit: int = 100) -> list[dict]:
    """Most recent route_events across every user, joined with the user's
    email, for the admin dashboard's live feed. Route analytics are
    metadata-only (see ROUTE_EVENT_FORBIDDEN_LEGACY_COLUMNS) -- no raw
    prompt text to show, only tier/skill/timing/outcome."""
    limit = max(1, min(int(limit), 500))
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT r.id, r.created_at, r.client, r.client_version,
                   r.query_chars, r.tier, r.skill_id, r.skill_name,
                   r.skill_url, r.latency_ms, r.skill_find_ms,
                   r.retrieval_ms, r.rerank_ms, r.content_ms,
                   r.result_count, r.input_tokens, r.hint_tokens,
                   r.candidate_tokens, r.content_tokens, r.capsule_tokens, r.injected_tokens,
                   r.response_tokens, r.guard_delivery, r.capsule_chars,
                   r.meaningfulness_score, r.config_version, r.outcome,
                   r.outcome_at, r.feedback_source, r.warnings,
                   u.email AS user_email
            FROM route_events r
            LEFT JOIN users u ON u.id = r.user_id
            ORDER BY r.created_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["warnings"] = json.loads(event["warnings"]) if event.get("warnings") else []
            events.append(event)
        return events
    finally:
        conn.close()


def list_skills_catalog(q: str = "", limit: int = 50, offset: int = 0, sort: str = "popular") -> dict:
    """Paginated public-safe slice of the skills table for the site's
    account-only browse page -- never raw/embedding columns, those stay
    internal. `q` is a case-insensitive substring match on name/description.
    Only 'active' skills are shown -- rejected/duplicate rows have no place
    in a browse experience. `sort` is 'popular' (GitHub stars, when known)
    or 'recent' (discovery order)."""
    limit = max(1, min(int(limit), 200))
    offset = max(0, int(offset))
    clauses = ["quality_status = 'active'"]
    params: list = []
    if q:
        clauses.append("(name LIKE ? COLLATE NOCASE OR description LIKE ? COLLATE NOCASE)")
        needle = f"%{q}%"
        params.extend([needle, needle])
    where = "WHERE " + " AND ".join(clauses)
    order_by = (
        "COALESCE(json_extract(raw, '$.stars'), 0) DESC, discovered_at DESC, id"
        if sort == "popular"
        else "discovered_at DESC, id"
    )
    conn = get_conn()
    try:
        total = conn.execute(f"SELECT COUNT(*) FROM skills {where}", params).fetchone()[0]
        rows = conn.execute(
            f"""
            SELECT id, name, description, source, url, tags, discovered_at, risk_score
            FROM skills {where}
            ORDER BY {order_by}
            LIMIT ? OFFSET ?
            """,
            [*params, limit, offset],
        ).fetchall()
        skills = []
        for row in rows:
            skill = dict(row)
            try:
                skill["tags"] = json.loads(skill["tags"]) if skill.get("tags") else []
            except (TypeError, ValueError):
                skill["tags"] = []
            skills.append(skill)
        return {"total": total, "skills": skills}
    finally:
        conn.close()


def add_favorite(user_id: str, skill_id: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT OR IGNORE INTO favorites (id, user_id, skill_id, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, skill_id, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def remove_favorite(user_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM favorites WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_favorites(user_id: str) -> list[dict]:
    """Favorited skills joined with their current skill row, newest first."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT s.*, f.created_at AS favorited_at FROM favorites f "
            "JOIN skills s ON s.id = f.skill_id WHERE f.user_id=? ORDER BY f.created_at DESC",
            (user_id,),
        ).fetchall()
        return [_row_to_dict(r, "skills", None) | {"favorited_at": r["favorited_at"]} for r in rows]
    finally:
        conn.close()


def record_install(user_id: str, skill_id: str | None, skill_url: str | None, target: str) -> None:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO installs (id, user_id, skill_id, skill_url, target, installed_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), user_id, skill_id, skill_url, target, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def list_installs(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM installs WHERE user_id=? ORDER BY installed_at DESC", (user_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_private_skill(owner_user_id: str, name: str, description: str | None, content: str) -> dict:
    conn = get_conn()
    try:
        skill_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO private_skills (id, owner_user_id, name, description, content, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (skill_id, owner_user_id, name, description, content, _now()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM private_skills WHERE id=?", (skill_id,)).fetchone())
    finally:
        conn.close()


def count_private_skills(owner_user_id: str) -> int:
    """Personal submissions only (org skills don't count against the free cap)."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT COUNT(*) FROM private_skills WHERE owner_user_id=? AND org_id IS NULL",
            (owner_user_id,),
        ).fetchone()
        return int(row[0])
    finally:
        conn.close()


def list_private_skills(owner_user_id: str) -> list[dict]:
    """The caller's personal submissions only; org skills list via list_org_skills."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM private_skills WHERE owner_user_id=? AND org_id IS NULL ORDER BY created_at DESC",
            (owner_user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def remove_private_skill(owner_user_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM private_skills WHERE id=? AND owner_user_id=? AND org_id IS NULL",
            (skill_id, owner_user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_routable_private_skills(user_id: str) -> list[dict]:
    """Everything /route may match for this caller: their own personal
    submissions plus every skill shared by an org they belong to. Org skills
    come first so a tie falls to the org standard, not a personal copy."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.* FROM private_skills p JOIN org_members m ON m.org_id = p.org_id "
            "WHERE m.user_id=? AND COALESCE(p.status, 'approved') = 'approved' "
            "UNION ALL "
            "SELECT * FROM private_skills WHERE owner_user_id=? AND org_id IS NULL "
            "ORDER BY created_at DESC",
            (user_id, user_id),
        ).fetchall()
        org_rows = [dict(r) for r in rows if r["org_id"]]
        personal_rows = [dict(r) for r in rows if not r["org_id"]]
        return org_rows + personal_rows
    finally:
        conn.close()


def get_user_by_email(email: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM users WHERE email=?", (email.strip(),)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def create_org(name: str, owner_user_id: str) -> dict:
    conn = get_conn()
    try:
        org_id = str(uuid.uuid4())
        now = _now()
        conn.execute(
            "INSERT INTO orgs (id, name, owner_user_id, created_at) VALUES (?, ?, ?, ?)",
            (org_id, name, owner_user_id, now),
        )
        conn.execute(
            "INSERT INTO org_members (id, org_id, user_id, role, created_at) VALUES (?, ?, ?, 'owner', ?)",
            (str(uuid.uuid4()), org_id, owner_user_id, now),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone())
    finally:
        conn.close()


def get_org(org_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM orgs WHERE id=?", (org_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def org_seat_limit(org: dict) -> int:
    return int(org.get("seat_limit") or TEAM_INCLUDED_MEMBERS)


def org_member_count(org_id: str) -> int:
    conn = get_conn()
    try:
        row = conn.execute("SELECT COUNT(*) FROM org_members WHERE org_id=?", (org_id,)).fetchone()
        return int(row[0])
    finally:
        conn.close()


def set_org_seat_limit(org_id: str, seats: int | None) -> bool:
    """None resets the org to the plan's included seat count."""
    if seats is not None and seats < 1:
        raise ValueError("seat limit must be at least 1")
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE orgs SET seat_limit=? WHERE id=?",
            (int(seats) if seats is not None else None, org_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def team_route_pool(user_id: str) -> tuple[int, int] | None:
    """Pooled fair-use quota for a team-plan caller: each workspace shares
    seat_limit x PRO_ROUTES_PER_MONTH across its members this month. Returns
    the (used, pool) pair of the caller's org with the most headroom, or None
    when they belong to no org (the per-user pro cap applies instead)."""
    month = _usage_month()
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT COALESCE(o.seat_limit, ?) AS seat_limit,
                   (SELECT COALESCE(SUM(ru.count), 0) FROM route_usage ru
                     WHERE ru.month = ?
                       AND ru.user_id IN (SELECT user_id FROM org_members WHERE org_id = o.id)
                   ) AS used
            FROM orgs o JOIN org_members m ON m.org_id = o.id
            WHERE m.user_id = ?
            """,
            (TEAM_INCLUDED_MEMBERS, month, user_id),
        ).fetchall()
        if not rows:
            return None
        best = max(rows, key=lambda r: int(r["seat_limit"]) * PRO_ROUTES_PER_MONTH - int(r["used"]))
        return int(best["used"]), int(best["seat_limit"]) * PRO_ROUTES_PER_MONTH
    finally:
        conn.close()


def list_orgs_for_user(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT o.*, m.role FROM orgs o JOIN org_members m ON m.org_id = o.id "
            "WHERE m.user_id=? ORDER BY o.created_at",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def org_role(org_id: str, user_id: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT role FROM org_members WHERE org_id=? AND user_id=?", (org_id, user_id)
        ).fetchone()
        return row["role"] if row else None
    finally:
        conn.close()


def add_org_member(org_id: str, user_id: str, role: str = "member") -> bool:
    if role not in ORG_ROLES:
        raise ValueError(f"unknown org role {role!r}; choose one of {', '.join(ORG_ROLES)}")
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO org_members (id, org_id, user_id, role, created_at) VALUES (?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), org_id, user_id, role, _now()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_org_member(org_id: str, user_id: str) -> bool:
    """Remove a membership. The owner's own row stays -- an org must keep its
    owner; ownership transfer is deliberately not supported yet."""
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM org_members WHERE org_id=? AND user_id=? AND role != 'owner'",
            (org_id, user_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_org_members(org_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT u.id, u.email, u.name, m.role, m.created_at AS member_since "
            "FROM org_members m JOIN users u ON u.id = m.user_id "
            "WHERE m.org_id=? ORDER BY m.created_at",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_org_skill(
    org_id: str,
    uploader_user_id: str,
    name: str,
    description: str | None,
    content: str,
    status: str | None = None,
) -> dict:
    conn = get_conn()
    try:
        skill_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO private_skills (id, owner_user_id, name, description, content, created_at, org_id, status) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (skill_id, uploader_user_id, name, description, content, _now(), org_id, status),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM private_skills WHERE id=?", (skill_id,)).fetchone())
    finally:
        conn.close()


def approve_org_skill(org_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE private_skills SET status='approved' WHERE id=? AND org_id=? AND status='pending'",
            (skill_id, org_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_org_skills(org_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT * FROM private_skills WHERE org_id=? ORDER BY created_at DESC", (org_id,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def remove_org_skill(org_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM private_skills WHERE id=? AND org_id=?", (skill_id, org_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- skill versions, pins, and change alerts (pro plan) ---------------------


def get_skill_hash(skill_id: str) -> str | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT content_hash FROM skills WHERE id=?", (skill_id,)).fetchone()
        return row["content_hash"] if row else None
    finally:
        conn.close()


def list_skill_versions(skill_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT content_hash, seen_at FROM skill_versions WHERE skill_id=? ORDER BY seen_at DESC",
            (skill_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def skill_version_exists(skill_id: str, content_hash: str) -> bool:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT 1 FROM skill_versions WHERE skill_id=? AND content_hash=?",
            (skill_id, content_hash),
        ).fetchone()
        if row:
            return True
        current = conn.execute(
            "SELECT 1 FROM skills WHERE id=? AND content_hash=?", (skill_id, content_hash)
        ).fetchone()
        return current is not None
    finally:
        conn.close()


def pin_skill(user_id: str, skill_id: str, content_hash: str) -> dict:
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO skill_pins (id, user_id, skill_id, content_hash, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, skill_id) DO UPDATE SET content_hash=excluded.content_hash, created_at=excluded.created_at",
            (str(uuid.uuid4()), user_id, skill_id, content_hash, _now()),
        )
        conn.commit()
        return dict(
            conn.execute(
                "SELECT * FROM skill_pins WHERE user_id=? AND skill_id=?", (user_id, skill_id)
            ).fetchone()
        )
    finally:
        conn.close()


def unpin_skill(user_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM skill_pins WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_pins(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT skill_id, content_hash, created_at FROM skill_pins WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def get_pin(user_id: str, skill_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT skill_id, content_hash FROM skill_pins WHERE user_id=? AND skill_id=?",
            (user_id, skill_id),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def watch_skill(user_id: str, skill_id: str) -> dict:
    """Subscribe to change alerts; the watch remembers the hash the user last
    saw so /alerts can report anything that moved since."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT content_hash FROM skills WHERE id=?", (skill_id,)).fetchone()
        current = row["content_hash"] if row else None
        conn.execute(
            "INSERT INTO skill_watches (id, user_id, skill_id, last_seen_hash, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, skill_id) DO NOTHING",
            (str(uuid.uuid4()), user_id, skill_id, current, _now()),
        )
        conn.commit()
        return dict(
            conn.execute(
                "SELECT skill_id, last_seen_hash, created_at FROM skill_watches WHERE user_id=? AND skill_id=?",
                (user_id, skill_id),
            ).fetchone()
        )
    finally:
        conn.close()


def unwatch_skill(user_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute("DELETE FROM skill_watches WHERE user_id=? AND skill_id=?", (user_id, skill_id))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_watches(user_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT skill_id, last_seen_hash, created_at FROM skill_watches WHERE user_id=? ORDER BY created_at DESC",
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def list_skill_alerts(user_id: str) -> list[dict]:
    """Watched skills whose current content hash differs from the hash the
    user last acknowledged."""
    conn = get_conn()
    try:
        rows = conn.execute(
            """
            SELECT w.skill_id, s.name, s.url, w.last_seen_hash, s.content_hash AS current_hash
            FROM skill_watches w JOIN skills s ON s.id = w.skill_id
            WHERE w.user_id=? AND s.content_hash IS NOT NULL
              AND COALESCE(w.last_seen_hash, '') != s.content_hash
            ORDER BY w.created_at DESC
            """,
            (user_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def ack_skill_alert(user_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "UPDATE skill_watches SET last_seen_hash=(SELECT content_hash FROM skills WHERE id=?) "
            "WHERE user_id=? AND skill_id=?",
            (skill_id, user_id, skill_id),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


# --- collections (pro personal; team shared) --------------------------------


def create_collection(owner_user_id: str, name: str, org_id: str | None = None) -> dict:
    conn = get_conn()
    try:
        collection_id = str(uuid.uuid4())
        conn.execute(
            "INSERT INTO collections (id, owner_user_id, org_id, name, created_at) VALUES (?, ?, ?, ?, ?)",
            (collection_id, owner_user_id, org_id, name, _now()),
        )
        conn.commit()
        return dict(conn.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone())
    finally:
        conn.close()


def get_collection(collection_id: str) -> dict | None:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM collections WHERE id=?", (collection_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_collections(user_id: str) -> list[dict]:
    """Personal collections plus every collection shared by the caller's orgs."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT c.* FROM collections c JOIN org_members m ON m.org_id = c.org_id AND m.user_id=? "
            "UNION ALL "
            "SELECT * FROM collections WHERE owner_user_id=? AND org_id IS NULL "
            "ORDER BY created_at",
            (user_id, user_id),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def delete_collection(collection_id: str) -> bool:
    conn = get_conn()
    try:
        conn.execute("DELETE FROM collection_skills WHERE collection_id=?", (collection_id,))
        cur = conn.execute("DELETE FROM collections WHERE id=?", (collection_id,))
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def add_collection_skill(collection_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "INSERT OR IGNORE INTO collection_skills (id, collection_id, skill_id, created_at) VALUES (?, ?, ?, ?)",
            (str(uuid.uuid4()), collection_id, skill_id, _now()),
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def remove_collection_skill(collection_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM collection_skills WHERE collection_id=? AND skill_id=?", (collection_id, skill_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_collection_skills(collection_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT cs.skill_id, cs.created_at, s.name, s.url, s.description "
            "FROM collection_skills cs LEFT JOIN skills s ON s.id = cs.skill_id "
            "WHERE cs.collection_id=? ORDER BY cs.created_at",
            (collection_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# --- routing preferences and org allow/block policies -----------------------


def get_routing_preferences(user_id: str) -> dict:
    conn = get_conn()
    try:
        row = conn.execute("SELECT * FROM routing_preferences WHERE user_id=?", (user_id,)).fetchone()
        if row is None:
            return {"excluded_skill_ids": [], "excluded_sources": []}
        return {
            "excluded_skill_ids": json.loads(row["excluded_skill_ids"] or "[]"),
            "excluded_sources": json.loads(row["excluded_sources"] or "[]"),
        }
    finally:
        conn.close()


def set_routing_preferences(user_id: str, excluded_skill_ids: list[str], excluded_sources: list[str]) -> dict:
    """Server-stored preferences are what makes exclusions cross-agent: every
    connector on every machine routes through the same account, so a single
    PUT applies everywhere without client-side sync."""
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO routing_preferences (user_id, excluded_skill_ids, excluded_sources, updated_at) "
            "VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET excluded_skill_ids=excluded.excluded_skill_ids, "
            "excluded_sources=excluded.excluded_sources, updated_at=excluded.updated_at",
            (user_id, json.dumps(excluded_skill_ids), json.dumps(excluded_sources), _now()),
        )
        conn.commit()
    finally:
        conn.close()
    return get_routing_preferences(user_id)


def set_org_skill_policy(org_id: str, skill_id: str, policy: str) -> dict:
    if policy not in ORG_SKILL_POLICIES:
        raise ValueError(f"unknown policy {policy!r}; choose one of {', '.join(ORG_SKILL_POLICIES)}")
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO org_skill_policies (id, org_id, skill_id, policy, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(org_id, skill_id) DO UPDATE SET policy=excluded.policy, created_at=excluded.created_at",
            (str(uuid.uuid4()), org_id, skill_id, policy, _now()),
        )
        conn.commit()
        return dict(
            conn.execute(
                "SELECT skill_id, policy, created_at FROM org_skill_policies WHERE org_id=? AND skill_id=?",
                (org_id, skill_id),
            ).fetchone()
        )
    finally:
        conn.close()


def remove_org_skill_policy(org_id: str, skill_id: str) -> bool:
    conn = get_conn()
    try:
        cur = conn.execute(
            "DELETE FROM org_skill_policies WHERE org_id=? AND skill_id=?", (org_id, skill_id)
        )
        conn.commit()
        return cur.rowcount > 0
    finally:
        conn.close()


def list_org_skill_policies(org_id: str) -> list[dict]:
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT skill_id, policy, created_at FROM org_skill_policies WHERE org_id=? ORDER BY created_at",
            (org_id,),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def routing_filters_for_user(user_id: str) -> dict:
    """Everything /route must exclude or restrict for this caller: their own
    exclusions plus their orgs' allow/block policies. Blocks always win. If any
    org defines allow rows, public-catalog candidates are limited to that
    allowlist (team-standard-only mode); private and org skills are unaffected."""
    prefs = get_routing_preferences(user_id)
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT p.skill_id, p.policy FROM org_skill_policies p "
            "JOIN org_members m ON m.org_id = p.org_id WHERE m.user_id=?",
            (user_id,),
        ).fetchall()
    finally:
        conn.close()
    blocked = {r["skill_id"] for r in rows if r["policy"] == "block"}
    allow_rows = {r["skill_id"] for r in rows if r["policy"] == "allow"}
    return {
        "excluded_ids": set(prefs["excluded_skill_ids"]),
        "excluded_sources": set(prefs["excluded_sources"]),
        "blocked_ids": blocked,
        "allowed_ids": allow_rows or None,
    }


# --- org audit log and analytics (team plan) --------------------------------


def record_org_audit(org_id: str, actor_user_id: str | None, action: str, subject: str | None = None) -> None:
    if action not in ORG_AUDIT_ACTIONS:
        raise ValueError(f"unknown audit action {action!r}")
    conn = get_conn()
    try:
        conn.execute(
            "INSERT INTO org_audit_log (id, org_id, actor_user_id, action, subject, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (str(uuid.uuid4()), org_id, actor_user_id, action, (subject or "")[:200] or None, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def record_install_audit(user_id: str, subject: str) -> None:
    """Installs are personal, but the team plan promises an install audit log:
    log the install to every org the installer belongs to."""
    conn = get_conn()
    try:
        org_ids = [
            r["org_id"]
            for r in conn.execute("SELECT org_id FROM org_members WHERE user_id=?", (user_id,)).fetchall()
        ]
    finally:
        conn.close()
    for org_id in org_ids:
        record_org_audit(org_id, user_id, "skill_installed", subject)


def list_org_audit(org_id: str, limit: int = 100) -> list[dict]:
    limit = max(1, min(int(limit or 100), 500))
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT a.action, a.subject, a.created_at, u.email AS actor_email "
            "FROM org_audit_log a LEFT JOIN users u ON u.id = a.actor_user_id "
            "WHERE a.org_id=? ORDER BY a.created_at DESC, a.id DESC LIMIT ?",
            (org_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def _route_event_rollup(conn: sqlite3.Connection, where: str, params: list) -> dict:
    total = int(conn.execute(f"SELECT COUNT(*) FROM route_events WHERE {where}", params).fetchone()[0])
    tiers = {
        row["tier"] or "none": row["count"]
        for row in conn.execute(
            f"SELECT tier, COUNT(*) AS count FROM route_events WHERE {where} GROUP BY tier", params
        ).fetchall()
    }
    outcomes = {
        row["outcome"] or "pending": row["count"]
        for row in conn.execute(
            f"SELECT outcome, COUNT(*) AS count FROM route_events WHERE {where} GROUP BY outcome", params
        ).fetchall()
    }
    top_skills = [
        dict(r)
        for r in conn.execute(
            f"""
            SELECT skill_name, skill_url, COUNT(*) AS count,
                   SUM(CASE WHEN outcome IN ('used', 'installed') THEN 1 ELSE 0 END) AS positive_count
            FROM route_events
            WHERE {where} AND skill_name IS NOT NULL AND skill_name != ''
            GROUP BY skill_name, skill_url
            ORDER BY count DESC, positive_count DESC, skill_name ASC
            LIMIT 10
            """,
            params,
        ).fetchall()
    ]
    for skill in top_skills:
        skill["count"] = int(skill["count"] or 0)
        skill["positive_count"] = int(skill["positive_count"] or 0)
    return {"total_routes": total, "tiers": tiers, "outcomes": outcomes, "top_skills": top_skills}


def user_route_analytics(user_id: str, days: int = 30) -> dict:
    days = max(1, min(int(days or 30), 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_conn()
    try:
        rollup = _route_event_rollup(conn, "user_id = ? AND created_at >= ?", [user_id, cutoff])
    finally:
        conn.close()
    rollup["window_days"] = days
    rollup["routes_this_month"] = get_route_usage(user_id)
    return rollup


def org_route_analytics(org_id: str, days: int = 30) -> dict:
    days = max(1, min(int(days or 30), 365))
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    conn = get_conn()
    try:
        member_ids = [
            r["user_id"]
            for r in conn.execute("SELECT user_id FROM org_members WHERE org_id=?", (org_id,)).fetchall()
        ]
        placeholders = ",".join("?" for _ in member_ids) or "''"
        rollup = _route_event_rollup(
            conn, f"user_id IN ({placeholders}) AND created_at >= ?", [*member_ids, cutoff]
        )
        month = _usage_month()
        pooled_used = int(
            conn.execute(
                f"SELECT COALESCE(SUM(count), 0) FROM route_usage WHERE month=? AND user_id IN ({placeholders})",
                [month, *member_ids],
            ).fetchone()[0]
        )
    finally:
        conn.close()
    org = get_org(org_id) or {}
    rollup["window_days"] = days
    rollup["members"] = len(member_ids)
    rollup["pooled_routes_this_month"] = pooled_used
    rollup["pooled_route_limit"] = org_seat_limit(org) * PRO_ROUTES_PER_MONTH if PRO_ROUTES_PER_MONTH > 0 else None
    return rollup
