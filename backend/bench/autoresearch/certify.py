#!/usr/bin/env python3
"""Paired comparison of two arms: uplift + exact McNemar p-value.

No scipy dependency: the McNemar exact test is a two-sided binomial tail on
the discordant pairs, computable with math.comb. Prints per-split numbers;
certification requires the HOLDOUT line to pass (p<0.05, uplift>=+5pts).
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def load(path: Path) -> dict[str, dict]:
    out = {}
    for line in path.open():
        d = json.loads(line)
        out[d["task_id"]] = d
    return out


def mcnemar_exact(b: int, c: int) -> float:
    """Two-sided exact p for discordant counts b (A wins) and c (B wins)."""
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    tail = sum(math.comb(n, i) for i in range(0, k + 1)) / (2 ** n)
    return min(1.0, 2 * tail)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("arm_a", help="results_<hash>.jsonl (e.g. baseline)")
    ap.add_argument("arm_b", help="results_<hash>.jsonl (candidate)")
    a = ap.parse_args()
    ra, rb = load(Path(a.arm_a)), load(Path(a.arm_b))
    common = sorted(set(ra) & set(rb))
    if not common:
        print("no paired tasks")
        return 1

    for split in ("dev", "holdout"):
        ids = [t for t in common if ra[t]["split"] == split]
        if not ids:
            continue
        both = a_only = b_only = neither = 0
        for t in ids:
            pa, pb = ra[t]["pass"], rb[t]["pass"]
            both += pa and pb
            a_only += pa and not pb
            b_only += pb and not pa
            neither += not pa and not pb
        n = len(ids)
        sa, sb = (both + a_only) / n, (both + b_only) / n
        uplift = (sb - sa) * 100
        p = mcnemar_exact(a_only, b_only)
        verdict = ""
        if split == "holdout":
            ok = p < 0.05 and uplift >= 5.0
            verdict = "  => CERTIFIED" if ok else "  => not certified"
        print(f"{split:8} n={n:3d}  A {sa*100:5.1f}%  B {sb*100:5.1f}%  "
              f"uplift {uplift:+5.1f}pts  discordant {a_only}/{b_only}  "
              f"p={p:.4f}{verdict}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
