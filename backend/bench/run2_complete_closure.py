#!/usr/bin/env python3
"""Complete missing dependency files for already-judged skills, then repackage.

Closes the last unrefetched failure path. When the storage-quality gate failed a
batch on dependency completeness, the daemon held the batch-size ladder and
moved on — nothing ever went back for the files. Observed: `vueuse-functions`
shipped with 0 of its 164 reference docs because the old 100-file closure cap
silently skipped them; its package was built incomplete and, being deduped by
content hash, would never be rebuilt.

For every kept row across all combined batches:
  expected  = text dependency files present in the skill's tree (audit rule),
              minus over-byte-cap files (policy-excluded)
  missing   = expected - held
Missing files are fetched via raw.githubusercontent (off-quota, path-encoded),
stored content-addressed, recorded in the closure (fetched + content_by_path).
Skills whose closure grew are then REPACKAGED: the old incomplete package is
replaced (package identity includes the file list, so completing the closure
produces a new package_hash; leaving the old one would double-serve).

Bounded by the same per-skill byte cap as the pipeline. Idempotent.
"""
from __future__ import annotations

import argparse
import glob
import hashlib
import json
import os
import re
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
LIB = BACKEND / "skills_library_v1"
WORK = BACKEND / "corpus_v0_work.sqlite"
sys.path.insert(0, str(BENCH))

from run2_repofetch import raw_url_fetch  # noqa: E402

DEP_DIR_RE = re.compile(
    r"/(scripts?|references?|assets?|templates?|examples?|data|bin|lib|prompts?|schemas?)/",
    re.IGNORECASE)
TEXT_RE = re.compile(
    r"\.(md|markdown|txt|py|sh|bash|zsh|js|mjs|ts|tsx|jsx|json|ya?ml|toml|ini|cfg|"
    r"sql|rb|go|rs|java|kt|c|h|cpp|hpp|cs|php|pl|r|jl|tf|dockerfile|env|template|tmpl)$",
    re.IGNORECASE)
MAX_FILE = 256 * 1024
MAX_TOTAL = 8 * 1024 * 1024
CONCURRENCY = int(os.environ.get("AUTOSKILL_COMPLETE_CONCURRENCY", "12"))


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
    ap.add_argument("--max-skills", type=int, default=0)
    args = ap.parse_args()

    files = ([Path(b) for b in args.batches] if args.batches
             else sorted(BENCH.glob("run2_combined_b*.json"),
                         key=lambda p: int(re.sub(r"\D", "", p.stem) or 0)))
    caches = {}
    for f in glob.glob(str(BENCH / "run2_fetch_b*.json")):
        try:
            caches.update(json.loads(Path(f).read_text()))
        except Exception:
            pass

    completed_norms: set[str] = set()
    tot_fetched = tot_failed = skills_done = 0
    for bf in files:
        try:
            d = json.loads(bf.read_text())
        except Exception:
            continue
        changed = False
        for r in d.get("rows", []):
            if r.get("label") != "included":
                continue
            fe = caches.get(r["skill_id"]) or {}
            if fe.get("status") != "ok":
                continue
            tree = {e["path"]: e for e in (fe.get("tree") or [])
                    if e.get("type") == "file"}
            cl = r.setdefault("closure", {})
            held = set((cl.get("fetched") or []) + (cl.get("already_present") or []))
            missing = [p for p, e in tree.items()
                       if p != fe.get("entry_path")
                       and DEP_DIR_RE.search("/" + p) and TEXT_RE.search(p)
                       and (e.get("size") or 0) <= MAX_FILE
                       and p not in held]
            if not missing:
                continue
            if args.max_skills and skills_done >= args.max_skills:
                break
            budget = MAX_TOTAL - sum((tree.get(p) or {}).get("size") or 0 for p in held)

            def one(path):
                st, body = raw_url_fetch(r["repo"], path)
                return path, st, body

            got = 0
            with ThreadPoolExecutor(max_workers=CONCURRENCY) as ex:
                for path, st, body in ex.map(one, missing):
                    if st != 200 or body is None or len(body) > MAX_FILE:
                        tot_failed += 1
                        continue
                    if budget - len(body) < 0:
                        break
                    budget -= len(body)
                    h = store(body)
                    cl.setdefault("fetched", []).append(path)
                    cl.setdefault("content_by_path", {})[path] = h
                    got += 1
            if got:
                tot_fetched += got
                skills_done += 1
                completed_norms.add(r["norm_hash"])
                changed = True
                print(f"  {r.get('name')}: +{got}/{len(missing)} deps", flush=True)
        if changed:
            tmp = Path(str(bf) + ".tmp")
            tmp.write_text(json.dumps(d, indent=1))
            os.replace(tmp, bf)

    # Repackage completed skills: drop the incomplete package so run2_package
    # rebuilds it with the full closure (norm-dedupe would otherwise skip it).
    dropped = 0
    if completed_norms:
        con = sqlite3.connect(WORK, timeout=120)
        con.execute("PRAGMA busy_timeout=60000")
        for ph, mj in list(con.execute(
                "select package_hash, manifest_json from skill_packages")):
            try:
                nh = (json.loads(mj).get("provenance") or {}).get("norm_hash")
            except Exception:
                continue
            if nh in completed_norms:
                for t in ("skill_packages", "skill_package_files", "skill_package_sources"):
                    con.execute(f"delete from {t} where package_hash=?", (ph,))
                dropped += 1
        con.commit()
        con.close()

    print(f"\ncompleted {skills_done} skills: +{tot_fetched} files "
          f"({tot_failed} unfetchable); {dropped} packages queued for rebuild")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
