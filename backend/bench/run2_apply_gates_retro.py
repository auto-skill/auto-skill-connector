#!/usr/bin/env python3
"""Apply the safety gates RETROACTIVELY to packages admitted before they existed.

The risk and fixture gates were added at batch 44. Everything judged before that
bypassed them, so the corpus was still serving content the gates exist to keep
out: an audit found 122 leaked packages, including 40 cases from a red-team
dataset carrying live injected payloads that the primary judge had rated real at
0.98-0.99 confidence with no risk flag set. A gate that only applies to new
arrivals is not a safety control; it is a policy for the future.

For every existing package this re-evaluates the CURRENT rules:

  fixture path (ancestor-anchored) -> excluded, package removed
  hard risk flag (prompt_injection / obfuscated_code) -> quarantined, removed

Quarantined rows land in the durable `quarantine` table with their verdict, so a
hold is auditable and reversible rather than a line in a batch file. Content is
never deleted from the object store -- only the servable package is withdrawn,
so any decision here can be revisited.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
WORK = BACKEND / "corpus_v0_work.sqlite"
E_DB = BACKEND / "enrichment_v1.db"
sys.path.insert(0, str(BENCH))

from run2_enrich import FIXTURE_PATH_RE, HARD_RISK_FLAGS  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(WORK, timeout=300)
    con.execute("PRAGMA busy_timeout=300000")
    econ = sqlite3.connect(E_DB, timeout=300)
    econ.execute("PRAGMA busy_timeout=300000")
    econ.execute("""CREATE TABLE IF NOT EXISTS quarantine (
      norm_hash TEXT PRIMARY KEY, skill_id TEXT, name TEXT, repo TEXT, url TEXT,
      reason TEXT, primary_json TEXT, secondary_json TEXT, recorded_at TEXT)""")

    fixture, risky = [], []
    for ph, ep, mj in con.execute(
            "select package_hash, entrypoint_path, manifest_json from skill_packages"):
        try:
            m = json.loads(mj)
        except Exception:
            continue
        prov = m.get("provenance") or {}
        if FIXTURE_PATH_RE.search("/" + (ep or "")):
            fixture.append((ph, prov, ep))
            continue
        flags = set(prov.get("risk_flags") or [])
        if flags & HARD_RISK_FLAGS:
            risky.append((ph, prov, ep, sorted(flags & HARD_RISK_FLAGS)))

    print(f"  fixture-path packages: {len(fixture)}")
    print(f"  hard-risk packages   : {len(risky)}")
    if args.dry_run:
        for ph, prov, ep in fixture[:5]:
            print(f"    [fixture] {prov.get('name')} <- {ep}")
        for ph, prov, ep, fl in risky[:5]:
            print(f"    [risk {fl}] {prov.get('name')} <- {ep}")
        return 0

    now = datetime.now(timezone.utc).isoformat()
    for ph, prov, ep, fl in risky:
        econ.execute("insert or replace into quarantine values (?,?,?,?,?,?,?,?,?)",
                     (prov.get("norm_hash"), None, prov.get("name"), None, None,
                      "hard risk flag(s): " + ", ".join(fl)
                      + " -- withdrawn retroactively when the gate was added",
                      json.dumps(prov), None, now))
    econ.commit()

    removed = 0
    for ph, *_ in fixture + [(r[0],) for r in risky]:
        for t in ("skill_packages", "skill_package_files", "skill_package_sources"):
            con.execute(f"delete from {t} where package_hash=?", (ph,))
        removed += 1
    con.commit()

    left = con.execute("select count(*) from skill_packages").fetchone()[0]
    qn = econ.execute("select count(*) from quarantine").fetchone()[0]
    print(f"\n  withdrawn {removed} packages -> {left} remain servable")
    print(f"  quarantine table now holds {qn} rows (durable, reversible)")
    con.close()
    econ.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
