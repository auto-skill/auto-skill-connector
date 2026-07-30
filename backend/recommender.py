"""Semantic skill recommender: hybrid pgvector+FTS retrieval with an optional
local Ollama LLM for conversational quality.

Self-contained APIRouter so scraper.py only needs:
    from recommender import router as recommender_router
    app.include_router(recommender_router)

Endpoints:
  POST /chat  {"messages":[{"role","content"}...], "prev_options":[urls]}
              -> {"type":"recommend","skill":{...},"message":...}
               | {"type":"clarify","message":...,"options":[...]}
               | {"type":"none","message":...}
  POST /find-semantic {"q": ..., "limit": ...}  body-based public search
  POST /route {"task": ..., "guard_mode": "hybrid", "supports_isolation": false}

Also runs a background loop that embeds any skills rows missing embeddings,
so freshly scraped skills become semantically searchable within minutes.
"""
import asyncio
import hashlib
import json
import os
import re
import time
import uuid
from datetime import datetime, timezone
from math import ceil
from typing import Literal
from urllib.parse import urlsplit

import httpx
from fastapi import APIRouter, Header, HTTPException, Request, Response
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import auth
from context_guard import (
    DEFAULT_CAPSULE_CHARS,
    DEFAULT_INLINE_CHARS,
    POLICY_VERSION as CONTEXT_GUARD_POLICY,
    build_context_guard,
    estimate_tokens as estimate_guard_tokens,
)
from embeddings import LibraryContent, build_embed_text, embed_text_hash, embed_texts
import local_store as store
from query_compiler import CompiledIntent, compile_intent_query, skills_sh_query
from quality import (
    CONFIG_VERSION,
    NAME_STOPWORDS,
    PLATFORM_ALIASES,
    _contains_alias,
    content_hash,
    content_digest,
    has_valid_skill_frontmatter,
    is_non_task_prompt,
    platform_mentions,
    rerank_candidates,
    skill_capability_flags,
    tier_for_ranked_candidates,
    tier_for_prompt,
)
from skills_sh_catalog import SkillsShCatalogError, default_catalog

# Storage moved local 2026-07-05 -- recommender.py always runs embedded inside
# scraper.py's process (same app/port), which now serves local_api.py's
# Supabase-shaped REST+RPC surface backed by local_skills.db.
LOCAL_DB_URL = os.getenv("LOCAL_DB_URL", f"http://127.0.0.1:{os.getenv('LOCAL_DB_PORT', '8000')}").rstrip("/")
HEADERS = {
    "Content-Type": "application/json",
    "Prefer": "resolution=merge-duplicates",
}

OLLAMA_URL = os.getenv("OLLAMA_URL", "http://localhost:11434").rstrip("/")
OLLAMA_MODEL = os.getenv("OLLAMA_MODEL", "llama3.2:3b")
ENABLE_OLLAMA_CHAT = os.getenv("ENABLE_OLLAMA_CHAT", "").lower() in {"1", "true", "yes"}
AUTO_START_EMBEDDER = os.getenv("AUTO_START_EMBEDDER", "1").lower() not in {"0", "false", "no"}
CONTEXT_GUARD_ENABLED = os.getenv("AUTOSKILL_CONTEXT_GUARD", "1").lower() not in {"0", "false", "no"}
WARM_SEARCH_RUNTIME = os.getenv("AUTOSKILL_WARM_SEARCH_RUNTIME", "1").lower() not in {"0", "false", "no"}
SKILLS_SH_LIVE_ROUTING = os.getenv("AUTOSKILL_SKILLS_SH_ROUTING", "1").lower() not in {
    "0",
    "false",
    "no",
}
# A local package corpus is useful for offline development and private/legacy
# migrations, but it must not silently bypass the skills.sh data gate in the
# normal public route.  Operators can opt in explicitly for an outage drill
# or an offline benchmark; production defaults to abstention when the remote
# catalog cannot be reached.
ALLOW_LOCAL_RETRIEVAL_FALLBACK = os.getenv(
    "AUTOSKILL_ALLOW_LOCAL_RETRIEVAL_FALLBACK", "0"
).lower() in {"1", "true", "yes"}
SKILLS_SH_SEARCH_LIMIT = min(50, max(2, int(os.getenv("AUTOSKILL_SKILLS_SH_SEARCH_LIMIT", "12"))))

# RRF scores cluster near 1/(rrf_k + ix), so near-ties sit ~1.0x apart; a top hit
# that both retrievers agree on lands well above 1.6x the runner-up.
RECOMMEND_GAP = 1.6
ROUTE_TTL_SECONDS = int(os.getenv("ROUTE_TTL_SECONDS", "300"))
ROUTE_LATENCY_WARN_MS = int(os.getenv("ROUTE_LATENCY_WARN_MS", "750"))
ROUTE_SKILL_FIND_WARN_MS = int(os.getenv("ROUTE_SKILL_FIND_WARN_MS", "500"))
ROUTE_INJECTED_TOKEN_WARN = int(os.getenv("ROUTE_INJECTED_TOKEN_WARN", "1000"))
ROUTE_RESPONSE_TOKEN_WARN = int(os.getenv("ROUTE_RESPONSE_TOKEN_WARN", "3500"))
CONTENT_HASH_RE = re.compile(r"^[a-f0-9]{64}$")
# Reconciliation note: a manual capsule-digest allowlist (ALLOW_UNVALIDATED_PUBLIC_FULL /
# OUTCOME_VALIDATED_CAPSULE_DIGESTS) used to additionally gate full delivery
# beyond quality.tier_for_ranked_candidates -- removed. Tiering is the sole
# full/hint/none decision (see find_deliverable_primary_candidate).
UPGRADE_URL = os.getenv("BACKEND_BASE_URL", "https://skills.autoskill.dev").rstrip("/") + "/account"
EMBED_INTERVAL_SECONDS = int(os.getenv("EMBED_INTERVAL_SECONDS", "300"))
# ONNX memory grows sharply with batch size at the 512-token window.  A batch
# of 128 exhausted the 4 GiB production droplet and put the worker into an OOM
# restart loop.  Collection is now off-host and one-shot, but keep conservative
# bounded defaults so an accidental or low-memory collector run fails safely.
EMBED_PAGE_SIZE = min(500, max(1, int(os.getenv("EMBED_PAGE_SIZE", "64"))))
EMBED_BATCH = min(32, max(1, int(os.getenv("EMBED_BATCH_SIZE", "8"))))
# Each upserted row triggers an HNSW index update, so keep statements small
# enough to stay well under any statement_timeout.
EMBED_UPSERT_CHUNK = min(100, max(1, int(os.getenv("EMBED_UPSERT_CHUNK", "50"))))

# Task-family policies are deliberately curated rather than discovered from a
# similarity search alone.  They shape every task in a family, so a malicious
# or merely popular skill must not be able to self-promote into this lane by
# stuffing its description with generic coding terms.
CODING_POLICY_QUERY = os.getenv(
    "AUTOSKILL_CODING_POLICY_QUERY",
    "ponytail coding policy minimal safe code reuse standard library native platform existing dependencies",
).strip()
CODING_POLICY_NAMES = tuple(
    name.strip().casefold()
    for name in os.getenv("AUTOSKILL_CODING_POLICY_NAMES", "ponytail").split(",")
    if name.strip()
)
CODING_POLICY_SOURCE_MARKERS = tuple(
    marker.strip().casefold()
    for marker in os.getenv(
        "AUTOSKILL_CODING_POLICY_SOURCE_MARKERS",
        "github.com/DietrichGebert/ponytail",
    ).split(",")
    if marker.strip()
)
POLICY_CAPSULE_CHARS = min(
    DEFAULT_CAPSULE_CHARS,
    max(400, int(os.getenv("AUTOSKILL_POLICY_CAPSULE_CHARS", "1200"))),
)

_TASK_TOKEN_RE = re.compile(r"[a-z0-9+#.-]+")
_CODING_TERMS = frozenset(
    {
        "code", "coding", "function", "class", "method", "component", "frontend", "backend",
        "website", "webapp", "landing", "page", "ui", "ux", "design", "app", "bug", "debug",
        "refactor", "repository", "repo", "test",
        "python", "javascript", "typescript", "java", "rust", "golang", "react", "vue", "angular",
        "nextjs", "fastapi", "django", "flask", "html", "css", "sql", "api", "endpoint",
    }
)
_INTEGRATION_REQUEST_TERMS = frozenset(
    {
        "integrate", "integration", "connect", "connector", "sync", "webhook", "oauth", "mcp",
    }
)
_GENERIC_INTEGRATION_NAME_TERMS = frozenset(
    {"auto", "automatic", "integration", "integrations", "skill", "mcp", "server", "tool", "agent", "workflow"}
)
_POLICY_MARKERS = (
    "always-on",
    "always on",
    "coding policy",
    "decision ladder",
)

router = APIRouter()


# --- Embedding backlog drain ---------------------------------------------

