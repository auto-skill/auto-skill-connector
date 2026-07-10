"""FastAPI router exposing local_store.py through the same tiny slice of the
PostgREST REST + RPC surface that scraper.py / recommender.py already speak
(GET/POST/PATCH/DELETE on /rest/v1/{skills,scrape_runs}, plus the three
/rest/v1/rpc/* search functions). Mounted into scraper.py's own FastAPI app
so scraper.py can just point SUPABASE_URL at its own loopback address.
"""
import asyncio
import math
import sqlite3

from fastapi import APIRouter, Request, Response

import local_store as store

router = APIRouter()

_KNOWN_PARAMS = {"select", "order", "limit", "offset", "on_conflict"}
_vector_warm_task: asyncio.Task | None = None
_vector_warm_generation = 0


def _schedule_vector_warm() -> None:
    """Debounce matrix rebuilds after batched scraper/embed writes.

    A worker can make dozens of small upserts in one burst. Building a new
    matrix after each would be worse than the original TTL churn, so wait until
    writes have been quiet briefly and rebuild once in the background.
    """
    global _vector_warm_task, _vector_warm_generation
    _vector_warm_generation += 1
    if _vector_warm_task is None or _vector_warm_task.done():
        _vector_warm_task = asyncio.create_task(_warm_vector_index_when_quiet())


async def _warm_vector_index_when_quiet() -> None:
    while True:
        generation = _vector_warm_generation
        await asyncio.sleep(2)
        if generation != _vector_warm_generation:
            continue
        try:
            stats = await asyncio.to_thread(store.warm_vector_index)
            print(f"[local_api] warmed vector cache with {stats['cache_vectors']} vectors")
        except Exception as exc:
            print(f"[local_api] vector cache warm failed: {exc}")
        if generation == _vector_warm_generation:
            return


@router.on_event("startup")
async def _init():
    store.init_db()


def _parse_filters(query_params) -> dict:
    return {k: v for k, v in query_params.items() if k not in _KNOWN_PARAMS}


@router.get("/rest/v1/{table}")
async def rest_get(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    params = dict(request.query_params)
    filters = _parse_filters(params)
    select = params.get("select")
    order = params.get("order")
    limit = int(params["limit"]) if "limit" in params else None

    range_start = range_end = None
    range_header = request.headers.get("range")
    if range_header and "-" in range_header:
        a, b = range_header.split("-", 1)
        range_start, range_end = int(a), int(b)
    elif "offset" in params:
        offset = int(params["offset"])
        range_start, range_end = offset, offset + (limit or 100) - 1

    count_exact = "count=exact" in (request.headers.get("prefer") or "")
    rows, total = await asyncio.to_thread(store.select_rows, table, select, filters, order, limit, range_start, range_end, count_exact)

    headers = {}
    if count_exact:
        end = (range_start or 0) + len(rows) - 1
        headers["Content-Range"] = f"{range_start or 0}-{max(end, 0)}/{total}"
    return Response(content=_dumps(rows), media_type="application/json", headers=headers)


@router.post("/rest/v1/{table}")
async def rest_post(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    body = await request.json()
    rows = body if isinstance(body, list) else [body]
    on_conflict = request.query_params.get("on_conflict")
    try:
        out = await asyncio.to_thread(store.upsert_rows, table, rows, on_conflict)
    except sqlite3.IntegrityError as exc:
        return Response(content=_dumps({"error": str(exc)}), media_type="application/json", status_code=409)
    if table == "skills" and out:
        _schedule_vector_warm()
    return Response(content=_dumps(out), media_type="application/json")


@router.patch("/rest/v1/{table}")
async def rest_patch(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    filters = _parse_filters(dict(request.query_params))
    data = await request.json()
    updated = await asyncio.to_thread(store.update_rows, table, filters, data)
    if table == "skills" and updated:
        _schedule_vector_warm()
    return Response(status_code=204)


@router.delete("/rest/v1/{table}")
async def rest_delete(table: str, request: Request):
    if table not in store.TABLES:
        return Response(status_code=404)
    filters = _parse_filters(dict(request.query_params))
    deleted = await asyncio.to_thread(store.delete_rows, table, filters)
    if table == "skills" and deleted:
        _schedule_vector_warm()
    return Response(status_code=204)


@router.post("/rest/v1/rpc/search_skills")
async def rpc_search_skills(request: Request):
    body = await request.json()
    rows = await asyncio.to_thread(store.search_skills_fts, body.get("query", ""), body.get("max_results", 10))
    return Response(content=_dumps(rows), media_type="application/json")


@router.post("/rest/v1/rpc/vector_search_skills")
async def rpc_vector_search_skills(request: Request):
    body = await request.json()
    try:
        emb = _parse_embedding(body.get("query_embedding"), required=True)
    except ValueError as exc:
        return Response(content=_dumps({"error": str(exc)}), media_type="application/json", status_code=400)
    rows = await asyncio.to_thread(store.vector_search_skills, emb, body.get("match_count", 10))
    return Response(content=_dumps(rows), media_type="application/json")


@router.post("/rest/v1/rpc/hybrid_search_skills")
async def rpc_hybrid_search_skills(request: Request):
    body = await request.json()
    try:
        emb = _parse_embedding(body.get("query_embedding"), required=False)
    except ValueError as exc:
        return Response(content=_dumps({"error": str(exc)}), media_type="application/json", status_code=400)
    rows = await asyncio.to_thread(
        store.hybrid_search_skills,
        body.get("query_text", ""),
        emb,
        body.get("match_count", 10),
        body.get("fts_weight", 1.0),
        body.get("vec_weight", 0.6),
        body.get("rrf_k", 20),
    )
    return Response(content=_dumps(rows), media_type="application/json")


@router.post("/rest/v1/rpc/recompute_feedback_scores")
async def rpc_recompute_feedback_scores(request: Request):
    body = await request.json() if await request.body() else {}
    updated = await asyncio.to_thread(
        store.recompute_feedback_scores,
        body.get("min_samples", 8),
        body.get("prior_strength", 8.0),
        body.get("prior_mean", 0.5),
    )
    return Response(content=_dumps({"updated": updated}), media_type="application/json")


def _parse_embedding(val, *, required: bool = False):
    if val is None:
        if required:
            raise ValueError("query_embedding is required")
        return None
    if isinstance(val, list):
        emb = val
    elif isinstance(val, str):
        import json as _json
        emb = _json.loads(val)
    else:
        raise ValueError("query_embedding must be a JSON array")

    if not isinstance(emb, list):
        raise ValueError("query_embedding must be a JSON array")
    if len(emb) != store.EMBEDDING_DIM:
        raise ValueError(f"query_embedding must have {store.EMBEDDING_DIM} dimensions")
    try:
        out = [float(value) for value in emb]
    except (TypeError, ValueError) as exc:
        raise ValueError("query_embedding must contain only numbers") from exc
    if not all(math.isfinite(value) for value in out):
        raise ValueError("query_embedding must contain only finite numbers")
    return out


def _dumps(obj) -> str:
    import json as _json
    return _json.dumps(obj)
