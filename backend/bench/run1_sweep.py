#!/usr/bin/env python3
"""Ingestion run 1 / Phase 4 — embedded-stratum sweep (sightings only).

Runs GitHub code search for the two `.claude/skills` deep-sweep queries with
adaptive `size:` slicing so the 1,000-hit-per-query cap can be worked around,
mirroring the approach in `scraper.py::sliced_code_search`. Every hit becomes a
row in `sightings`. Up to `--snapshot-limit` sighted packages are snapshotted
(entrypoint + file tree) content-addressed into `backend/skills_library_v1/`.

Deliberately does NOT import or run `scraper.py`: that module writes to the
production store. The slicing *pattern* is reused, the code is not.

Resumable: slice/page progress lives in `backend/sweep_state_v1.json` and every
write is idempotent, so re-running continues where it stopped.

Read-only w.r.t. every pre-existing artifact. Writes only:
  backend/enrichment_v1.db          (sightings)
  backend/skills_library_v1/        (content-addressed snapshots)
  backend/sweep_state_v1.json       (resume cursor)
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
LIB = BACKEND / "skills_library_v1"
STATE = BACKEND / "sweep_state_v1.json"

RUN_ID = "run1-20260802"
QUERIES = [
    "path:.claude/skills filename:SKILL.md",
    "path:.claude/skills",
]
CODE_SEARCH_MAX_SIZE = 384_000          # same ceiling scraper.py sweeps to
PER_PAGE = 100
MAX_PAGES = 10                          # search API hard cap: 1,000 results
MIN_INTERVAL = 6.5                      # code search is 10 req/min
MAX_SNAPSHOT_FILE_BYTES = 256 * 1024

_last_search = [0.0]


# ------------------------------------------------------------------ transport

def github_token() -> str:
    tok = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if tok:
        return tok
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    if hosts.exists():
        m = re.search(r"oauth_token:\s*(\S+)", hosts.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    raise SystemExit("FLAG: no GitHub token available — Phase 4 skipped")


class Budget:
    def __init__(self, searches: int, core: int):
        self.searches = searches
        self.core = core
        self.searches_used = 0
        self.core_used = 0

    def take_search(self) -> bool:
        if self.searches_used >= self.searches:
            return False
        self.searches_used += 1
        return True

    def take_core(self) -> bool:
        if self.core_used >= self.core:
            return False
        self.core_used += 1
        return True


def api(url: str, token: str, *, search: bool = False) -> tuple[int, dict]:
    if search:
        wait = MIN_INTERVAL - (time.time() - _last_search[0])
        if wait > 0:
            time.sleep(wait)
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "autoskill-run1-sweep",
    })
    for attempt in range(5):
        try:
            with urllib.request.urlopen(req, timeout=60) as f:
                body = json.loads(f.read() or b"{}")
                if search:
                    _last_search[0] = time.time()
                return f.status, body
        except urllib.error.HTTPError as e:
            if search:
                _last_search[0] = time.time()
            if e.code in (403, 429):
                # secondary rate limit / abuse detection: exponential backoff
                if attempt < 4:
                    time.sleep(min(120, 15 * (2 ** attempt)))
                    continue
            try:
                return e.code, json.loads(e.read() or b"{}")
            except Exception:
                return e.code, {}
        except Exception as e:
            if attempt == 4:
                return -1, {"error": str(e)}
            time.sleep(5)
    return -1, {}


def search_code(q: str, token: str, page: int, budget: Budget) -> tuple[int, dict]:
    if not budget.take_search():
        return -2, {}
    url = ("https://api.github.com/search/code?q=" + urllib.parse.quote(q, safe="")
           + f"&per_page={PER_PAGE}&page={page}")
    return api(url, token, search=True)


# ---------------------------------------------------------------------- state

def load_state() -> dict:
    if STATE.exists():
        try:
            return json.loads(STATE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"run_id": RUN_ID, "queries": {}, "snapshots_done": [], "history": []}


def save_state(st: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st, indent=2) + "\n", encoding="utf-8")
    os.replace(tmp, STATE)


def qstate(st: dict, q: str) -> dict:
    return st["queries"].setdefault(q, {
        "pending_slices": [[0, CODE_SEARCH_MAX_SIZE]],
        "done_slices": [],
        "oversized_slices": [],   # <=1 byte wide and still >1000 hits: unreachable tail
        "total_count": None,
        "hits_seen": 0,
        "complete": False,
    })


# ------------------------------------------------------------------- storage

def db() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.execute("PRAGMA journal_mode=WAL")
    return con


def sighting_id(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()[:32]


def record_sightings(con: sqlite3.Connection, items: list, collector: str, slice_label: str) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for it in items:
        repo = (it.get("repository") or {}).get("full_name") or ""
        path = it.get("path") or ""
        url = it.get("html_url") or f"https://github.com/{repo}/blob/HEAD/{path}"
        rows.append((sighting_id(url), RUN_ID, collector, slice_label, url, path,
                     f"{repo}::{path}", now, "pending", None))
    cur = con.executemany(
        "INSERT OR IGNORE INTO sightings"
        " (id,run_id,collector,query_or_slice,url,path,external_id,observed_at,resolution,resolution_detail)"
        " VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return cur.rowcount if cur.rowcount and cur.rowcount > 0 else 0


def store_object(content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()
    p = LIB / "objects" / h[:2] / h[2:4] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)
    return h


# ---------------------------------------------------------------- the sweep

def sweep_query(q: str, token: str, con: sqlite3.Connection, st: dict, budget: Budget) -> None:
    """Adaptive size-slicing, mirroring scraper.py::sliced_code_search."""
    s = qstate(st, q)
    if s["complete"]:
        print(f"  [{q}] already complete, skipping")
        return
    if s["total_count"] is None:
        status, body = search_code(q, token, 1, budget)
        if status == -2:
            return
        if status != 200:
            s.setdefault("errors", []).append({"status": status, "at": "total_count"})
            save_state(st)
            return
        s["total_count"] = body.get("total_count")
        save_state(st)

    while s["pending_slices"]:
        if budget.searches_used >= budget.searches:
            print(f"  [{q}] search budget exhausted; {len(s['pending_slices'])} slices left")
            save_state(st)
            return
        lo, hi = s["pending_slices"].pop()
        sq = f"{q} size:{lo}..{hi}"
        status, body = search_code(sq, token, 1, budget)
        if status == -2:
            s["pending_slices"].append([lo, hi])
            save_state(st)
            return
        if status != 200:
            s.setdefault("errors", []).append({"slice": [lo, hi], "status": status,
                                               "body": str(body)[:200]})
            save_state(st)
            continue
        total = body.get("total_count", 0)
        if total == 0:
            s["done_slices"].append([lo, hi, 0])
            save_state(st)
            continue
        if total > 1000 and lo < hi:
            mid = (lo + hi) // 2
            s["pending_slices"].append([lo, mid])
            s["pending_slices"].append([mid + 1, hi])
            save_state(st)
            continue
        if total > 1000 and lo >= hi:
            # a single byte-size bucket with >1000 files: the tail is unreachable
            # through this API. Record it rather than silently truncating.
            s["oversized_slices"].append([lo, hi, total])

        new = record_sightings(con, body.get("items", []), "github_code_search", sq)
        s["hits_seen"] += len(body.get("items", []))
        pages = min(MAX_PAGES, (min(total, 1000) + PER_PAGE - 1) // PER_PAGE)
        for page in range(2, pages + 1):
            if budget.searches_used >= budget.searches:
                s["pending_slices"].append([lo, hi])
                print(f"  [{q}] budget hit mid-slice {lo}..{hi}; requeued")
                save_state(st)
                return
            status, body = search_code(sq, token, page, budget)
            if status == -2:
                s["pending_slices"].append([lo, hi])
                save_state(st)
                return
            if status != 200:
                s.setdefault("errors", []).append({"slice": [lo, hi], "page": page,
                                                   "status": status})
                break
            new += record_sightings(con, body.get("items", []), "github_code_search", sq)
            s["hits_seen"] += len(body.get("items", []))
        s["done_slices"].append([lo, hi, total])
        print(f"  [{q}] slice {lo}..{hi}: total={total} new_sightings={new} "
              f"(searches {budget.searches_used}/{budget.searches})", flush=True)
        save_state(st)

    s["complete"] = True
    save_state(st)
    print(f"  [{q}] COMPLETE — {len(s['done_slices'])} slices, {s['hits_seen']} hits seen")


# ------------------------------------------------------------- package snapshot

def snapshot_packages(token: str, con: sqlite3.Connection, st: dict, budget: Budget,
                      limit: int) -> dict:
    """Snapshot entrypoint + file tree for up to `limit` sighted SKILL.md packages."""
    stats = {"attempted": 0, "snapshotted": 0, "failed": 0, "skipped_non_entrypoint": 0,
             "files_stored": 0, "bytes_stored": 0}
    cur = con.execute(
        "SELECT id,url,path,external_id FROM sightings"
        " WHERE resolution='pending' AND path LIKE '%SKILL.md'"
        " ORDER BY id LIMIT ?", (limit * 3,))
    rows = cur.fetchall()
    done = set(st.get("snapshots_done", []))
    for sid, url, path, ext in rows:
        if stats["snapshotted"] >= limit:
            break
        if sid in done:
            continue
        if budget.core_used + 4 > budget.core:
            print("  core budget exhausted during snapshots")
            break
        repo = (ext or "").split("::", 1)[0]
        if not repo or "/" not in repo:
            continue
        stats["attempted"] += 1
        skill_dir = str(Path(path).parent).replace("\\", "/")

        # entrypoint
        budget.take_core()
        s1, b1 = api(f"https://api.github.com/repos/{repo}/contents/"
                     + urllib.parse.quote(path), token)
        if s1 != 200 or b1.get("encoding") != "base64":
            con.execute("UPDATE sightings SET resolution='fetch_failed', resolution_detail=?"
                        " WHERE id=?", (f"contents status={s1}", sid))
            con.commit()
            stats["failed"] += 1
            continue
        try:
            entry_bytes = base64.b64decode(b1.get("content") or "")
        except Exception:
            con.execute("UPDATE sightings SET resolution='fetch_failed',"
                        " resolution_detail='b64 decode' WHERE id=?", (sid,))
            con.commit()
            stats["failed"] += 1
            continue
        entry_hash = store_object(entry_bytes)
        stats["files_stored"] += 1
        stats["bytes_stored"] += len(entry_bytes)

        # sibling tree (the skill directory listing)
        budget.take_core()
        s2, b2 = api(f"https://api.github.com/repos/{repo}/contents/"
                     + urllib.parse.quote(skill_dir), token)
        tree = []
        if s2 == 200 and isinstance(b2, list):
            for e in b2:
                tree.append({"path": e.get("path"), "name": e.get("name"),
                             "type": e.get("type"), "size": e.get("size"),
                             "sha": e.get("sha")})

        manifest = {
            "schema_version": 1,
            "run_id": RUN_ID,
            "sighting_id": sid,
            "repo": repo,
            "entrypoint_path": path,
            "entrypoint_sha256": entry_hash,
            "entrypoint_bytes": len(entry_bytes),
            "skill_dir": skill_dir,
            "tree": tree,
            "tree_status": s2,
            "source_url": url,
            "snapshotted_at": datetime.now(timezone.utc).isoformat(),
        }
        mh = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
        mp = LIB / "manifests" / f"{mh}.json"
        mp.parent.mkdir(parents=True, exist_ok=True)
        mp.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")

        con.execute("UPDATE sightings SET resolution='snapshotted', resolution_detail=?"
                    " WHERE id=?", (mh, sid))
        con.commit()
        done.add(sid)
        stats["snapshotted"] += 1
        if stats["snapshotted"] % 10 == 0:
            print(f"  snapshotted {stats['snapshotted']}/{limit}", flush=True)
    st["snapshots_done"] = sorted(done)
    save_state(st)
    return stats


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--search-budget", type=int, default=110,
                    help="max code-search requests this run (10/min limit)")
    ap.add_argument("--core-budget", type=int, default=400)
    ap.add_argument("--snapshot-limit", type=int, default=100)
    ap.add_argument("--skip-snapshots", action="store_true")
    args = ap.parse_args()

    token = github_token()
    LIB.mkdir(parents=True, exist_ok=True)
    st = load_state()
    budget = Budget(args.search_budget, args.core_budget)
    con = db()

    started = datetime.now(timezone.utc).isoformat()
    print(f"sweep start {started}  search_budget={args.search_budget}")
    for q in QUERIES:
        print(f"query: {q}")
        sweep_query(q, token, con, st, budget)

    total_sightings = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    print(f"sightings total: {total_sightings}")

    snap = {"skipped": True}
    if not args.skip_snapshots:
        print(f"snapshotting up to {args.snapshot_limit} packages …")
        snap = snapshot_packages(token, con, st, budget, args.snapshot_limit)
        print("snapshots:", json.dumps(snap))

    st.setdefault("history", []).append({
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "searches_used": budget.searches_used,
        "core_used": budget.core_used,
        "sightings_total": con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0],
        "snapshots": snap,
    })
    save_state(st)
    con.close()
    print("state saved to", STATE)
    return 0


if __name__ == "__main__":
    sys.exit(main())
