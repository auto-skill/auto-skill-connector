#!/usr/bin/env python3
"""Find PRINCIPLE/APPROACH skills (how to work) vs FACT skills (what is true).

Fact skills lift a knowledge quiz (+21.7pts measured) but did nothing on
agentic tasks, where the model's gap is method, not recall. If a distinct
population of methodology skills exists ("prefer the laziest thing that
works", "read the error before changing code", "reproduce before fixing"),
those are the ones that could move competence-bound benchmarks.

Two views, because either alone misleads:
  probe  -- seed queries phrased as methodology, top hits by cosine
  sample -- uniform random skills, to measure the BASE RATE of principle
            skills (a probe always returns something; that says nothing
            about how common the class is)
"""
from __future__ import annotations

import json
import random
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from retrieve import Retriever  # noqa: E402

PROBES = [
    "principles and philosophy for approaching a problem the simplest way",
    "methodology: how to decide what to do before writing any code",
    "debugging approach: reproduce the failure before attempting a fix",
    "checklist to follow before declaring work complete",
    "how to think about tradeoffs when choosing an implementation",
    "systematic workflow for investigating an unfamiliar codebase",
    "guidelines for writing minimal code and avoiding over-engineering",
    "verify assumptions with evidence instead of guessing",
    "process for breaking a large task into verifiable steps",
    "review discipline: what to check and in what order",
]

PRINCIPLE_HINTS = (
    "principle", "philosophy", "approach", "methodology", "mindset",
    "guideline", "best practice", "workflow", "process", "discipline",
    "checklist", "strategy", "heuristic", "rule of thumb", "how to think",
    "avoid over", "minimal", "systematic", "step by step", "before you",
)


def looks_principled(text: str) -> int:
    t = (text or "").lower()
    return sum(1 for h in PRINCIPLE_HINTS if h in t)


def main() -> int:
    r = Retriever(k=12)
    print(f"corpus: {len(r.meta):,} skills\n", flush=True)

    print("=== PROBE: methodology-phrased queries ===")
    seen = {}
    for q in PROBES:
        hits = r.search(q)
        print(f"\n-- {q[:62]}")
        for h in hits[:6]:
            score = looks_principled(h["name"] + " " + h["summary"])
            mark = "***" if score >= 2 else ("*" if score == 1 else "   ")
            print(f" {mark} {h['cosine']:.3f} {h['name'][:38]:<38} {h['summary'][:70]}")
            if score >= 1:
                seen[h["canonical_id"]] = h

    print(f"\n=== RANDOM SAMPLE: base rate of principle skills ===")
    random.seed(20260824)
    idx = random.sample(range(len(r.meta)), 40)
    n_princ = 0
    for i in idx[:18]:
        m = r.meta[i]
        score = looks_principled(m["name"] + " " + m["summary"])
        n_princ += score >= 2
        mark = "***" if score >= 2 else ("*" if score == 1 else "   ")
        print(f" {mark} {m['name'][:36]:<36} {m['summary'][:74]}")
    full = sum(1 for i in idx
               if looks_principled(r.meta[i]["name"] + " " + r.meta[i]["summary"]) >= 2)
    print(f"\nbase rate (lexical proxy, n=40): {full}/40 = {full/40:.0%} principle-ish")
    print(f"probe surfaced {len(seen)} distinct principle-ish skills")

    out = HERE / "principle_seeds.json"
    out.write_text(json.dumps(
        [{"canonical_id": v["canonical_id"], "name": v["name"],
          "summary": v["summary"], "url": v["url"]} for v in seen.values()], indent=1))
    print(f"seeds -> {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
