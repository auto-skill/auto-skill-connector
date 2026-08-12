#!/usr/bin/env python3
"""Ingestion run 2 / Part B step 5 — build a hardening batch.

One batch = N previously-unjudged skills, drawn from BOTH pools the plan names:

  * frozen-corpus rows  (backend/corpus_v0_work.sqlite — the scrubbed working copy)
  * fresh sweep sightings (enrichment_v1.db `sightings`, from run1_sweep.py)

plus **every canary rides in every batch** as a seeded regression test. Canaries whose
content hash is already judged under the current prompt version cost zero model calls;
they are present so the batch's combine step re-asserts their label.

"Previously unjudged" is decided by skill id / sighting url against the ids already
present in `enrichments` for the current prompt version, so batches never overlap and
re-running is idempotent.

Read-only against the corpus. Writes only backend/bench/run2_batch_<n>.json
"""
from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
WORK = BACKEND / "corpus_v0_work.sqlite"
DB = BACKEND / "enrichment_v1.db"
CANARIES = BACKEND / "evals" / "corpus_canaries.json"
SAMPLE_V1 = BENCH / "enrichment_sample_v1.json"

# Which blob-sha first digits THIS machine may judge. Empty = all (solo mode).
# Set to the complement of a collaborator's manifest partition so the two sides
# cannot draw the same skill. See the note in the sweep draw loop.
SHA_PARTITION = set(os.environ.get("AUTOSKILL_SHA_PARTITION", "").lower().strip())


def parse_raw(raw):
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def entrypoint_ref(row: dict) -> dict:
    d = parse_raw(row.get("raw"))
    repo, path = (d.get("parent_repo") or "").strip(), (d.get("path") or "").strip()
    if repo and path:
        return {"mode": "repo_path", "repo": repo, "path": path}
    url = (row.get("url") or "").strip()
    if "github.com/" in url:
        parts = url.split("github.com/", 1)[1].strip("/").split("/")
        if len(parts) >= 5 and parts[2] in ("tree", "blob"):
            return {"mode": "repo_dir", "repo": f"{parts[0]}/{parts[1]}",
                    "ref": parts[3], "dir": "/".join(parts[4:])}
        if len(parts) == 2:
            return {"mode": "repo_root", "repo": f"{parts[0]}/{parts[1]}"}
    return {"mode": "unresolvable"}


def retryable_ids(prompt_version: str) -> set[str]:
    """Skill ids whose ONLY verdict is a not-yet-exhausted retryable failure.

    These are eligible to be offered again: a transient fetch failure says
    nothing about the skill, so it must not retire it from the pipeline.
    """
    if not DB.exists():
        return set()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    RETRYABLE_RULES = {"fetch_failed"}
    RETRY_LIMIT = int(os.environ.get("AUTOSKILL_FETCH_RETRY_LIMIT", "3"))
    # Filter in SQL, not Python. This used to SELECT output_json for EVERY row
    # (68,226 today) and then discard 67% of them with `if role != "deterministic"`,
    # decoding a JSON blob per surviving row. json_extract does the same work in C
    # against only the rows that matter: measured 2.8s -> 0.3s (8x), identical set.
    # The cost is O(corpus) and this runs before every batch, so it degrades
    # forever if left alone.
    cand: set[str] = {
        r[0] for r in con.execute(
            "select skill_id from enrichments"
            " where prompt_version=? and skill_id is not null"
            "   and judge_role='deterministic'"
            "   and json_extract(output_json,'$._rule') IN (%s)"
            "   and coalesce(json_extract(output_json,'$._attempts'),0) < ?"
            % ",".join("?" * len(RETRYABLE_RULES)),
            (prompt_version, *sorted(RETRYABLE_RULES), RETRY_LIMIT))}
    real = {r[0] for r in con.execute(
        "select distinct skill_id from enrichments where prompt_version=?"
        " and judge_role!='deterministic' and skill_id is not null", (prompt_version,))}
    con.close()
    return cand - real


