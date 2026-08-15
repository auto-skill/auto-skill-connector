#!/usr/bin/env python3
"""Import a collaborator's verdict batches into enrichment_v1.db.

Verified before this was written (2026-08-14, 445,234 rows over 9 batches):
checksums and gzip streams clean, 0 unparseable rows, 100% inside the
collaborator's 0-7 sha partition, and their norm_hash agreed with ours on
192/202 comparable skills (the 10 differ because they fetched at HEAD and the
upstream file has since changed -- genuine drift, not a hashing bug).

Three reconciliations are needed, each found by that verification:

1. prompt_version. They emitted "v2" (the code default, which is what our
   handoff document wrongly quoted); production passes "v2.1". There is no
   distinct v2.1 prompt file -- BATCHED_PROMPT is enrichment_prompt_v2_batched.md
   for both -- so the label differs while the prompt content is identical.
   Left as "v2", judged_ids() (which filters on prompt_version) would not see
   these rows and the pipeline would re-judge all 445k. Relabelled.

2. model_snapshot. Theirs is "gpt-5.6-luna@medium", ours
   "gpt-5.6-luna@medium/codex-cli-0.144.6". Kept DISTINCT on purpose: it is the
   honest provenance of who produced the verdict, and the PK includes it, so
   their rows sit alongside ours rather than silently overwriting them.
   Attribution is preserved without losing dedup, because dedup for batch
   selection keys on norm_hash + prompt_version.

3. status. Theirs uses "deterministic" for prefilter rejects and
   "failed:<msg>" for dead calls; ours uses "ok" / "failed(...)". Normalised so
   already_judged() and judged_ids() classify them the same way. A failed row
   MUST remain non-ok so the skill stays retryable rather than looking judged.

Idempotent: INSERT OR REPLACE on the content-addressed PK, so re-running the
same batches converges rather than duplicating.
"""
from __future__ import annotations

import argparse
import gzip
import json
import sqlite3
import sys
from pathlib import Path

BENCH = Path(__file__).resolve().parent
DB = BENCH.parent / "enrichment_v1.db"
TARGET_PROMPT_VERSION = "v2.1"


def norm_status(raw: str) -> str:
    s = (raw or "").strip()
    if s == "ok":
        return "ok"
    if s == "deterministic":
        # A prefilter reject IS a real, final verdict about the content -- it
        # carries is_real_skill=false and a reject_reason. It is not a failure.
        return "ok"
    if s.startswith("failed"):
        # Preserve the reason, normalise the shape to ours: failed(...)
        detail = s.split(":", 1)[1].strip() if ":" in s else ""
        return f"failed({detail[:180]})" if detail else "failed(unknown)"
    return f"failed({s[:180]})" if s else "failed(unknown)"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()

    files = sorted(a.inbox.glob("*.jsonl.gz"))
    if not files:
        print(f"  no batches in {a.inbox}")
        return 1
    print(f"  batches: {len(files)}", flush=True)

    con = sqlite3.connect(DB)
    con.execute("pragma busy_timeout=180000")
    before = con.execute(
        "select count(*) from enrichments where judge_role='primary'").fetchone()[0]

    ins = skipped = bad = 0
    stat_counts: dict[str, int] = {}
    rows: list[tuple] = []

    def flush():
        nonlocal rows
        if rows and not a.dry_run:
            con.executemany(
                "INSERT OR REPLACE INTO enrichments"
                " (norm_hash,skill_id,skill_url,judge_role,prompt_version,"
                "  model_snapshot,output_json,tokens_in,tokens_out,status,created_at)"
                " VALUES (?,?,?,?,?,?,?,?,?,?,?)", rows)
            con.commit()
        rows = []

    for f in files:
        n_file = 0
        with gzip.open(f, "rt", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if a.limit and ins >= a.limit:
                    break
                try:
                    d = json.loads(line)
                except Exception:
                    bad += 1
                    continue
                nh = d.get("norm_hash")
                oj = d.get("output_json")
                if not nh or oj is None:
                    skipped += 1
                    continue
                st = norm_status(d.get("status"))
                stat_counts[st.split("(")[0]] = stat_counts.get(st.split("(")[0], 0) + 1
                rows.append((
                    nh,
                    d.get("skill_id"),
                    d.get("skill_url"),
                    d.get("judge_role") or "primary",
                    TARGET_PROMPT_VERSION,
                    d.get("model_snapshot") or "collab-unknown",
                    json.dumps(oj, ensure_ascii=False) if not isinstance(oj, str) else oj,
                    d.get("tokens_in") or 0,
                    d.get("tokens_out") or 0,
                    st,
                    d.get("created_at") or "2026-08-14T00:00:00+00:00",
                ))
                ins += 1
                n_file += 1
                if len(rows) >= 5000:
                    flush()
        flush()
        print(f"    {f.name}: {n_file:,}", flush=True)

    after = con.execute(
        "select count(*) from enrichments where judge_role='primary'").fetchone()[0]
    con.close()

    print(f"\n  === IMPORT {'(DRY RUN) ' if a.dry_run else ''}DONE ===")
    print(f"    rows read:        {ins:,}")
    print(f"    skipped (no key): {skipped:,}")
    print(f"    unparseable:      {bad:,}")
    print(f"    status mix:       {stat_counts}")
    print(f"    primary rows:     {before:,} -> {after:,}  (+{after-before:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
