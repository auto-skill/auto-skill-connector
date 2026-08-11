#!/usr/bin/env python3
"""Build a judging manifest for a sha-partitioned slice of the backlog.

The manifest is a work list, not data: one row per unjudged skill, carrying only
what a second machine needs to FETCH the content itself (repo + path), IDENTIFY
it (git blob sha), and PRIORITISE it (repo_count). No skill bytes leave here.

Partitioning is by the FIRST HEX DIGIT of the git blob sha. Because the sha is
content-addressed, the same skill always lands in the same partition on every
machine -- so two people running disjoint digit sets can never judge the same
skill, with zero coordination. Default here is Pranay's half: 0-7.

Read-only. Writes a gzipped TSV.
"""
from __future__ import annotations

import argparse
import gzip
import sqlite3
import sys
from pathlib import Path

BACKEND = Path("/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/"
               "auto-skill-connector/backend")
ENRICH = BACKEND / "enrichment_v1.db"
ANALYTICS = BACKEND / "analytics_v1.db"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--digits", default="01234567",
                    help="first-hex-digit partition to include (default 0-7)")
    ap.add_argument("--out", type=Path,
                    default=Path(__file__).resolve().parent / "manifest_pranay.tsv.gz")
    ap.add_argument("--min-repo-count", type=int, default=1)
    a = ap.parse_args()
    digits = tuple(a.digits)

    con = sqlite3.connect(f"file:{ANALYTICS}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=120000")
    con.execute(f"attach 'file:{ENRICH}?mode=ro' as e")
    print("  building judged-set...", flush=True)
    con.execute("create temp table j as select distinct norm_hash from e.enrichments "
                "where judge_role='primary' and status='ok'")

    placeholders = ",".join("?" * len(digits))
    # A skill is unjudged if its normalized-content hash is not in j. Rows whose
    # norm_hash is null/'' have never been fetched here at all -> definitely
    # unjudged. repo_trees maps blob sha -> a (repo, path) we can fetch from.
    q = f"""
      select p.sha, p.repo_count,
             (select t.repo || char(9) || t.path from e.repo_trees t
              where t.sha = p.sha limit 1) as loc
      from skill_popularity p
      where substr(p.sha,1,1) in ({placeholders})
        and p.repo_count >= ?
        and (p.norm_hash is null or p.norm_hash='' or
             p.norm_hash not in (select norm_hash from j))
      order by p.repo_count desc, p.sha
    """
    print("  querying backlog slice (this scans ~1.1M rows)...", flush=True)
    rows = con.execute(q, (*digits, a.min_repo_count)).fetchall()
    con.close()

    def clean(s: str) -> str:
        # A repo/path is untrusted GitHub data -- a tab or newline in it would
        # split the TSV row and misalign every column after it. Collapse both to
        # a single space; real GitHub paths never contain them, so anything that
        # does is corruption or an attempt to break the manifest format.
        return s.replace("\t", " ").replace("\r", " ").replace("\n", " ")

    written = skipped = malformed = 0
    with gzip.open(a.out, "wt", encoding="utf-8") as fh:
        fh.write("blob_sha\trepo_count\trepo\tpath\n")
        for sha, rc, loc in rows:
            if not loc:            # no fetch location known -> undeliverable, skip
                skipped += 1
                continue
            # A git blob sha is exactly 40 lowercase hex chars. Anything else is
            # a bad row (and would also break the partition guarantee), so drop it.
            if len(sha) != 40 or any(c not in "0123456789abcdef" for c in sha):
                malformed += 1
                continue
            repo, _, path = loc.partition("\t")
            fh.write(f"{sha}\t{rc}\t{clean(repo)}\t{clean(path)}\n")
            written += 1

    mb = a.out.stat().st_size / 1e6
    print(f"\n  manifest: {a.out}")
    print(f"  rows written : {written:,}")
    print(f"  skipped (no fetch location): {skipped:,}")
    print(f"  dropped (malformed sha): {malformed:,}")
    print(f"  gzipped size : {mb:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
