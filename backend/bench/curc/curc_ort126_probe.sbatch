#!/bin/bash
#SBATCH --partition=acpu
#SBATCH --qos=cpu-normal
#SBATCH --job-name=askill-ort126
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=8G
#SBATCH --time=00:30:00
#SBATCH --export=NONE
#SBATCH --output=/scratch/alpine/saah8460/askill/logs/ort126.out
# Discriminates version-vs-ISA for the cosine divergence: embed the exact
# verification sample (every 956th line) with onnxruntime pinned to the
# local box's 1.26.0, on the same Xeon nodes that produced the 1.29 vectors.
set -euo pipefail
ROOT=/scratch/alpine/saah8460/askill
cd "$ROOT"
module purge
module load anaconda 2>/dev/null || module load python 2>/dev/null
if [ ! -x "$ROOT/env126/bin/python" ]; then
  python3 -m venv "$ROOT/env126"
fi
source "$ROOT/env126/bin/activate"
pip install -q --upgrade pip
pip install -q onnxruntime==1.26.0 tokenizers huggingface_hub numpy
export HF_HOME="$ROOT/hf" HF_HUB_OFFLINE=1
python - <<'EOF'
import json, sys
sys.path.insert(0, "/scratch/alpine/saah8460/askill")
import onnxruntime
from embeddings import embed_texts
STRIDE = 956
rows = []
with open("/scratch/alpine/saah8460/askill/embed_texts_v2.jsonl", encoding="utf-8") as fh:
    for i, line in enumerate(fh):
        if i % STRIDE == 0:
            rows.append(json.loads(line))
vecs = embed_texts([r["text"] for r in rows], batch_size=32)
with open("/scratch/alpine/saah8460/askill/out/sample_ort126.jsonl", "w") as ofh:
    for r, v in zip(rows, vecs):
        ofh.write(json.dumps({"canonical_id": r["canonical_id"],
                              "ort": onnxruntime.__version__,
                              "vector": [round(float(x), 7) for x in v]}) + "\n")
print("PROBE_DONE", len(rows), onnxruntime.__version__)
EOF
