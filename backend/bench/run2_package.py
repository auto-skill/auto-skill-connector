#!/usr/bin/env python3
"""Package judged skills into the production package store.

The enrichment pipeline judges skills and stores their bytes, but it never
produced the *manifest* that turns stored bytes into a servable skill. Without
this stage the object store holds the content and nothing can find it: as of the
first run, 2,283 skills had been judged `included` while only 100 packages
existed, all of them written by the older run1 sweep.

This closes that gap using the **production** builder (`package_store.build_
package_manifest`) rather than a bench-local format, so a package produced here
is byte-identical in shape to one produced by the live scraper.

Resolution chain for a file's bytes:

    closure path -> fetch-cache tree entry (git blob sha) -> blob_index -> CAS object

Idempotent: a package_hash already present is skipped, so this can be re-run
after every batch. Read-only with respect to GitHub; it touches no network.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import os
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BACKEND))

from package_store import (  # noqa: E402
    ImmutablePackageStore,
    PackageFileInput,
    build_package_manifest,
)

LIB = BACKEND / "skills_library_v1"
DB = BACKEND / "corpus_v0_work.sqlite"


def obj_path(sha256: str) -> Path:
    return LIB / "objects" / sha256[:2] / sha256[2:4] / sha256


def read_obj(sha256: str | None) -> bytes | None:
    if not sha256:
        return None
    p = obj_path(sha256)
    try:
        return p.read_bytes()
    except OSError:
        return None


def load_rows() -> dict[str, dict]:
    """Included skills, deduped by content hash — the *final* version wins.

    Where the same content appears in several repos we keep the first sighting;
    the bytes are identical by construction, so the choice only affects the
    recorded provenance URL.
    """
    out: dict[str, dict] = {}
    for f in sorted(glob.glob(str(BENCH / "run2_combined_b*.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for r in d.get("rows", []):
            if r.get("label") == "included":
                out.setdefault(r["norm_hash"], r)
    return out


def _fetch_batch_no(name: str) -> int:
    m = re.search(r"run2_fetch_b(\d+)\.json$", name)
    return int(m.group(1)) if m else -1


def load_fetch_cache() -> dict:
    """Merge every batch's fetch cache, NEWEST BATCH LAST.

    glob() returns filesystem order, not sorted order. These dicts are keyed by
    skill_id and merged with update(), so an arbitrary file won. A retried skill
    is recorded as a FAILURE in the batch where it first failed and as `ok` (with
    an entry_hash) in the batch where it finally fetched -- so a stale failure
    could overwrite the fresh success, leaving entry_hash absent.

    The cost of that is not cosmetic: the skill was already judged, so the Luna
    call was already paid for, and then packaging drops it as `no_entry_bytes`
    and it never becomes servable. `no_entry_bytes` went 10 -> 153 -> 216 across
    the retry-heavy batches after batch 116's fetch failures, which is exactly
    that waste.

    Sorting by batch number makes the merge deterministic and lets the most
    recent observation win. Same bug and same fix as run2_quality_audit.py.
    """
    out: dict = {}
    for f in sorted(glob.glob(str(BENCH / "run2_fetch_b*.json")), key=_fetch_batch_no):
        try:
            out.update(json.loads(Path(f).read_text()))
        except Exception:
            pass
    return out


def ensure_tables(con: sqlite3.Connection) -> None:
    con.executescript("""
    CREATE TABLE IF NOT EXISTS skill_packages (
      package_hash TEXT PRIMARY KEY, source_url TEXT, source_provider TEXT,
      source_commit_sha TEXT, root_path TEXT, entrypoint_path TEXT, tree_sha TEXT,
      license_spdx TEXT, completeness_status TEXT, dependency_closure_status TEXT,
      entrypoint_truncated INTEGER, manifest_json TEXT, created_at TEXT);
    CREATE TABLE IF NOT EXISTS skill_package_files (
      package_hash TEXT, path TEXT, raw_sha256 TEXT, git_blob_sha TEXT, size INTEGER,
      role TEXT, media_type TEXT, text_indexable INTEGER, in_dependency_closure INTEGER);
    CREATE TABLE IF NOT EXISTS skill_package_sources (
      package_hash TEXT, source_url TEXT, source_commit_sha TEXT,
      provenance_json TEXT, observed_at TEXT);
    """)


def main() -> int:
    # Phase timing. Every hypothesis about where this stage's 334s/batch went
    # (manifest building, object writes, the have_norm scan) measured under 20s
    # in isolation, so the cost is contention-dependent and only visible in situ.
    # Guessing was wrong three times; measure in production instead.
    import time as _t
    PHASE_T = {}
    _mark = [_t.time()]

    def phase(name):
        now = _t.time()
        PHASE_T[name] = PHASE_T.get(name, 0.0) + now - _mark[0]
        _mark[0] = now

    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    rows = load_rows()
    phase('load_rows')
    cache = load_fetch_cache()
    phase('load_fetch_cache')
    blob_index = json.loads((LIB / "blob_index.json").read_text())
    phase("blob_index")
    # fsync-per-object costs 252 ms on this ext4 volume (0.20 ms without) --
    # ~575 s/batch, the single largest cost in the whole pipeline. Deferred to one
    # os.sync() after the final flush below: same atomicity (os.replace), same
    # bytes, durability moved from per-file to per-batch. Safe here because the
    # store is content-addressed (filename == sha256, so truncation is
    # detectable) and every object is re-fetchable from its recorded source.
    # scraper.py keeps the fsync default and is untouched.
    store = ImmutablePackageStore(LIB, fsync=False)

    con = sqlite3.connect(DB, timeout=300)
    # synchronous=NORMAL is SQLite's recommended setting for WAL mode: still
    # corruption-safe and still durable against process crash, trading only the
    # last few transactions on machine power loss. Measured on this volume:
    # FULL = 244 ms/commit, NORMAL = 2 ms (122x). With six writers committing
    # continuously that fsync was a dominant, invisible cost.
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute("PRAGMA busy_timeout=300000")
    ensure_tables(con)
    have = {r[0] for r in con.execute("select package_hash from skill_packages")}
    # Dedupe by CONTENT, not just package identity. The same normalized content
    # mirrored in two repos yields two different package_hashes (identity =
    # entrypoint+paths), and two concurrent packaging runs raced this check into
    # storing 7 such twin pairs. One norm_hash = one servable package.
    # json_extract does this in C instead of parsing ~29k manifests through
    # Python (2.5x faster, byte-identical set). This scan is O(corpus) on EVERY
    # batch, so it is the piece that silently degrades as the corpus grows --
    # measured at 16.7s under load today and rising with every package added.
    have_norm = {
        r[0] for r in con.execute(
            "select json_extract(manifest_json,'$.provenance.norm_hash')"
            " from skill_packages") if r[0]}
    phase("scan_existing")
    print(f"  candidates={len(rows)} already_packaged={len(have)} norm_hashes={len(have_norm)}")

    stats = {"packaged": 0, "skipped_existing": 0, "no_entry_bytes": 0,
             "partial_closure": 0, "complete": 0, "files": 0, "errors": 0}
    items = list(rows.items())
    if args.limit:
        items = items[:args.limit]

    # Reading closure bytes from the CAS and hashing them into a manifest is
    # independent per skill and dominated by disk latency plus SHA-256 (both of
    # which release the GIL), so it parallelises cleanly. The WRITES do not:
    # store.put and the sqlite inserts stay on the main thread, in the original
    # item order, so dedup semantics and write ordering are byte-for-byte what
    # the serial version produced. This stage was 334s/batch -- 17% of the cycle
    # -- entirely single-threaded.
    def prepare(item):
        """Pure: read bytes + build manifest. No shared state is mutated."""
        norm_hash, r = item
        if norm_hash in have_norm:
            return ("skipped_existing", norm_hash, r, None, None)
        fe = cache.get(r["skill_id"]) or {}
        entry_path = fe.get("entry_path") or r.get("entry_path")
        entry_bytes = read_obj(fe.get("entry_hash"))
        if not entry_bytes or not entry_path:
            return ("no_entry_bytes", norm_hash, r, None, None)

        # path -> git blob sha, from the tree we recorded at fetch time
        tree = {e["path"]: e for e in (fe.get("tree") or []) if e.get("type") == "file"}
        files = [PackageFileInput(
            path=entry_path, content=entry_bytes, mode="100644",
            git_blob_sha=(tree.get(entry_path) or {}).get("sha"),
            expected_size=(tree.get(entry_path) or {}).get("size"))]

        cl = r.get("closure") or {}
        for p in sorted(set((cl.get("fetched") or []) + (cl.get("already_present") or []))):
            if p == entry_path:
                continue
            gsha = (tree.get(p) or {}).get("sha")
            content = read_obj(blob_index.get(gsha)) if gsha else None
            if content is None:
                continue
            files.append(PackageFileInput(path=p, content=content, mode="100644",
                                          git_blob_sha=gsha,
                                          expected_size=(tree.get(p) or {}).get("size")))

        # The judge verdict is what retrieval actually ranks on, so it travels
        # with the package rather than living only in the enrichment DB.
        pr = r.get("primary") or {}
        provenance = {
            "pipeline": "run2_enrich", "norm_hash": norm_hash,
            "name": r.get("name"), "is_canary": r.get("is_canary"),
            "summary": pr.get("summary"), "triggers": pr.get("triggers"),
            "specificity": pr.get("specificity"), "confidence": pr.get("confidence"),
            "vendor_convention": pr.get("vendor_convention"),
            "risk_flags": pr.get("risk_flags"), "decided_by": r.get("decided_by"),
        }
        try:
            manifest, objects = build_package_manifest(
                source={"provider": "github", "repo": r.get("repo"),
                        "root_path": fe.get("skill_dir") or str(Path(entry_path).parent)},
                source_url=r.get("url") or "",
                entrypoint=entry_path,
                files=files,
                tree_complete=fe.get("tree_status", "complete") == "complete",
                provenance=provenance,
            )
        except Exception as e:  # noqa: BLE001
            return ("errors", norm_hash, r, None, str(e))
        return ("ok", norm_hash, r, (manifest, objects, provenance), None)

    workers = int(os.environ.get("AUTOSKILL_PACKAGE_CONCURRENCY", "8"))
    with ThreadPoolExecutor(max_workers=workers) as ex:
        prepared = list(ex.map(prepare, items))
    phase('prepare_parallel')

    # Batch the sqlite writes. Instrumentation on batch 70 measured
    # write_serial=277.5s of the package stage -- ~3,200 individual statements
    # (1 package + 1 delete + N files + 1 source, per package) against a database
    # with six concurrent writers and a WAL that has exceeded 400 MB. The cost is
    # per-statement contention, not the data volume, so executemany inside one
    # transaction collapses it. Flushed in chunks so a crash loses at most a
    # chunk, matching the previous commit-every-250 crash behaviour.
    # write_serial measured 611 ms/package -- far too slow for ~4 small file
    # writes plus a few rows. Split it so the object store and sqlite are
    # attributable separately instead of guessing again.
    PUT_T = [0.0]
    pend_pkg: list = []
    pend_del: list = []
    pend_files: list = []
    pend_src: list = []
    FLUSH_EVERY = 250

    def flush():
        if not (pend_pkg or pend_files or pend_src):
            return
        con.executemany(
            "insert or replace into skill_packages"
            " values (?,?,?,?,?,?,?,?,?,?,?,?,?)", pend_pkg)
        con.executemany(
            "delete from skill_package_files where package_hash=?", pend_del)
        con.executemany(
            "insert into skill_package_files values (?,?,?,?,?,?,?,?,?)", pend_files)
        con.executemany(
            "insert into skill_package_sources values (?,?,?,?,?)", pend_src)
        con.commit()
        pend_pkg.clear(); pend_del.clear(); pend_files.clear(); pend_src.clear()

    for kind, norm_hash, r, built, err in prepared:
        if kind == "skipped_existing":
            stats["skipped_existing"] += 1
            continue
        if kind == "no_entry_bytes":
            stats["no_entry_bytes"] += 1
            continue
        if kind == "errors":
            stats["errors"] += 1
            print(f"    ! {r.get('name')}: {err}")
            continue
        manifest, objects, provenance = built
        fe = cache.get(r["skill_id"]) or {}
        # Re-check under the serial phase: two items in the SAME batch can share
        # a norm_hash, and the parallel pass evaluated them against a snapshot
        # taken before either was written.
        if norm_hash in have_norm:
            stats["skipped_existing"] += 1
            continue

        ph = manifest["package_hash"]
        if ph in have:
            stats["skipped_existing"] += 1
            continue
        if manifest["dependency_closure_status"] == "complete":
            stats["complete"] += 1
        else:
            stats["partial_closure"] += 1

        if not args.dry_run:
            _t0 = _t.time()
            store.put(manifest, objects)
            PUT_T[0] += _t.time() - _t0
            pend_pkg.append(
                (ph, manifest["source_url"], "github",
                 fe.get("commit_sha") or "", manifest["source"].get("root_path"),
                 manifest["entrypoint"], "", (manifest["license"] or {}).get("spdx_id"),
                 manifest["completeness_status"], manifest["dependency_closure_status"],
                 int(manifest["entrypoint_truncated"]), json.dumps(manifest),
                 manifest["created_at"]))
            pend_del.append((ph,))
            for fm in manifest["files"]:
                pend_files.append(
                    (ph, fm["path"], fm["raw_sha256"], fm["git_blob_sha"], fm["size"],
                     fm["role"], fm["media_type"], int(fm["text_indexable"]),
                     int(fm["in_dependency_closure"])))
            pend_src.append(
                        (ph, manifest["source_url"], fe.get("commit_sha") or "",
                         json.dumps(provenance),
                         datetime.now(timezone.utc).isoformat()))
        have.add(ph)
        have_norm.add(norm_hash)
        stats["packaged"] += 1
        stats["files"] += len(manifest["files"])
        if stats["packaged"] % FLUSH_EVERY == 0:
            flush()
            print(f"    packaged {stats['packaged']}...", flush=True)

    flush()
    con.commit()
    # One durability barrier for the whole batch, replacing ~2,280 per-file ones.
    os.sync()
    phase('write_serial')
    con.close()
    PHASE_T['store_put'] = round(PUT_T[0], 1)
    PHASE_T['sqlite_flush'] = round(PHASE_T.get('write_serial', 0) - PUT_T[0], 1)
    print('  phase seconds: ' + json.dumps({k: round(v, 1) for k, v in PHASE_T.items()}))
    print("\n  " + json.dumps(stats, indent=1).replace("\n", "\n  "))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
