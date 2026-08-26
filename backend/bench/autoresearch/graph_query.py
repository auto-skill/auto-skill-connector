#!/usr/bin/env python3
"""Retrieval over Pranay's knowledge-graph snapshot (coliseum arm).

Pure graph, no embeddings: query tokens -> capability nodes (token overlap)
-> `provides` edges weighted by judgment evidence -> skills, suppressed by
risk-flag boundaries. Measures what the graph itself buys, not graph+vector.
"""
from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
GRAPH = Path("/srv/mobile-codex/sessions/autoskill_7e0dd3fa/graph_work.sqlite")
STOP = set("a an the to for of in on with and or how what which using use "
           "exact provide give write answer only command option flag".split())


class GraphRetriever:
    def __init__(self, k: int = 3):
        self.k = k
        g = sqlite3.connect(f"file:{GRAPH}?mode=ro", uri=True)
        g.execute("pragma temp_store=memory")
        # capability label -> token set (only reasonably selective caps)
        self.cap_tokens: dict[str, set] = {}
        for nid, label in g.execute(
                "select node_id, label from nodes where node_type='capability'"):
            toks = {t for t in re.findall(r"[a-z0-9]+", (label or "").lower())
                    if t not in STOP and len(t) > 2}
            if 1 <= len(toks) <= 8:
                self.cap_tokens[nid] = toks
        # provides edges with weights
        self.provides = defaultdict(list)   # cap -> [(skill_cid, weight)]
        for src, dst, w in g.execute(
                "select src, dst, weight from edges where edge_type='provides'"):
            self.provides[dst].append((src[2:], w))
        # boundary suppression
        self.suppressed = {r[0] for r in g.execute(
            "select distinct canonical_id from boundaries where boundary_type='risk_flag'")}
        # skill metadata
        self.meta = {}
        for nid, label, mj in g.execute(
                "select node_id, label, meta_json from nodes where node_type='skill_revision'"):
            m = json.loads(mj)
            self.meta[nid[2:]] = {"name": label, "url": m.get("url"),
                                  "quality": m.get("quality") or 0}
        g.close()

    def search(self, query: str) -> list[dict]:
        qtoks = {t for t in re.findall(r"[a-z0-9]+", query.lower())
                 if t not in STOP and len(t) > 2}
        if not qtoks:
            return []
        scores: dict[str, float] = defaultdict(float)
        for cap, ctoks in self.cap_tokens.items():
            ov = len(ctoks & qtoks)
            if ov >= max(1, len(ctoks) - 1):        # near-full capability match
                capscore = ov / len(ctoks)
                for cid, w in self.provides.get(cap, []):
                    if cid in self.suppressed:
                        continue
                    scores[cid] += capscore * (w or 0.1)
        ranked = sorted(scores.items(), key=lambda kv: -kv[1])[: self.k]
        out = []
        for cid, s in ranked:
            m = self.meta.get(cid, {})
            out.append({"canonical_id": cid, "name": m.get("name") or cid[:12],
                        "url": m.get("url"), "summary": "", "triggers": [],
                        "cosine": round(min(s, 9.99), 3)})
        return out

    def content_of(self, canonical_id: str) -> str:
        p = (HERE.parent.parent / "judged_library_v2" / "files"
             / f"{canonical_id}.md")
        try:
            return p.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return ""