async def embed_missing_skills(client: httpx.AsyncClient) -> int:
    """Embed every skills row missing a current vector.

    Ingest/rescan explicitly clears vectors when content changes or loses its
    active quality status, so the ``is.null`` checkpoint is safe to interrupt
    and resumes both brand-new and invalidated rows.
    """
    library = LibraryContent()  # fresh each drain to pick up newly saved .md files
    total = 0
    while True:
        r = await client.get(
            f"{LOCAL_DB_URL}/rest/v1/skills",
            params={
                "select": "id,url,name,source,description,tags,capability_summary,retrieval_text",
                "embedding": "is.null",
                "url": "not.is.null",
                "quality_status": "eq.active",
                "order": "id.asc",
                "limit": str(EMBED_PAGE_SIZE),
            },
            headers=HEADERS,
            timeout=30,
        )
        r.raise_for_status()
        rows = r.json()
        if not rows:
            break
        texts = [build_embed_text(row, library.get(row.get("url") or "")) for row in rows]
        vectors = await asyncio.to_thread(embed_texts, texts, EMBED_BATCH)
        now = datetime.now(timezone.utc).isoformat()
        payload = [
            {
                "url": row["url"],
                "name": row.get("name") or "",
                "source": row.get("source") or "",
                "embedding": vec,
                "embedding_text_hash": embed_text_hash(text),
                "embedded_at": now,
            }
            for row, text, vec in zip(rows, texts, vectors)
        ]
        for i in range(0, len(payload), EMBED_UPSERT_CHUNK):
            await _upsert_chunk(client, payload[i:i + EMBED_UPSERT_CHUNK])
        total += len(rows)
    return total


async def _upsert_chunk(client: httpx.AsyncClient, chunk: list) -> None:
    """Retry transient failures (statement timeouts, blips) before giving up on
    the whole drain; the is-null checkpoint makes re-runs safe either way."""
    last = ""
    for attempt in range(3):
        if attempt:
            await asyncio.sleep(2 ** attempt)
        pr = await client.post(
            f"{LOCAL_DB_URL}/rest/v1/skills?on_conflict=url",
            json=chunk,
            headers=HEADERS,
            timeout=60,
        )
        if pr.status_code in (200, 201, 204):
            return
        last = f"{pr.status_code}: {pr.text[:300]}"
    raise RuntimeError(f"embedding upsert failed after retries ({last})")


async def _embed_backlog_loop():
    delay = EMBED_INTERVAL_SECONDS
    while True:
        try:
            async with httpx.AsyncClient() as client:
                n = await embed_missing_skills(client)
                if n:
                    print(f"[recommender] embedded {n} new skills")
            delay = EMBED_INTERVAL_SECONDS
        except Exception as e:
            print(f"[recommender] embed loop error (retrying in {delay}s): {e}")
            delay = min(delay * 2, 3600)  # back off instead of spamming a down DB
        await asyncio.sleep(delay)


@router.on_event("startup")
async def _start_embed_loop():
    if AUTO_START_EMBEDDER:
        asyncio.create_task(_embed_backlog_loop())
    if WARM_SEARCH_RUNTIME:
        asyncio.create_task(_warm_embedding_model())
        if _uses_local_store():
            asyncio.create_task(_warm_local_vector_index())
            asyncio.create_task(_warm_local_lexical_index())


async def _warm_local_vector_index() -> None:
    """Publish the in-memory vector matrix before the first route request."""
    try:
        stats = await asyncio.to_thread(store.warm_vector_index)
        print(f"[recommender] warmed local vector index ({stats.get('cache_vectors', 0)} vectors)")
    except Exception as exc:
        print(f"[recommender] local vector warm-up failed: {exc}")


async def _warm_local_lexical_index() -> None:
    try:
        stats = await asyncio.to_thread(store.warm_lexical_index)
        print(f"[recommender] warmed lexical index ({stats.get('skills', 0)} skills, {stats.get('tokens', 0)} tokens)")
    except Exception as exc:
        print(f"[recommender] lexical warm-up failed: {exc}")


async def _warm_embedding_model() -> None:
    """Load the ONNX model + tokenizer and run one throwaway embed at
    startup instead of on the first real request. embeddings._load() is
    lazy, so without this the first /find-semantic call after any restart
    pays the full model-load cost inline -- the hook's UserPromptSubmit
    timeout is 3s, well under what a cold load takes, so that first prompt
    would silently get no routing at all."""
    start = time.monotonic()
    try:
        await asyncio.to_thread(embed_texts, ["warm-up query for model load"])
        print(f"[recommender] embedding model warmed in {time.monotonic() - start:.1f}s")
    except Exception as e:
        print(f"[recommender] embedding model warm-up failed (will load lazily on first request): {e}")


# --- Retrieval ------------------------------------------------------------

def _stars(row: dict) -> int:
    if row.get("stars") is not None:
        return row["stars"] or 0
    raw = row.get("raw") or {}
    try:
        return int(raw.get("stars") or 0)
    except (TypeError, ValueError):
        return 0


async def embed_query(text: str) -> list[float]:
    return (await asyncio.to_thread(embed_texts, [text]))[0]


def _uses_local_store() -> bool:
    return LOCAL_DB_URL.startswith(("http://127.0.0.1", "http://localhost", "http://[::1]"))


# Minimum top-hit cosine similarity for a query to count as having a real
# match. Calibrated 2026-07-06 against gte-small on this corpus: genuine task
# queries score >= 0.885 at top-1; conversational/meta prompts ("thanks",
# "remember this is a product", "ok sounds good") land 0.82-0.88. Queries whose
# best vector hit falls below the floor return no results instead of noise.
MIN_SIMILARITY = float(os.getenv("MIN_SIMILARITY", "0.87"))


def _passes_similarity_floor(results: list[dict]) -> bool:
    """Check the selected candidate, never an unrelated lower-ranked row.

    FTS-only fallback has no cosine value and remains hint-only in
    ``tier_for_prompt``. A low-similarity top candidate must not borrow a
    high score from another result to become a full route.
    """
    if not results:
        return False
    similarity = results[0].get("similarity")
    if similarity is None:
        return True
    try:
        return float(similarity) >= MIN_SIMILARITY
    except (TypeError, ValueError):
        return False


def _results_are_ranked(results: list[dict]) -> bool:
    """Accept legacy/raw callers while avoiding a second rank pass internally."""
    return bool(results) and all(isinstance(result, dict) and "route_score" in result for result in results)


def injection_tier(query_text: str, results: list[dict], *, ranked: bool = False) -> str:
    """Decide how much of the top result to hand to a caller.

    The similarity floor rejects junk/meta prompts. Full vs. hint is then a
    deterministic quality/platform decision, not an RRF-gap heuristic: eval
    evidence showed the RRF gap is a smooth continuum and wrongly downgraded
    many real tasks, while platform traps need a hard cap.
    """
    if not results or not _passes_similarity_floor(results):
        return "none"
    if ranked:
        return tier_for_ranked_candidates(results)
    return tier_for_prompt(query_text, results, RECOMMEND_GAP)


def _task_tokens(text: str) -> set[str]:
    return {token.casefold() for token in _TASK_TOKEN_RE.findall(text or "")}


def analyze_task(
    query: str,
    *,
    requested_family: str = "",
    languages: list[str] | None = None,
    frameworks: list[str] | None = None,
    project_tags: list[str] | None = None,
) -> dict:
    """Return a privacy-safe, deterministic task classification.

    Clients may provide coarse project tags, but never need to send source code
    or file contents.  Explicit supported families win over inference.
    """
    explicit = (requested_family or "").strip().casefold()
    supported = {"coding", "research", "documents", "data", "general"}
    context = [*(languages or []), *(frameworks or []), *(project_tags or [])]
    tokens = _task_tokens(" ".join([query, *context]))
    signals: list[str] = []
    if explicit in supported:
        family = explicit
        signals.append("client-task-family")
    elif tokens & _CODING_TERMS or any(
        suffix in query.casefold()
        for suffix in (".py", ".js", ".jsx", ".ts", ".tsx", ".rs", ".go", ".java", ".html", ".css")
    ):
        family = "coding"
        signals.append("coding-language-or-artifact")
    elif tokens & {"research", "competitor", "sources", "citations", "browse", "web"}:
        family = "research"
        signals.append("research-intent")
    elif tokens & {"spreadsheet", "excel", "csv", "dataset", "analytics", "dashboard", "kpi"}:
        family = "data"
        signals.append("data-artifact")
    elif tokens & {"document", "docx", "pdf", "slides", "presentation", "report"}:
        family = "documents"
        signals.append("document-artifact")
    else:
        family = "general"

    action = "work"
    for candidate, markers in (
        ("review", {"review", "audit", "inspect"}),
        ("debug", {"debug", "bug", "fix", "repair"}),
        ("test", {"test", "verify", "validate"}),
        ("refactor", {"refactor", "simplify", "cleanup"}),
        ("implement", {"build", "create", "implement", "write", "add"}),
    ):
        if tokens & markers:
            action = candidate
            break
    return {
        "family": family,
        "action": action,
        "signals": signals,
        "languages": sorted({str(v).strip().casefold() for v in (languages or []) if str(v).strip()})[:8],
        "frameworks": sorted({str(v).strip().casefold() for v in (frameworks or []) if str(v).strip()})[:8],
        "project_tags": sorted({str(v).strip().casefold() for v in (project_tags or []) if str(v).strip()})[:12],
    }


def skill_role(candidate: dict) -> str:
    name = str(candidate.get("name") or "").strip().casefold()
    description = str(candidate.get("description") or "").casefold()
    text = f"{name} {description}"
    if (
        name in CODING_POLICY_NAMES
        or candidate.get("category") == "policy"
        or any(marker in text for marker in _POLICY_MARKERS)
    ):
        return "policy"
    platforms = candidate.get("platforms") or []
    if candidate.get("category") == "integration" or platforms or "integration" in name:
        return "integration"
    if any(marker in text for marker in ("debug", "review", "testing", "workflow", "migration", "deployment")):
        return "workflow"
    return "specialist"


