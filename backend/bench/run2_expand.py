#!/usr/bin/env python3
"""Corpus v1 / step 4a — expansion pass.

Every `entrypoint_absent` repo-level corpus row becomes ONE bounded recursive tree
scan, emitting sightings for skills-directory paths only. This is the fix for the
run-1 finding that ~44% of sampled corpus rows point at a *repo*, not a skill file,
and so can never be routed.

One `GET /git/trees/{ref}?recursive=1` per repo (bounded, cached, resumable). Only
paths under a recognised skills directory AND named like an entrypoint are emitted.

Writes only: backend/enrichment_v1.db (sightings), backend/expand_state_v1.json
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
WORK = BACKEND / "corpus_v0_work.sqlite"
STATE = BACKEND / "expand_state_v1.json"
RUN_ID = "corpusv1-expand"

# Directory conventions that actually hold agent skills, across vendors.
SKILL_DIR_RE = re.compile(
    r"(^|/)(\.claude|\.gemini|\.codex|\.cursor|\.github|\.agents|\.continue|\.agent)/skills/|"
    r"(^|/)skills/|(^|/)bundled_skills/|(^|/)Skills/",
    re.IGNORECASE,
)
ENTRY_NAME_RE = re.compile(r"^(SKILL|AGENTS?)\.md$", re.IGNORECASE)
MAX_TREE_ENTRIES = 60_000          # refuse pathological monorepos
MAX_EMIT_PER_REPO = 300


def token() -> str:
    t = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if t:
        return t
    hosts = Path.home() / ".config" / "gh" / "hosts.yml"
    return re.search(r"oauth_token:\s*(\S+)", hosts.read_text(encoding="utf-8")).group(1)


import sys as _sys
_sys.path.insert(0, str(Path(__file__).resolve().parent))
from run2_enrich import gh_trip_cooldown  # noqa: E402

def api(url: str, tok: str) -> tuple[int, object]:
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}", "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "autoskill-corpusv1-expand"})
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=90) as f:
                return f.status, json.loads(f.read() or b"{}")
        except urllib.error.HTTPError as e:
            if e.code in (403, 429):
                # remaining=0 marks GitHub's SECONDARY limit, which is token-wide.
                # Every unit must stand down together -- any unit still calling
                # renews the block for all of them. Measured: clears only after
                # ~300s of TOTAL quiet.
                if str(e.headers.get("X-RateLimit-Remaining") or "") == "0":
                    gh_trip_cooldown()
                    return e.code, {}
            if e.code in (403, 429) and attempt < 3:
                time.sleep(min(120, 15 * (2 ** attempt)))
                continue
            return e.code, {}
        except Exception:
            if attempt == 3:
                return -1, {}
            time.sleep(4)
    return -1, {}


def load_state() -> dict:
    if STATE.exists():
        return json.loads(STATE.read_text(encoding="utf-8"))
    return {"run_id": RUN_ID, "done_repos": {}, "history": []}


def save(st: dict) -> None:
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(st) + "\n", encoding="utf-8")
    os.replace(tmp, STATE)


def repo_level_rows(limit: int | None) -> list[str]:
    """Corpus rows that carry only a repo URL — no entrypoint path."""
    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
    repos: dict[str, None] = {}
    for (url, raw) in con.execute(
            "select url, raw from skills where url like 'https://github.com/%'"
            " and quality_status in ('active','metadata_only','pending')"):
        try:
            d = json.loads(raw or "{}")
        except Exception:
            d = {}
        if d.get("parent_repo") and d.get("path"):
            continue                     # already has an entrypoint; not our problem
        parts = url.split("github.com/", 1)[1].strip("/").split("/")
        if len(parts) >= 2 and parts[0] and parts[1]:
            repos.setdefault(f"{parts[0]}/{parts[1]}", None)
    con.close()
    out = sorted(repos)
    return out[:limit] if limit else out


def emit(con: sqlite3.Connection, repo: str, paths: list[str]) -> int:
    now = datetime.now(timezone.utc).isoformat()
    rows = []
    for p in paths:
        url = f"https://github.com/{repo}/blob/HEAD/{p}"
        rows.append((hashlib.sha256(url.encode()).hexdigest()[:32], RUN_ID,
                     "repo_tree_expansion", f"repo:{repo}", url, p,
                     f"{repo}::{p}", now, "pending", None))
    cur = con.executemany(
        "INSERT OR IGNORE INTO sightings"
        " (id,run_id,collector,query_or_slice,url,path,external_id,observed_at,"
        "  resolution,resolution_detail) VALUES (?,?,?,?,?,?,?,?,?,?)", rows)
    con.commit()
    return max(0, cur.rowcount or 0)


def scan_repo(repo: str, tok: str) -> tuple[str, list[str]]:
    st, body = api(f"https://api.github.com/repos/{repo}/git/trees/HEAD?recursive=1", tok)
    if st == 404:
        return "gone", []
    if st != 200 or not isinstance(body, dict):
        return f"http_{st}", []
    tree = body.get("tree") or []
    if len(tree) > MAX_TREE_ENTRIES:
        return "tree_too_large", []
    hits = []
    for e in tree:
        if e.get("type") != "blob":
            continue
        p = e.get("path") or ""
        if not SKILL_DIR_RE.search(p):
            continue
        if not ENTRY_NAME_RE.match(p.rsplit("/", 1)[-1]):
            continue
        hits.append(p)
        if len(hits) >= MAX_EMIT_PER_REPO:
            break
    return ("truncated" if body.get("truncated") else "ok"), hits


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--budget", type=int, default=1500, help="max repo tree scans this run")
    ap.add_argument("--limit-repos", type=int)
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

    repos = repo_level_rows(args.limit_repos)
    todo = [r for r in repos if r not in st["done_repos"]]
    print(f"repo-level rows: {len(repos)} distinct repos; {len(st['done_repos'])} already scanned; "
          f"{len(todo)} remaining; budget {args.budget}", flush=True)

    started = datetime.now(timezone.utc).isoformat()
    used = 0
    emitted = 0
    stats: dict[str, int] = {}
    for repo in todo:
        if used >= args.budget:
            break
        status, paths = scan_repo(repo, tok)
        used += 1
        stats[status] = stats.get(status, 0) + 1
        n = emit(con, repo, paths) if paths else 0
        emitted += n
        # A 403/409/5xx is OUR failure, not a fact about the repo. Marking those
        # done retired 113 repos from the corpus on the first throttle they ever
        # hit -- the same transient-becomes-permanent bug already fixed in
        # enrichment and treeharvest, still live here.
        if status in ("ok", "truncated", "gone", "tree_too_large"):
            st["done_repos"][repo] = {"status": status, "found": len(paths), "new": n}
        if used % 25 == 0:
            save(st)
            print(f"  {used}/{args.budget} scanned, {emitted} new sightings, {stats}", flush=True)
    save(st)
    total = con.execute("SELECT COUNT(*) FROM sightings").fetchone()[0]
    st["history"].append({"started_at": started,
                          "finished_at": datetime.now(timezone.utc).isoformat(),
                          "scanned": used, "new_sightings": emitted, "statuses": stats,
                          "sightings_total": total,
                          "repos_remaining": len(todo) - used})
    save(st)
    print(f"\nscanned {used} repos, emitted {emitted} new sightings")
    print("statuses:", json.dumps(stats))
    print(f"sightings total now: {total}; repos still unscanned: {len(todo) - used}")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
