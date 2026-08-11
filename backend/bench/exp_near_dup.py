#!/usr/bin/env python3
"""How much of the corpus is Luna judging more than once, in effect?

Inheritance currently fires only on an EXACT normalized-hash match (CRLF -> LF,
per-line rstrip, strip). Anything less identical than that is a fresh Luna call.
But skills are forked, vendored, version-bumped and re-frontmattered constantly,
so a large share of "new" content may be a trivial variant of something Luna has
already judged. Every such variant is quota spent to re-derive a known answer.

This measures that, with NO API cost of any kind -- pure local hashing.

Normalisation ladder, each level strictly weaker (admits more) than the last:

  L0  production norm_hash: CRLF->LF, per-line rstrip, strip
  L1  + lowercase, collapse all whitespace runs to one space
  L2  + strip YAML frontmatter, URLs, and long digit runs (version bumps,
        dates, commit shas -- the usual trivial diffs)
  L3  + keep only the multiset of alphanumeric word stems, order-insensitive

Reported per level: how many distinct skills remain, i.e. how many Luna calls a
perfect deduper at that level would have needed. The gap between L0 and L1/L2 is
recoverable quota.

L3 is deliberately included as an OVER-collapse control: if L3 collapses a lot
more than L2, that is a warning that word-bag matching merges genuinely different
skills, not evidence of more savings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import sys
from collections import Counter
from pathlib import Path

BENCH = Path(__file__).resolve().parent
LIB = BENCH.parent / "skills_library_v1"
OBJ = LIB / "objects"

FRONTMATTER = re.compile(r"\A---\r?\n.*?\r?\n---\r?\n", re.S)
URL = re.compile(r"https?://\S+")
DIGITS = re.compile(r"\d{2,}")
WORD = re.compile(r"[a-z0-9]+")


def h(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8", "replace")).hexdigest()


def l0(t: str) -> str:
    t = t.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(x.rstrip() for x in t.split("\n")).strip()


def l1(t: str) -> str:
    return " ".join(l0(t).lower().split())


def l2(t: str) -> str:
    s = l0(t)
    s = FRONTMATTER.sub("", s)
    s = URL.sub(" ", s)
    s = DIGITS.sub(" ", s)
    return " ".join(s.lower().split())


def l3(t: str) -> str:
    ws = WORD.findall(l2(t))
    # multiset, order-insensitive
    return " ".join(f"{w}:{c}" for w, c in sorted(Counter(ws).items()))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=0, help="0 = all objects")
    ap.add_argument("--seed", type=int, default=5)
    a = ap.parse_args()

    cache = json.loads((LIB / "norm_hash_index.json").read_text())
    cshas = sorted(cache)
    if a.limit and a.limit < len(cshas):
        cshas = random.Random(a.seed).sample(cshas, a.limit)
    print(f"  scanning {len(cshas):,} stored skill objects", flush=True)

    sets = {k: set() for k in ("L0", "L1", "L2", "L3")}
    read = 0
    for i, c in enumerate(cshas):
        p = OBJ / c[:2] / c[2:4] / c
        try:
            t = p.read_bytes().decode("utf-8", "replace")
        except Exception:
            continue
        read += 1
        sets["L0"].add(h(l0(t)))
        sets["L1"].add(h(l1(t)))
        sets["L2"].add(h(l2(t)))
        sets["L3"].add(h(l3(t)))
        if read % 20000 == 0:
            print(f"    {read:,} ...", flush=True)

    print(f"\n  objects read: {read:,}\n")
    base = len(sets["L0"])
    print("  level                                    distinct   luna calls needed   saving vs L0")
    labels = {
        "L0": "L0 production norm_hash (today)",
        "L1": "L1 + lowercase, collapse whitespace",
        "L2": "L2 + strip frontmatter/URLs/digits",
        "L3": "L3 + word-multiset (OVER-collapse control)",
    }
    for k in ("L0", "L1", "L2", "L3"):
        n = len(sets[k])
        save = (base - n) / base * 100 if base else 0
        print(f"  {labels[k]:<42} {n:>8,}   {n:>15,}   {save:>10.1f}%")
    print(f"\n  L2 is the safe recommendation; L3 only bounds how much of the L2"
          f"\n  collapse could be spurious ({(len(sets['L2'])-len(sets['L3']))/base*100:.1f}pp beyond L2).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