def _integration_is_explicit(query: str, candidate: dict) -> bool:
    """Require a named service for platform-specific integrations.

    Broad name overlap such as "storefront" or "blog" is not an explicit
    integration request and must not route a generic task to Shopify or
    WordPress.
    """
    platforms = [str(p) for p in (candidate.get("platforms") or []) if p]
    if platforms:
        return platform_mentions(query, platforms)

    prompt_tokens = _task_tokens(query)
    if prompt_tokens & _INTEGRATION_REQUEST_TERMS:
        return True

    name_blob = str(candidate.get("name") or "").casefold()
    return any(
        _contains_alias(name_blob, alias) and platform_mentions(query, [platform])
        for platform, aliases in PLATFORM_ALIASES.items()
        for alias in aliases
    )


def candidate_matches_task_contract(query: str, candidate: dict) -> bool:
    """Apply role-level gates before confidence scoring.

    Policies are selected through a curated lane. Integrations require an
    explicit service/tool/action signal so generic catalog entries such as
    ``auto-integration`` cannot displace a real coding specialist.
    """
    role = skill_role(candidate)
    if role == "policy":
        return False
    if role == "integration":
        return _integration_is_explicit(query, candidate)
    return True


def _policy_candidate_allowed(candidate: dict, family: str, routing_filters: dict) -> bool:
    if family != "coding" or skill_role(candidate) != "policy":
        return False
    name = str(candidate.get("name") or "").strip().casefold()
    url = str(candidate.get("url") or candidate.get("source_url") or "").casefold()
    if name not in CODING_POLICY_NAMES:
        return False
    if not CODING_POLICY_SOURCE_MARKERS or not any(marker in url for marker in CODING_POLICY_SOURCE_MARKERS):
        return False
    if not _passes_routing_filters(candidate, routing_filters):
        return False
    return (
        (candidate.get("quality_status") or "active") == "active"
        and int(candidate.get("quality_score") or 0) >= 70
        and int(candidate.get("risk_score") or 0) == 0
        and bool(candidate.get("content_hash"))
    )


async def find_default_policy_candidate(
    client: httpx.AsyncClient,
    task_analysis: dict,
    routing_filters: dict,
) -> dict | None:
    if task_analysis.get("family") != "coding" or not CODING_POLICY_QUERY:
        return None
    candidates = await retrieve_skills(client, CODING_POLICY_QUERY, 8)
    if not _results_are_ranked(candidates):
        candidates = rerank_candidates(CODING_POLICY_QUERY, candidates)
    return next(
        (candidate for candidate in candidates if _policy_candidate_allowed(candidate, "coding", routing_filters)),
        None,
    )


def _rank_retrieval_lanes(
    query_text: str,
    results: list[dict],
    limit: int,
    *,
    vector_available: bool,
) -> list[dict]:
    """Keep not-yet-embedded active skills visible without polluting routing.

    A pending-embedding exact lexical match is useful as a reviewable hint, but
    must not displace the evidence-backed semantic lane or inherit another
    row's confidence. Reserve at most the final result slot for that lane.
    """
    if not vector_available:
        return rerank_candidates(query_text, results)[:limit]
    semantic = [row for row in results if row.get("similarity") is not None]
    pending = [row for row in results if row.get("similarity") is None]
    ranked_semantic = rerank_candidates(query_text, semantic)
    if not pending or limit < 2:
        return ranked_semantic[:limit]
    ranked_pending = rerank_candidates(query_text, pending)
    return ranked_semantic[: limit - 1] + ranked_pending[:1]


async def _retrieve_local_skills(client: httpx.AsyncClient, query_text: str, limit: int = 10) -> list[dict]:
    """Hybrid FTS+vector retrieval against the local DB. The frozen Supabase
    corpus was fully migrated into local_skills.db (migrate_state.json:
    202,367 rows on 2026-07-05), so local is the single source of truth.
    Falls back to pure FTS if embedding fails."""
    fetch_limit = max(limit, 20)
    body = {"query_text": query_text, "match_count": fetch_limit}
    try:
        query_embedding = await embed_query(query_text)
    except Exception:
        query_embedding = None

    if _uses_local_store():
        try:
            results = await asyncio.to_thread(
                store.hybrid_search_skills,
                query_text,
                query_embedding,
                fetch_limit,
            )
            return _rank_retrieval_lanes(
                query_text,
                results,
                limit,
                vector_available=query_embedding is not None,
            )
        except Exception as exc:
            print(f"[recommender] local retrieval fast path failed: {exc}")

    if query_embedding is not None:
        body["query_embedding"] = query_embedding
        rpc = "hybrid_search_skills"
    else:
        rpc = "search_skills"
        body = {"query": query_text, "max_results": fetch_limit}

    r = await client.post(f"{LOCAL_DB_URL}/rest/v1/rpc/{rpc}", json=body, headers=HEADERS, timeout=20)
    if r.status_code != 200:
        return []
    results = list(r.json())
    return _rank_retrieval_lanes(
        query_text,
        results,
        limit,
        vector_available=query_embedding is not None,
    )


async def retrieve_skills(client: httpx.AsyncClient, query_text: str, limit: int = 10) -> list[dict]:
    """Retrieve public candidates through skills.sh, abstaining on failure.

    The local package corpus is deliberately not a silent second public index:
    its freshness, provenance, and audit state differ from skills.sh.  Keep it
    behind an explicit development/outage flag so a production route cannot
    accidentally evade the skills.sh gate.
    """
    if SKILLS_SH_LIVE_ROUTING:
        catalog = default_catalog()
        try:
            # ``retrieve`` selects the documented authenticated API when an
            # OIDC token is present and the public discovery lane otherwise.
            # Calling it in both modes is essential: ``configured`` only means
            # authenticated, not that public discovery is unavailable.
            rows = await catalog.retrieve(query_text, min(max(limit, 10), SKILLS_SH_SEARCH_LIMIT))
            if rows:
                for row in rows:
                    row["retrieval_backend"] = "skills_sh"
                return rows[:limit]
        except SkillsShCatalogError as exc:
            print(f"[recommender] skills.sh live retrieval unavailable: {exc}")
        if not ALLOW_LOCAL_RETRIEVAL_FALLBACK:
            return []
    rows = await _retrieve_local_skills(client, query_text, limit)
    for row in rows:
        row.setdefault("retrieval_backend", "local")
    return rows


async def retrieve_skills_for_intent(
    client: httpx.AsyncClient,
    intent: CompiledIntent,
    limit: int = 10,
) -> list[dict]:
    """Search original and compiled queries, then fuse their lanes.

    When the documented skills.sh OIDC token is configured, the live catalog
    is the primary data gate. The local corpus remains a bounded availability
    fallback for development, outages, and legacy/private rows; it is never
    silently blended with live candidates because the two stores have
    different freshness and provenance guarantees.
    """
    variants = (
        [intent.original_query, skills_sh_query(intent)]
        if SKILLS_SH_LIVE_ROUTING
        else list(intent.query_variants)[:2]
    )
    variants = list(dict.fromkeys(value for value in variants if value))[:2]

    async def _lane(query: str) -> list[dict]:
        return await retrieve_skills(client, query, max(limit, 12))

    lanes = await asyncio.gather(*(_lane(query) for query in variants))
    fused: dict[str, dict] = {}
    for lane_index, (query, rows) in enumerate(zip(variants, lanes)):
        for rank, row in enumerate(rows):
            key = str(row.get("id") or row.get("content_hash") or row.get("url") or "")
            if not key:
                continue
            current = fused.setdefault(key, dict(row))
            current["query_rrf_score"] = float(current.get("query_rrf_score") or 0.0) + 1.0 / (60 + rank + 1)
            current.setdefault("retrieval_queries", []).append(
                "original" if lane_index == 0 else "compiled"
            )
            current["retrieval_priority"] = max(
                int(current.get("retrieval_priority") or 0),
                1 if lane_index == 0 else 0,
            )
            similarity = row.get("similarity")
            if similarity is not None and (
                current.get("similarity") is None or float(similarity) > float(current["similarity"])
            ):
                current["similarity"] = similarity
            current["rank"] = max(float(current.get("rank") or 0.0), float(row.get("rank") or 0.0))
            current["query_variant"] = query
    rows = list(fused.values())
    rows.sort(
        key=lambda row: (
            -float(row.get("query_rrf_score") or 0.0),
            -float(row.get("similarity") or 0.0),
            str(row.get("id") or row.get("url") or ""),
        )
    )
    # Quality reranking remains task-centric, but query-RRF breaks otherwise
    # equal candidates in favour of cross-query agreement.
    ranked = rerank_candidates(intent.original_query, rows)
    ranked.sort(
        key=lambda row: (
            -float(row.get("route_score") or 0.0),
            -float(row.get("query_rrf_score") or 0.0),
            -int(row.get("retrieval_priority") or 0),
            str(row.get("id") or row.get("url") or ""),
        )
    )
    return ranked[:limit]


