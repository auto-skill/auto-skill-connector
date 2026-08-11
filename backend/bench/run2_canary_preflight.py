#!/usr/bin/env python3
"""Check canary FETCH health before spending any Luna calls.

The canary gate runs after `combine`, i.e. after the batch has already been
judged. When canaries come back unfetched (GitHub availability, not a judging
regression) the batch is discarded -- and the ~520 Luna calls it consumed are
gone with it.

Measured 2026-08-06: 6 batches discarded this way in one day (128, 135, 143,
147, 152, 155), each burning roughly 0.9% of a full Luna quota cycle, ~5.4%
total. With quota as the binding constraint on corpus growth, that is the single
most wasteful failure mode in the pipeline.

Nothing about the decision requires the verdicts: whether a canary FETCHED is
known the moment the fetch stage finishes. This moves the same check earlier, so
an unfetchable batch costs a GitHub round trip instead of a judging round.

Exit codes:
  0  all canaries fetched -- safe to judge
  2  one or more canaries unfetched -- caller should discard and back off
  1  could not evaluate (missing/unreadable inputs) -- caller should proceed,
     since failing open here must never block a healthy batch
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", required=True)
    ap.add_argument("--fetch-cache", type=Path, required=True)
    args = ap.parse_args()

    batch_file = BENCH / f"run2_batch_{args.batch}.json"
    try:
        sample = json.loads(batch_file.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"canary-preflight: cannot read {batch_file.name}: {exc}", flush=True)
        return 1
    try:
        cache = json.loads(args.fetch_cache.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"canary-preflight: cannot read fetch cache: {exc}", flush=True)
        return 1
    if not isinstance(cache, dict):
        print("canary-preflight: fetch cache is not an object", flush=True)
        return 1

    canaries = [s for s in sample.get("skills", [])
                if "canary" in (s.get("buckets") or [])]
    if not canaries:
        # A batch with no canaries riding cannot be checked here; the existing
        # post-combine gate still covers it.
        print("canary-preflight: no canaries in this batch", flush=True)
        return 1

    # A canary whose upstream file is GONE can never be fetched again, so
    # treating it like a transient fetch failure stalls the pipeline forever:
    # every batch is discarded at preflight, 0 judged, indefinitely. Measured
    # 2026-08-11: canary `error-handling-security` was deleted from GitHub and
    # ~80 consecutive batches were discarded over ~6h with zero verdicts.
    #
    # These statuses mean "the content no longer exists / is not admissible
    # upstream", which says nothing about GitHub health or our fetcher -- the
    # canary has simply retired itself and must be dropped from the gate.
    GONE = {"source_deleted", "symlink_escapes_repo", "not_found", "repo_deleted"}

    unfetched, retired = [], []
    for s in canaries:
        rec = cache.get(s["id"])
        st = (rec or {}).get("status") if isinstance(rec, dict) else None
        if st == "ok":
            continue
        name = s.get("name") or s["id"]
        if st in GONE:
            retired.append((name, st))
        else:
            unfetched.append((name, st or "absent"))

    live = len(canaries) - len(retired)
    bad = len(unfetched)
    print(f"canary-preflight: {live - bad}/{live} canaries fetched"
          f"{f' ({len(retired)} retired upstream)' if retired else ''}", flush=True)
    for name, st in retired[:5]:
        print(f"  RETIRED (gone upstream, not blocking): {name} ({st})", flush=True)

    if live == 0:
        # Every canary is gone upstream -- we have no regression coverage at all.
        # Don't silently judge without a gate; surface it and let the caller's
        # fail-open path decide, same as a batch with no canaries riding.
        print("canary-preflight: NO live canaries remain -- refresh the canary set",
              flush=True)
        return 1
    if bad:
        for name, st in unfetched[:5]:
            print(f"  unfetched: {name} ({st})", flush=True)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
