#!/usr/bin/env python3
"""Attribute the residual cosine divergence to shards (= compute nodes).

Sample line i*956 belongs to modulo-shard (i*956) % 16. If divergence is
node-ISA, whole shards diverge while others sit at exactly 1.0; if it were
version or model bytes, every shard would diverge uniformly.
"""
import gzip
import json
import sys
from collections import defaultdict
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
from embeddings import embed_texts  # noqa: E402

TEXTS = BACKEND / "embed_texts_v2.jsonl"
VECS = BACKEND / "vectors_v2" / "vectors_all.jsonl.gz"
STRIDE = 956

sample, shard_of = {}, {}
with open(TEXTS, encoding="utf-8") as fh:
    for i, line in enumerate(fh):
        if i % STRIDE == 0:
            d = json.loads(line)
            sample[d["canonical_id"]] = d["text"]
            shard_of[d["canonical_id"]] = i % 16

remote = {}
with gzip.open(VECS, "rt", encoding="utf-8") as fh:
    for line in fh:
        i0 = line.find(': "') + 3
        if line[i0:i0 + 64] in sample:
            d = json.loads(line)
            remote[d["canonical_id"]] = d["vector"]

ids = list(sample)
local = dict(zip(ids, embed_texts([sample[k] for k in ids], batch_size=32)))

per = defaultdict(list)
for k in ids:
    cos = sum(a * b for a, b in zip(local[k], remote[k]))
    per[shard_of[k]].append(cos)

for s in sorted(per):
    xs = sorted(per[s])
    print(f"shard {s:2d}: n={len(xs):3d}  worst {xs[0]:.6f}  median {xs[len(xs)//2]:.6f}")