async def fetch_skills_by_urls(client: httpx.AsyncClient, urls: list[str]) -> list[dict]:
    if not urls:
        return []
    if SKILLS_SH_LIVE_ROUTING:
        # Conversational follow-ups must not resurrect a row from the legacy
        # local corpus.  Resolve only stable skills.sh IDs (or skills.sh page
        # URLs) through the same remote catalog used for first-pass search.
        skill_ids: list[str] = []
        for value in urls[:10]:
            candidate = str(value or "").strip()
            if not candidate:
                continue
            parsed = urlsplit(candidate)
            if parsed.netloc.casefold() in {"skills.sh", "www.skills.sh"}:
                path = parsed.path.strip("/")
                if path and not path.startswith("api/"):
                    skill_ids.append(path)
            elif "://" not in candidate and candidate.count("/") >= 2:
                # Clients may send the stable skills.sh ID directly.
                skill_ids.append(candidate.strip("/"))
        if not skill_ids:
            return []
        try:
            rows = await default_catalog().retrieve_ids(skill_ids, limit=10)
        except SkillsShCatalogError as exc:
            print(f"[recommender] skills.sh follow-up unavailable: {exc}")
            return []
        for row in rows:
            row["retrieval_backend"] = "skills_sh"
        return rows
    quoted = ",".join('"' + u.replace('"', "") + '"' for u in urls[:10])
    r = await client.get(
        f"{LOCAL_DB_URL}/rest/v1/skills",
        params={
            "select": (
                "id,name,description,source,url,tags,risk_score,risk_flags,raw,"
                "content_hash,quality_status,quality_score,platforms,category"
            ),
            "url": f"in.({quoted})",
        },
        headers=HEADERS,
        timeout=15,
    )
    if r.status_code != 200:
        return []
    rows = r.json()
    for row in rows:
        row["stars"] = _stars(row)
        row.pop("raw", None)
        row.setdefault("rank", 0.0)
    return rows


# --- Ollama (optional local LLM) -------------------------------------------

_ollama_probe = {"at": 0.0, "up": False}


async def ollama_available(client: httpx.AsyncClient) -> bool:
    if not ENABLE_OLLAMA_CHAT:
        return False
    now = time.monotonic()
    if now - _ollama_probe["at"] < 60:
        return _ollama_probe["up"]
    up = False
    try:
        r = await client.get(f"{OLLAMA_URL}/api/tags", timeout=1.5)
        up = r.status_code == 200
    except Exception:
        up = False
    _ollama_probe.update(at=now, up=up)
    return up


async def ollama_json(client: httpx.AsyncClient, messages: list[dict], schema: dict, timeout: float = 45) -> dict | None:
    """One structured-output chat call; one retry on malformed JSON; None on failure."""
    for _ in range(2):
        try:
            r = await client.post(
                f"{OLLAMA_URL}/api/chat",
                json={
                    "model": OLLAMA_MODEL,
                    "messages": messages,
                    "stream": False,
                    "format": schema,
                    "options": {"temperature": 0},
                },
                timeout=timeout,
            )
            if r.status_code != 200:
                return None
            content = r.json().get("message", {}).get("content", "")
            return json.loads(content)
        except json.JSONDecodeError:
            continue
        except Exception:
            return None
    return None


QUERY_SCHEMA = {
    "type": "object",
    "properties": {
        "search_query": {"type": "string"},
        "intent": {"type": "string", "enum": ["new_search", "refine", "correction", "pick_option"]},
        "excluded": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["search_query", "intent"],
}

QUERY_SYSTEM = """You turn a conversation into a search query for a directory of Claude Code skills, MCP servers, and plugins.
Output JSON:
- search_query: a short, keyword-rich description of the task the user wants a skill for (their latest need, incorporating earlier context). No filler words.
- intent: "new_search" for a fresh request; "refine" if the latest message adds constraints to the same request; "correction" if the user rejected what was suggested; "pick_option" if the user is choosing one of the options they were offered.
- excluded: names or URLs of skills the user rejected, if any."""

CHOOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {"type": "string", "enum": ["recommend", "clarify", "none"]},
        "chosen_url": {"type": "string"},
        "reply": {"type": "string"},
        "clarify_question": {"type": "string"},
        "option_urls": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["action", "reply"],
}

CHOOSE_SYSTEM = """You are a recommender for Claude Code skills / MCP servers. Given the user's task and a JSON list of candidate skills, pick a usable fit only when one is clearly relevant.
Output JSON:
- action: "recommend" when one candidate clearly fits the task; "clarify" when several fit about equally; "none" when nothing genuinely fits.
- chosen_url: the url of the recommended candidate (required for recommend).
- reply: 1-3 friendly sentences. For recommend: say why this skill fits their task. For clarify: ask ONE short question that would disambiguate. For none: suggest how to rephrase.
- clarify_question / option_urls: for clarify, the question plus 2-3 candidate urls to offer.
Rules: only ever use urls that appear in the candidate list. Prefer well-described, higher-star candidates when quality seems equal. Never pick a candidate whose risk_score is 3 or more."""


# --- Chat orchestration -----------------------------------------------------

class ChatRequest(BaseModel):
    messages: list[dict]
    prev_options: list[str] = []


class RouteRequest(BaseModel):
    prompt: str | None = None
    task: str | None = None
    client: str = ""
    client_version: str = ""
    limit: int = 8
    guard_mode: Literal["hybrid", "capsule_only"] = "hybrid"
    supports_isolation: bool = False
    max_inline_chars: int = DEFAULT_INLINE_CHARS
    max_capsule_chars: int = DEFAULT_CAPSULE_CHARS
    anonymous_id: str | None = None
    session_id: str | None = None
    task_family: str = ""
    languages: list[str] = []
    frameworks: list[str] = []
    project_tags: list[str] = []


class SemanticSearchRequest(BaseModel):
    q: str
    limit: int = 8
    gate: bool = True


class RouteFeedbackRequest(BaseModel):
    route_id: str
    outcome: str
    source: str = ""
    note: str = ""


NONE_MESSAGE = ("I couldn't find anything matching that. Try describing the task with "
                "different words - e.g. the tool, file type, or service involved.")


def _blurb(skill: dict) -> str:
    parts = [f"Best match: {skill['name']}."]
    if skill.get("description"):
        parts.append(skill["description"][:200])
    if skill.get("stars"):
        parts.append(f"({skill['stars']} GitHub stars)")
    if (skill.get("risk_score") or 0) > 0:
        parts.append(f"Note: the malware scan gave this a low-level risk score of {skill['risk_score']} - review it before installing.")
    return " ".join(parts)


def _heuristic_response(candidates: list[dict]) -> dict:
    top = candidates[0]
    runner_up = candidates[1] if len(candidates) > 1 else None
    top_score = top.get("route_score", top.get("rank", 0))
    runner_score = runner_up.get("route_score", runner_up.get("rank", 0)) if runner_up else 0
    if runner_up is None or top_score >= runner_score * RECOMMEND_GAP:
        return {"type": "recommend", "skill": top, "message": _blurb(top)}
    return {
        "type": "clarify",
        "message": "A few skills fit that about equally well - which of these is closest to what you're doing? Pick one, or describe your task in a bit more detail.",
        "options": candidates[:3],
    }


def _compact(c: dict) -> dict:
    return {
        "name": c.get("name"),
        "description": (c.get("description") or "")[:200],
        "source": c.get("source"),
        "url": c.get("url"),
        "tags": (c.get("tags") or [])[:6],
        "stars": c.get("stars") or 0,
        "risk_score": c.get("risk_score") or 0,
    }


@router.post("/chat")
async def chat_recommend(body: ChatRequest):
    messages = [m for m in body.messages if m.get("role") in ("user", "assistant") and m.get("content")]
    user_texts = [m["content"] for m in messages if m["role"] == "user"]
    if not user_texts:
        return {"type": "none", "message": "Tell me what you're trying to do and I'll find a skill for it."}

    query = " ".join(user_texts)[-500:]
    intent, excluded = "new_search", []

    async with httpx.AsyncClient() as client:
        use_llm = await ollama_available(client)

        if use_llm:
            convo = json.dumps(messages[-8:], ensure_ascii=False)
            parsed = await ollama_json(
                client,
                [{"role": "system", "content": QUERY_SYSTEM},
                 {"role": "user", "content": f"Conversation:\n{convo}"}],
                QUERY_SCHEMA,
            )
            if parsed and (parsed.get("search_query") or "").strip():
                query = parsed["search_query"].strip()
                intent = parsed.get("intent", "new_search")
                excluded = [str(x).lower() for x in (parsed.get("excluded") or [])]

        candidates = await retrieve_skills(client, query, 10)

        # Follow-ups keep the previously offered options in play so the LLM can
        # rerank the union rather than starting from scratch.
        if body.prev_options and intent in ("refine", "correction", "pick_option"):
            prev = await fetch_skills_by_urls(client, body.prev_options)
            seen = {c.get("url") for c in candidates}
            candidates.extend(p for p in prev if p.get("url") not in seen)

        def _excluded(c: dict) -> bool:
            name = (c.get("name") or "").lower()
            url = (c.get("url") or "").lower()
            return any(x == name or (x and x in url) for x in excluded)

        candidates = [c for c in candidates if (c.get("risk_score") or 0) < 3 and not _excluded(c)]
        if not candidates or not _passes_similarity_floor(candidates):
            return {"type": "none", "message": NONE_MESSAGE}

        if use_llm:
            by_url = {c["url"]: c for c in candidates if c.get("url")}
            choice = await ollama_json(
                client,
                [{"role": "system", "content": CHOOSE_SYSTEM},
                 {"role": "user", "content": json.dumps({
                     "task": query,
                     "latest_user_message": user_texts[-1],
                     "candidates": [_compact(c) for c in candidates[:8]],
                 }, ensure_ascii=False)}],
                CHOOSE_SCHEMA,
            )
            if choice:
                action = choice.get("action")
                reply = (choice.get("reply") or "").strip()
                if action == "recommend" and choice.get("chosen_url") in by_url:
                    skill = by_url[choice["chosen_url"]]
                    return {"type": "recommend", "skill": skill, "message": reply or _blurb(skill)}
                if action == "clarify":
                    options = [by_url[u] for u in (choice.get("option_urls") or []) if u in by_url][:3]
                    if not options:
                        options = candidates[:3]
                    return {"type": "clarify",
                            "message": reply or choice.get("clarify_question") or "Which of these is closest?",
                            "options": options}
                if action == "none":
                    return {"type": "none", "message": reply or NONE_MESSAGE}
            # malformed/hallucinated LLM output -> deterministic path

        return _heuristic_response(candidates)


