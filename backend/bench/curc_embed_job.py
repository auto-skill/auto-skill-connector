#!/usr/bin/env python3
"""Embed pre-built texts on a CURC node. Byte-identical math to embeddings.py.

Why this exists: embedding ~50k skills on the ingestion box (4 weak cores) takes
~10 hours. A CURC node has 128 cores, so the same work takes minutes.

The math here is a deliberate line-for-line copy of `embeddings.embed_texts`
rather than an import, because the vectors MUST land in the same space as the
query vectors Supabase Edge computes:

  * same repo/file  : Supabase/gte-small, onnx/model_quantized.onnx (shipped, not
                      re-downloaded -- compute nodes have no internet and a
                      different quantization would silently shift every vector)
  * same truncation : enable_truncation(max_length=512)
  * same pooling    : mean over the attention mask
  * same norm       : L2, clipped at 1e-9
  * same dtype      : float32 out

The embed TEXT and its hash are computed upstream on the ingestion box and passed
through untouched, so this job cannot change what was embedded -- only where the
matmul ran.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import numpy as np
import onnxruntime
from tokenizers import Tokenizer

MAX_TOKENS = 512


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--texts", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--model", type=Path, required=True)
    ap.add_argument("--tokenizer", type=Path, required=True)
    ap.add_argument("--batch-size", type=int, default=64)
    ap.add_argument("--threads", type=int, default=0, help="0 = all cores")
    a = ap.parse_args()

    opts = onnxruntime.SessionOptions()
    if a.threads:
        opts.intra_op_num_threads = a.threads
    session = onnxruntime.InferenceSession(
        str(a.model), sess_options=opts, providers=["CPUExecutionProvider"])
    tokenizer = Tokenizer.from_file(str(a.tokenizer))
    tokenizer.enable_truncation(max_length=MAX_TOKENS)

    records = []
    with open(a.texts, encoding="utf-8") as fh:
        for line in fh:
            if line.strip():
                records.append(json.loads(line))
    print(f"loaded {len(records):,} texts", flush=True)

    done = 0
    with open(a.out, "w", encoding="utf-8") as out:
        for i in range(0, len(records), a.batch_size):
            chunk = records[i:i + a.batch_size]
            batch = [(r.get("text") or " ") if (r.get("text") or "").strip() else " "
                     for r in chunk]
            encodings = tokenizer.encode_batch(batch)
            max_len = max(len(e.ids) for e in encodings)
            input_ids = np.zeros((len(batch), max_len), dtype=np.int64)
            attention_mask = np.zeros((len(batch), max_len), dtype=np.int64)
            token_type_ids = np.zeros((len(batch), max_len), dtype=np.int64)
            for j, enc in enumerate(encodings):
                input_ids[j, :len(enc.ids)] = enc.ids
                attention_mask[j, :len(enc.ids)] = enc.attention_mask
            (hidden,) = session.run(None, {
                "input_ids": input_ids,
                "attention_mask": attention_mask,
                "token_type_ids": token_type_ids,
            })
            mask = attention_mask[:, :, None].astype(np.float32)
            pooled = (hidden * mask).sum(axis=1) / np.clip(mask.sum(axis=1), 1e-9, None)
            norms = np.linalg.norm(pooled, axis=1, keepdims=True)
            pooled = pooled / np.clip(norms, 1e-9, None)
            for rec, vec in zip(chunk, pooled.astype(np.float32).tolist()):
                out.write(json.dumps({
                    "canonical_id": rec["canonical_id"],
                    "embedding_text_hash": rec["embedding_text_hash"],
                    "embedded_at": rec["embedded_at"],
                    "vector": vec,
                }) + "\n")
            done += len(chunk)
            if done % 5000 < a.batch_size:
                print(f"  {done:,}/{len(records):,}", flush=True)
    print(f"wrote {done:,} vectors -> {a.out}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
