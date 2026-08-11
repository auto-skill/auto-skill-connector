#!/usr/bin/env python3
"""Corpus v1 / step 2 — finish enumeration across ALL vendor skill directories.

Extends run1_sweep.py from 2 queries to 6, keeping the same adaptive `size:`
slicing (mirroring `scraper.py::sliced_code_search`) and the same sightings
schema. Migrates run-1/run-2 progress out of `sweep_state_v1.json` so nothing
already enumerated is re-fetched.

Sightings only: no judging, no package fetch, no dependence on the enrichment
harness. Resumable — slice progress is checkpointed after every slice.

Writes only: backend/enrichment_v1.db (sightings), backend/sweep_state_v2.json
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
STATE = BACKEND / "sweep_state_v2.json"
OLD_STATE = BACKEND / "sweep_state_v1.json"

RUN_ID = "corpusv1-20260802"
QUERIES = [
    "path:.claude/skills filename:SKILL.md",
    "path:.claude/skills",
    "path:.gemini/skills",
    "path:.codex/skills",
    "path:.cursor/skills",
    "path:.github/skills",
    # Coverage audit 2026-08-03: the namespace queries above reach only 19% of
    # the universe -- bare filename:SKILL.md counts ~602k files vs ~115k covered.
    # The unqualified query subsumes every namespace (bare skills/ dirs ~220k,
    # no-"skills"-segment paths ~216k, .agents/.opencode/.windsurf/... tails);
    # adaptive size-slicing handles the volume, round-robin gives it budget.
    "filename:SKILL.md",
]
import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from run2_enrich import gh_trip_cooldown  # noqa: E402

CODE_SEARCH_MAX_SIZE = 384_000
PER_PAGE = 100
MAX_PAGES = 10
MIN_INTERVAL = 6.2
_last = [0.0]


def token() -> str:
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    m = re.search(r"oauth_token:\s*(\S+)", hosts.read_text(encoding="utf-8"))
    return m.group(1)


def search(q: str, tok: str, page: int = 1) -> tuple[int, dict]:
    wait = MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    url = ("https://api.github.com/search/code?q=" + urllib.parse.quote(q, safe="")
           + f"&per_page={PER_PAGE}&page={page}")
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "autoskill-corpusv1-sweep"})
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                _last[0] = time.time()
                return f.status, json.loads(f.read() or b"{}")
        except urllib.error.HTTPError as e:
            _last[0] = time.time()
            if e.code in (403, 429):
                # remaining=0 marks GitHub's SECONDARY limit, which is token-wide.
                # Every unit must stand down together -- any unit still calling
                # renews the block for all of them. Measured: clears only after
                # ~300s of TOTAL quiet.
                # NOT gh_trip_cooldown(): this unit calls /search/code, which
                # GitHub meters in a SEPARATE, far stricter bucket (10/min) from
                # the core API. Search 403s are routine here and say nothing
                # about core capacity -- tripping the shared cooldown on them
                # stood down enrich's unrelated content fetches, which failed
                # its canaries and discarded whole batches before judging.
                if str(e.headers.get("X-RateLimit-Remaining") or "") == "0":
                    return e.code, {}
            if e.code in (403, 429) and attempt < 4:
                time.sleep(min(150, 20 * (2 ** attempt)))
                continue
            return e.code, {}
        except Exception:
            if attempt == 4:
                return -1, {}
            time.sleep(5)
    return -1, {}


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    st = {"run_id": RUN_ID, "queries": {}, "history": []}
    if OLD_STATE.exists():                       # migrate run-1/run-2 progress
        old = json.loads(OLD_STATE.read_text(encoding="utf-8"))
        for q, s in old.get("queries", {}).items():
            if q in QUERIES:
                st["queries"][q] = {
                    "pending_slices": s.get("pending_slices", [[0, CODE_SEARCH_MAX_SIZE]]),
                    "done_slices": s.get("done_slices", []),
                    "oversized_slices": s.get("oversized_slices", []),
                    "total_count": s.get("total_count"),
                    "hits_seen": s.get("hits_seen", 0),
                    "complete": s.get("complete", False),
                    "migrated_from": "sweep_state_v1.json",
                }
        st["migrated_at"] = datetime.now(timezone.utc).isoformat()
    return st


def save(st: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=1) + "\n", encoding="utf-8")
    os.replace(tmp, STATE)


def qstate(st: dict, q: str) -> dict:
    return st["queries"].setdefault(q, {
        "pending_slices": [[0, CODE_SEARCH_MAX_SIZE]], "done_slices": [],
        "oversized_slices": [], "total_count": None, "hits_seen": 0, "complete": False})


def record(con: sqlite3.Connection, items: list, slice_label: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for it in items:
        repo = (it.get("repository") or {}).get("full_name") or ""
        path = it.get("path") or ""
        url = it.get("html_url") or f"https://github.com/{repo}/blob/HEAD/{path}"
        rows.append((hashlib.sha256(url.encode()).hexdigest()[:32], RUN_ID,
                     "github_code_search", slice_label, url, path,
                     f"{repo}::{path}", now, "pending", None))
    cur = con.executemany(
        "INSERT OR IGNORE INTO sightings"
        " (id,run_id,collector,query_or_slice,url,path,external_id,observed_at,"
        "  resolution,resolution_detail) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return max(0, cur.rowcount or 0)


def sweep(q: str, tok: str, con: sqlite3.Connection, st: dict, budget: dict) -> None:
    s = qstate(st, q)
    if s["complete"]:
        print(f"  [{q}] complete, skipping", flush=True)
        return
    if s["total_count"] is None:
        code, body = search(q, tok)
        budget["used"] += 1
        if code != 200:
            s.setdefault("errors", []).append({"at": "total_count", "status": code})
            save(st)
            return
        s["total_count"] = body.get("total_count")
        save(st)

    while s["pending_slices"]:
        if budget["used"] >= budget["max"]:
            print(f"  [{q}] budget exhausted, {len(s['pending_slices'])} slices left", flush=True)
            save(st)
            return
        lo, hi = s["pending_slices"].pop()
        sq = f"{q} size:{lo}..{hi}"
        code, body = search(sq, tok)
        budget["used"] += 1
        if code != 200:
            s.setdefault("errors", []).append({"slice": [lo, hi], "status": code})
            save(st)
            continue
        total = body.get("total_count", 0)
        if total == 0:
            s["done_slices"].append([lo, hi, 0])
            save(st)
            continue
        if total > 1000 and lo < hi:
            mid = (lo + hi) // 2
            s["pending_slices"] += [[lo, mid], [mid + 1, hi]]
            save(st)
            continue
        if total > 1000 and lo >= hi:
            s["oversized_slices"].append([lo, hi, total])
        new = record(con, body.get("items", []), sq)
        s["hits_seen"] += len(body.get("items", []))
        pages = min(MAX_PAGES, (min(total, 1000) + PER_PAGE - 1) // PER_PAGE)
        for page in range(2, pages + 1):
            if budget["used"] >= budget["max"]:
                s["pending_slices"].append([lo, hi])
                save(st)
                return
            code, body = search(sq, tok, page)
            budget["used"] += 1
            if code != 200:
                s.setdefault("errors", []).append({"slice": [lo, hi], "page": page,
                                                   "status": code})
                break
            new += record(con, body.get("items", []), sq)
            s["hits_seen"] += len(body.get("items", []))
        s["done_slices"].append([lo, hi, total])
        print(f"  [{q}] {lo}..{hi}: total={total} new={new} "
              f"({budget['used']}/{budget['max']})", flush=True)
        save(st)
    s["complete"] = True
    save(st)
    print(f"  [{q}] COMPLETE — {len(s['done_slices'])} slices, {s['hits_seen']} hits", flush=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search-budget", type=int, default=3000)
    args = ap.parse_args()
    tok = token()
    st = load_state()
    # Six processes write this DB now. A 60s timeout errored the whole run
    # when a harvester held the write lock; busy_timeout waits instead, which
    # is what a discovery loop should do rather than abandoning its budget.
    con = sqlite3.connect(DB, timeout=300)
    # synchronous=NORMAL is SQLite's recommended setting for WAL mode: still
    # corruption-safe and still durable against process crash, trading only the
    # last few transactions on machine power loss. Measured on this volume:
    # FULL = 244 ms/commit, NORMAL = 2 ms (122x). With six writers committing
    # continuously that fsync was a dominant, invisible cost.
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=300000")
    budget = {"used": 0, "max": args.search_budget}
    started = datetime.now(timezone.utc).isoformat()
    print(f"sweep start {started} budget={args.search_budget}", flush=True)
    # Round-robin the budget across INCOMPLETE queries. The old sequential loop
    # let query 1 exhaust the whole per-run budget every run: after 12 runs the
    # other five queries -- ~160k results in .codex/.cursor/.gemini/.github
    # namespaces -- had literally never executed a single search (hits_seen=0).
    # A complete corpus cannot come from one query; every incomplete query gets
    # a guaranteed share each run.
    incomplete = [q for q in QUERIES
                  if not st.get("queries", {}).get(q, {}).get("complete")]
    share = max(10, args.search_budget // max(1, len(incomplete)))
    for q in QUERIES:
        print(f"query: {q}", flush=True)
        qb = {"used": 0, "max": min(share, budget["max"] - budget["used"])}
        sweep(q, tok, con, st, qb)
        budget["used"] += qb["used"]
        if budget["used"] >= budget["max"]:
            break
    total = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    st["history"].append({"started_at": started,
                          "finished_at": datetime.now(timezone.utc).isoformat(),
                          "searches_used": budget["used"], "sightings_total": total})
    save(st)
    print(f"sightings total: {total}")
    for q in QUERIES:
        s = st["queries"].get(q, {})
        print(f"  {q}: complete={s.get('complete')} total={s.get('total_count')} "
              f"hits={s.get('hits_seen')} pending={len(s.get('pending_slices', []))}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