def _public_skill(row: dict | None) -> dict | None:
    if not row:
        return None
    install_url = row.get("install_url") or row.get("url")
    skill_name = row.get("name")
    session_activation = None
    if row.get("retrieval_backend") == "skills_sh" and install_url:
        session_activation = {
            "mode": "skills_sh_use",
            "scope": "session",
            "source": install_url,
            "skill": skill_name,
            "agent": "codex",
            "snapshot_hash": row.get("source_snapshot_hash"),
            "command": [
                "npx", "skills", "use", install_url, "--skill", str(skill_name or ""),
                "--agent", "codex",
            ],
        }
    return {
        "id": row.get("id"),
        "slug": row.get("slug") or row.get("name"),
        "name": skill_name,
        "summary": row.get("capability_summary") or row.get("description"),
        "description": row.get("description"),
        "source": row.get("source"),
        "registry": row.get("registry"),
        "source_url": row.get("url"),
        "url": row.get("url"),
        "skills_sh_id": row.get("skills_sh_id"),
        "skills_sh_url": row.get("skills_sh_url"),
        "install_url": row.get("install_url"),
        "source_snapshot_hash": row.get("source_snapshot_hash"),
        "audit_status": row.get("audit_status"),
        "audit_risk_level": row.get("audit_risk_level"),
        "audit_count": row.get("audit_count"),
        "is_duplicate": bool(row.get("is_duplicate")),
        "retrieval_backend": row.get("retrieval_backend"),
        "session_activation": session_activation,
        "stars": row.get("stars") or 0,
        "installs": row.get("installs"),
        "source_type": row.get("source_type"),
        "content_hash": row.get("content_hash"),
        "quality_status": row.get("quality_status"),
        "quality_score": row.get("quality_score"),
        "prominence_score": row.get("prominence_score"),
        "provenance_score": row.get("provenance_score"),
        "meaningfulness_score": row.get("meaningfulness_score"),
        "duplicate_group_size": row.get("duplicate_group_size"),
        "platforms": row.get("platforms") or [],
        "category": row.get("category"),
        "risk_score": row.get("risk_score"),
        "rank": row.get("rank"),
        "route_score": row.get("route_score"),
        "similarity": row.get("similarity"),
        "candidate_role": row.get("candidate_role"),
        "role_confidence": row.get("role_confidence"),
        "role_reasons": row.get("role_reasons") or [],
        "package_hash": row.get("package_hash"),
        "source_commit_sha": row.get("source_commit_sha"),
        "license_spdx": row.get("license_spdx"),
        "package_completeness": row.get("package_completeness"),
        "dependency_closure_status": row.get("dependency_closure_status"),
        "retrieval_record_hash": row.get("retrieval_record_hash"),
    }


def _private_skill_as_row(skill: dict) -> dict:
    """Shape a private_skills row like a public `skills` row so it can flow
    through `_public_skill`/`_hint_candidates` unchanged. Private skills are
    trusted (the caller owns them), so they get top-of-scale quality/rank."""
    return {
        "id": skill["id"],
        "name": skill["name"],
        "description": skill.get("description"),
        "source": "private",
        "url": None,
        "content_hash": None,
        "quality_status": "private",
        "quality_score": 0,
        "platforms": [],
        "category": "private",
        "risk_score": 99,
        "rank": 1.0,
        "route_score": 1.0,
        "similarity": None,
    }


def _private_words(text: str) -> set[str]:
    return {
        token
        for token in re.findall(r"[a-z0-9]+", text.lower())
        if len(token) > 2 and token not in NAME_STOPWORDS
    }


def _best_private_skill_match(query_text: str, private_skills: list[dict]) -> dict | None:
    """Conservative relevance match for caller-owned private skills.

    Ownership makes the content trusted, not automatically relevant. Require
    either the complete skill name in the task, two meaningful name words, or
    an exact match for a single distinctive name word.
    """
    query_words = _private_words(query_text)
    if not query_words:
        return None
    normalized_query = " ".join(re.findall(r"[a-z0-9]+", query_text.lower()))
    best, best_score = None, 0
    for skill in private_skills:
        name_words = _private_words(skill["name"])
        normalized_name = " ".join(re.findall(r"[a-z0-9]+", skill["name"].lower()))
        if not name_words or not normalized_name:
            continue
        overlap = len(query_words & name_words)
        exact_phrase = normalized_name in normalized_query
        distinctive_single_word = len(name_words) == 1 and len(next(iter(name_words))) >= 6 and overlap == 1
        if not (exact_phrase or overlap >= 2 or distinctive_single_word):
            continue
        score = 100 + overlap if exact_phrase else overlap
        if score > best_score:
            best, best_score = skill, score
    return best


def _hint_candidates(results: list[dict], limit: int = 3) -> list[dict]:
    """Return a small content-free option set for medium-confidence routes."""
    candidates: list[dict] = []
    seen: set[str] = set()
    for row in results:
        public = _public_skill(row)
        if not public:
            continue
        key = (public.get("source_url") or public.get("url") or public.get("name") or "").strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        candidates.append(public)
        if len(candidates) >= limit:
            break
    return candidates


def _score_debug(results: list[dict], tier: str) -> dict:
    if not results:
        return {"tier": tier, "reason": "no-results"}
    top = results[0]
    runner = results[1] if len(results) > 1 else None
    top_score = float(top.get("route_score") or top.get("rank") or 0.0)
    runner_score = float(runner.get("route_score") or runner.get("rank") or 0.0) if runner else 0.0
    return {
        "tier": tier,
        "top_route_score": top_score,
        "runner_route_score": runner_score,
        "margin": round(top_score - runner_score, 6),
        "lexical_overlap": top.get("lexical_overlap"),
        "platform_mismatch": bool(top.get("platform_mismatch")),
        "similarity": top.get("similarity"),
        "quality_status": top.get("quality_status"),
        "quality_score": top.get("quality_score"),
        "quality_component": top.get("quality_component"),
        "prominence_score": top.get("prominence_score"),
        "provenance_score": top.get("provenance_score"),
        "evidence_score": top.get("evidence_score"),
        "meaningfulness_score": top.get("meaningfulness_score"),
        "trust_signal": bool(top.get("trust_signal")),
        "duplicate_group_size": top.get("duplicate_group_size", 1),
        "winner_reason": (
            "verified static content with strong relevance and meaningfulness"
            if tier == "full"
            else "candidate retained as a hint because one or more confidence/context gates did not clear"
        ),
        "recommend_gap": RECOMMEND_GAP,
        "min_similarity": MIN_SIMILARITY,
    }


def _estimate_tokens(value) -> int:
    """Cheap, deterministic token estimate for budgets and trend tracking."""
    if value is None:
        return 0
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    if not value:
        return 0
    return max(1, ceil(len(value) / 4))


def _estimate_candidate_tokens(candidates: list[dict]) -> int:
    """Estimate what an adapter actually presents, not duplicated JSON fields."""
    compact = [
        {
            "name": candidate.get("name"),
            "description": (candidate.get("description") or "")[:160],
            "url": candidate.get("url") or candidate.get("source_url"),
        }
        for candidate in candidates
    ]
    return _estimate_tokens(compact)


def _empty_context_guard(reason: str = "no-route") -> dict:
    return {
        "policy": CONTEXT_GUARD_POLICY,
        "delivery": "none",
        "reason": reason,
        "capsule": None,
        "capsule_chars": 0,
        "estimated_tokens": 0,
        "content_hash": None,
        "content_digest": None,
        "complete": False,
        "fetch_hint": None,
    }


async def _record_route_event(event: dict) -> None:
    try:
        await asyncio.to_thread(store.insert_route_event, event)
    except Exception as exc:
        print(f"[recommender] route event logging failed: {exc}")


def _anonymous_id_hash(value: str | None) -> str | None:
    """Hash a client-generated UUID before it reaches retained analytics."""
    if not value:
        return None
    try:
        normalized = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return None
    if normalized == str(uuid.UUID(int=0)):
        return None
    return hashlib.sha256(f"autoskill-anonymous-installation-v1:{normalized}".encode("ascii")).hexdigest()


def _route_client_ip(request: Request) -> str | None:
    """Best-effort caller IP for per-user route metrics: the proxy-reported
    client (Cloudflare first, then the nearest x-forwarded-for hop), falling
    back to the direct peer for self-hosted deployments with no proxy.
    Validation happens at the storage boundary (local_store._safe_route_ip)."""
    raw = request.headers.get("cf-connecting-ip") or (request.headers.get("x-forwarded-for") or "").split(",")[0]
    raw = raw.strip()
    if raw:
        return raw
    return request.client.host if request.client else None


def _require_route_user(authorization: str | None) -> dict:
    """Require identity at the handler boundary, even when proxy headers are absent."""
    user = auth.user_from_authorization_header(authorization)
    if user is None:
        raise HTTPException(
            status_code=401,
            detail={"error": "account required", "signup_url": "/signup"},
        )
    return user


