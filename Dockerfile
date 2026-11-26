# syntax=docker/dockerfile:1
# meshoptimizer (site 3D model simplification) ships as source only: build its wheel here, so the app image
# needs no compiler.
FROM python:3.12-slim AS wheels
RUN apt-get update && apt-get install -y --no-install-recommends g++ && rm -rf /var/lib/apt/lists/*
RUN pip wheel --no-cache-dir --no-deps -w /wheels meshoptimizer==0.2.30a0

# Site VPN connections (app/tunnels.py): wireproxy (userspace WireGuard -> SOCKS5) and Tailscale, pinned + checksummed.
FROM python:3.12-slim AS vpn
ADD --checksum=sha256:512f613ed7d04fc7282f7dc5bd76aa105aae28168fbb9357eb749ebcbc29f0ae \
    https://pkgs.tailscale.com/stable/tailscale_1.104.2_amd64.tgz /tmp/tailscale.tgz
ADD --checksum=sha256:e88c1d090740373fc606c1bafd81d9a5eadc642cce5667616e20e9d7a444f51c \
    https://github.com/windtf/wireproxy/releases/download/v1.1.3/wireproxy_linux_amd64.tar.gz /tmp/wireproxy.tgz
RUN mkdir -p /x /out && tar -xzf /tmp/tailscale.tgz -C /x && tar -xzf /tmp/wireproxy.tgz -C /x \
    && for b in tailscale tailscaled wireproxy; do find /x -type f -name "$b" -exec install -m 0755 {} /out/ \; ; done \
    && test -x /out/tailscale && test -x /out/tailscaled && test -x /out/wireproxy

FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# ffmpeg: makes NX clips browser-playable (H.264 rewrap/transcode) and reads their start time.
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg && rm -rf /var/lib/apt/lists/*

COPY --from=vpn /out/ /usr/local/bin/
COPY requirements.txt .
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir --find-links /wheels -r requirements.txt && rm -rf /wheels

COPY alembic.ini ./
COPY migrations ./migrations
COPY app ./app

ENV TILE_CACHE_DIR=/app/tile-cache \
    MEDIA_CACHE_DIR=/app/media-cache \
    TUNNELS_DIR=/app/tunnels-data
RUN useradd --create-home --uid 10001 appuser && mkdir -p /app/tile-cache /app/media-cache /app/site-models /app/tunnels-data && chown -R appuser:appuser /app
USER appuser

EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)" || exit 1

# One worker on purpose: site pollers and the live-update bus run in-process.
# --forwarded-allow-ips: only Caddy (on the docker network) may set X-Forwarded-For. app/net.py then
# decides whether to believe Cloudflare's CF-Connecting-IP (only from the trusted tunnel connector).
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn app.main:app --host 0.0.0.0 --port 8080 --workers 1 --timeout-graceful-shutdown 2 --proxy-headers --forwarded-allow-ips=\"${FORWARDED_ALLOW_IPS:-172.16.0.0/12}\""]
