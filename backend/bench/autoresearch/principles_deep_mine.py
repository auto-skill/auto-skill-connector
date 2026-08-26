#!/usr/bin/env python3
"""Exhaustive principle-skill mining: every signal we have, not one probe.

The first pass used 10 coding-flavoured probes + one centroid. This sweep:
  1. 40 probes across ALL work domains (coding, debugging, research, writing,
     planning, ops, communication, decision-making, learning, review).
  2. Graph signal: capabilities provided by MANY distinct skills across MANY
     repos with LOW tool-attachment = generic method, not tool knowledge.
  3. Name-pattern mining over all 478k names (methodology morphology).
  4. Judgment metadata: high specificity + zero risk flags + generic category.
Union -> name-dedup (best instance by judged quality) -> ranked candidates
for a human read-through, which is the final filter.
"""
from __future__ import annotations

import json
import re
import sqlite3
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from retrieve import Retriever  # noqa: E402
from principles_discover import looks_principled  # noqa: E402

GRAPH = Path("/srv/mobile-codex/sessions/autoskill_7e0dd3fa/graph_work.sqlite")
OUT = HERE / "principle_candidates_deep.json"

PROBES = [
    # problem-solving / thinking
    "first principles reasoning break the problem down to fundamentals",
    "how to think through a hard problem before acting",
    "decision making framework weigh options and tradeoffs",
    "when to stop and ask for clarification instead of guessing",
    "estimate before you build to catch bad ideas early",
    # coding craft
    "write the simplest thing that works avoid over-engineering",
    "refactor safely in small verifiable steps",
    "code review discipline what to check in what order",
    "test driven development write the failing test first",
    "handle errors explicitly never swallow failures",
    # debugging
    "systematic debugging reproduce isolate fix verify",
    "read the error message carefully before changing anything",
    "binary search the failure space to localize a bug",
    "form a hypothesis and design an experiment to test it",
    # workflow / execution
    "break large work into small verifiable increments",
    "definition of done checklist before declaring complete",
    "plan before executing and update the plan as you learn",
    "timebox investigation and escalate when stuck",
    "keep a working state commit early and often",
    # investigation / research
    "explore an unfamiliar codebase systematically",
    "verify claims against primary sources before trusting them",
    "research methodology gather evidence before concluding",
    "take notes and build a map while investigating",
    # communication / collaboration
    "write clear commit messages and document decisions",
    "give actionable feedback focused on the work not the person",
    "ask good questions that unblock work quickly",
    "escalate risks early instead of hiding problems",
    # ops / safety
    "make changes reversible and roll back fast when wrong",
    "check assumptions about the environment before running commands",
    "backup before destructive operations",
    "principle of least privilege when configuring access",
    # quality / rigor
    "measure before optimizing avoid premature optimization",
    "prefer boring proven technology over novelty",
    "delete code rather than adding when possible",
    "make the invisible state visible before changing it",
    "reproduce results independently before believing them",
    # learning / meta
    "learn from failure with blameless retrospectives",
    "spaced repetition and deliberate practice for retention",
    "teach it back to test your own understanding",
    "know when good enough beats perfect",
]

NAME_RE = re.compile(
    r"(principle|method|methodolog|philosoph|discipline|mindset|workflow|"
    r"process|checklist|heuristic|approach|thinking|first|driven|practice|"
    r"guideline|rigor|systematic|craft|debug|review|plan|decompos)", re.I)


def main() -> int:
    r = Retriever(k=1)
    n = len(r.meta)
    print(f"corpus: {n:,}", flush=True)
    cand: dict[int, dict] = {}

    def add(idx, source, score):
        c = cand.setdefault(idx, {"sources": set(), "score": 0.0})
        c["sources"].add(source)
        c["score"] = max(c["score"], score)

    # 1. probe sweep
    qvecs = np.asarray(
        __import__("embeddings").embed_texts(PROBES, batch_size=16), dtype=np.float32)
    sims = r.mat @ qvecs.T          # (n, probes)
    for pi in range(len(PROBES)):
        col = sims[:, pi]
        for idx in np.argsort(-col)[:40]:
            if col[idx] >= 0.87:
                add(int(idx), f"probe", float(col[idx]))
    print(f"after probes: {len(cand):,}", flush=True)

    # 2. graph: high-spread capabilities with low tool attachment
    if GRAPH.exists():
        g = sqlite3.connect(f"file:{GRAPH}?mode=ro", uri=True)
        # snapshot dir is deliberately 555/444; keep temp b-trees off disk
        g.execute("pragma temp_store=memory")
        pos = {m["canonical_id"]: i for i, m in enumerate(r.meta)}
        rows = g.execute("""
          select e.src, count(distinct e.dst) tools from edges e
          where e.edge_type='uses_tool' group by e.src""").fetchall()
        toolcount = dict(rows)
        cap_rows = g.execute("""
          select n.node_id, n.label, json_extract(n.meta_json,'$.df') df
          from nodes n where n.node_type='capability'
          and json_extract(n.meta_json,'$.df') >= 30""").fetchall()
        generic_caps = {nid for nid, label, df in cap_rows
                        if looks_principled(label or "") >= 1}
        if generic_caps:
            q = ("select src, dst from edges where edge_type='provides' and dst in (%s)"
                 % ",".join("?" * len(generic_caps)))
            for src, dst in g.execute(q, tuple(generic_caps)):
                cid = src[2:]
                if cid in pos and toolcount.get(src, 0) <= 2:
                    add(pos[cid], "graph", 0.9)
        g.close()
        print(f"after graph: {len(cand):,}", flush=True)

    # 3. name morphology + 4. metadata gate, single pass over corpus
    for i, m in enumerate(r.meta):
        nm = m["name"] or ""
        if NAME_RE.search(nm) and looks_principled(nm + " " + m["summary"]) >= 2:
            add(i, "name", 0.85)
    print(f"after name-mine: {len(cand):,}", flush=True)

    # dedup by name, keep best judged quality; require summary substance
    best: dict[str, dict] = {}
    for idx, c in cand.items():
        m = r.meta[idx]
        if len(m["summary"]) < 40:
            continue
        key = re.sub(r"[^a-z0-9]+", "-", (m["name"] or "").lower()).strip("-")
        rec = {"canonical_id": m["canonical_id"], "name": m["name"],
               "summary": m["summary"], "url": m["url"],
               "quality": m["quality"], "sources": sorted(c["sources"]),
               "score": round(c["score"], 3),
               "nsources": len(c["sources"])}
        cur = best.get(key)
        if cur is None or (rec["nsources"], rec["quality"]) > (cur["nsources"], cur["quality"]):
            best[key] = rec

    ranked = sorted(best.values(),
                    key=lambda d: (-d["nsources"], -d["quality"], -d["score"]))
    OUT.write_text(json.dumps(ranked, indent=1))
    multi = sum(1 for d in ranked if d["nsources"] >= 2)
    print(f"\ndistinct principle candidates: {len(ranked):,} "
          f"({multi} multi-source) -> {OUT}")
    for d in ranked[:25]:
        print(f" [{'+'.join(d['sources'])}] q{d['quality']:.2f} {d['name'][:32]:<32} {d['summary'][:60]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
