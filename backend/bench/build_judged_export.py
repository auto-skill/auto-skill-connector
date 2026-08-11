#!/usr/bin/env python3
"""Project the JUDGED corpus into a production-shaped collector DB + library.

Production currently serves `corpus_v0_work.sqlite:skills` -- 271,957 rows of the
original UNVERIFIED scrape, of which 72,270 are `active`. Only 83 of our 59,763
judged packages are linked into it. So none of the judging, gating, closure
completion or canary work reaches what actually gets served.

This builds a SEPARATE database in the exact schema `skill_delta.export_package`
expects, populated only from packages that passed every gate, so it can be
shipped through the existing validated delta channel (allowlisted columns,
online backup, audit, activate) rather than by overwriting the live corpus.

Strict tier only: `completeness_status='complete'` AND
`dependency_closure_status='complete'` -- 47,593 of 59,763. Packages with a
readable entrypoint but a partial closure are deliberately EXCLUDED here; a
skill that references a file we do not hold can mislead an agent mid-task, and
that failure is worse than the skill simply being absent.

Derived score mappings are explicit, not invented per-row:
  quality_score, meaningfulness_score <- judge `specificity` (0-1, direct)
  prominence_score                    <- log-scaled repo/sighting counts
  provenance_score                    <- immutable ref + license + closure
  risk_score                          <- 0 unless the judge raised risk_flags

Embeddings are computed LOCALLY (quantized gte-small via onnxruntime), so this
costs no API budget of any kind.

Read-only against the corpus. Writes only the new export DB + library dir.
"""
from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BACKEND))

import quality  # noqa: E402
from embeddings import build_embed_text, embed_text_hash, embed_texts  # noqa: E402

SRC_DB = BACKEND / "corpus_v0_work.sqlite"
LIB = BACKEND / "skills_library_v1"

SKILL_COLUMNS = (
    "name", "description", "source", "url", "tags", "discovered_at",
    "risk_score", "risk_flags", "scanned_at", "content_hash", "canonical_id",
    "quality_status", "quality_reasons", "quality_score", "prominence_score",
    "provenance_score", "meaningfulness_score", "platforms", "category",
    "capability_summary", "triggers", "embedding_text_hash", "embedded_at",
)

# Column types matter: an all-TEXT table makes ints/floats round-trip as
# strings, and _validate_skill rejects a str where it wants an int.
JSON_COLUMNS = frozenset({"tags", "risk_flags", "quality_reasons",
                          "platforms", "triggers"})