def _route_quota(user: dict) -> tuple[int, int] | None:
    """(used, limit) for this month under the caller's plan, or None when
    unmetered. Free gets the published quota; pro gets the internal fair-use
    cap behind "unlimited"; team pools that cap across the workspace's seats."""
    plan = user.get("plan") or "free"
    if plan == "free":
        if store.FREE_ROUTES_PER_MONTH <= 0:
            return None
        return store.get_route_usage(user["id"]), store.FREE_ROUTES_PER_MONTH
    if store.PRO_ROUTES_PER_MONTH <= 0:
        return None
    if plan == "team":
        pool = store.team_route_pool(user["id"])
        if pool is not None:
            return pool
    return store.get_route_usage(user["id"]), store.PRO_ROUTES_PER_MONTH


def _passes_routing_filters(candidate: dict, filters: dict) -> bool:
    """Apply the caller's exclusions and their orgs' allow/block policies to a
    public-catalog candidate (private/org skills never pass through here)."""
    skill_id = candidate.get("id")
    if skill_id in filters["excluded_ids"] or skill_id in filters["blocked_ids"]:
        return False
    if (candidate.get("source") or "") in filters["excluded_sources"]:
        return False
    allowed = filters["allowed_ids"]
    return allowed is None or skill_id in allowed


def route_quota_exceeded(user: dict) -> bool:
    """True when the caller has used up this month's routes. Quota is
    checked before retrieval and counted only for task-shaped queries, so
    empty/non-task rejects never burn quota."""
    quota = _route_quota(user)
    return quota is not None and quota[0] >= quota[1]


def _quota_route_payload(user: dict, start: float) -> dict:
    payload = _none_route_payload("quota-exceeded", start, str(uuid.uuid4()))
    used, limit = _route_quota(user) or (store.get_route_usage(user["id"]), 0)
    payload["quota"] = {
        "plan": user.get("plan") or "free",
        "limit": limit,
        "used": used,
        "upgrade_url": UPGRADE_URL,
    }
    return payload


def _none_route_payload(reason: str, start: float, route_id: str | None = None) -> dict:
    metrics = {
        "latency_ms": int((time.monotonic() - start) * 1000),
        "skill_find_ms": 0,
        "retrieval_ms": 0,
        "rerank_ms": 0,
        "content_ms": 0,
        "result_count": 0,
        "input_tokens": 0,
        "hint_tokens": 0,
        "candidate_tokens": 0,
        "content_tokens": 0,
        "injected_tokens": 0,
        "response_tokens": 0,
        "latency_warn_ms": ROUTE_LATENCY_WARN_MS,
        "response_token_warn": ROUTE_RESPONSE_TOKEN_WARN,
    }
    return {
        "tier": "none",
        "skill": None,
        "candidates": [],
        "content": None,
        "content_url": None,
        "context_guard": _empty_context_guard(reason),
        "route_id": route_id,
        "score_debug": {"tier": "none", "reason": reason, "metrics": metrics},
        "config_version": CONFIG_VERSION,
        "ttl": ROUTE_TTL_SECONDS,
    }


def _library_content_by_hash(target_hash: str) -> str:
    if not target_hash or not CONTENT_HASH_RE.match(target_hash):
        return ""
    return LibraryContent().get_by_hash(target_hash)


def _candidate_context_guard(
    query: str,
    candidate: dict,
    text: str,
    max_capsule_chars: int = DEFAULT_CAPSULE_CHARS,
) -> dict:
    package_hash = str(candidate.get("package_hash") or "") or None
    manifest = store.get_skill_package_manifest(package_hash) if package_hash else None
    return build_context_guard(
        task=query,
        content=text,
        content_hash=str(candidate.get("content_hash") or ""),
        content_digest=content_digest(text),
        max_capsule_chars=max_capsule_chars,
        force_capsule=True,
        package_manifest=manifest,
        source_url=str(candidate.get("url") or candidate.get("source_url") or "") or None,
        source_commit_sha=str(candidate.get("source_commit_sha") or "") or None,
        package_hash=package_hash,
    )


def _public_plan_item(item: dict | None) -> dict | None:
    """Remove serving-only fields before returning a composed plan item."""
    if not item:
        return None
    return {key: value for key, value in item.items() if not key.startswith("_")}


async def _build_verified_policy_item(
    user_id: str,
    candidate: dict | None,
    task: str,
    max_capsule_chars: int,
) -> dict | None:
    """Verify and bound a curated policy independently from the primary skill."""
    if not candidate:
        return None
    public = _public_skill(candidate)
    if not public:
        return None
    text = await asyncio.to_thread(LibraryContent().get, public.get("url") or "")
    pin = await asyncio.to_thread(store.get_pin, user_id, str(public.get("id") or ""))
    if pin and text and pin["content_hash"] != public.get("content_hash"):
        pinned_text = await asyncio.to_thread(_library_content_by_hash, pin["content_hash"])
        if pinned_text:
            text = pinned_text
            public["content_hash"] = pin["content_hash"]
            public["pinned"] = True
    if (
        not text
        or not has_valid_skill_frontmatter(text)
        or not public.get("content_hash")
        or content_hash(text) != public.get("content_hash")
        or skill_capability_flags(text)
    ):
        return None
    capsule_budget = max(200, min(POLICY_CAPSULE_CHARS, int(max_capsule_chars or POLICY_CAPSULE_CHARS)))
    guard = _candidate_context_guard(task, candidate, text, capsule_budget)
    capsule = guard.get("capsule")
    if not capsule:
        return None
    digest = content_digest(text)
    public.update(
        {
            "role": "policy",
            "activation": "task-family-default",
            "routing_tier": "full",
            "capsule": capsule,
            "capsule_chars": len(capsule),
            "estimated_tokens": estimate_guard_tokens(capsule),
            "verification": {
                "content_hash_verified": True,
                "static_instruction_only": True,
                "hash_kind": "canonical_normalized",
                "content_digest": digest,
                "capsule_digest": guard.get("capsule_digest"),
                "source": "indexed-local-copy",
                "publisher_verified": False,
            },
            "_content": text,
            "_row": candidate,
        }
    )
    return public


def _policy_context_guard(policy_item: dict) -> dict:
    return {
        "policy": CONTEXT_GUARD_POLICY,
        "delivery": "capsule",
        "reason": "task_family_policy",
        "complete": False,
        "capsule": policy_item["capsule"],
        "capsule_chars": policy_item["capsule_chars"],
        "estimated_tokens": policy_item["estimated_tokens"],
        "content_hash": policy_item.get("content_hash"),
        "content_digest": (policy_item.get("verification") or {}).get("content_digest"),
        "capsule_digest": (policy_item.get("verification") or {}).get("capsule_digest"),
    }


def _verified_static_candidate_content(candidate: dict) -> str:
    """Return current indexed content only when it is safe for full delivery."""
    public = _public_skill(candidate)
    if not public or not public.get("content_hash"):
        return ""
    text = str(candidate.get("_content") or "") or LibraryContent().get(public.get("url") or "")
    if (
        not text
        or not has_valid_skill_frontmatter(text)
        or content_hash(text) != public["content_hash"]
        or skill_capability_flags(text)
    ):
        return ""
    return text


async def find_deliverable_primary_candidate(
    query: str,
    candidates: list[dict],
    max_candidates: int = 5,
    *,
    ranked: bool = False,
) -> tuple[dict | None, str]:
    """Choose the highest-ranked relevant candidate that can actually ship.

    A capability-bearing top result remains available as a hint, but must not
    prevent a slightly lower-ranked verified static specialist from becoming
    the active route. quality.tier_for_ranked_candidates (score/risk/
    similarity/active-status gates) is the sole full/hint/none decision --
    reaching "full" here is sufficient to deliver, no separate manual
    capsule-digest allowlist on top of it.
    """
    plausible = [
        candidate
        for candidate in candidates
        if injection_tier(query, [candidate], ranked=ranked) == "full"
    ][:max_candidates]
    if not plausible:
        return None, ""
    contents = await asyncio.gather(
        *(asyncio.to_thread(_verified_static_candidate_content, candidate) for candidate in plausible)
    )
    for candidate, text in zip(plausible, contents):
        guard = _candidate_context_guard(query, candidate, text) if text else None
        if guard and guard.get("capsule"):
            return candidate, text
    return None, ""


@router.get("/content/{hash_value}")
async def get_content(hash_value: str):
    text = await asyncio.to_thread(_library_content_by_hash, hash_value)
    if not text:
        return Response(status_code=404)
    return PlainTextResponse(
        text,
        media_type="text/markdown; charset=utf-8",
        headers={"Cache-Control": "public, max-age=3600"},
    )


