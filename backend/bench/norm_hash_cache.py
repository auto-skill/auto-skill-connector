"""Persistent content-sha -> normalized-hash cache.

WHY THIS EXISTS
---------------
The object store is content-addressed, so the bytes behind a content sha are
immutable and their normalized hash is immutable with them. Recomputing that
hash is therefore pure waste -- yet three separate stages each recomputed it for
the WHOLE corpus on every single run:

  * run2_build_batch.py  judged_shas   (measured ~3,052s/batch at 132,651 blobs)
  * run2_popularity.py   norm_of()     (treeharvest loop, continuous)
  * run2_inherit.py      norm_for_content()

Each held only a per-run memo, so every run started cold: read the object off
disk, decode it, normalize it, sha256 it. On this box (WSL2, ~1.75ms per small
file read) that is ~23ms per object, and the cost grew linearly with the corpus.
It had already overrun the prebuild overlap window and was showing up as 23-27
minutes of dead time between batches, getting worse every batch.

This module makes the mapping persistent, so only genuinely new objects are ever
hashed. Steady-state cost becomes proportional to a batch, not to the corpus,
and it stops growing.

CORRECTNESS NOTES
-----------------
* All three callers normalize identically (run2_enrich.normalize) and decode
  identically (utf-8/replace), so a single shared cache is valid for all of
  them. Do not add a caller with different normalization without versioning the
  cache file.
* Negative results are NEVER persisted. treeharvest routinely learns a blob sha
  before enrichment has stored the object, so an absent object is a normal
  transient state, not a fact. Persisting "absent" would permanently mark a real
  object as missing. Absences are recomputed each run (one stat call).
* Writes merge under an flock, because the enrichment pipeline and the
  treeharvest loop run concurrently and a blind overwrite would drop the other
  writer's entries.
"""
from __future__ import annotations

import fcntl
import hashlib
import json
import os
from pathlib import Path

LIB = Path(__file__).resolve().parent.parent / "skills_library_v1"
CACHE_PATH = LIB / "norm_hash_index.json"


def normalize(text: str) -> str:
    """Byte-for-byte identical to run2_enrich.normalize (kept local so this
    module has no import cycle with the harness)."""
    t = text.replace("\r\n", "\n").replace("\r", "\n")
    t = "\n".join(line.rstrip() for line in t.split("\n"))
    return t.strip()


def load() -> dict:
    try:
        return json.loads(CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def norm_for(content_sha: str, cache: dict) -> str | None:
    """Normalized hash for a content sha, or None if the object is not stored.

    Populates `cache` on a miss. Absent objects are deliberately not recorded.
    """
    hit = cache.get(content_sha)
    if hit:
        return hit
    p = LIB / "objects" / content_sha[:2] / content_sha[2:4] / content_sha
    if not p.exists():
        return None
    nh = hashlib.sha256(
        normalize(p.read_bytes().decode("utf-8", "replace")).encode("utf-8")
    ).hexdigest()
    cache[content_sha] = nh
    return nh


def save(cache: dict) -> int:
    """Merge `cache` into the on-disk index under lock. Returns total entries."""
    if not cache:
        return 0
    lock = CACHE_PATH.with_suffix(".lock")
    tmp = CACHE_PATH.with_suffix(".json.tmp")
    with open(lock, "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        cur = load()
        cur.update(cache)
        tmp.write_text(json.dumps(cur), encoding="utf-8")
        os.replace(tmp, CACHE_PATH)
        return len(cur)
