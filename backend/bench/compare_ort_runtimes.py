#!/usr/bin/env python3
"""Three-way cosine comparison to attribute the embed divergence.

Pairs:
  A: CURC ort1.29 vs CURC ort1.26  (same Xeon, different version)
  B: CURC ort1.26 vs local ort1.26 (same version, different CPU/ISA)
  C: CURC ort1.29 vs local ort1.26 (the original failing gate)

If A ~= 1.0 and B shows the gap, the divergence is CPU-ISA int8 kernels and
version pinning cannot close it -- the reference for "production space" must
be the serving CPU, not this box.
"""
from __future__ import annotations

import gzip
import json
import statistics
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(BACKEND))
from embeddings import embed_texts  # noqa: E402

TEXTS = BACKEND / "embed_texts_v2.jsonl"
VECS129 = BACKEND / "vectors_v2" / "vectors_all.jsonl.gz"
VECS126 = BACKEND / "vectors_v2" / "sample_ort126.jsonl"
STRIDE = 956


def main() -> int:
    sample: dict[str, str] = {}
    with open(TEXTS, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i % STRIDE == 0:
                d = json.loads(line)
                sample[d["canonical_id"]] = d["text"]

    v126 = {}
    with open(VECS126, encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            v126[d["canonical_id"]] = d["vector"]

    v129 = {}
    with gzip.open(VECS129, "rt", encoding="utf-8") as fh:
        for line in fh:
            i0 = line.find(': "') + 3
            if line[i0:i0 + 64] in sample:
                d = json.loads(line)
                v129[d["canonical_id"]] = d["vector"]

    ids = [k for k in sample if k in v126 and k in v129]
    print(f"  comparable ids: {len(ids)}", flush=True)
    local = dict(zip(ids, embed_texts([sample[k] for k in ids], batch_size=32)))

    def report(name, xs, ys):
        cos = sorted(sum(a * b for a, b in zip(xs[k], ys[k])) for k in ids)
        n = len(cos)
        print(f"  {name:<28} worst {cos[0]:.6f}  p01 {cos[n//100]:.6f}"
              f"  median {statistics.median(cos):.6f}  frac<0.999 {sum(c < 0.999 for c in cos)/n:.3f}")

    report("A curc129 vs curc126", v129, v126)
    report("B curc126 vs local126", v126, local)
    report("C curc129 vs local126", v129, local)
    return 0


if __name__ == "__main__":
    sys.exit(main())
