"""Map tile proxy with an on-disk cache.

Browsers fetch tiles from the portal, never from OpenStreetMap or USGS directly. That lets
us follow OSM's tile usage policy (identifying User-Agent, caching, few connections)
regardless of browser referrer settings, and keeps operators' browsers from talking
to third parties.

Two sources: the street map (/tiles/…png, OSM) for the 2D map and the 3D view, and aerial
imagery (/tiles/sat/…jpg, USGS The National Map) for the 3D site view.
"""

import asyncio
import math
import os
import time
from pathlib import Path

import httpx
from fastapi import APIRouter, HTTPException, Request, Response

from app.config import get_settings

router = APIRouter()

_client: httpx.AsyncClient | None = None
_osm_slots = asyncio.Semaphore(2)   # OSM asks for no more than 2 parallel downloads
_sat_slots = asyncio.Semaphore(6)   # USGS has no stated limit; stay modest
_inflight: dict[str, asyncio.Future] = {}
_MERC = math.pi * 6378137           # half the Web Mercator world width, in metres


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30, headers={"User-Agent": get_settings().geocoder_user_agent})
    return _client


async def _download(url: str, slots: asyncio.Semaphore) -> bytes:
    async with slots:
        r = await _http().get(url)
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, f"Tile server returned {r.status_code}")
    return r.content


async def _cached(path: Path, ttl_s: int, url: str, slots: asyncio.Semaphore) -> bytes:
    """The tile at `path`, downloaded from `url` when missing or older than `ttl_s`.

    Concurrent requests for the same tile share one download; if upstream fails, a stale
    copy is served rather than an error.
    """
    if path.exists() and time.time() - path.stat().st_mtime < ttl_s:
        return path.read_bytes()
    key = str(path)
    fut = _inflight.get(key)
    if fut is not None:
        try:
            return await fut
        except Exception:
            if not path.exists():
                raise
            return path.read_bytes()
    fut = asyncio.get_running_loop().create_future()
    _inflight[key] = fut
    try:
        data = await _download(url, slots)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)
        fut.set_result(data)
        return data
    except Exception as exc:
        fut.set_exception(exc)
        fut.exception()  # mark retrieved so a lone failure doesn't log "never retrieved"
        if not path.exists():
            raise
        return path.read_bytes()  # upstream down: serve the stale copy
    finally:
        _inflight.pop(key, None)


def _check(request: Request, z: int, x: int, y: int, max_zoom: int) -> None:
    # Signed session cookie only (no DB hit per tile): keeps this from being an open proxy.
    if not request.session.get("uid"):
        raise HTTPException(401)
    if not (0 <= z <= max_zoom and 0 <= x < 2 ** z and 0 <= y < 2 ** z):
        raise HTTPException(404)


@router.get("/tiles/{z}/{x}/{y}.png", include_in_schema=False)
async def tile(z: int, x: int, y: int, request: Request):
    settings = get_settings()
    _check(request, z, x, y, settings.map_tile_max_zoom)
    path = Path(settings.tile_cache_dir) / str(z) / str(x) / f"{y}.png"
    data = await _cached(path, settings.tile_cache_ttl_s, settings.map_tile_url.format(z=z, x=x, y=y, s="a"), _osm_slots)
    return Response(data, media_type="image/png", headers={"Cache-Control": "private, max-age=86400"})


def tile_bbox(z: int, x: int, y: int) -> str:
    """Web Mercator bounds of an XYZ tile as "minx,miny,maxx,maxy" (metres)."""
    size = 2 * _MERC / 2 ** z
    minx, maxy = -_MERC + x * size, _MERC - y * size
    return f"{minx:.3f},{maxy - size:.3f},{minx + size:.3f},{maxy:.3f}"


@router.get("/tiles/sat/{z}/{x}/{y}.jpg", include_in_schema=False)
async def sat_tile(z: int, x: int, y: int, request: Request):
    settings = get_settings()
    _check(request, z, x, y, settings.sat_tile_max_zoom)
    if z <= settings.sat_tile_max_tiled:
        url = settings.sat_tile_url.format(z=z, x=x, y=y)
    else:
        url = settings.sat_hires_url.format(bbox=tile_bbox(z, x, y))
    path = Path(settings.tile_cache_dir) / "sat" / str(z) / str(x) / f"{y}.jpg"
    data = await _cached(path, settings.sat_tile_cache_ttl_s, url, _sat_slots)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
