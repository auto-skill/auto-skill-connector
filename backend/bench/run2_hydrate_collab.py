#!/usr/bin/env python3
"""Hydrate collaborator-judged skills: fetch their bytes so packaging can run.

Pranay judged ~379k keeps on his machine; we imported the VERDICTS but never
fetched the CONTENT, and run2_package.py deliberately touches no network -- it
resolves bytes from fetch caches + the CAS. So his keeps are invisible to
packaging (candidates=224k vs 658k judged).

This closes the gap the production way: for each keep, fetch the entrypoint and
its closure files via raw.githubusercontent (OFF the REST quota), store bytes in
the CAS, and emit run2_fetch_b9xxx.json / run2_combined_b9xxx.json chunks in the
exact shape run2_package.py already consumes. The running enrich loop packages
every chunk automatically, so servability flows through the SAME completeness
and dependency-closure gates as everything else -- no bespoke path, no gate
bypass.

Correctness properties:
  * Verdict-content binding. A verdict describes exact bytes. We fetch at HEAD,
    recompute norm_hash, and only hydrate when it MATCHES the verdict's
    norm_hash. Upstream drift -> recorded as `drifted`, skipped: packaging bytes
    the judge never saw would poison the corpus quality bar.
  * git blob shas are computed locally (sha1("blob <len>\\0" + bytes)) --
    identical to GitHub's, zero REST calls.
  * blob_index updates are merge-before-save: reload the current file and
    update() our entries in, so concurrent enrich writes are not clobbered.
  * Idempotent. A journal records every processed norm_hash; reruns skip them, and
    packaging itself dedups by norm_hash on top.

Culled skills are never silently dropped: drifted / gone / reject counts are
journaled per class so the quality bar stays auditable.
"""
from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
sys.path.insert(0, str(BENCH))
from run2_repofetch import raw_url_fetch  # noqa: E402
from run2_enrich import normalize, norm_hash_of  # noqa: E402

LIB = BACKEND / "skills_library_v1"
WORK = BACKEND / "corpus_v0_work.sqlite"
INBOX = Path("/srv/mobile-codex/sessions/autoskill_7e0dd3fa/workspace/"
             "deliverables/pranay-inbox")
JOURNAL = BENCH / "collab_hydrate_journal.txt"
CHUNK_BASE = 9001          # sorts after every real batch -> newest-wins merge
CHUNK_SIZE = 2000
CONCURRENCY = int(os.environ.get("AUTOSKILL_HYDRATE_CONCURRENCY", "12"))
MAX_BYTES = 256 * 1024
MAX_CLOSURE_FILES = 40


def git_blob_sha(content: bytes) -> str:
    return hashlib.sha1(b"blob %d\0" % len(content) + content).hexdigest()


def cas_store(content: bytes) -> str:
    h = hashlib.sha256(content).hexdigest()
    p = LIB / "objects" / h[:2] / h[2:4] / h
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp")
        tmp.write_bytes(content)
        os.replace(tmp, p)
    return h


def load_done() -> set[str]:
    done = set()
    if JOURNAL.exists():
        for line in JOURNAL.read_text().splitlines():
            parts = line.split("\t")
            if parts:
                done.add(parts[0])
    return done


def already_packaged() -> set[str]:
    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=300000")
    have = {r[0] for r in con.execute(
        "select json_extract(manifest_json,'$.provenance.norm_hash')"
        " from skill_packages") if r[0]}
    con.close()
    return have


def keeps_from_inbox(skip: set[str]) -> list[dict]:
    out, seen = [], set()
    for f in sorted(INBOX.glob("pranay_verdicts_*.jsonl.gz")):
        with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    d = json.loads(line)
                except Exception:
                    continue
                nh = d.get("norm_hash")
                oj = d.get("output_json") or {}
                if (not nh or nh in seen or nh in skip
                        or oj.get("is_real_skill") is not True
                        or not d.get("repo") or not d.get("path")):
                    continue
                seen.add(nh)
                out.append(d)
    return out


def fetch_with_backoff(repo: str, path: str):
    """raw fetch that does not mistake throttling for deletion.

    Measured 2026-08-15: after ~50k sustained fetches at 12-way,
    raw.githubusercontent began refusing; the run's "gone" rate jumped from
    4.9% to ~40%, and a same-day refetch of 60 "gone" samples returned 59x
    HTTP 200. Only a 404 is evidence the content is absent -- anything else
    gets one backoff+retry and is otherwise classed `throttled` (retryable),
    never `gone` (terminal).
    """
    for attempt in (0, 1):
        try:
            st, body = raw_url_fetch(repo, path)
        except Exception:
            st, body = 0, None
        if st in (200, 404):
            return st, body
        if attempt == 0:
            time.sleep(25)
    return st, None


