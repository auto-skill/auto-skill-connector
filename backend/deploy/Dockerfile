FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV HF_HOME=/opt/autoskill-hf

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

# Fetch model artifacts during image build, while the container still has
# network access. Runtime then uses this owned cache instead of trying to write
# under a missing /home/appuser or silently falling back to FTS.
RUN python -c "from huggingface_hub import hf_hub_download; hf_hub_download('Supabase/gte-small', 'onnx/model_quantized.onnx'); hf_hub_download('Supabase/gte-small', 'tokenizer.json')"

COPY . /app

# Fixed UID/GID (not the next-available one pip/apt would pick) so it's
# predictable across rebuilds -- docker-compose.yml bind-mounts ../data and
# ../skills_library from the host, and this UID must own those directories on
# the host for writes (SQLite, embeddings, scraped content) to keep working.
RUN groupadd -g 10001 appuser \
    && useradd -m -d /home/appuser -u 10001 -g appuser appuser \
    && chown -R appuser:appuser /app /opt/autoskill-hf
ENV HOME=/home/appuser
ENV HF_HUB_OFFLINE=1
USER appuser
