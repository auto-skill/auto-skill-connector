#!/usr/bin/env python3
"""Gate the CURC-computed vectors before they touch the export DB.

Two checks, both against the local production embed path:
  1. binding: the vector file's embedding_text_hash must equal the texts
     dump's hash for the same canonical_id (rules out shard/join mixups).
  2. cosine: recompute a spread sample locally with the SAME embeddings.py
     and require cosine > 0.999 (identical model + provider should give
     ~1.0; anything lower means the remote env diverged and NOTHING loads).
"""
from __future__ import annotations

import gzip
import json
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
from embeddings import embed_texts  # noqa: E402

TEXTS = BACKEND / "embed_texts_v2.jsonl"
VECS = BACKEND / "vectors_v2" / "vectors_all.jsonl.gz"
SAMPLE = 500
TOTAL = 478_051
STRIDE = max(TOTAL // SAMPLE, 1)


def main() -> int:
    sample: dict[str, dict] = {}
    with open(TEXTS, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i % STRIDE == 0:
                d = json.loads(line)
                sample[d["canonical_id"]] = d
    print(f"  sampled {len(sample)} texts (stride {STRIDE})", flush=True)

    found: dict[str, dict] = {}
    n_lines = 0
    with gzip.open(VECS, "rt", encoding="utf-8") as fh:
        for line in fh:
            n_lines += 1
            i0 = line.find(': "') + 3  # {"canonical_id": "<64 hex>...
            cid = line[i0:i0 + 64]
            d = None
            if cid in sample:
                d = json.loads(line)
            elif n_lines % 100000 == 0:
                print(f"    scanned {n_lines:,} vector rows", flush=True)
            if d:
                found[d["canonical_id"]] = d
    print(f"  vector rows total: {n_lines:,}; sample matched: {len(found)}", flush=True)

    missing = [k for k in sample if k not in found]
    if missing:
        print(f"  FAIL: {len(missing)} sampled ids missing from vectors")
        return 1

    hash_bad = [k for k in sample
                if sample[k]["embedding_text_hash"] != found[k]["embedding_text_hash"]]
    if hash_bad:
        print(f"  FAIL: {len(hash_bad)} embedding_text_hash mismatches")
        return 1
    print("  binding ok: all sampled embedding_text_hashes match", flush=True)

    ids = list(sample)
    local = embed_texts([sample[k]["text"] for k in ids], batch_size=32)
    worst, worst_id = 1.0, None
    for k, lv in zip(ids, local):
        rv = found[k]["vector"]
        cos = sum(a * b for a, b in zip(lv, rv))
        if cos < worst:
            worst, worst_id = cos, k
    print(f"  cosine: worst {worst:.6f} ({worst_id and worst_id[:12]})")
    if worst <= 0.999:
        print("  FAIL: below 0.999 gate -- do not load")
        return 1
    print(f"  PASS: {len(ids)} sampled vectors identical to production path "
          f"(rows={n_lines:,})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