def hydrate_one(d: dict):
    """Fetch entry + closure. Returns (class, norm_hash, payload)."""
    repo, path, nh = d["repo"], d["path"], d["norm_hash"]
    st, body = fetch_with_backoff(repo, path)
    if st == 404:
        return ("gone", nh, None)
    if st != 200 or body is None or len(body) > MAX_BYTES:
        return ("throttled" if st != 200 else "gone", nh, None)
    if norm_hash_of(body.decode("utf-8", "replace")) != nh:
        return ("drifted", nh, None)

    oj = d.get("output_json") or {}
    skill_dir = str(Path(path).parent)
    closure_paths = [p for p in (oj.get("closure_paths") or [])
                     if isinstance(p, str) and p and "$" not in p
                     and "*" not in p and ".." not in p][:MAX_CLOSURE_FILES]
    fetched, tree_extra, blobs = [], [], {}
    for cp in closure_paths:
        full = cp if cp.startswith(skill_dir) else str(Path(skill_dir) / cp)
        cst, cbody = fetch_with_backoff(repo, full)
        if cst == 200 and cbody is not None and len(cbody) <= MAX_BYTES:
            gsha = git_blob_sha(cbody)
            blobs[gsha] = cbody
            tree_extra.append({"path": full, "type": "file", "sha": gsha,
                               "size": len(cbody)})
            fetched.append(full)

    entry_gsha = git_blob_sha(body)
    blobs[entry_gsha] = body
    tree = [{"path": path, "type": "file", "sha": entry_gsha,
             "size": len(body)}] + tree_extra
    skill_id = f"collab:{d.get('blob_sha') or nh[:16]}"
    fe = {"status": "ok", "entry_path": path, "skill_dir": skill_dir,
          "tree": tree}
    row = {"label": "included", "norm_hash": nh, "skill_id": skill_id,
           "repo": repo, "url": d.get("skill_url"),
           "name": Path(skill_dir).name or path,
           "entry_path": path, "is_canary": False,
           "decided_by": "collab-import",
           "primary": oj,
           "closure": {"fetched": fetched, "already_present": []}}
    return ("ok", nh, (skill_id, fe, row, blobs))


def flush_chunk(idx: int, cache: dict, rows: list, new_blob_index: dict):
    n = CHUNK_BASE + idx
    (BENCH / f"run2_fetch_b{n}.json").write_text(
        json.dumps(cache, ensure_ascii=False))
    (BENCH / f"run2_combined_b{n}.json").write_text(
        json.dumps({"rows": rows}, ensure_ascii=False))
    # merge-before-save so concurrent enrich writes survive
    bi_path = LIB / "blob_index.json"
    try:
        current = json.loads(bi_path.read_text())
    except Exception:
        current = {}
    current.update(new_blob_index)
    tmp = bi_path.with_name("blob_index.json.tmp")
    tmp.write_text(json.dumps(current))
    os.replace(tmp, bi_path)
    print(f"  chunk b{n}: {len(rows)} skills, blob_index +{len(new_blob_index)}"
          f" -> {len(current)}", flush=True)


def main() -> int:
    t0 = time.time()
    done = load_done()
    packaged = already_packaged()
    skip = done | packaged
    todo = keeps_from_inbox(skip)
    print(f"  keeps to hydrate: {len(todo):,} (skipped {len(done):,} journaled,"
          f" {len(packaged):,} already packaged)", flush=True)

    import glob as _g, re as _re
    existing = [int(m.group(1)) for f in _g.glob(str(BENCH / "run2_fetch_b9*.json"))
                if (m := _re.search(r"b(9\d+)\.json$", f))]
    chunk_start = (max(existing) - CHUNK_BASE + 1) if existing else 0

    stats = {"ok": 0, "gone": 0, "drifted": 0, "throttled": 0}
    jf = open(JOURNAL, "a", encoding="utf-8")
    chunk_cache: dict = {}
    chunk_rows: list = []
    chunk_blobs: dict = {}
    chunk_idx = chunk_start

    with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
        for cls, nh, payload in ex.map(hydrate_one, todo):
            stats[cls] += 1
            # `throttled` is NOT journaled: it must stay eligible for rerun.
            if cls != "throttled":
                jf.write(f"{nh}\t{cls}\n")
            if cls == "ok":
                sid, fe, row, blobs = payload
                for gsha, content in blobs.items():
                    chunk_blobs[gsha] = cas_store(content)
                fe["entry_hash"] = chunk_blobs[
                    [e["sha"] for e in fe["tree"] if e["path"] == fe["entry_path"]][0]]
                chunk_cache[sid] = fe
                chunk_rows.append(row)
            if len(chunk_rows) >= CHUNK_SIZE:
                flush_chunk(chunk_idx, chunk_cache, chunk_rows, chunk_blobs)
                chunk_idx += 1
                chunk_cache, chunk_rows, chunk_blobs = {}, [], {}
                jf.flush()
            n = sum(stats.values())
            if n % 2000 == 0:
                el = time.time() - t0
                print(f"    {n:,}/{len(todo):,}  ok={stats['ok']:,}"
                      f" gone={stats['gone']:,} drifted={stats['drifted']:,} thr={stats['throttled']:,}"
                      f"  ({n/max(el,1):.1f}/s)", flush=True)

    if chunk_rows:
        flush_chunk(chunk_idx, chunk_cache, chunk_rows, chunk_blobs)
    jf.close()
    print(f"\n  === HYDRATE DONE ({time.time()-t0:.0f}s) ===")
    for k, v in stats.items():
        print(f"    {k:8} {v:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
