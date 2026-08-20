#!/bin/bash
#SBATCH --partition=acpu
#SBATCH --qos=cpu-normal
#SBATCH --job-name=askill-embed
#SBATCH --array=0-15
#SBATCH --ntasks=1
#SBATCH --cpus-per-task=16
#SBATCH --mem=24G
#SBATCH --time=06:00:00
#SBATCH --export=NONE
#SBATCH --output=/scratch/alpine/saah8460/askill/logs/embed_%a.out
set -euo pipefail
ROOT=/scratch/alpine/saah8460/askill
cd "$ROOT"
module purge
module load anaconda 2>/dev/null || module load python 2>/dev/null
source "$ROOT/env126/bin/activate"
export HF_HOME="$ROOT/hf" HF_HUB_OFFLINE=1 ASKILL_SHARDS=16
python curc_embed_shard.py embed_texts_v2.jsonl out
