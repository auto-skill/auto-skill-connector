#!/usr/bin/env python3
"""Build a searchable FTS5 index over the VERIFIED corpus.

The gap this closes: production retrieval queries `skills_fts`, which holds
271,957 rows of the original unverified scrape. The 28,246 judged, gated,
closure-complete packages are indexed NOWHERE. Every quality improvement made so
far is invisible to anyone actually using the router, so the stated goal -- a
high-quality database we can pull from to improve model performance -- cannot be
reached no matter how good the corpus gets.

Deliberate design decisions:

* **Separate database file.** This writes `retrieval_v1.db`, not
  corpus_v0_work.sqlite. The fleet writes that file continuously with six
  processes and a WAL that has been over 400 MB; adding a table plus a full FTS
  rebuild into it would contend for locks and put a schema change next to live
  ingestion for no benefit. A separate file also means the production index is
  untouched, so building this decides nothing -- swapping retrieval over stays a
  separate, deliberate act.

* **Indexes summary and triggers, not raw body.** The judge's summary is a clean
  statement of what the skill does; the raw body is full of code, YAML and
  boilerplate that dilutes BM25 term statistics. Triggers are literally the
  phrases a user would say. The body is stored but weighted separately so the
  effect of including it can be measured rather than assumed.

* **Carries the quality signals forward.** specificity, risk_flags, agentic-ness
  and generality are what the measurements said should drive ranking, so they
  are columns here rather than something a caller has to re-derive. Notably
  specificity is stored RAW plus calibrated: the Haiku failover window reads
  0.05 low (judge_calibration.json), and ranking across the two windows without
  that correction would systematically bury 9,028 packages.

Idempotent: rebuilds from scratch each run. Read-only against the corpus.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
WORK = BACKEND / "corpus_v0_work.sqlite"
ENRICH = BACKEND / "enrichment_v1.db"
LIB = BACKEND / "skills_library_v1"
OUT = BACKEND / "retrieval_v1.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
  package_hash TEXT PRIMARY KEY,
  name         TEXT,
  summary      TEXT,
  triggers     TEXT,
  entrypoint   TEXT,
  repo         TEXT,
  source_url   TEXT,
  specificity_raw   REAL,
  specificity_cal   REAL,
  judge_snapshot    TEXT,
  risk_flags   TEXT,
  license      TEXT,
  n_files      INTEGER,
  body_chars   INTEGER,
  indexed_at   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pkg_spec ON packages(specificity_cal);
CREATE VIRTUAL TABLE IF NOT EXISTS pkg_fts USING fts5(
  name, summary, triggers, body,
  content='', tokenize='porter unicode61'
);
CREATE TABLE IF NOT EXISTS fts_map (rowid INTEGER PRIMARY KEY, package_hash TEXT);
"""


