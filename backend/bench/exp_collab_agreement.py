#!/usr/bin/env python3
"""Do the collaborator's verdicts agree with ours on skills BOTH judged?

Before merging ~410k outside verdicts into the corpus we should know whether
they are equivalent to ours. The partition bug that let us judge into their half
turned out to be a gift: ~87k skills carry a verdict from BOTH sides, on the
same content, from the same model family and the same prompt file. That is a
natural paired experiment far stronger than a sampled preference test.

Deliberately spends ZERO model quota. An LLM-graded blind A/B would cost Luna or
Claude budget and only sample a few hundred pairs; agreement over tens of
thousands of real pairs is both cheaper and more decisive. If agreement is high
the corpus is homogeneous and we stop. Escalate to LLM grading only if it isn't.

Read-only. Compares on the SAME norm_hash, so both rows describe identical bytes
-- any disagreement is judge behaviour, not different input.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
DB = BACKEND / "enrichment_v1.db"
OURS = "gpt-5.6-luna@medium/codex-cli-0.144.6"
THEIRS = "gpt-5.6-luna@medium"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=40000)
    a = ap.parse_args()

    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.execute("pragma busy_timeout=180000")
    rows = con.execute("""
        select o.norm_hash, o.output_json, t.output_json
        from enrichments o
        join enrichments t
          on t.norm_hash = o.norm_hash
         and t.judge_role = 'primary'
         and t.model_snapshot = ?
        where o.judge_role = 'primary'
          and o.model_snapshot = ?
          and o.status = 'ok' and t.status = 'ok'
        limit ?""", (THEIRS, OURS, a.limit)).fetchall()
    con.close()

    if not rows:
        print("  no overlapping pairs found")
        return 1

    both_real = ours_only = theirs_only = neither = 0
    disagree_examples = []
    len_ours, len_theirs = [], []
    spec_ours, spec_theirs = [], []
    trig_ours, trig_theirs = [], []

    for nh, oj_ours, oj_theirs in rows:
        try:
            a_ = json.loads(oj_ours)
            b_ = json.loads(oj_theirs)
        except Exception:
            continue
        ra, rb = a_.get("is_real_skill") is True, b_.get("is_real_skill") is True
        if ra and rb:
            both_real += 1
        elif ra:
            ours_only += 1
            if len(disagree_examples) < 5:
                disagree_examples.append((nh, "ours=real theirs=reject",
                                          str(b_.get("reject_reason"))[:70]))
        elif rb:
            theirs_only += 1
            if len(disagree_examples) < 5:
                disagree_examples.append((nh, "theirs=real ours=reject",
                                          str(a_.get("reject_reason"))[:70]))
        else:
            neither += 1
        if ra and rb:
            sa, sb = (a_.get("summary") or ""), (b_.get("summary") or "")
            if sa and sb:
                len_ours.append(len(sa)); len_theirs.append(len(sb))
            for src, dst in ((a_, spec_ours), (b_, spec_theirs)):
                v = src.get("specificity")
                if isinstance(v, (int, float)):
                    dst.append(float(v))
            trig_ours.append(len(a_.get("triggers") or []))
            trig_theirs.append(len(b_.get("triggers") or []))

    n = both_real + ours_only + theirs_only + neither
    agree = both_real + neither
    print(f"\n  === COLLABORATOR AGREEMENT (n={n:,} paired, same norm_hash) ===")
    print(f"    agree                {agree:,}  ({agree*100/max(n,1):.1f}%)")
    print(f"      both real          {both_real:,}")
    print(f"      both reject        {neither:,}")
    print(f"    DISAGREE             {ours_only+theirs_only:,}  "
          f"({(ours_only+theirs_only)*100/max(n,1):.1f}%)")
    print(f"      ours real only     {ours_only:,}")
    print(f"      theirs real only   {theirs_only:,}")

    def stat(label, xs, ys):
        if xs and ys:
            print(f"    {label:<18} ours {statistics.median(xs):>7.2f}   "
                  f"theirs {statistics.median(ys):>7.2f}")

    print("\n    -- metadata shape (median, on skills both kept) --")
    stat("summary chars", len_ours, len_theirs)
    stat("specificity", spec_ours, spec_theirs)
    stat("trigger count", trig_ours, trig_theirs)

    if disagree_examples:
        print("\n    -- sample disagreements --")
        for nh, kind, why in disagree_examples:
            print(f"      {nh[:12]}  {kind}: {why}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
