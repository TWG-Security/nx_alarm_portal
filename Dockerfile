# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg: makes NX clips browser-playable (H.264 rewrap/transcode) and reads their start time.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

ENV TILE_CACHE_DIR=/app/tile-cache \
    MEDIA_CACHE_DIR=/app/media-cache
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/tile-cache /app/media-cache && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

# One worker on purpose: site pollers and the live-update bus run in-process.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --proxy-headers --forwarded-allow-ips='*'"]