def judged_ids(prompt_version: str) -> set[str]:
    if not DB.exists():
        return set()
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    # status='ok' is load-bearing: a `failed(...)` row records a call that DIED
    # (quota exhausted, stream disconnected, model at capacity) and carries no
    # verdict. Excluding those skills here drops them from every future batch,
    # so they are never re-offered -- a transient infra blip becomes permanent
    # corpus loss. Measured 2026-08-11: 9,106 skills lost this way.
    ids = {r[0] for r in con.execute(
        "select distinct skill_id from enrichments"
        " where prompt_version=? and skill_id is not null and status='ok'",
        (prompt_version,))}

    con.close()
    return ids - retryable_ids(prompt_version)


def main() -> int:
    # Phase timing. The build gap has grown 30s -> 163s -> 386s -> 1571s as the
    # corpus grew, and every component measures trivially fast in isolation
    # (warm cache) while costing minutes in situ. Measure in production instead
    # of guessing -- that pattern has already been wrong five times here.
    import time as _bt
    BPHASE = {}
    _bm = [_bt.time()]

    def bphase(name):
        now = _bt.time()
        BPHASE[name] = BPHASE.get(name, 0.0) + now - _bm[0]
        _bm[0] = now

    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, required=True)
    ap.add_argument("--size", type=int, default=100)
    ap.add_argument("--prompt-version", default=os.environ.get("RUN2_PROMPT_VERSION", "v2"))
    ap.add_argument("--corpus-frac", type=float, default=0.5,
                    help="fraction drawn from the frozen corpus; rest from sweep sightings")
    args = ap.parse_args()

    out_path = BENCH / f"run2_batch_{args.batch}.json"
    if out_path.exists():
        print(f"{out_path} already exists — reusing (batches are stable by design)")
        return 0

    rnd = random.Random(20260802 + args.batch)
    seen = judged_ids(args.prompt_version)
    bphase('judged_ids')
    # Ids used by any earlier batch file, so batches never overlap.
    #
    # This is a SECOND retirement path and it had the same flaw as judged_ids:
    # appearing in an earlier batch retired a skill even when that batch only
    # produced a retryable fetch failure. Releasing it in judged_ids alone moved
    # just 22 of 1,168 stranded skills, because this set re-blocked the rest.
    # Skills still eligible for retry are subtracted back out.
    retryable = retryable_ids(args.prompt_version)
    bphase('retryable_ids')
    for f in sorted(BENCH.glob("run2_batch_*.json")):
        try:
            seen |= {s["id"] for s in json.loads(f.read_text())["skills"]}
        except Exception:
            pass
    bphase('prior_batch_files')
    seen -= retryable
    try:
        seen |= {s["id"] for s in json.loads(SAMPLE_V1.read_text())["skills"]}
    except Exception:
        pass

    picked: dict[str, dict] = {}

    def add(row: dict, bucket: str) -> bool:
        if row["id"] in picked or row["id"] in seen:
            return False
        r = dict(row)
        r["buckets"] = [bucket]
        r["entrypoint_ref"] = entrypoint_ref(r)
        picked[r["id"]] = r
        return True

    # ---- canaries (free; already-judged hashes are skipped downstream) --------
    con = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
    cur = con.cursor()
    base = ("select id,name,description,source,url,raw,quality_status,quality_score,"
            "risk_score,content_hash,category from skills")
    canary_ids = []
    for c in json.loads(CANARIES.read_text())["canaries"]:
        repo, path = c["parent_repo"].casefold(), c["path"].casefold()
        cur.execute(base + " where source='github_skill_file' and lower(raw) like ?",
                    (f"%{path}%",))
        for r in cur.fetchall():
            row = dict(zip([d[0] for d in cur.description], r))
            d = parse_raw(row["raw"])
            if (d.get("parent_repo", "").casefold() == repo
                    and d.get("path", "").casefold() == path):
                row["buckets"] = ["canary"]
                row["entrypoint_ref"] = entrypoint_ref(row)
                picked[row["id"]] = row
                canary_ids.append(row["id"])
                found = True
                break
        else:
            found = False
        if not found:
            # A canary is DEFINED by repo+path in the canary file; requiring a
            # matching row in the frozen corpus DB made that an accident of what
            # the old scrape happened to contain. oxcaml/oxcaml is absent from
            # it, so the oxcaml-address-review canary -- the original acceptance
            # test for this corpus -- never once rode in a batch. Synthesise the
            # row instead: repo+path is all the entrypoint resolver needs.
            synth = {
                "id": f"canary:{c['id']}", "name": c.get("expected_name") or c["id"],
                "description": "", "source": "github_skill_file",
                "url": c.get("source_url") or "",
                "raw": json.dumps({"parent_repo": c["parent_repo"], "path": c["path"]}),
                "quality_status": None, "quality_score": None, "risk_score": None,
                "content_hash": c.get("expected_content_hash"), "category": None,
                "buckets": ["canary"]}
            synth["entrypoint_ref"] = entrypoint_ref(synth)
            picked[synth["id"]] = synth
            canary_ids.append(synth["id"])

    n_corpus = int(args.size * args.corpus_frac)
    n_sweep = args.size - n_corpus

    # ---- pool R: previously-stranded retries, drained FIRST -------------------
    # Making these merely *eligible* was not enough: 1,168 retryable skills
    # against a pool of ~750k means random sampling essentially never draws one
    # (measured: 0 of 172 in a rebuilt batch). They get an explicit share of each
    # batch so the backlog actually drains instead of nominally existing.
    n_retry = min(len(retryable), max(1, args.size // 4)) if retryable else 0
    got_retry = 0
    if n_retry:
        # Retry candidates live in TWO id namespaces: corpus rows and sweep
        # sightings ("sighting:<hash>"). The first version of this pool queried
        # only the corpus DB, so every sighting-sourced retryable -- 297 of 297
        # at the time -- was silently undrainable by the mechanism built to
        # drain it. Sighting rows are reconstructed the same way pool B builds
        # them.
        rlist = sorted(retryable)
        rnd.shuffle(rlist)
        corpus_ids = [i for i in rlist if not i.startswith("sighting:")]
        sight_ids = [i for i in rlist if i.startswith("sighting:")]
        if corpus_ids:
            rcon = sqlite3.connect(f"file:{WORK}?mode=ro", uri=True)
            rcur = rcon.cursor()
            for sid in corpus_ids:
                if got_retry >= n_retry:
                    break
                rcur.execute(base + " where id=?", (sid,))
                r = rcur.fetchone()
                if not r:
                    continue
                row = dict(zip([d[0] for d in rcur.description], r))
                row["buckets"] = ["retry"]
                row["entrypoint_ref"] = entrypoint_ref(row)
                if row["id"] not in picked:
                    picked[row["id"]] = row
                    got_retry += 1
            rcon.close()
        if sight_ids and got_retry < n_retry and DB.exists():
            scon = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
            for sid in sight_ids:
                if got_retry >= n_retry:
                    break
                r = scon.execute(
                    "select id,url,path,external_id from sightings where id=?",
                    (sid.split("sighting:", 1)[1],)).fetchone()
                if not r:
                    continue
                _id, url, path, ext = r
                repo = (ext or "").split("::", 1)[0]
                row = {"id": sid, "name": Path(path).parent.name or path,
                       "description": "", "source": "github_skill_file", "url": url,
                       "raw": json.dumps({"parent_repo": repo, "path": path}),
                       "quality_status": None, "quality_score": None,
                       "risk_score": None, "content_hash": None, "category": None,
                       "buckets": ["retry"]}
                row["entrypoint_ref"] = entrypoint_ref(row)
                if row["id"] not in picked:
                    picked[row["id"]] = row
                    got_retry += 1
            scon.close()
        n_corpus = max(0, n_corpus - got_retry // 2)
        n_sweep = max(0, n_sweep - (got_retry - got_retry // 2))

    # ---- pool A: frozen corpus, unjudged ------------------------------------
    cur.execute(base + " where source='github_skill_file' order by id")
    cols = [d[0] for d in cur.description]
    pool = [dict(zip(cols, r)) for r in cur.fetchall()]
    rnd.shuffle(pool)
    got_corpus = 0
    for row in pool:
        if got_corpus >= n_corpus:
            break
        if add(row, "corpus"):
            got_corpus += 1
    con.close()

    # The frozen-corpus pool is now exhausted. Keep the proven batch-size
    # contract by filling its unused allocation from the independent sweep
    # pool, rather than quietly shrinking every future batch by that amount.
    # This only changes scheduling: all candidates still pass the same fetch,
    # primary/secondary judging, canary, and storage-quality gates.
    if got_corpus < n_corpus:
        n_sweep += n_corpus - got_corpus
        print(f"  corpus pool short by {n_corpus - got_corpus}; reallocating to sweep")

    # ---- pool B: fresh sweep sightings --------------------------------------
    got_sweep = 0
    if DB.exists():
        scon = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        rows = scon.execute(
            "select id,url,path,external_id from sightings where path like '%SKILL.md'"
            " order by id").fetchall()
        scon.close()
        rnd.shuffle(rows)
        # Draw ONE representative per blob sha. Measured: 13.3% of random draws
        # were byte-identical duplicates of other draws (mega-forked skills
        # appeared 31x in a 7k sample) -- every such draw wasted a fetch and a
        # batch slot to rediscover known bytes. Where the tree harvest has
        # coverage, skip sightings whose sha is already judged (inheritance
        # handles them for free) or already drawn into this batch.
        tcon = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        sha_of = {}
        try:
            sha_of = {f"{r}::{p}": h for r, p, h in tcon.execute(
                "select repo, path, sha from repo_trees where path like '%SKILL.md'")}
        except Exception:
            pass
        judged_shas = set()
        try:
            import hashlib as _h
            bi = json.loads((BACKEND / "skills_library_v1" / "blob_index.json").read_text())
            jn = {r[0] for r in tcon.execute(
                "select distinct norm_hash from enrichments"
                " where prompt_version=? and status='ok'",   # see judged_ids()
                (args.prompt_version,))}
            # A content sha -> normalized hash mapping is IMMUTABLE: the object
            # store is content-addressed, so the bytes behind a csha never change
            # and neither can their normalized hash. This loop nevertheless
            # re-read, decoded, normalised and re-hashed EVERY object from disk on
            # EVERY batch: 132,651 entries, measured 46s per 2,000 = ~3,052s per
            # batch, against an instrumented select_and_shape of 2,390-2,924s.
            # It was the single largest cost in the pipeline and grew linearly
            # with the corpus.
            #
            # Cached permanently instead. Only genuinely new objects are hashed,
            # so the steady-state cost is proportional to a batch (~600), not to
            # the corpus.
            # A content sha -> normalized hash mapping is IMMUTABLE (the store is
            # content-addressed), yet this loop used to re-read, decode,
            # normalize and re-hash EVERY object from disk on EVERY batch:
            # 132,651 entries, ~3,052s/batch, growing with the corpus. It was
            # the pipeline's single largest cost. Now cached permanently; see
            # norm_hash_cache.py.
            import norm_hash_cache as _nhc
            _cache = _nhc.load()
            _n0 = len(_cache)
            for gsha, csha in bi.items():
                nh = _nhc.norm_for(csha, _cache)
                if nh and nh in jn:
                    judged_shas.add(gsha)
            if len(_cache) != _n0:
                print(f"  norm-hash cache: +{len(_cache)-_n0:,} -> {_nhc.save(_cache):,}",
                      flush=True)
        except Exception:
            pass
        tcon.close()
        # Popularity-first: judge the most-forked content before one-offs. A
        # skill mirrored in 222 repos serves more future retrievals than a
        # personal experiment; at this writing the top-forked skills were still
        # unjudged because random order had never favoured them.
        occ_of = {}
        try:
            acon = sqlite3.connect(f"file:{BACKEND / 'analytics_v1.db'}?mode=ro", uri=True)
            occ_of = dict(acon.execute("select sha, occurrences from skill_popularity"))
            acon.close()
        except Exception:
            pass
        if occ_of and sha_of:
            # Measured correction (audit 2026-08-03): fork count is *negatively*
            # correlated with quality (Spearman -0.14; top-10 most-copied mean
            # specificity 0.775 vs 0.854 corpus-wide). Pure popularity-first
            # pushed recent batches to a median of 19 copies vs 2 early, and
            # mass-produced boilerplate among includes rose 3.9x.
            #
            # Popularity still matters -- a skill in 591 repos is what people
            # actually reach for -- so this interleaves rather than reverses:
            # half the batch from the copied head (retrieval demand), half from
            # unique content (diversity and specificity). Neither signal alone
            # is a quality prior.
            ranked = sorted(rows, key=lambda r: occ_of.get(sha_of.get(r[3] or ""), 0),
                            reverse=True)
            unique = [r for r in ranked if occ_of.get(sha_of.get(r[3] or ""), 0) <= 2]
            popular = [r for r in ranked if occ_of.get(sha_of.get(r[3] or ""), 0) > 2]
            rnd.shuffle(unique)
            rows, i, j = [], 0, 0
            while i < len(popular) or j < len(unique):
                if i < len(popular):
                    rows.append(popular[i]); i += 1
                if j < len(unique):
                    rows.append(unique[j]); j += 1
        drawn_shas: set = set()
        for sid, url, path, ext in rows:
            if got_sweep >= n_sweep:
                break
            sha = sha_of.get(ext or "")
            # Collaborator partition. Judging is content-addressed, so splitting
            # the backlog by the first hex digit of the blob sha gives two halves
            # that cannot overlap, with no lock and no coordination -- but ONLY
            # if both sides filter. Measured 2026-08-12: we shipped a 0-7 manifest
            # to a collaborator and told them to stay out of 8-f, but never
            # constrained this builder, so our draws stayed uniform across all 16
            # digits and ~87k of our verdicts landed inside their half. Duplicated
            # work is wasted quota on both sides, not corruption (the enrichment
            # PK is content-addressed, so a double judge is idempotent).
            if sha and SHA_PARTITION and sha[0] not in SHA_PARTITION:
                continue
            if sha and (sha in judged_shas or sha in drawn_shas):
                continue
            if sha:
                drawn_shas.add(sha)
            repo = (ext or "").split("::", 1)[0]
            if not repo or "/" not in repo:
                continue
            key = "sighting:" + sid
            if key in picked or key in seen:
                continue
            picked[key] = {
                "id": key, "name": str(Path(path).parent.name), "description": "",
                "source": "sweep_sighting", "url": url, "raw": json.dumps(
                    {"parent_repo": repo, "path": path}),
                "quality_status": None, "quality_score": None, "risk_score": None,
                "content_hash": None, "category": None,
                "buckets": ["sweep"],
                "entrypoint_ref": {"mode": "repo_path", "repo": repo, "path": path},
            }
            got_sweep += 1

    rows_out = sorted(picked.values(), key=lambda r: r["id"])
    modes: dict[str, int] = {}
    for r in rows_out:
        modes[r["entrypoint_ref"]["mode"]] = modes.get(r["entrypoint_ref"]["mode"], 0) + 1

    out = {
        "schema_version": 1, "run_id": "run2-20260802", "batch": args.batch,
        "seed": 20260802 + args.batch, "prompt_version": args.prompt_version,
        "requested_size": args.size,
        "counts": {"corpus": got_corpus, "sweep": got_sweep,
                   "canary": len(canary_ids), "total": len(rows_out)},
        "entrypoint_modes": modes,
        "canary_ids": canary_ids,
        "skills": rows_out,
    }
    bphase("select_and_shape")
    print("  build phases: " + json.dumps({k: round(v,1) for k,v in BPHASE.items()}), flush=True)
    out_path.write_text(json.dumps(out, indent=1), encoding="utf-8")
    print(f"batch {args.batch}: {out['counts']} -> {out_path}")
    print("entrypoint modes:", json.dumps(modes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
