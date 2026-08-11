#!/usr/bin/env python3
"""Materialize per-skill popularity/provenance features for retrieval and ML.

Frequency was always *derivable* (sightings + repo_trees preserve every
occurrence with timestamps) but not *queryable* — and a ranking model cannot
train on a join nobody ran. This produces `skill_popularity`, keyed by git blob
sha with the norm_hash attached where the content is known:

    sha, norm_hash
    occurrences      how many (repo, path) locations carry these exact bytes
    repo_count       distinct repos among them  (394/222 for the top skill)
    sighting_count   search-visible occurrences (code-search dedupes forks, so
                     this is a *different* popularity lens than the tree count)
    first_seen / last_seen    from sighting timestamps
    vendor_dirs      distinct convention dirs it appears under (.claude/.codex/...)
    computed_at

`fork_count`/`repo_count`/`first_seen`/`last_seen` are also stamped into the
provenance of every existing package so the servable store carries its own
popularity prior. Idempotent; refreshed from the daemon loops as harvest grows.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
LIB = BACKEND / "skills_library_v1"
E_DB = BACKEND / "enrichment_v1.db"
SNAP = BACKEND / "analytics_v1.db"
WORK = BACKEND / "corpus_v0_work.sqlite"

sys.path.insert(0, str(BENCH))
import norm_hash_cache  # noqa: E402

VENDOR_RE = re.compile(r"(^|/)(\.(claude|codex|cursor|gemini|github|windsurf|cline)|skills?)/",
                       re.IGNORECASE)


def norm_of(content_sha: str, memo: dict) -> str | None:
    """Persistent now -- this used to re-hash the whole corpus from disk on
    every treeharvest iteration, continuously, against the same disk the
    enrichment pipeline needs. See norm_hash_cache.py."""
    return norm_hash_cache.norm_for(content_sha, memo)


def main() -> int:
    e = sqlite3.connect(f"file:{E_DB}?mode=ro", uri=True)
    now = datetime.now(timezone.utc).isoformat()

    # occurrences + repo spread + vendor dirs, from the harvest (fork-visible)
    pop: dict[str, dict] = {}
    for sha, path, repo in e.execute(
            "select sha, path, repo from repo_trees where path like '%SKILL.md'"):
        d = pop.setdefault(sha, {"occ": 0, "repos": set(), "vendors": set()})
        d["occ"] += 1
        d["repos"].add(repo)
        m = VENDOR_RE.search("/" + path)
        if m:
            d["vendors"].add(m.group(2).lower().lstrip("."))

    # sighting counts + first/last seen (search-visible lens)
    sight: dict[str, dict] = {}
    for ext, ts in e.execute(
            "select external_id, observed_at from sightings where path like '%SKILL.md'"):
        sight.setdefault(ext, {"n": 0, "first": ts, "last": ts})
        s = sight[ext]
        s["n"] += 1
        s["first"] = min(s["first"], ts)
        s["last"] = max(s["last"], ts)
    # map ext (repo::path) -> sha where harvested
    ext_sha = {f"{r}::{p}": sha for r, p, sha in e.execute(
        "select repo, path, sha from repo_trees where path like '%SKILL.md'")}
    sight_by_sha: dict[str, dict] = {}
    for ext, s in sight.items():
        sha = ext_sha.get(ext)
        if not sha:
            continue
        d = sight_by_sha.setdefault(sha, {"n": 0, "first": s["first"], "last": s["last"]})
        d["n"] += s["n"]
        d["first"] = min(d["first"], s["first"])
        d["last"] = max(d["last"], s["last"])

    blob_index = json.loads((LIB / "blob_index.json").read_text())
    memo: dict = norm_hash_cache.load()
    _n0 = len(memo)

    snap = sqlite3.connect(SNAP, timeout=60)
    snap.execute("PRAGMA busy_timeout=30000")
    snap.execute("""CREATE TABLE IF NOT EXISTS skill_popularity (
        sha TEXT PRIMARY KEY, norm_hash TEXT, occurrences INTEGER, repo_count INTEGER,
        sighting_count INTEGER, first_seen TEXT, last_seen TEXT,
        vendor_dirs TEXT, computed_at TEXT)""")
    snap.execute("CREATE INDEX IF NOT EXISTS pop_norm ON skill_popularity(norm_hash)")
    rows = []
    for sha, d in pop.items():
        csha = blob_index.get(sha)
        nh = norm_of(csha, memo) if csha else None
        sg = sight_by_sha.get(sha, {})
        rows.append((sha, nh, d["occ"], len(d["repos"]), sg.get("n", 0),
                     sg.get("first"), sg.get("last"),
                     ",".join(sorted(d["vendors"])), now))
    snap.executemany("insert or replace into skill_popularity values (?,?,?,?,?,?,?,?,?)",
                     rows)
    snap.commit()

    # Persist the hashes NOW, before the provenance stamping below -- that step
    # writes the packages DB and intermittently dies on "database is locked"
    # (3 of the last 41 runs, pre-existing). Saving only at the end would throw
    # away this run's hashing work every time that happens.
    if len(memo) != _n0:
        norm_hash_cache.save(memo)
        _n0 = len(memo)

    # stamp popularity into package provenance (the servable store's own prior)
    by_norm: dict[str, tuple] = {}
    for sha, nh, occ, rc, sc, fs, ls, vd, _ in rows:
        if nh:
            cur = by_norm.get(nh)
            if not cur or occ > cur[0]:
                by_norm[nh] = (occ, rc, sc, fs, ls)
    w = sqlite3.connect(WORK, timeout=120)
    w.execute("PRAGMA busy_timeout=60000")
    upd = 0
    for ph, mj in list(w.execute("select package_hash, manifest_json from skill_packages")):
        try:
            m = json.loads(mj)
        except Exception:
            continue
        nh = (m.get("provenance") or {}).get("norm_hash")
        v = by_norm.get(nh)
        if not v:
            continue
        prov = m["provenance"]
        if prov.get("fork_count") == v[0] and prov.get("repo_count") == v[1]:
            continue
        prov.update({"fork_count": v[0], "repo_count": v[1], "sighting_count": v[2],
                     "first_seen": v[3], "last_seen": v[4]})
        w.execute("update skill_packages set manifest_json=? where package_hash=?",
                  (json.dumps(m), ph))
        upd += 1
    w.commit()
    w.close()

    n_norm = sum(1 for r in rows if r[1])
    print(f"popularity: {len(rows)} shas ({n_norm} with norm_hash), "
          f"{upd} package provenances stamped")
    e.close()
    snap.close()
    if len(memo) != _n0:
        norm_hash_cache.save(memo)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
