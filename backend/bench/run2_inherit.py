#!/usr/bin/env python3
"""Verdict inheritance: identical bytes need only one judgement.

A git blob sha is a hash of the exact bytes, so when an unjudged sighting's
SKILL.md sha (from `repo_trees`, one API call per repo) maps to content whose
normalized hash has already been judged, the verdict transfers by construction —
zero fetch, zero Luna call. Measured duplication that motivates this: 13.3% of
random draws were exact duplicates; the effective unique corpus is ~26-80k
against 517k sightings, so most of the remaining "work" is re-discovering
already-judged bytes.

Chain:  repo_trees.sha -> blob_index (git sha -> content sha)
        -> CAS object -> norm_hash -> existing enrichments verdict
        -> deterministic `duplicate_of` row for the duplicate sighting

Quality invariants:
  * Only verdicts under the CURRENT prompt version transfer.
  * Canary sightings never inherit — they are judged live every batch, that is
    their job.
  * The inherited row records the source norm_hash, so provenance is auditable
    and reversible.

Idempotent; safe to run repeatedly (cron/daemon).
"""
from __future__ import annotations

import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
LIB = BACKEND / "skills_library_v1"
sys.path.insert(0, str(BENCH))

from run2_enrich import db, normalize  # noqa: E402
import norm_hash_cache  # noqa: E402

PROMPT_VERSION = "v2.1"


def norm_hash_of_bytes(b: bytes) -> str:
    return hashlib.sha256(normalize(b.decode("utf-8", "replace")).encode()).hexdigest()


def main() -> int:
    con = db()
    blob_index = json.loads((LIB / "blob_index.json").read_text())

    # verdict lookup: norm_hash -> (label-ish) from current-prompt enrichments
    judged = {r[0] for r in con.execute(
        "select distinct norm_hash from enrichments where prompt_version=?"
        " and judge_role in ('primary', 'deterministic')", (PROMPT_VERSION,))}
    already = {r[0] for r in con.execute(
        "select distinct skill_id from enrichments where skill_id is not null")}

    # canaries never inherit
    canary_ids = set()
    try:
        for c in json.loads((BACKEND / "evals" / "corpus_canaries.json").read_text())["canaries"]:
            canary_ids.add((c["parent_repo"] + "::" + c["path"]).casefold())
    except Exception:
        pass

    # content-sha -> norm-hash. PERSISTENT across runs: the store is
    # content-addressed, so this mapping can never change once computed.
    # Previously a per-run memo, which meant re-reading and re-hashing the
    # whole corpus from disk on every treeharvest iteration. See
    # norm_hash_cache.py.
    memo: dict = norm_hash_cache.load()
    _n0 = len(memo)

    def norm_for_content(csha: str) -> str | None:
        return norm_hash_cache.norm_for(csha, memo)

    stats = {"examined": 0, "inherited": 0, "no_blob_mapping": 0,
             "content_unjudged": 0, "already_has_row": 0, "canary_skipped": 0}
    now = datetime.now(timezone.utc).isoformat()
    # Incremental: only consider sightings NOT already carrying a verdict.
    # The full-scan version examined 238,216 rows to inherit 17,262 on every
    # batch -- simultaneously the fastest-growing stage (+359%) and the longest
    # write transaction in the system, which made it the main generator of the
    # lock contention that stalled judging. Anti-joining in SQL moves the filter
    # into the index instead of into Python.
    con.execute("CREATE INDEX IF NOT EXISTS enr_skill_id ON enrichments(skill_id)")
    rows = list(con.execute(
        "select s.id, s.external_id, t.sha from sightings s"
        " join repo_trees t on t.repo = substr(s.external_id,1,instr(s.external_id,'::')-1)"
        "  and t.path = substr(s.external_id, instr(s.external_id,'::')+2)"
        " where s.path like '%SKILL.md'"
        "   and not exists (select 1 from enrichments e"
        "                   where e.skill_id = 'sighting:' || s.id)"))
    print(f"  candidates after anti-join: {len(rows)}", flush=True)
    for sid, ext, sha in rows:
        stats["examined"] += 1
        skill_id = f"sighting:{sid}"
        if skill_id in already:
            stats["already_has_row"] += 1
            continue
        if (ext or "").casefold() in canary_ids:
            stats["canary_skipped"] += 1
            continue
        csha = blob_index.get(sha)
        if not csha:
            stats["no_blob_mapping"] += 1
            continue
        nh = norm_for_content(csha)
        if not nh or nh not in judged:
            stats["content_unjudged"] += 1
            continue
        out = {"_rule": "duplicate_of", "_source_norm_hash": nh,
               "_via": "repo_trees blob sha", "is_real_skill": None,
               "reject_reason": None,
               "summary": "byte-identical duplicate of already-judged content"}
        con.execute(
            "INSERT OR REPLACE INTO enrichments"
            " (norm_hash,skill_id,skill_url,judge_role,prompt_version,model_snapshot,"
            "  output_json,tokens_in,tokens_out,status,created_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (nh, skill_id, None, "deterministic", PROMPT_VERSION,
             "inherit/blob-sha", json.dumps(out), 0, 0, "ok", now))
        stats["inherited"] += 1
        if stats["inherited"] % 250 == 0:
            con.commit()
    con.commit()
    con.close()
    if len(memo) != _n0:
        norm_hash_cache.save(memo)
    print(json.dumps(stats, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
