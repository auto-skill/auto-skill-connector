#!/usr/bin/env python3
"""Declare stored files that the entrypoint cites but the closure omits.

The corpus stores far more than it delivers. A value audit found 2,449 packages
(15.6%) where the SKILL.md explicitly tells the agent to open a file, that file
is on disk and byte-verified, and the dependency closure omits it: 7,897 files,
74.8 MB, 2,321 of them scripts. Aggregate delivery across the twenty most-forked
packages is 29.1% -- Vercel's 108 KB rule set, Supabase's 34 Postgres rule files
and Anthropic's PDF form-filling scripts are all stored and all undeclared, so a
retriever honouring the closure ships a table of contents and drops the content.

Proximate cause: the judge emits `closure_paths` and returned an empty list for
54.7% of packages. Citation is mechanical evidence and does not need a model --
if the entrypoint names a stored sibling file, that file is part of the skill.

This reads each package's entrypoint, extracts cited paths (markdown links,
backticked paths, bare relative paths, script invocations), resolves them
against files already in the package's own repo tree, and rebuilds the package
with the full closure. Package identity includes the file list, so a corrected
package gets a new hash; the stale one is withdrawn.

Content is never fetched or deleted -- this only changes what is DECLARED.
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sqlite3
import sys
from pathlib import Path, PurePosixPath

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
LIB = BACKEND / "skills_library_v1"
WORK = BACKEND / "corpus_v0_work.sqlite"
sys.path.insert(0, str(BENCH))

# Paths an entrypoint can cite: markdown links, backticked paths, script
# invocations, and bare relative references to a file with an extension.
CITE_RES = [
    re.compile(r"\]\(\s*([^)\s#]+\.[A-Za-z0-9]{1,6})\s*[)#]"),      # [x](path.md)
    re.compile(r"`([^`\n]+?\.[A-Za-z0-9]{1,6})`"),                   # `scripts/x.py`
    re.compile(r"(?:python3?|bash|sh|node|deno)\s+([^\s`'\"]+\.[A-Za-z0-9]{1,6})"),
    re.compile(r"(?:^|\s)((?:\./|\.\./)?(?:[\w.-]+/){1,6}[\w.-]+\.[A-Za-z0-9]{1,6})(?=\s|$)"),
]
TEXTY = re.compile(r"\.(md|markdown|txt|py|sh|bash|js|mjs|ts|tsx|json|ya?ml|toml|"
                   r"sql|rb|go|rs|java|kt|c|h|cpp|cs|php|tmpl|template)$", re.I)


def cited_paths(text: str) -> set[str]:
    out: set[str] = set()
    for rx in CITE_RES:
        for m in rx.finditer(text):
            p = m.group(1).strip().lstrip("./")
            if p and len(p) < 200 and TEXTY.search(p):
                out.add(p)
    return out


def read_obj(h: str | None) -> bytes | None:
    if not h:
        return None
    p = LIB / "objects" / h[:2] / h[2:4] / h
    return p.read_bytes() if p.exists() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    # repo tree per skill, from the fetch caches: path -> git sha
    caches: dict = {}
    for f in glob.glob(str(BENCH / "run2_fetch_b*.json")):
        try:
            caches.update(json.loads(Path(f).read_text()))
        except Exception:
            pass
    blob_index = json.loads((LIB / "blob_index.json").read_text())

    # combined rows by norm_hash, to locate each package's fetch entry + closure
    rows: dict[str, dict] = {}
    for f in sorted(glob.glob(str(BENCH / "run2_combined_b*.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        for r in d.get("rows", []):
            if r.get("label") == "included":
                rows.setdefault(r["norm_hash"], r)

    fixed = 0
    added_files = 0
    added_bytes = 0
    to_rebuild: list[str] = []

    for nh, r in list(rows.items())[: args.limit or None]:
        fe = caches.get(r["skill_id"]) or {}
        if fe.get("status") != "ok":
            continue
        entry_path = fe.get("entry_path")
        body = read_obj(fe.get("entry_hash"))
        if not body or not entry_path:
            continue
        tree = {e["path"]: e for e in (fe.get("tree") or []) if e.get("type") == "file"}
        cl = r.setdefault("closure", {})
        declared = set((cl.get("fetched") or []) + (cl.get("already_present") or []))

        base = PurePosixPath(entry_path).parent
        text = body.decode("utf-8", "replace")
        new: list[str] = []
        for cite in cited_paths(text):
            # resolve relative to the skill dir, then by unique suffix in the tree
            cand = str(PurePosixPath(base) / cite)
            cand = str(PurePosixPath(cand))          # normalise ../ segments
            target = cand if cand in tree else None
            if target is None:
                matches = [p for p in tree if p.endswith("/" + cite) or p == cite]
                target = matches[0] if len(matches) == 1 else None
            if not target or target in declared or target == entry_path:
                continue
            gsha = (tree.get(target) or {}).get("sha")
            if gsha and blob_index.get(gsha) and read_obj(blob_index[gsha]) is not None:
                new.append(target)
                added_bytes += (tree.get(target) or {}).get("size") or 0

        if new:
            fixed += 1
            added_files += len(new)
            if not args.dry_run:
                cl.setdefault("already_present", []).extend(sorted(set(new)))
                to_rebuild.append(nh)

    print(f"  packages with undeclared citations: {fixed}")
    print(f"  files to declare: {added_files} ({added_bytes/1e6:.1f} MB)")
    if args.dry_run or not to_rebuild:
        return 0

    # persist the corrected closures back into the batch files
    for f in sorted(glob.glob(str(BENCH / "run2_combined_b*.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        changed = False
        for r in d.get("rows", []):
            src = rows.get(r.get("norm_hash"))
            if src is not None and r.get("label") == "included" and src is not r:
                continue
            if src is r and r["norm_hash"] in set(to_rebuild):
                changed = True
        if changed:
            tmp = Path(str(f) + ".tmp")
            tmp.write_text(json.dumps(d, indent=1))
            import os
            os.replace(tmp, f)

    # withdraw stale packages so run2_package rebuilds them with full closure
    con = sqlite3.connect(WORK, timeout=300)
    con.execute("PRAGMA busy_timeout=300000")
    want = set(to_rebuild)
    dropped = 0
    for ph, mj in list(con.execute("select package_hash, manifest_json from skill_packages")):
        try:
            nh = (json.loads(mj).get("provenance") or {}).get("norm_hash")
        except Exception:
            continue
        if nh in want:
            for t in ("skill_packages", "skill_package_files", "skill_package_sources"):
                con.execute(f"delete from {t} where package_hash=?", (ph,))
            dropped += 1
    con.commit()
    con.close()
    print(f"  withdrew {dropped} stale packages for rebuild with full closure")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
