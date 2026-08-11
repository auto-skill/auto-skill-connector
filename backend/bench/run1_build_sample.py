#!/usr/bin/env python3
"""Ingestion run 1 / Phase 5 — build the enrichment sample.

Sample composition (per plan):
  * stratified random over `source` from the frozen v0 corpus
  * ~20 lowest-quality rows
  * ~10 known-generic skills
  * EVERY known-real canary skill present in the corpus (seeded false-negative test)

Deterministic: fixed seed, stable ordering, so re-running reproduces the same
sample and Phase 5 stays resumable.

Read-only against the frozen corpus. Writes only
`backend/bench/enrichment_sample_v1.json`.
"""
from __future__ import annotations

import json
import os
import random
import sqlite3
from pathlib import Path

BENCH = Path(__file__).resolve().parent
BACKEND = BENCH.parent
FROZEN = BACKEND / "corpus_v0_frozen" / "corpus_v0.sqlite"
CANARIES = BACKEND / "evals" / "corpus_canaries.json"
OUT = BENCH / "enrichment_sample_v1.json"

SEED = 20260802
TARGET_TOTAL = 200
N_LOW_QUALITY = 20
N_GENERIC = 10

# Names that signal a skill is generic advice rather than a specific capability.
# `ponytail` is the empirical case: it was injected on 74/75 Terminal-Bench routes.
GENERIC_NAMES = [
    "ponytail", "code-review", "code-reviewer", "codereview", "testing", "debugging",
    "best-practices", "general", "helper", "assistant", "utils", "utilities",
    "documentation", "refactor", "refactoring", "clean-code", "coding-standards",
    "git-commit", "commit", "pr-review", "writing", "planning", "research",
]


def rows_to_dicts(cur) -> list[dict]:
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, r)) for r in cur.fetchall()]


