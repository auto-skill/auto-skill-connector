#!/usr/bin/env python3
"""Persist every quarantine hold from the batch files into the durable table.

The quarantine table was populated once, by hand, in a two-second window. Every
hold recorded after that -- 38 rows spanning batches 39-46, including all four
of the new risk gate's security holds -- existed only inside a batch JSON file.
A safety hold that lives in an artifact nobody queries is not a record.

Idempotent; runs after each batch so holds accumulate instead of being lost.
"""
from __future__ import annotations

import glob
import json
from datetime import datetime, timezone
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
# One definition of "the judge never answered", shared with run2_enrich,
# so the two guards cannot drift apart.
from run2_enrich import infra_failure  # noqa: E402

BENCH = Path(__file__).resolve().parent
DB = BENCH.parent / "enrichment_v1.db"


def main() -> int:
    con = sqlite3.connect(DB, timeout=300)
    # synchronous=NORMAL is SQLite's recommended setting for WAL mode: still
    # corruption-safe and still durable against process crash, trading only the
    # last few transactions on machine power loss. Measured on this volume:
    # FULL = 244 ms/commit, NORMAL = 2 ms (122x). With six writers committing
    # continuously that fsync was a dominant, invisible cost.
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=300000")
    con.execute("""CREATE TABLE IF NOT EXISTS quarantine (
      norm_hash TEXT PRIMARY KEY, skill_id TEXT, name TEXT, repo TEXT, url TEXT,
      reason TEXT, primary_json TEXT, secondary_json TEXT, recorded_at TEXT)""")
    before = con.execute("select count(*) from quarantine").fetchone()[0]
    skipped_infra = 0
    for f in sorted(glob.glob(str(BENCH / "run2_combined_b*.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for r in d.get("rows", []):
            if r.get("label") != "quarantine":
                continue
            # Quarantine means "this content is suspect". A judge that never
            # answered has said nothing about the content, so an outage must not
            # become a safety hold.
            #
            # The guard lives HERE, not only in run2_enrich, because this script
            # replays EVERY historical run2_combined_b*.json on every batch. The
            # 56 holds created during the Luna outage were deleted from the table
            # and then silently reinserted on the next run, all carrying one
            # identical recorded_at -- the artifacts are the source of truth, so
            # a fix applied only to new batches (or only to the table) cannot
            # hold. Filtering at the point of ingest is what actually sticks.
            if infra_failure(r.get("reason")):
                skipped_infra += 1
                continue
            # ISO-8601 UTC with the T separator, matching every other timestamp
            # column. SQLite's datetime('now') yields "YYYY-MM-DD HH:MM:SS" with
            # a SPACE, and ' ' (0x20) sorts BELOW 'T' (0x54) -- so a single
            # space-format row compares as older than every ISO row forever, and
            # any `recorded_at <= cutoff` window would silently include it in
            # every bucket. Keep one format everywhere.
            con.execute(
                "insert or replace into quarantine values (?,?,?,?,?,?,?,?,?)",
                (r.get("norm_hash"), r.get("skill_id"), r.get("name"), r.get("repo"),
                 r.get("url"), r.get("reason"), json.dumps(r.get("primary")),
                 json.dumps(r.get("secondary")),
                 datetime.now(timezone.utc).isoformat()))
    con.commit()
    after = con.execute("select count(*) from quarantine").fetchone()[0]
    print(f"quarantine: {before} -> {after} rows (+{after - before})")
    if skipped_infra:
        print(f"  skipped {skipped_infra} holds caused by judge/infra failure "
              f"(retryable, not a fact about the content)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