COLUMN_TYPES = {
    "risk_score": "INTEGER", "quality_score": "INTEGER",
    "prominence_score": "REAL", "provenance_score": "REAL",
    "meaningfulness_score": "REAL",
}
DDL = f"""
CREATE TABLE IF NOT EXISTS skills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  {', '.join(f'{c} {COLUMN_TYPES.get(c, "TEXT")}' for c in SKILL_COLUMNS)},
  embedding BLOB
);
CREATE UNIQUE INDEX IF NOT EXISTS skills_url ON skills(url);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def read_object(csha: str) -> bytes | None:
    if not csha:
        return None
    p = LIB / "objects" / csha[:2] / csha[2:4] / csha
    try:
        return p.read_bytes()
    except OSError:
        return None


def prominence(prov: dict) -> float:
    """Log-scaled popularity. A skill mirrored across 200 repos is more
    prominent than one in 2, but not 100x more -- linear counts would let a few
    heavily-vendored skills dominate ranking outright."""
    n = (prov.get("repo_count") or 0) + (prov.get("sighting_count") or 0)
    return round(min(math.log10(n + 1) / 3.0, 1.0), 4)


def provenance_score(m: dict, prov: dict) -> float:
    s = 0.0
    if prov.get("immutable_ref"):
        s += 0.4
    if (m.get("license") or {}).get("spdx"):
        s += 0.3
    if m.get("dependency_closure_status") == "complete":
        s += 0.3
    return round(min(s, 1.0), 4)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-db", type=Path,
                    default=BACKEND / "skills_judged_v1.db")
    ap.add_argument("--out-lib", type=Path,
                    default=BACKEND / "judged_library_v1")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--batch", type=int, default=64)
    # Embedding 48k skills on this box's 4 weak cores takes ~10h. Splitting the
    # build lets a 128-core CURC node do it in minutes with the IDENTICAL model,
    # so the vectors stay in the same space as Supabase Edge's query vectors.
    ap.add_argument("--dump-texts", type=Path, default=None,
                    help="write rows with NULL embedding + a texts jsonl, skip embedding")
    ap.add_argument("--load-vectors", type=Path, default=None,
                    help="load vectors jsonl produced elsewhere into an existing db")
    a = ap.parse_args()

    if a.load_vectors:
        import array as _arr
        con = sqlite3.connect(a.out_db)
        con.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
        n = 0
        with open(a.load_vectors, encoding="utf-8") as fh:
            for line in fh:
                if not line.strip():
                    continue
                d = json.loads(line)
                blob = _arr.array("f", d["vector"]).tobytes()
                con.execute("UPDATE skills SET embedding=?, embedding_text_hash=?,"
                            " embedded_at=? WHERE canonical_id=?",
                            (blob, d["embedding_text_hash"], d["embedded_at"],
                             d["canonical_id"]))
                n += 1
                if n % 5000 == 0:
                    con.commit()
        con.commit()
        got = con.execute("select count(*) from skills where embedding is not null").fetchone()[0]
        tot = con.execute("select count(*) from skills").fetchone()[0]
        con.close()
        print(f"  loaded {n:,} vectors; {got:,}/{tot:,} rows now embedded")
        return 0

    files_root = a.out_lib / "files"
    files_root.mkdir(parents=True, exist_ok=True)

    src = sqlite3.connect(f"file:{SRC_DB}?mode=ro", uri=True)
    src.execute("pragma busy_timeout=120000")
    q = ("select package_hash, source_url, entrypoint_path, license_spdx,"
         "       completeness_status, dependency_closure_status, manifest_json,"
         "       created_at"
         "  from skill_packages"
         " where completeness_status='complete'"
         "   and dependency_closure_status='complete'")
    if a.limit:
        q += f" limit {a.limit}"
    rows = src.execute(q).fetchall()
    src.close()
    print(f"  strict-tier packages: {len(rows):,}", flush=True)

    out = sqlite3.connect(a.out_db)
    out.executescript("PRAGMA journal_mode=WAL; PRAGMA synchronous=NORMAL;")
    out.executescript(DDL)

    index: dict[str, dict] = {}
    pending: list[tuple[dict, str]] = []
    written = skipped = 0

    dump_fh = open(a.dump_texts, "w", encoding="utf-8") if a.dump_texts else None

    def flush(batch: list[tuple[dict, str]]):
        nonlocal written
        if not batch:
            return
        texts = [build_embed_text(rec, content) for rec, content in batch]
        if dump_fh is not None:
            # Same embed text, same hash -- only the matmul moves off this box.
            for (rec, _c), text in zip(batch, texts):
                rec["embedding_text_hash"] = embed_text_hash(text)
                rec["embedded_at"] = now()
                dump_fh.write(json.dumps({"canonical_id": rec["canonical_id"],
                                          "text": text,
                                          "embedding_text_hash": rec["embedding_text_hash"],
                                          "embedded_at": rec["embedded_at"]}) + "\n")
                cols = ",".join(SKILL_COLUMNS) + ",embedding"
                ph = ",".join("?" * (len(SKILL_COLUMNS) + 1))
                vals = [json.dumps(rec.get(c)) if c in JSON_COLUMNS else rec.get(c)
                        for c in SKILL_COLUMNS]
                out.execute(f"INSERT OR REPLACE INTO skills ({cols}) VALUES ({ph})",
                            tuple(vals) + (None,))
                written += 1
            out.commit()
            batch.clear()
            return
        vecs = embed_texts(texts, batch_size=32)
        import array
        for (rec, _c), text, vec in zip(batch, texts, vecs):
            rec["embedding_text_hash"] = embed_text_hash(text)
            rec["embedded_at"] = now()
            blob = array.array("f", vec).tobytes()
            cols = ",".join(SKILL_COLUMNS) + ",embedding"
            ph = ",".join("?" * (len(SKILL_COLUMNS) + 1))
            vals = []
            for c in SKILL_COLUMNS:
                v = rec.get(c)
                vals.append(json.dumps(v) if c in JSON_COLUMNS else v)
            out.execute(
                f"INSERT OR REPLACE INTO skills ({cols}) VALUES ({ph})",
                tuple(vals) + (blob,))
            written += 1
        out.commit()
        batch.clear()

    for (phash, url, entry_path, lic, comp, closure, mj, created) in rows:
        try:
            m = json.loads(mj)
        except Exception:
            skipped += 1
            continue
        prov = m.get("provenance") or {}
        summary = (prov.get("summary") or "").strip()
        if not url or not summary:
            skipped += 1
            continue
        # `entrypoint` is a PATH string; the stored bytes are addressed by the
        # matching files[] entry's raw_sha256 (which is how store_object keys
        # the CAS). Prefer role=='entrypoint', fall back to a path match.
        ep = m.get("entrypoint")
        csha = None
        for f in (m.get("files") or []):
            if f.get("role") == "entrypoint" or (ep and f.get("path") == ep):
                csha = f.get("raw_sha256")
                break
        content_b = read_object(csha or "")
        if content_b is None:
            # entrypoint bytes are the whole point of shipping the skill
            skipped += 1
            continue
        content = content_b.decode("utf-8", "replace")

        name = (Path(entry_path or "").parent.name
                or (m.get("source") or {}).get("repo") or phash[:12])
        flags = prov.get("risk_flags") or []
        spec = float(prov.get("specificity") or 0.0)
        rec = {
            "name": name,
            "description": summary[:500],
            "source": "github",
            "url": url,
            "tags": [],
            "discovered_at": prov.get("first_seen") or created,
            "risk_score": int(len(flags)),
            "risk_flags": list(flags),
            "scanned_at": created,
            "content_hash": quality.content_hash(content),
            "canonical_id": phash,
            "quality_status": "active",
            "quality_reasons": [],
            "quality_score": int(round(spec * 100)),
            "prominence_score": prominence(prov),
            "provenance_score": provenance_score(m, prov),
            "meaningfulness_score": spec,
            "platforms": [],
            "category": prov.get("vendor_convention") or "generic",
            "capability_summary": summary,
            "triggers": list(prov.get("triggers") or []),
        }
        fname = f"{phash}.md"
        (files_root / fname).write_text(content, encoding="utf-8")
        index[url] = {"file": fname}
        pending.append((rec, content))
        if len(pending) >= a.batch:
            flush(pending)
            if written % 5000 < a.batch:
                print(f"    {written:,} embedded ...", flush=True)

    flush(pending)
    if dump_fh is not None:
        dump_fh.close()
    (a.out_lib / "index.json").write_text(json.dumps(index), encoding="utf-8")
    out.close()
    print(f"\n  written: {written:,}   skipped: {skipped:,}")
    print(f"  db:      {a.out_db}")
    print(f"  library: {a.out_lib}  ({len(index):,} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
