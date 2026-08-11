#!/usr/bin/env python3
"""Drop export rows that production's own quality gate would reject.

Our judge decides "is this a real skill". Production's `quality.evaluate_quality`
is a separate, independent heuristic gate, and `skill_delta` re-runs it during
package self-verification. A row our judge accepted can still fail it -- and when
that happens the ENTIRE export aborts, not just that row.

Rather than weaken the validator (it is the last line of defence before the
served corpus), this pre-filters the export DB to exactly what production will
accept. Anything dropped here stays in the judged corpus; it is simply not
shipped.

Also recomputes content_hash from the shipped bytes via quality.content_hash, so
the row and the library file cannot disagree.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BACKEND))

import quality  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", type=Path, default=BACKEND / "skills_judged_v1.db")
    ap.add_argument("--lib", type=Path, default=BACKEND / "judged_library_v1")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete failing rows (default: report only)")
    a = ap.parse_args()

    index = json.loads((a.lib / "index.json").read_text(encoding="utf-8"))
    files_root = a.lib / "files"
    con = sqlite3.connect(a.db)
    con.execute("pragma busy_timeout=120000")
    con.row_factory = sqlite3.Row
    rows = con.execute(
        "select id,url,name,description,source,tags,content_hash from skills"
    ).fetchall()
    print(f"  rows: {len(rows):,}", flush=True)

    bad_gate, bad_hash, missing, checked = [], [], [], 0
    for r in rows:
        meta = index.get(r["url"])
        if not meta:
            missing.append(r["id"])
            continue
        try:
            content = (files_root / meta["file"]).read_text(encoding="utf-8")
        except OSError:
            missing.append(r["id"])
            continue
        try:
            tags = json.loads(r["tags"] or "[]")
        except Exception:
            tags = []
        assessed = quality.evaluate_quality(
            {"name": r["name"], "description": r["description"],
             "source": r["source"], "url": r["url"], "tags": tags},
            content,
        )
        checked += 1
        if assessed.get("quality_status") not in quality.ACTIVE_STATUSES:
            bad_gate.append(r["id"])
        elif r["content_hash"] != assessed.get("content_hash"):
            bad_hash.append((r["id"], assessed.get("content_hash")))
        if checked % 10000 == 0:
            print(f"    checked {checked:,}", flush=True)

    print(f"\n  checked:              {checked:,}")
    print(f"  fail quality gate:    {len(bad_gate):,}")
    print(f"  content_hash mismatch:{len(bad_hash):,}")
    print(f"  missing library file: {len(missing):,}")

    if a.apply:
        for i in range(0, len(bad_gate), 500):
            chunk = bad_gate[i:i + 500]
            con.execute(f"DELETE FROM skills WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        for i in range(0, len(missing), 500):
            chunk = missing[i:i + 500]
            con.execute(f"DELETE FROM skills WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        for rid, ch in bad_hash:
            con.execute("UPDATE skills SET content_hash=? WHERE id=?", (ch, rid))
        con.commit()
        left = con.execute("select count(*) from skills").fetchone()[0]
        emb = con.execute("select count(*) from skills where embedding is not null").fetchone()[0]
        print(f"\n  APPLIED. remaining rows: {left:,}  (embedded {emb:,})")
    else:
        print("\n  (report only; pass --apply to delete)")
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
