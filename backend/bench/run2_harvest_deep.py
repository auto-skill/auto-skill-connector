#!/usr/bin/env python3
"""Deep-harvest repos whose recursive tree listing was TRUNCATED.

GitHub caps `git/trees?recursive=1` at ~100k entries / 7MB. 41 harvested repos
hit that cap — and they are exactly the mega skill marketplaces, holding 42,398
visible skills with an unknown number cut off past the cap. Marking them
"truncated" and moving on would leave the census structurally undercounted at
its densest points.

Strategy: walk the tree by SUBTREE instead. One non-recursive listing at HEAD,
then a recursive listing per top-level directory (each gets its own 100k-entry
budget); recurse deeper only if a subtree is itself truncated. Cost per repo is
a handful of calls instead of one, bounded by MAX_CALLS_PER_REPO.

Updates repo_trees rows and flips repo_tree_meta.status truncated -> ok-deep
(or truncated-partial if the walk hit its own bound, so the gap stays visible).
Also retries repos stuck in error:5xx. Same pacing discipline as the harvester.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
DB = BACKEND / "enrichment_v1.db"
sys.path.insert(0, str(BENCH))
from run2_enrich import github_token  # noqa: E402
from run2_treeharvest import skill_rows  # noqa: E402

PACE = float(os.environ.get("AUTOSKILL_HARVEST_PACE", "0.7"))
MAX_CALLS_PER_REPO = int(os.environ.get("AUTOSKILL_DEEP_MAX_CALLS", "40"))


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def api(url: str, tok: str):
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}", "User-Agent": "autoskill-deepharvest",
        "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.urlopen(req, timeout=90) as fh:
            return fh.status, json.load(fh)
    except urllib.error.HTTPError as e:  # type: ignore[attr-defined]
        return e.code, None
    except Exception:
        return 0, None


def walk(repo: str, tok: str) -> tuple[list, bool, int]:
    """All blob entries via subtree recursion. Returns (entries, complete, calls)."""
    calls = 0

    def get_tree(sha_or_ref: str, recursive: bool):
        nonlocal calls
        time.sleep(PACE)
        calls += 1
        url = (f"https://api.github.com/repos/{repo}/git/trees/{sha_or_ref}"
               + ("?recursive=1" if recursive else ""))
        st, body = api(url, tok)
        if st == 403:
            time.sleep(45)
            calls += 1
            st, body = api(url, tok)
        return body if st == 200 and isinstance(body, dict) else None

    out: list = []
    complete = True

    def descend(sha_or_ref: str, prefix: str, depth: int):
        nonlocal complete
        if calls >= MAX_CALLS_PER_REPO or depth > 4:
            complete = False
            return
        body = get_tree(sha_or_ref, recursive=True)
        if body is None:
            complete = False
            return
        if not body.get("truncated"):
            for e in body.get("tree") or []:
                if e.get("type") in ("blob", "tree"):
                    out.append({**e, "path": (prefix + e["path"]) if prefix else e["path"]})
            return
        # truncated: list this level flat, recurse into each subdirectory
        flat = get_tree(sha_or_ref, recursive=False)
        if flat is None:
            complete = False
            return
        for e in flat.get("tree") or []:
            p = (prefix + e["path"]) if prefix else e["path"]
            if e.get("type") == "blob":
                out.append({**e, "path": p})
            elif e.get("type") == "tree":
                descend(e["sha"], p + "/", depth + 1)

    descend("HEAD", "", 0)
    return out, complete, calls


def main() -> int:
    tok = github_token()
    con = sqlite3.connect(DB, timeout=300)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA busy_timeout=300000")
    con.execute("PRAGMA synchronous=NORMAL")
    targets = [r[0] for r in con.execute(
        "select repo from repo_tree_meta where status='truncated'"
        " or status like 'error:5%'")]
    print(f"deep-harvest: {len(targets)} truncated/errored repos", flush=True)
    for repo in targets:
        entries, complete, calls = walk(repo, tok)
        rows = skill_rows(entries)
        if not rows and not complete:
            print(f"  {repo}: walk failed ({calls} calls), left as-is", flush=True)
            continue
        con.execute("delete from repo_trees where repo=?", (repo,))
        con.executemany("insert or replace into repo_trees values (?,?,?,?,?,?)",
                        [(repo, p, sha, mode, size, now())
                         for p, sha, mode, size in rows])
        status = "ok-deep" if complete else "truncated-partial"
        con.execute("insert or replace into repo_tree_meta values (?,?,?,?,?)",
                    (repo, status, len(rows),
                     sum(1 for p, *_ in rows if p.endswith("SKILL.md")), now()))
        con.commit()
        print(f"  {repo}: {len(rows)} skill-files, "
              f"{sum(1 for p, *_ in rows if p.endswith('SKILL.md'))} skills, "
              f"{calls} calls, {status}", flush=True)
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
