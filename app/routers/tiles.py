"""Map tile proxy with an on-disk cache.

Browsers fetch tiles from the portal, never from OpenStreetMap directly. That lets
us follow OSM's tile usage policy (identifying User-Agent, caching, few connections)
regardless of browser referrer settings, and keeps operators' browsers from talking
to third parties.
"""

import asyncio
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from app.config import get_settings

router = APIRouter()

_client: httpx.AsyncClient | None = None
_upstream_slots = asyncio.Semaphore(2)   # OSM asks for no more than 2 parallel downloads
_inflight: dict[str, asyncio.Future] = {}


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=15, headers={"User-Agent": get_settings().geocoder_user_agent})
    return _client


async def _download(url: str) -> bytes:
    async with _upstream_slots:
        r = await _http().get(url)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, f"Tile server returned {r.status_code}")
    return r.content


@router.get("/tiles/{z}/{x}/{y}.png", include_in_schema=False)
async def tile(z: int, x: int, y: int, request: Request):
    # Signed session cookie only (no DB hit per tile): keeps this from being an open proxy.
    if not request.session.get("uid"):
        raise HTTPException(401)
    settings = get_settings()
    if not (0 <= z <= settings.map_tile_max_zoom and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404)

    path = Path(settings.tile_cache_dir) / str(z) / str(x) / f"{y}.png"
    fresh = path.exists() and time.time() - path.stat().st_mtime < settings.tile_cache_ttl_s
    if not fresh:
        key = f"{z}/{x}/{y}"
        fut = _inflight.get(key)
        if fut is None:  # first request downloads; concurrent requests for the same tile wait on it
            fut = asyncio.get_running_loop().create_future()
            _inflight[key] = fut
            try:
                data = await _download(settings.map_tile_url.format(z=z, x=x, y=y, s="a"))
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_suffix(".tmp")
                tmp.write_bytes(data)
                os.replace(tmp, path)
                fut.set_result(data)
            except Exception as exc:
                fut.set_exception(exc)
                fut.exception()  # mark retrieved so a lone failure doesn't log "never retrieved"
                if not path.exists():
                    raise
                data = path.read_bytes()  # upstream down: serve the stale copy
            finally:
                _inflight.pop(key, None)
        else:
            try:
                data = await fut
            except Exception:
                if not path.exists():
                    raise
                data = path.read_bytes()
    else:
        data = path.read_bytes()
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})