@router.post("/route")
async def route(request: Request, body: RouteRequest, authorization: str | None = Header(None)):
    """Deterministic backend-owned route contract for connectors."""
    start = time.monotonic()
    user = _require_route_user(authorization)
    ip_address = _route_client_ip(request)
    anonymous_id_hash = None
    query = (body.task or body.prompt or "").strip()
    if not query:
        return _none_route_payload("empty-query", start)

    if is_non_task_prompt(query):
        route_id = str(uuid.uuid4())
        payload = _none_route_payload("non-task-prompt", start, route_id)
        await _record_route_event(
            {
                "client": body.client[:80],
                "client_version": body.client_version[:80],
                "id": route_id,
                "user_id": user["id"] if user else None,
                "anonymous_id_hash": anonymous_id_hash,
                "ip_address": ip_address,
                "query_chars": len(query),
                "tier": "none",
                "config_version": CONFIG_VERSION,
                "warnings": [],
            }
        )
        return payload

    if route_quota_exceeded(user):
        return _quota_route_payload(user, start)
    await asyncio.to_thread(store.increment_route_usage, user["id"])

    limit = max(2, min(int(body.limit or 8), 20))
    query_compile_start = time.monotonic()
    intent = compile_intent_query(
        query,
        languages=body.languages,
        frameworks=body.frameworks,
        project_tags=body.project_tags,
    )
    query_compile_ms = int((time.monotonic() - query_compile_start) * 1000)
    task_analysis = analyze_task(
        query,
        requested_family=body.task_family,
        languages=body.languages,
        frameworks=body.frameworks,
        project_tags=body.project_tags,
    )
    filters_start = time.monotonic()
    routing_filters = await asyncio.to_thread(store.routing_filters_for_user, user["id"])
    routing_filters_ms = int((time.monotonic() - filters_start) * 1000)
    retrieval_start = time.monotonic()
    async with httpx.AsyncClient() as client:
        primary_retrieval_start = time.monotonic()
        results = await retrieve_skills_for_intent(client, intent, limit)
        primary_retrieval_ms = int((time.monotonic() - primary_retrieval_start) * 1000)
        policy_lookup_start = time.monotonic()
        try:
            policy_candidate = await find_default_policy_candidate(client, task_analysis, routing_filters)
        except Exception:
            # A missing curated policy must never take the primary route down.
            policy_candidate = None
        policy_lookup_ms = int((time.monotonic() - policy_lookup_start) * 1000)
    retrieval_ms = int((time.monotonic() - retrieval_start) * 1000)
    rerank_start = time.monotonic()
    # retrieve_skills normally produces this prompt's deterministic ordering.
    # Preserve compatibility with raw/legacy callers before skipping the repeat.
    results_are_ranked = _results_are_ranked(results)
    candidate_rerank_start = time.monotonic()
    if not results_are_ranked:
        results = rerank_candidates(query, results)
    candidate_rerank_ms = int((time.monotonic() - candidate_rerank_start) * 1000)
    candidate_filter_start = time.monotonic()
    results = [
        result
        for result in results
        if _passes_routing_filters(result, routing_filters)
        and candidate_matches_task_contract(query, result)
    ]
    # Reconciliation note: candidate role classification (routing_roles.py --
    # primary/supporting/policy/harmful, gated behind a manual capsule-digest
    # allowlist) was not adopted. quality.tier_for_ranked_candidates (score/
    # risk/similarity/active-status gates, applied via injection_tier below)
    # is the sole full/hint/none decision, same as before either redesign.
    candidate_filter_ms = int((time.monotonic() - candidate_filter_start) * 1000)
    primary_results = results
    deliverable_validation_start = time.monotonic()
    deliverable_primary, preverified_primary_text = await find_deliverable_primary_candidate(
        query, primary_results, ranked=results_are_ranked
    )
    deliverable_validation_ms = int((time.monotonic() - deliverable_validation_start) * 1000)
    tier_decision_start = time.monotonic()
    if deliverable_primary:
        primary_results = [deliverable_primary]
        primary_tier = "full"
    else:
        primary_tier = injection_tier(query, primary_results, ranked=results_are_ranked)
    tier_decision_ms = int((time.monotonic() - tier_decision_start) * 1000)
    policy_build_start = time.monotonic()
    policy_item = (
        await _build_verified_policy_item(user["id"], policy_candidate, query, body.max_capsule_chars)
        if primary_tier == "full"
        else None
    )
    policy_build_ms = int((time.monotonic() - policy_build_start) * 1000)
    tier = primary_tier
    rerank_ms = int((time.monotonic() - rerank_start) * 1000)
    skill_find_ms = int((time.monotonic() - retrieval_start) * 1000)
    warnings: list[str] = []
    content = None
    content_url = None
    skill = _public_skill(primary_results[0]) if primary_results else None
    selected_role = "primary" if primary_results else None
    content_ms = 0
    route_id = str(uuid.uuid4())
    context_guard = _empty_context_guard("no-route")

    # A caller's own private skills and their orgs' shared skills never enter
    # the public quality gate or embedding index (see
    # _best_private_skill_match) -- when one matches, it wins outright over
    # public results, and org skills are listed first so an equal-scoring tie
    # falls to the org standard rather than a personal copy.
    private_match = store.list_routable_private_skills(user["id"]) if user else []
    private_match = _best_private_skill_match(query, private_match) if private_match else None

    if private_match:
        policy_item = None
        skill = _public_skill(_private_skill_as_row(private_match))
        selected_role = "private"
        private_content = private_match["content"]
        skill["content_hash"] = content_hash(private_content)
        tier = "hint"
        capability_flags = skill_capability_flags(private_content)
        if capability_flags:
            skill["capability_flags"] = capability_flags
        context_guard = _empty_context_guard("private_hint")
        warnings.append("Private skills are hint-only until they pass the public verification and capability gates.")
    elif tier == "full" and skill:
        content_start = time.monotonic()
        library = LibraryContent()
        text = (
            policy_item["_content"]
            if selected_role == "policy" and policy_item
            else preverified_primary_text
            or str(primary_results[0].get("_content") or "")
            or library.get(skill.get("url") or "")
        )
        # Version pinning: a pinned skill serves the pinned hash's content, so
        # an upstream update never changes what this account gets until they
        # unpin (or re-pin to roll forward). All verification below still runs
        # against the pinned text.
        pin = None if selected_role == "policy" else await asyncio.to_thread(
            store.get_pin, user["id"], str(skill.get("id") or "")
        )
        if pin and text and pin["content_hash"] != skill.get("content_hash"):
            pinned_text = await asyncio.to_thread(_library_content_by_hash, pin["content_hash"])
            if pinned_text:
                text = pinned_text
                skill["content_hash"] = pin["content_hash"]
                skill["pinned"] = True
                warnings.append("Serving the version pinned by this account; the skill has newer content.")
            else:
                warnings.append("Pinned version content is unavailable; serving the current version.")
        content_ms = int((time.monotonic() - content_start) * 1000)
        if not text:
            tier = "hint"
            warnings.append("Matched skill has no locally stored SKILL.md content; downgraded to hint.")
        elif not has_valid_skill_frontmatter(text):
            # Backfill is deliberately asynchronous on the large legacy
            # corpus. Keep the serving path safe even before it has reached
            # every old row.
            tier = "hint"
            warnings.append("Matched content is not a valid SKILL.md document; downgraded to hint.")
        elif not skill.get("content_hash"):
            tier = "hint"
            warnings.append("Matched skill has no indexed content hash; downgraded to hint.")
        elif content_hash(text) != skill.get("content_hash"):
            tier = "hint"
            warnings.append("Matched skill content no longer matches its indexed hash; downgraded to hint.")
        else:
            digest = content_digest(text)
            context_guard = (
                _candidate_context_guard(query, primary_results[0], text, body.max_capsule_chars)
                if CONTEXT_GUARD_ENABLED
                else _empty_context_guard("safe-capsule-required")
            )
            if context_guard.get("delivery") != "capsule":
                tier = "hint"
                warnings.append("Public skill could not be safely distilled; downgraded to hint.")
            else:
                warnings.append("Verified public skill delivered as the whole safety-stripped skill.")
            skill["verification"] = {
                "content_hash_verified": True,
                "safe_distilled_capsule": context_guard.get("delivery") == "capsule",
                "hash_kind": "canonical_normalized",
                "content_digest": digest,
                "capsule_digest": context_guard.get("capsule_digest"),
                "source": "indexed-local-copy",
                "publisher_verified": False,
            }

    # Family policies are modifiers, never standalone task routes. If the
    # specialist cannot remain full, discard the policy rather than promoting it.
    if tier != "full" or selected_role in {"policy", "private"}:
        policy_item = None

    if skill:
        skill["role"] = selected_role or "specialist"
        skill["activation"] = "task-family-default" if selected_role == "policy" else (
            "private-hint" if selected_role == "private" else "primary"
        )
        skill["routing_tier"] = tier
        activation = skill.get("session_activation")
        if isinstance(activation, dict):
            activation["session_id"] = (body.session_id or route_id).strip()[:160]

    policy_skills = [_public_plan_item(policy_item)] if policy_item else []
    primary_plan = None
    if selected_role not in {"policy", "private"} and tier == "full" and skill:
        primary_plan = dict(skill)
    elif selected_role == "private" and skill:
        primary_plan = dict(skill)
    selected_roles = ["policy"] if policy_skills else []
    if primary_plan:
        selected_roles.append(primary_plan.get("role") or "specialist")
    skill_plan = {
        "task_family": task_analysis["family"],
        "policy_skills": policy_skills,
        "primary_skill": primary_plan,
        "supporting_skills": [],
        "selected_roles": selected_roles,
        "precedence": ["user-project-team", "policy", "primary", "supporting"],
        "composition_reason": (
            "Verified task-family policy plus the highest-confidence relevant specialist."
            if policy_skills and primary_plan
            else "Verified task-family policy; no specialist cleared all relevance and delivery gates."
            if policy_skills
            else "Highest-confidence relevant specialist; no verified task-family policy was available."
            if primary_plan
            else "No skill cleared the route gates."
        ),
    }

    if tier == "hint":
        context_guard = _empty_context_guard("confidence_or_safety_gate")
        context_guard["delivery"] = "hint"
    debug = _score_debug(results, tier)
    debug["intent_compiler"] = {
        "version": intent.compiler_version,
        "compressed_query": intent.compressed_query,
        "technology": list(intent.technology),
        "operation": list(intent.operation),
        "artifact": list(intent.artifact),
        "constraints": list(intent.constraints),
        "failure_mode": list(intent.failure_mode),
        "query_count": len(intent.query_variants),
    }
    debug["context_guard"] = {
        "policy": context_guard.get("policy"),
        "delivery": context_guard.get("delivery"),
        "reason": context_guard.get("reason"),
        "capsule_chars": context_guard.get("capsule_chars", 0),
        "estimated_tokens": context_guard.get("estimated_tokens", 0),
    }
    debug["retrieval_backend"] = sorted(
        {str(row.get("retrieval_backend") or "local") for row in results}
    )
    # The merged route planner currently exposes only primary results here;
    # supporting candidates are tracked in the plan when available but are
    # intentionally not allowed to displace the primary strategy in hints.
    candidates = _hint_candidates(primary_results) if tier == "hint" else []
    input_tokens = _estimate_tokens(query)
    hint_tokens = _estimate_tokens(skill)
    candidate_tokens = _estimate_candidate_tokens(candidates)
    content_tokens = _estimate_tokens(content)
    guard_tokens = estimate_guard_tokens(context_guard.get("capsule"))
    policy_tokens = sum(
        int(policy.get("estimated_tokens") or 0)
        for policy in policy_skills
        if selected_role != "policy"
    )
    injected_tokens = 0
    if tier == "full":
        injected_tokens = hint_tokens + (content_tokens or guard_tokens) + policy_tokens
    elif tier == "hint":
        injected_tokens = candidate_tokens
    response_preview = {
        "tier": tier,
        "skill": skill,
        "candidates": candidates,
        "content": content,
        "content_url": content_url,
        "context_guard": context_guard,
        "task_analysis": task_analysis,
        "skill_plan": skill_plan,
        "config_version": CONFIG_VERSION,
    }
    metrics = {
        "latency_ms": int((time.monotonic() - start) * 1000),
        "skill_find_ms": skill_find_ms,
        "retrieval_ms": retrieval_ms,
        "rerank_ms": rerank_ms,
        "routing_filters_ms": routing_filters_ms,
        "query_compile_ms": query_compile_ms,
        "query_variant_count": len(intent.query_variants),
        "primary_retrieval_ms": primary_retrieval_ms,
        "policy_lookup_ms": policy_lookup_ms,
        "candidate_rerank_ms": candidate_rerank_ms,
        "candidate_filter_ms": candidate_filter_ms,
        "deliverable_validation_ms": deliverable_validation_ms,
        "tier_decision_ms": tier_decision_ms,
        "policy_build_ms": policy_build_ms,
        "content_ms": content_ms,
        "result_count": len(results),
        "input_tokens": input_tokens,
        "hint_tokens": hint_tokens,
        "candidate_tokens": candidate_tokens,
        "content_tokens": content_tokens,
        "capsule_tokens": guard_tokens,
        "policy_tokens": policy_tokens,
        "skill_count": len(policy_skills) + (1 if primary_plan else 0),
        "injected_tokens": injected_tokens,
        "response_tokens": _estimate_tokens(response_preview),
        "latency_warn_ms": ROUTE_LATENCY_WARN_MS,
        "response_token_warn": ROUTE_RESPONSE_TOKEN_WARN,
    }
    if metrics["latency_ms"] > ROUTE_LATENCY_WARN_MS:
        warnings.append(f"Route latency exceeded {ROUTE_LATENCY_WARN_MS}ms budget.")
    if metrics["skill_find_ms"] > ROUTE_SKILL_FIND_WARN_MS:
        warnings.append(f"Skill find exceeded {ROUTE_SKILL_FIND_WARN_MS}ms budget.")
    if metrics["injected_tokens"] > ROUTE_INJECTED_TOKEN_WARN:
        warnings.append(f"Injected content exceeded {ROUTE_INJECTED_TOKEN_WARN} token budget.")
    if metrics["response_tokens"] > ROUTE_RESPONSE_TOKEN_WARN:
        warnings.append(f"Route response exceeded {ROUTE_RESPONSE_TOKEN_WARN} token budget.")
    debug["metrics"] = metrics
    if warnings:
        debug["warnings"] = warnings
    await _record_route_event(
        {
            "client": body.client[:80],
            "client_version": body.client_version[:80],
            "id": route_id,
            "user_id": user["id"] if user else None,
            "anonymous_id_hash": anonymous_id_hash,
            "ip_address": ip_address,
            "query_chars": len(query),
            "tier": tier,
            "skill_id": skill.get("id") if skill else None,
            "skill_name": skill.get("name") if skill else None,
            "skill_url": skill.get("source_url") if skill else None,
            "latency_ms": metrics["latency_ms"],
            "skill_find_ms": skill_find_ms,
            "retrieval_ms": retrieval_ms,
            "rerank_ms": rerank_ms,
            "content_ms": content_ms,
            "result_count": len(results),
            "input_tokens": metrics["input_tokens"],
            "hint_tokens": metrics["hint_tokens"],
            "candidate_tokens": metrics["candidate_tokens"],
            "content_tokens": metrics["content_tokens"],
            "capsule_tokens": metrics["capsule_tokens"],
            "injected_tokens": metrics["injected_tokens"],
            "response_tokens": metrics["response_tokens"],
            "config_version": CONFIG_VERSION,
            "guard_delivery": context_guard.get("delivery"),
            "capsule_chars": context_guard.get("capsule_chars", 0),
            "meaningfulness_score": skill.get("meaningfulness_score") if skill else None,
            "warnings": warnings,
        }
    )
    return {
        "tier": tier,
        "skill": skill,
        "candidates": candidates,
        "content": content,
        "content_url": content_url,
        "context_guard": context_guard,
        "task_analysis": task_analysis,
        "skill_plan": skill_plan,
        "route_id": route_id,
        "session_id": (body.session_id or route_id).strip()[:160],
        "score_debug": debug,
        "config_version": CONFIG_VERSION,
        "ttl": ROUTE_TTL_SECONDS,
    }


