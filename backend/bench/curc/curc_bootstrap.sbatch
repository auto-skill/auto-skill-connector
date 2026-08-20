#!/bin/bash
#SBATCH --partition=acpu
#SBATCH --qos=cpu-normal
#SBATCH --job-name=askill-boot
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=4
#SBATCH --mem=16G
#SBATCH --time=00:45:00
#SBATCH --export=NONE
#SBATCH --output=/scratch/alpine/saah8460/askill/logs/boot.out
# Build the embed venv on a compute node (login nodes expose no software
# modules) and smoke-test the EXACT production module + model. The model
# comes from the shipped HF cache, never the network: vectors must be in
# the same space as production's, so the bytes are pinned, not re-resolved.
set -euo pipefail
ROOT=/scratch/alpine/saah8460/askill
mkdir -p "$ROOT/logs" "$ROOT/out"
cd "$ROOT"
module purge
module load anaconda 2>/dev/null || module load python 2>/dev/null
python3 --version
if [ ! -x "$ROOT/env/bin/python" ]; then
  python3 -m venv "$ROOT/env"
fi
source "$ROOT/env/bin/activate"
pip install -q --upgrade pip
pip install -q onnxruntime tokenizers huggingface_hub numpy
export HF_HOME="$ROOT/hf" HF_HUB_OFFLINE=1
python - <<'EOF'
import sys
sys.path.insert(0, "/scratch/alpine/saah8460/askill")
from embeddings import embed_texts
vs = embed_texts(["hello world", "a skill for testing the embed path"])
assert len(vs) == 2 and len(vs[0]) == 384, (len(vs), len(vs[0]))
import math
n = math.sqrt(sum(x * x for x in vs[0]))
assert abs(n - 1.0) < 1e-3, n
print("smoke ok: 2 vectors, 384-dim, L2-normalized")
EOF
echo BOOT_DONE
