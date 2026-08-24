#!/usr/bin/env python3
"""Grow the principle-skill seeds into a shortlist via vector-space neighbours.

The seeds came from methodology-phrased probes; their neighbourhood is where
the rest of the class lives. Two guards against drift, because raw cosine
neighbours of "debug workflow" include plenty of tool-specific debuggers:
  * lexical principle score must stay >= 1 (methodology vocabulary present)
  * one entry per skill NAME (the corpus mirrors popular skills hundreds of
    times; without this the shortlist is 40 copies of `debug`)
Ranked by judged quality so the shortlist is the best instance of each idea.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from retrieve import Retriever  # noqa: E402
from principles_discover import looks_principled  # noqa: E402

TOP_NEIGHBOURS = 4000
OUT = HERE / "principle_shortlist.json"


def main() -> int:
    seeds = json.loads((HERE / "principle_seeds.json").read_text())
    r = Retriever(k=1)
    pos = {m["canonical_id"]: i for i, m in enumerate(r.meta)}
    rows = [pos[s["canonical_id"]] for s in seeds if s["canonical_id"] in pos]
    print(f"seeds located: {len(rows)}/{len(seeds)}", flush=True)

    centroid = r.mat[rows].mean(axis=0)
    centroid /= np.linalg.norm(centroid)
    scores = r.mat @ centroid
    order = np.argsort(-scores)[:TOP_NEIGHBOURS]

    best: dict[str, dict] = {}
    for idx in order:
        m = r.meta[idx]
        lex = looks_principled(m["name"] + " " + m["summary"])
        if lex < 1:
            continue
        key = m["name"].strip().lower()
        cand = {"canonical_id": m["canonical_id"], "name": m["name"],
                "summary": m["summary"], "url": m["url"],
                "cos_to_centroid": round(float(scores[idx]), 4),
                "lex": lex, "quality": m["quality"]}
        cur = best.get(key)
        if cur is None or (cand["quality"], cand["lex"]) > (cur["quality"], cur["lex"]):
            best[key] = cand

    short = sorted(best.values(), key=lambda d: -d["cos_to_centroid"])
    OUT.write_text(json.dumps(short, indent=1))
    print(f"shortlist: {len(short)} distinct principle skills -> {OUT}\n")
    for d in short[:30]:
        print(f" {d['cos_to_centroid']:.3f} q{d['quality']:.2f} {d['name'][:34]:<34} {d['summary'][:66]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