@router.post("/route-skip")
async def route_skip():
    """Deprecated metadata endpoint.

    Current clients skip locally and make no request. Keeping a 410 response
    for loopback callers makes the privacy change explicit without parsing or
    retaining a legacy raw-prompt body.
    """
    return Response(status_code=410)


async def _find_semantic(q: str, limit: int = 8, gate: bool = True, authorization: str | None = None):
    """Body-only discovery; results are never an actionable full route."""
    q = (q or "").strip()[:3000]
    limit = max(2, min(int(limit or 8), 20))
    if is_non_task_prompt(q):
        return {
            "results": [],
            "tier": "none",
            "gated": bool(gate),
            "message": "This prompt does not need a reusable skill route.",
            "score_debug": {"tier": "none", "reason": "non-task-prompt"},
            "config_version": CONFIG_VERSION,
        }

    async with httpx.AsyncClient() as client:
        results = await retrieve_skills(client, q, limit)
    tier = injection_tier(q, results, ranked=_results_are_ranked(results))

    # A private match is the caller's own trusted content (never another
    # user's) -- it's prepended regardless of the public similarity gate
    # below, same as /route bypassing the public quality gate for it.
    user = auth.user_from_authorization_header(authorization)
    private_row = None
    if user:
        private_match = _best_private_skill_match(q, store.list_private_skills(user["id"]))
        if private_match:
            private_row = _private_skill_as_row(private_match)
            tier = "hint"

    if gate and tier == "none":
        return {"results": [], "tier": tier, "gated": True,
                "message": f"No result cleared the similarity floor ({MIN_SIMILARITY}).",
                "score_debug": _score_debug(results, tier),
            "config_version": CONFIG_VERSION}
    if private_row:
        results = [private_row] + results
    return {"results": results, "tier": ("hint" if tier == "full" else tier),
            "score_debug": _score_debug(results, tier),
            "config_version": CONFIG_VERSION}


@router.post("/find-semantic")
async def find_semantic_post(
    body: SemanticSearchRequest,
    authorization: str | None = Header(None),
):
    """Body-based public search contract.

    JSON POST is the only search contract so task text never appears in
    access-log URLs.
    """
    _require_route_user(authorization)
    return await _find_semantic(body.q, body.limit, body.gate, authorization)


@router.get("/route-metrics")
async def route_metrics(hours: int = 24, config_version: str = CONFIG_VERSION):
    hours = max(1, min(int(hours or 24), 24 * 30))
    selected_config = (config_version or "").strip()
    if selected_config.lower() == "all":
        selected_config = ""
    summary = await asyncio.to_thread(
        store.route_event_summary,
        hours,
        config_version=selected_config or None,
        max_latency_ms=ROUTE_LATENCY_WARN_MS,
        max_skill_find_ms=ROUTE_SKILL_FIND_WARN_MS,
        max_injected_tokens=ROUTE_INJECTED_TOKEN_WARN,
        max_response_tokens=ROUTE_RESPONSE_TOKEN_WARN,
    )
    return {"ok": True, **summary, "config_version": CONFIG_VERSION}


@router.post("/route-feedback")
async def route_feedback(body: RouteFeedbackRequest, authorization: str | None = Header(None)):
    """Outcome feedback for route analytics, reported by the Claude Code hook,
    the CLI, and the hosted connector. Public callers need an account (see
    scraper.py's guards). Keep payloads privacy-safe: route_id plus a small
    enum-style outcome and source. ``note`` is accepted only for compatibility
    and is deliberately ignored rather than retained.
    """
    _require_route_user(authorization)
    outcome = (body.outcome or "").strip().lower()
    allowed = {"used", "skipped", "installed", "failed", "dismissed", "shown", "injected"}
    if outcome not in allowed:
        return Response(status_code=400)
    route_id = (body.route_id or "").strip()
    if not route_id:
        return Response(status_code=400)
    updated = await asyncio.to_thread(
        store.update_route_event_feedback,
        route_id,
        outcome,
        body.source,
    )
    if not updated:
        return Response(status_code=404)
    return {"ok": True, "route_id": route_id, "outcome": outcome}
