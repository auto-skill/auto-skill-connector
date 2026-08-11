#!/bin/bash
#SBATCH --job-name=autoskill-embed
#SBATCH --partition=acpu
#SBATCH --account=ucb-general
#SBATCH --qos=cpu-normal
#SBATCH --nodes=1
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=32
#SBATCH --mem=32G
#SBATCH --time=01:00:00
#SBATCH --output=/scratch/alpine/saah8460/autoskill_embed/logs/embed-%j.out
#SBATCH --error=/scratch/alpine/saah8460/autoskill_embed/logs/embed-%j.err
set -euo pipefail

R=/scratch/alpine/saah8460/autoskill_embed
V=/scratch/alpine/saah8460/autoskill_embed_venv

# onnxruntime reads OMP_NUM_THREADS for its CPU kernels; without this it can
# spin up far more threads than the cgroup allows and thrash.
export OMP_NUM_THREADS="${SLURM_CPUS_PER_TASK}"

echo "host=$(hostname) cpus=${SLURM_CPUS_PER_TASK} start=$(date -u +%FT%TZ)"

"$V/bin/python" "$R/curc_embed_job.py" \
  --texts "$R/judged_texts.jsonl" \
  --out "$R/judged_vectors.jsonl" \
  --model "$R/model/model_quantized.onnx" \
  --tokenizer "$R/model/tokenizer.json" \
  --batch-size 64 \
  --threads "${SLURM_CPUS_PER_TASK}"

echo "done=$(date -u +%FT%TZ)"
wc -l "$R/judged_vectors.jsonl"
