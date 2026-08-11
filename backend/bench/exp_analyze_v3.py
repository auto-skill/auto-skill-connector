#!/usr/bin/env python3
"""Post-hoc analysis of the round-3 arms: where, if anywhere, is there headroom?

Round 3 is expected to be null, and a null result is only useful if it says WHY.
This answers three questions the raw means cannot:

  1. Ceiling.   On how many tasks was baseline already perfect? A task where the
                model scores 1.00 unaided cannot show a skill effect in either
                direction, so it contributes nothing but noise to the mean. The
                honest denominator is the non-ceiling subset.
  2. Headroom.  Restricted to tasks with actual headroom, do the arms separate?
                This is the only comparison with any power, and it is stated as
                a subset analysis -- decided after seeing the data, so it is
                exploratory and flagged as such rather than reported as a test.
  3. Harm.      Does injecting a skill ever make the answer WORSE? A retrieval
                system that degrades good answers is worse than no system, and
                that risk is invisible in a mean that is pinned at the ceiling.

Also dumps the specific required elements that models omitted most often. Those
omissions are the only place a skill could add value on this task family, so
they are the input to deciding whether another round is worth running.
"""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent
ARMS = ("baseline", "placebo", "prod", "new")


def main() -> int:
    res = json.loads((BENCH / "exp_results_v3.json").read_text())
    rows = res["rows"]
    tasks = {t["id"]: t for t in
             json.loads((BENCH / "exp_tasks_v3.json").read_text())["tasks"]}
    n = len(rows)

    print("=== 1. CEILING ===")
    ceil = [r for r in rows if r["baseline"] >= 0.999]
    head = [r for r in rows if r["baseline"] < 0.999]
    print(f"  tasks where baseline was already PERFECT: {len(ceil)}/{n} "
          f"({100*len(ceil)/n:.0f}%)")
    print(f"  tasks with any headroom at all:           {len(head)}/{n}")
    print("  A task at the ceiling cannot show a skill effect. Any mean computed")
    print("  over all 30 is diluted by those tasks toward 'no difference'.")

    if head:
        print(f"\n=== 2. HEADROOM SUBSET (n={len(head)}, exploratory) ===")
        for arm in ARMS:
            m = sum(r[arm] for r in head) / len(head)
            print(f"  {arm:9} {100*m:5.1f}%   {'#' * int(m*40)}")
        print("  Chosen after seeing the data, so this is a hypothesis to test in a")
        print("  future pre-registered round -- not a result.")
        print("\n  per-task detail:")
        for r in sorted(head, key=lambda x: x["baseline"]):
            print(f"    {r['id']:24} base={r['baseline']:.2f} plac={r['placebo']:.2f} "
                  f"prod={r['prod']:.2f} new={r['new']:.2f}   <- new_hit: "
                  f"{str(r.get('new_hit'))[:28]}")

    print("\n=== 3. HARM: did injecting a skill make answers worse? ===")
    for arm in ("placebo", "prod", "new"):
        worse = [r for r in rows if r[arm] < r["baseline"] - 1e-9]
        better = [r for r in rows if r[arm] > r["baseline"] + 1e-9]
        dmg = sum(r["baseline"] - r[arm] for r in worse)
        print(f"  {arm:8} worse on {len(worse):>2}/{n} tasks, better on {len(better):>2}"
              f"  (total damage {dmg:.2f} completeness pts)")
        for r in sorted(worse, key=lambda x: x[arm] - x["baseline"])[:4]:
            print(f"      {r['id']:24} {r['baseline']:.2f} -> {r[arm]:.2f}"
                  f"   len {r['baseline_len']} -> {r[arm+'_len']}")

    print("\n=== 4. ELEMENTS MODELS OMIT (the only place a skill could help) ===")
    miss = Counter()
    for r in rows:
        t = tasks.get(r["id"])
        if not t or "baseline_hits" not in r:
            continue
        for g, hit in zip(t["groups"], r["baseline_hits"]):
            if not hit:
                miss[f"{r['id']}: {g[0]}"] += 1
    if miss:
        print(f"  {len(miss)} distinct required elements were missed at baseline:")
        for k, _ in miss.most_common(25):
            print(f"    {k}")
    else:
        print("  none -- baseline produced every required element on every task.")
        print("  There is literally nothing for a skill to add on this task family.")

    print("\n=== 5. RETRIEVAL SANITY ===")
    zero_new = [r for r in rows if not r.get("new_hit")]
    print(f"  tasks with no retrieval hit in our corpus:  {len(zero_new)}")
    print("  what our corpus retrieved (top-1 BM25):")
    for r in rows[:12]:
        print(f"    {r['id']:24} -> {str(r.get('new_hit'))[:44]:44} "
              f"(bm25 {r.get('new_score')})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
