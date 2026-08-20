#!/usr/bin/env python3
"""Embed one modulo-shard of the texts dump through the production embed path.

Sharding is line-index modulo ASKILL_SHARDS, so no task needs the total line
count and a re-run of any single task is idempotent (atomic tmp+rename plus a
.done marker). Vectors come from the same embeddings.py, the same quantized
ONNX model bytes, and the same CPUExecutionProvider as production -- the only
thing that moves to CURC is the matmul.
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from embeddings import embed_texts  # noqa: E402


def main() -> int:
    src, outdir = sys.argv[1], sys.argv[2]
    tid = int(os.environ.get("SLURM_ARRAY_TASK_ID", "0"))
    nshards = int(os.environ.get("ASKILL_SHARDS", "16"))
    os.makedirs(outdir, exist_ok=True)
    out = os.path.join(outdir, f"vec_{tid:03d}.jsonl")
    if os.path.exists(out + ".done"):
        print("already done")
        return 0

    rows = []
    with open(src, encoding="utf-8") as fh:
        for i, line in enumerate(fh):
            if i % nshards == tid:
                rows.append(json.loads(line))
    print(f"shard {tid}/{nshards}: {len(rows):,} texts", flush=True)

    t0 = time.time()
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as ofh:
        step = 512
        for j in range(0, len(rows), step):
            chunk = rows[j:j + step]
            vecs = embed_texts([r["text"] for r in chunk], batch_size=32)
            for r, v in zip(chunk, vecs):
                ofh.write(json.dumps({
                    "canonical_id": r["canonical_id"],
                    "embedding_text_hash": r["embedding_text_hash"],
                    "embedded_at": r["embedded_at"],
                    # float32 carries ~7 significant digits; rounding there
                    # halves the JSON size without moving the vector.
                    "vector": [round(float(x), 7) for x in v],
                }) + "\n")
            done = j + len(chunk)
            if (j // step) % 10 == 0:
                print(f"  {done:,}/{len(rows):,}"
                      f"  {done / max(time.time() - t0, 1):.1f}/s", flush=True)
    os.replace(tmp, out)
    with open(out + ".done", "w") as f:
        f.write("ok\n")
    print(f"DONE {len(rows):,} in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
