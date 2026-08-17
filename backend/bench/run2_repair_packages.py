#!/usr/bin/env python3
"""Recover packages held out of the servable corpus by a partial closure.

A package is servable only when it is `complete` AND its dependency closure is
`complete` -- zero unresolved references. Measured 2026-08-12: 41,569 packages
are complete in themselves but reference files we never captured, so a retriever
honouring the closure would ship a table of contents with no chapters.

Those references are overwhelmingly real files that still exist upstream: of a
25-ref live sample, 21 returned HTTP 200. They were missed because capture is
bounded (skill dir + byte caps), not because they are absent. Re-fetching them
costs ZERO judge quota -- it is pure GitHub I/O -- so this is the cheapest
corpus growth available, and it is the only growth available while the judge is
blacked out on a usage limit.

Fetches go through raw.githubusercontent (`raw_url_fetch`), which is OFF the REST
quota, so this does not compete with the enrich fetch budget or risk the
secondary abuse limit that pins that budget to concurrency 3.

Not every reference is recoverable and this does not pretend otherwise: shell
variables ($VAR), globs, and parent traversal are runtime constructs that no
fetch can resolve (~4% of refs). Those packages stay `partial`, correctly.

Idempotent. Only writes the content-addressed object store and the manifest of
packages it actually completes. Never downgrades a package.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BENCH))
from run2_repofetch import raw_url_fetch  # noqa: E402

WORK = BACKEND / "corpus_v0_work.sqlite"
LIB = BACKEND / "skills_library_v1"
CONCURRENCY = int(os.environ.get("AUTOSKILL_REPAIR_CONCURRENCY", "16"))
MAX_BYTES = 256 * 1024

# References no fetch can ever resolve -- they are resolved at runtime on the
# user's machine, not in the repo. Counting them as failures would make the
# recovery rate look worse than it is and would retry them forever.
UNRESOLVABLE = re.compile(r"[$*?{]|\.\.")


def store(content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()
    p = LIB / "objects" / h[:2] / h[2:4] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)
    return h


def repo_of(source_url: str) -> str | None:
    m = re.search(r"github\.com/([^/]+/[^/]+?)(?:/|$)", source_url or "")
    return m.group(1) if m else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="max packages (0=all)")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    con = sqlite3.connect(WORK)
    con.execute("pragma busy_timeout=120000")
    q = ("select package_hash, source_url, source_commit_sha, manifest_json"
         " from skill_packages"
         " where completeness_status='complete' and dependency_closure_status='partial'")
    if a.limit:
        q += f" limit {a.limit}"
    rows = con.execute(q).fetchall()
    print(f"  candidate packages: {len(rows):,}", flush=True)

    completed = partial_still = skipped = 0
    fetched_ok = fetched_404 = unresolvable = 0
    t0 = time.time()

    for i, (phash, src, commit, mj) in enumerate(rows, 1):
        try:
            m = json.loads(mj)
        except Exception:
            skipped += 1
            continue
        repo = repo_of(src)
        unres = m.get("unresolved_references") or []
        if not repo or not unres:
            skipped += 1
            continue

        # A commit sha pins the fetch to the exact tree the package came from;
        # HEAD may have moved on and would give us bytes the verdict never saw.
        ref = commit or "HEAD"
        targets = [u for u in unres if not UNRESOLVABLE.search(u)]
        unresolvable += len(unres) - len(targets)
        if not targets:
            partial_still += 1
            continue

        def one(path):
            # Only 404 means absent; throttling must not be recorded as gone.
            for attempt in (0, 1):
                try:
                    st, body = raw_url_fetch(repo, path, ref=ref)
                except Exception:
                    st, body = 0, None
                if st in (200, 404):
                    return path, st, body
                if attempt == 0:
                    import time as _t; _t.sleep(25)
            return path, st, None

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            results = list(ex.map(one, targets))

        got = {}
        for path, st, body in results:
            if st == 200 and body is not None and len(body) <= MAX_BYTES:
                got[path] = body
                fetched_ok += 1
            else:
                fetched_404 += 1

        if not got:
            partial_still += 1
            continue

        # Only a package whose EVERY resolvable reference came back can move to
        # complete; a partial recovery is still a hole, and shipping it would
        # defeat the gate this is trying to satisfy.
        still_missing = [p for p in targets if p not in got]
        new_files = list(m.get("files") or [])
        for path, body in got.items():
            if a.dry_run:
                continue
            h = store(body)
            new_files.append({
                "path": path,
                "raw_sha256": h,
                "role": "dependency",
                "in_dependency_closure": True,
                "media_type": "text/markdown" if path.endswith(".md") else "text/plain",
                "recovered_by": "run2_repair_packages",
            })

        remaining = still_missing + [u for u in unres if UNRESOLVABLE.search(u)]
        if remaining:
            partial_still += 1
            if not a.dry_run:
                m["files"] = new_files
                m["unresolved_references"] = remaining
                m["stored_files"] = len(new_files)
                con.execute("update skill_packages set manifest_json=? where package_hash=?",
                            (json.dumps(m, ensure_ascii=False), phash))
        else:
            completed += 1
            if not a.dry_run:
                m["files"] = new_files
                m["unresolved_references"] = []
                m["dependency_closure_status"] = "complete"
                m["stored_files"] = len(new_files)
                con.execute(
                    "update skill_packages set manifest_json=?,"
                    " dependency_closure_status='complete' where package_hash=?",
                    (json.dumps(m, ensure_ascii=False), phash))

        if i % 200 == 0:
            con.commit()
            el = time.time() - t0
            print(f"    {i:,}/{len(rows):,}  completed={completed:,}"
                  f"  still-partial={partial_still:,}"
                  f"  files+{fetched_ok:,}  ({i/max(el,1):.1f} pkg/s)", flush=True)

    if not a.dry_run:
        con.commit()
    con.close()

    print(f"\n  === REPAIR DONE ({time.time()-t0:.0f}s) ===")
    print(f"    packages COMPLETED (now servable): {completed:,}")
    print(f"    still partial:                     {partial_still:,}")
    print(f"    skipped (no repo/refs):            {skipped:,}")
    print(f"    files recovered:                   {fetched_ok:,}")
    print(f"    files gone upstream:               {fetched_404:,}")
    print(f"    refs unresolvable by design:       {unresolvable:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
