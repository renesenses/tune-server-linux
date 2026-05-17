# ---- builder: install deps + prepare web client ----
FROM python:3.12-slim AS builder

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    libasound2-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

COPY pyproject.toml README.md ./
COPY tune_server/ tune_server/

RUN pip install --no-cache-dir --prefix=/install .

# Web client (pre-built Svelte SPA)
COPY web/ /build/web/

# ---- runtime: slim image with only what we need ----
FROM python:3.12-slim

RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    libportaudio2 \
    libasound2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Python packages from builder
COPY --from=builder /install /usr/local

# Application code
COPY --from=builder /build/tune_server/ /app/tune_server/

# Web client
COPY --from=builder /build/web/ /app/web/

# Volumes for persistent data and music library
VOLUME /music /data

# Default configuration (all overridable via TUNE_ env vars)
ENV TUNE_MUSIC_DIRS='["/music"]' \
    TUNE_DB_PATH=/data/tune.db \
    TUNE_ARTWORK_CACHE_DIR=/data/artwork_cache \
    TUNE_WEB_DIR=/app/web \
    TUNE_API_PORT=8888 \
    TUNE_LOG_LEVEL=INFO

# REST API + UPnP MediaServer
EXPOSE 8888 8080

CMD ["tune-server"]
