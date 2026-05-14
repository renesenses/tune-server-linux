FROM python:3.12-slim

# System deps for audio + FFmpeg
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    build-essential \
    libasound2-dev \
    libportaudio2 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python deps
COPY pyproject.toml README.md ./
COPY tune_server/ tune_server/
RUN pip install --no-cache-dir .

# Web client (pre-built)
COPY web/ web/

# Data volume
VOLUME /data

# Environment
ENV TUNE_DB_PATH=/data/tune_server.db \
    TUNE_ARTWORK_CACHE_DIR=/data/artwork_cache \
    TUNE_MUSIC_DIRS='["/music"]' \
    TUNE_WEB_DIR=/app/web \
    TUNE_API_PORT=8888 \
    TUNE_LOG_LEVEL=INFO

EXPOSE 8888 8080

CMD ["python", "-m", "tune_server"]