def parse_raw(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def entrypoint_ref(row: dict) -> dict:
    """How (or whether) this row's entrypoint can be fetched from public source."""
    d = parse_raw(row.get("raw"))
    repo, path = (d.get("parent_repo") or "").strip(), (d.get("path") or "").strip()
    if repo and path:
        return {"mode": "repo_path", "repo": repo, "path": path}
    url = (row.get("url") or "").strip()
    # https://github.com/<owner>/<repo>/tree/<ref>/<dir...>  -> directory we can list
    if "github.com/" in url:
        tail = url.split("github.com/", 1)[1].strip("/")
        parts = tail.split("/")
        if len(parts) >= 5 and parts[2] in ("tree", "blob"):
            return {"mode": "repo_dir", "repo": f"{parts[0]}/{parts[1]}",
                    "ref": parts[3], "dir": "/".join(parts[4:])}
        if len(parts) == 2:
            return {"mode": "repo_root", "repo": f"{parts[0]}/{parts[1]}"}
    return {"mode": "unresolvable"}


def base_select() -> str:
    return ("select id,name,description,source,url,raw,quality_status,quality_score,"
            "risk_score,content_hash,category from skills")


def main() -> int:
    if not FROZEN.exists():
        raise SystemExit(f"frozen corpus missing: {FROZEN}")
    con = sqlite3.connect(f"file:{FROZEN}?mode=ro", uri=True)
    rnd = random.Random(SEED)

    picked: dict[str, dict] = {}          # id -> row (+ bucket)

    def add(row: dict, bucket: str) -> bool:
        if row["id"] in picked:
            picked[row["id"]]["buckets"].append(bucket)
            return False
        row = dict(row)
        row["buckets"] = [bucket]
        row["entrypoint_ref"] = entrypoint_ref(row)
        picked[row["id"]] = row
        return True

    # ---- 1. canaries: every known-real canary present in the corpus -----------
    canary_def = json.loads(CANARIES.read_text(encoding="utf-8"))["canaries"]
    canary_report = []
    cur = con.cursor()
    for c in canary_def:
        repo, path = c["parent_repo"].casefold(), c["path"].casefold()
        cur.execute(base_select() + " where source='github_skill_file'"
                    " and lower(raw) like ? and lower(raw) like ?",
                    (f'%"parent_repo": "{repo}"%'.replace('"', '%'), f"%{path}%"))
        found = rows_to_dicts(cur)
        # tighten: exact match on parsed raw
        exact = []
        for r in found:
            d = parse_raw(r["raw"])
            if (d.get("parent_repo", "").casefold() == repo
                    and d.get("path", "").casefold() == path):
                exact.append(r)
        rec = {"canary_id": c["id"], "parent_repo": c["parent_repo"], "path": c["path"],
               "present": bool(exact)}
        if exact:
            r = exact[0]
            add(r, "canary")
            rec.update({"skill_id": r["id"], "name": r["name"],
                        "quality_status": r["quality_status"],
                        "quality_score": r["quality_score"]})
        canary_report.append(rec)

    # ---- 2. lowest-quality rows (fetchable, so a judge can actually see them) --
    cur.execute(base_select() + " where source='github_skill_file'"
                " order by quality_score asc, id asc limit ?", (N_LOW_QUALITY * 4,))
    added = 0
    for r in rows_to_dicts(cur):
        if added >= N_LOW_QUALITY:
            break
        if add(r, "low_quality"):
            added += 1

    # ---- 3. known-generic skills ---------------------------------------------
    placeholders = ",".join("?" * len(GENERIC_NAMES))
    cur.execute(base_select() + f" where lower(name) in ({placeholders})"
                " and quality_status='active' order by quality_score desc, id asc limit ?",
                [n.casefold() for n in GENERIC_NAMES] + [N_GENERIC * 6])
    generic_pool = rows_to_dicts(cur)
    seen_names, added = set(), 0
    for r in generic_pool:
        if added >= N_GENERIC:
            break
        key = (r["name"] or "").casefold()
        if key in seen_names:
            continue
        if add(r, "generic"):
            seen_names.add(key)
            added += 1

    # ---- 4. stratified random over `source` -----------------------------------
    cur.execute("select source, count(*) from skills group by 1")
    src_counts = dict(cur.fetchall())
    remaining = max(0, TARGET_TOTAL - len(picked))
    total_rows = sum(src_counts.values())
    # proportional, but give every source a floor of 3 so small strata are represented
    alloc: dict[str, int] = {}
    for s, n in src_counts.items():
        alloc[s] = max(3, round(remaining * n / total_rows))
    # trim proportionally back down to `remaining`
    while sum(alloc.values()) > remaining:
        s = max(alloc, key=lambda k: (alloc[k], src_counts[k]))
        if alloc[s] <= 3:
            break
        alloc[s] -= 1

    strata_report = {}
    for s, want in sorted(alloc.items()):
        cur.execute(base_select() + " where source=? order by id", (s,))
        pool = rows_to_dicts(cur)
        rnd.shuffle(pool)
        got = 0
        for r in pool:
            if got >= want:
                break
            if add(r, f"stratified:{s}"):
                got += 1
        strata_report[s] = {"population": src_counts[s], "allocated": want, "taken": got}

    rows = list(picked.values())
    rows.sort(key=lambda r: r["id"])

    modes: dict[str, int] = {}
    for r in rows:
        modes[r["entrypoint_ref"]["mode"]] = modes.get(r["entrypoint_ref"]["mode"], 0) + 1

    out = {
        "schema_version": 1,
        "run_id": "run1-20260802",
        "seed": SEED,
        "frozen_corpus_sha256": "c8485180753ed9aa5d36eabbe751127d2a4b3403ae3917245b12b340689a56f9",
        "target_total": TARGET_TOTAL,
        "actual_total": len(rows),
        "bucket_counts": {
            b: sum(1 for r in rows if b in [x.split(":")[0] for x in r["buckets"]])
            for b in ("canary", "low_quality", "generic", "stratified")
        },
        "strata": strata_report,
        "entrypoint_modes": modes,
        "canary_presence": canary_report,
        "canaries_present": sum(1 for c in canary_report if c["present"]),
        "canaries_total": len(canary_report),
        "skills": [
            {k: r[k] for k in ("id", "name", "description", "source", "url",
                               "quality_status", "quality_score", "risk_score",
                               "content_hash", "category")}
            | {"buckets": r["buckets"], "entrypoint_ref": r["entrypoint_ref"],
               "raw": r["raw"]}
            for r in rows
        ],
    }
    OUT.write_text(json.dumps(out, indent=2) + "\n", encoding="utf-8")
    con.close()

    print(f"sample: {out['actual_total']} skills -> {OUT}")
    print("buckets:", json.dumps(out["bucket_counts"]))
    print("entrypoint modes:", json.dumps(modes))
    print(f"canaries present: {out['canaries_present']}/{out['canaries_total']}")
    for c in canary_report:
        if not c["present"]:
            print(f"  ABSENT canary: {c['canary_id']} ({c['parent_repo']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