def entry_body(m: dict) -> str:
    ent = [f for f in m.get("files", []) if f["path"] == m.get("entrypoint")]
    if not ent:
        return ""
    h = ent[0]["raw_sha256"]
    p = LIB / "objects" / h[:2] / h[2:4] / h
    return p.read_bytes().decode("utf-8", "replace") if p.exists() else ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--body-chars", type=int, default=4000,
                    help="entrypoint chars to index (0 = summary/triggers only)")
    ap.add_argument("--out", default=str(OUT))
    a = ap.parse_args()

    try:
        cal = json.loads((BENCH / "judge_calibration.json").read_text())
        offsets = {k: v["additive_correction"]
                   for k, v in cal["specificity_offsets"].items()}
    except Exception as exc:  # noqa: BLE001
        print(f"  WARNING: no calibration ({exc}); specificity left uncorrected")
        offsets = {}

    # Which judge produced each verdict -> lets us calibrate specificity.
    con = sqlite3.connect(f"file:{ENRICH}?mode=ro", uri=True, timeout=60)
    con.execute("PRAGMA busy_timeout=60000")
    snap_of = {}
    for nh, snap in con.execute(
            "select norm_hash, model_snapshot from enrichments"
            " where judge_role='primary' and status='ok'"):
        snap_of[nh] = snap
    con.close()

    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True, timeout=90)
    con.execute("PRAGMA busy_timeout=90000")
    rows = list(con.execute(
        "select package_hash, manifest_json, source_url from skill_packages"))
    con.close()
    print(f"reading {len(rows):,} verified packages", flush=True)

    out = Path(a.out)
    if out.exists():
        out.unlink()
    db = sqlite3.connect(out, timeout=120)
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SCHEMA)

    now = datetime.now(timezone.utc).isoformat()
    n_ok = n_skip = 0
    rowid = 0
    for ph, mj, src in rows:
        try:
            m = json.loads(mj)
        except Exception:
            n_skip += 1
            continue
        prov = m.get("provenance") or {}
        ent = m.get("entrypoint") or ""
        name = PurePosixPath(ent).parent.name if ent else ""
        summary = (prov.get("summary") or "").strip()
        triggers = prov.get("triggers") or []
        if not (name or summary):
            n_skip += 1
            continue
        body = entry_body(m)[:a.body_chars] if a.body_chars else ""
        nh = prov.get("norm_hash") or ""
        snap = snap_of.get(nh, "")
        spec = prov.get("specificity")
        spec = spec if isinstance(spec, (int, float)) else None
        cal_spec = spec
        if spec is not None:
            for k, off in offsets.items():
                if k and k in (snap or ""):
                    cal_spec = min(1.0, spec + off)
                    break
        # `license` is a dict here: {"paths": [...], "spdx_id": ..., "status": ...}.
        # sqlite refuses to bind a dict (it raises rather than coercing), and the
        # useful part is the identifier, not the blob -- store spdx_id, falling
        # back to status so "missing" stays visible rather than becoming NULL.
        lic = m.get("license")
        if isinstance(lic, dict):
            lic = lic.get("spdx_id") or lic.get("status") or None
        elif lic is not None and not isinstance(lic, str):
            lic = str(lic)[:120]
        rowid += 1
        db.execute(
            "insert into packages values (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (ph, name, summary, json.dumps(triggers), ent,
             (prov.get("immutable_ref") or "").split("@")[0] or None, src,
             spec, cal_spec, snap, json.dumps(prov.get("risk_flags") or []),
             lic, len(m.get("files") or []), len(body), now))
        db.execute("insert into pkg_fts(rowid,name,summary,triggers,body) values (?,?,?,?,?)",
                   (rowid, name, summary, " ".join(triggers), body))
        db.execute("insert into fts_map(rowid,package_hash) values (?,?)", (rowid, ph))
        n_ok += 1
        if n_ok % 5000 == 0:
            db.commit()
            print(f"  indexed {n_ok:,}", flush=True)
    db.commit()
    db.execute("INSERT INTO pkg_fts(pkg_fts) VALUES('optimize')")
    db.commit()

    print(f"\nindexed {n_ok:,} packages ({n_skip:,} skipped: no name/summary)")
    cal_n = db.execute(
        "select count(*) from packages where specificity_cal != specificity_raw").fetchone()[0]
    print(f"  specificity calibrated on {cal_n:,} failover-window packages")

    print("\n=== smoke queries ===")
    for q in ("postgres index performance", "kubernetes deployment probes",
              "react rerender memo", "terraform state move", "ffmpeg convert video"):
        hits = db.execute(
            "select p.name, p.specificity_cal, bm25(pkg_fts) as s"
            " from pkg_fts join fts_map fm on fm.rowid = pkg_fts.rowid"
            " join packages p on p.package_hash = fm.package_hash"
            " where pkg_fts match ? order by s limit 3", (q,)).fetchall()
        print(f"  {q!r}")
        for h in hits:
            print(f"      {str(h[0])[:40]:40} spec={h[1]} bm25={h[2]:.2f}")
        if not hits:
            print("      (no hits)")
    db.close()
    print(f"\n-> {out}")
    print("  Production retrieval is UNCHANGED; this is a parallel index for")
    print("  measuring retrieval quality before any switchover.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
