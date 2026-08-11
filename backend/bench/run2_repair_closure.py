#!/usr/bin/env python3
"""Re-fetch closure files that were throttled during a batch.

`fetch_closure` records a file GitHub refused us as `throttled` rather than
`missing` — correctly, because a 403 says nothing about whether the file exists.
But nothing ever went back for them: once a batch completed, its throttled files
were never retried, so a transient refusal became a permanent hole in the skill.

Measured before this existed: 25 throttled files across the corpus, 0 of them on
disk. Small in absolute terms, but they are exactly the reference docs and
scripts that make a skill useful (`references/sql.md` among them), and the count
grows with concurrency.

Idempotent and safe to run repeatedly. Only writes: the content-addressed store
and the `closure` block of the batch files it repairs.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
sys.path.insert(0, str(BENCH))
from run2_repofetch import raw_url_fetch  # noqa: E402

LIB = BENCH.parent / "skills_library_v1"
CONCURRENCY = int(os.environ.get("AUTOSKILL_REPAIR_CONCURRENCY", "12"))
MAX_BYTES = 256 * 1024


def store(content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()
    p = LIB / "objects" / h[:2] / h[2:4] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)
    return h


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batches", nargs="*")
    args = ap.parse_args()

    files = ([Path(b) for b in args.batches] if args.batches
             else sorted(BENCH.glob("run2_combined_b*.json"),
                         key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)))
    total = recovered = still_bad = 0

    for f in files:
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        jobs = []
        for idx, r in enumerate(d.get("rows", [])):
            for t in ((r.get("closure") or {}).get("throttled") or []):
                jobs.append((idx, r.get("repo"), t.split(" (HTTP")[0]))
        if not jobs:
            continue
        total += len(jobs)

        def one(job):
            idx, repo, path = job
            st, body = raw_url_fetch(repo, path)
            return idx, path, st, body

        with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
            results = list(ex.map(one, jobs))

        changed = False
        for idx, path, st, body in results:
            r = d["rows"][idx]
            cl = r.setdefault("closure", {})
            if st == 200 and body is not None and len(body) <= MAX_BYTES:
                h = store(body)
                cl.setdefault("fetched", []).append(path)
                cl.setdefault("content_by_path", {})[path] = h
                cl["throttled"] = [t for t in (cl.get("throttled") or [])
                                   if not t.startswith(path)]
                recovered += 1
                changed = True
            elif st == 404 or (st == 200 and body is not None and len(body) > MAX_BYTES):
                # Terminal, not transient: either the file is gone, or it is over
                # our per-file cap (one was 2.9MB — the contents API refuses to
                # return bodies over 1MB, which is how it got misfiled as a
                # throttle in the first place). Either way stop retrying it.
                why = "gone" if st == 404 else f">{MAX_BYTES}B"
                cl.setdefault("missing", []).append(f"{path} ({why})")
                cl["throttled"] = [t for t in (cl.get("throttled") or [])
                                   if not t.startswith(path)]
                changed = True
            else:
                still_bad += 1
        if changed:
            tmp = f.with_suffix(".tmp")
            tmp.write_text(json.dumps(d, indent=1))
            os.replace(tmp, f)
            print(f"  repaired {f.name}", flush=True)

    print(f"\n  throttled seen={total} recovered={recovered} still-unavailable={still_bad}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
