FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Fixed UID/GID (not the next-available one pip/apt would pick) so it's
# predictable across rebuilds -- docker-compose.yml bind-mounts ../data and
# ../skills_library from the host, and this UID must own those directories on
# the host for writes (SQLite, embeddings, scraped content) to keep working.
RUN groupadd -g 10001 appuser && useradd -u 10001 -g appuser -M appuser
USER appuser
