#!/usr/bin/env python3
"""Production-faithful retriever over the judged corpus.

Loads the export DB's vectors (the SAME bytes the droplet serves) and embeds
queries through the SAME embeddings.py/ort-1.26 path, so any policy the
autoresearch loop certifies transfers to production unchanged. Policy knobs
are constructor args; one Retriever instance = one policy configuration.
"""
from __future__ import annotations

import array
import json
import sqlite3
import sys
from pathlib import Path

import numpy as np

BACKEND = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(BACKEND))
from embeddings import embed_texts  # noqa: E402

DB = BACKEND / "skills_judged_v2.db"
DIM = 384


class Retriever:
    def __init__(self, k: int = 3, floor: float = 0.0,
                 quality_weight: float = 0.0, prominence_weight: float = 0.0,
                 dedup_by_repo: bool = False):
        self.k, self.floor = k, floor
        self.qw, self.pw = quality_weight, prominence_weight
        self.dedup_by_repo = dedup_by_repo
        npy = BACKEND / "vectors_v2" / "vectors.f32.npy"
        rid = BACKEND / "vectors_v2" / "row_ids.json"
        con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
        con.execute("pragma busy_timeout=120000")
        if npy.exists() and rid.exists():
            # Fast path: the matrix artifact built straight from the verified
            # vector dump; metadata joins by canonical_id, embeddings never
            # touch SQLite. Identical numbers to the DB path.
            mat = np.load(npy, mmap_mode="r")
            ids = json.loads(rid.read_text())
            metadata = {r[0]: r for r in con.execute(
                "select canonical_id, name, url, capability_summary, triggers,"
                "       quality_score, prominence_score from skills")}
            con.close()
            self.meta, keep_rows = [], []
            for i, cid in enumerate(ids):
                r = metadata.get(cid)
                if r is None:
                    continue
                keep_rows.append(i)
                self.meta.append({
                    "canonical_id": cid, "name": r[1], "url": r[2],
                    "summary": r[3] or "", "triggers": json.loads(r[4] or "[]"),
                    "quality": (r[5] or 0) / 100.0, "prominence": r[6] or 0.0,
                })
            self.mat = np.asarray(mat[keep_rows], dtype=np.float32) \
                if len(keep_rows) != len(ids) else np.asarray(mat, dtype=np.float32)
        else:
            rows = con.execute(
                "select canonical_id, name, url, capability_summary, triggers,"
                "       quality_score, prominence_score, embedding"
                "  from skills where embedding is not null").fetchall()
            con.close()
            if not rows:
                raise RuntimeError("no embedded rows in export DB -- load vectors first")
            self.meta = []
            mat = np.empty((len(rows), DIM), dtype=np.float32)
            for i, (cid, name, url, summary, triggers, q, p, blob) in enumerate(rows):
                mat[i] = np.frombuffer(blob, dtype=np.float32, count=DIM)
                self.meta.append({
                    "canonical_id": cid, "name": name, "url": url,
                    "summary": summary or "",
                    "triggers": json.loads(triggers or "[]"),
                    "quality": (q or 0) / 100.0, "prominence": p or 0.0,
                })
            self.mat = mat  # rows are already L2-normalized at embed time

    def content_of(self, canonical_id: str) -> str:
        p = BACKEND / "judged_library_v2" / "files" / f"{canonical_id}.md"
        try:
            return p.read_text(encoding="utf-8")
        except OSError:
            return ""

    def search(self, query: str) -> list[dict]:
        qv = np.asarray(embed_texts([query])[0], dtype=np.float32)
        scores = self.mat @ qv
        if self.qw or self.pw:
            blended = scores.copy()
            for i, m in enumerate(self.meta):
                blended[i] += self.qw * m["quality"] + self.pw * m["prominence"]
            order = np.argsort(-blended)
        else:
            order = np.argsort(-scores)
        out, seen_repo = [], set()
        for idx in order[: max(self.k * 8, 64)]:
            cos = float(scores[idx])
            if cos < self.floor:
                break
            m = self.meta[idx]
            if self.dedup_by_repo:
                repo = (m["url"] or "").split("github.com/")[-1].split("/")[0:2]
                key = "/".join(repo)
                if key in seen_repo:
                    continue
                seen_repo.add(key)
            out.append({**m, "cosine": cos})
            if len(out) >= self.k:
                break
        return out


def vector_blob(vec: list[float]) -> bytes:
    return array.array("f", vec).tobytes()


if __name__ == "__main__":
    r = Retriever(k=int(sys.argv[2]) if len(sys.argv) > 2 else 3)
    for hit in r.search(sys.argv[1]):
        print(f"{hit['cosine']:.4f} {hit['name']:<40} {hit['url']}")
