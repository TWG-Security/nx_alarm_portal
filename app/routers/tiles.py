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
log = __import__("logging").getLogger(__name__)

_client: httpx.AsyncClient | None = None
_osm_slots = asyncio.Semaphore(2)   # OSM asks for no more than 2 parallel downloads
_sat_slots = asyncio.Semaphore(6)   # USGS tiled imagery (z <= 16): fast, cached by USGS; no stated limit, stay modest
# NAIP (z17+) is rendered per request and can be very slow: measured 2026-10-16, 24 s and 30 s+ timeouts per tile.
# It gets its own slots and a short timeout so it can never hold up the coarser tiles (with one shared queue, the
# 3D and top-down views showed nothing: the user, 2026-10-16 "the earth view is not working right and not loading").
_naip_slots = asyncio.Semaphore(4)
NAIP_TIMEOUT_S = 12.0
NAIP_TRIP_FAILS, NAIP_TRIP_WINDOW_S, NAIP_REST_S = 3, 60.0, 120.0
_naip_fails: list[float] = []          # monotonic times of recent NAIP failures
_naip_rest_until = 0.0                 # while resting: z17+ is drawn from the z16 tile at once, NAIP isn't asked
_inflight: dict[str, asyncio.Future] = {}
_MERC = math.pi * 6378137           # half the Web Mercator world width, in metres


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(timeout=30, headers={"User-Agent": get_settings().geocoder_user_agent})
    return _client


async def _download(url: str, slots: asyncio.Semaphore, timeout: float | None = None) -> bytes:
    async with slots:
        try:
            # timeout: the whole download (httpx's own limit is per network step: a trickling server outlasts it)
            r = await (asyncio.wait_for(_http().get(url), timeout) if timeout else _http().get(url))
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:   # slow or unreachable upstream: a clean 502
            raise HTTPException(502, f"Tile server unreachable: {type(exc).__name__}") from None
    if r.status_code != 200 or not r.headers.get("content-type", "").startswith("image/"):
        raise HTTPException(502, f"Tile server returned {r.status_code}")
    return r.content


async def _cached(path: Path, ttl_s: int, url: str, slots: asyncio.Semaphore, timeout: float | None = None) -> bytes:
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
        data = await _download(url, slots, timeout)
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


def _sat_path(z: int, x: int, y: int) -> Path:
    return Path(get_settings().tile_cache_dir) / "sat" / str(z) / str(x) / f"{y}.jpg"


async def _tiled(z: int, x: int, y: int) -> bytes:
    settings = get_settings()
    return await _cached(_sat_path(z, x, y), settings.sat_tile_cache_ttl_s, settings.sat_tile_url.format(z=z, x=x, y=y), _sat_slots)


def naip_resting() -> bool:
    return time.monotonic() < _naip_rest_until


def _naip_failed() -> None:
    global _naip_rest_until
    now = time.monotonic()
    _naip_fails[:] = [t for t in _naip_fails if now - t < NAIP_TRIP_WINDOW_S] + [now]
    if len(_naip_fails) >= NAIP_TRIP_FAILS and not naip_resting():
        _naip_rest_until = now + NAIP_REST_S
        log.warning("imagery: NAIP failing (%d in %.0f s): sharp tiles drawn from level %d for %.0f s",
                    len(_naip_fails), NAIP_TRIP_WINDOW_S, get_settings().sat_tile_max_tiled, NAIP_REST_S)


async def _from_parent(z: int, x: int, y: int) -> bytes:
    """A z17+ tile cut from its z16 ancestor and enlarged: softer than NAIP, but a picture now."""
    from PIL import Image
    top = get_settings().sat_tile_max_tiled
    d = z - top
    parent = await _tiled(top, x >> d, y >> d)

    def cut() -> bytes:
        import io
        im = Image.open(io.BytesIO(parent)).convert("RGB")
        size = im.width / 2 ** d
        ox, oy = (x % 2 ** d) * size, (y % 2 ** d) * size
        out = io.BytesIO()
        im.crop((round(ox), round(oy), round(ox + size), round(oy + size))).resize((256, 256), Image.Resampling.BICUBIC) \
            .save(out, "JPEG", quality=85)
        return out.getvalue()
    return await asyncio.to_thread(cut)


@router.get("/tiles/sat/{z}/{x}/{y}.jpg", include_in_schema=False)
async def sat_tile(z: int, x: int, y: int, request: Request):
    settings = get_settings()
    _check(request, z, x, y, settings.sat_tile_max_zoom)
    if z <= settings.sat_tile_max_tiled:
        data = await _tiled(z, x, y)
        return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    path = _sat_path(z, x, y)
    fresh = path.exists() and time.time() - path.stat().st_mtime < settings.sat_tile_cache_ttl_s
    if not fresh and not naip_resting():
        try:
            data = await _cached(path, settings.sat_tile_cache_ttl_s, settings.sat_hires_url.format(bbox=tile_bbox(z, x, y)),
                                 _naip_slots, NAIP_TIMEOUT_S)
            return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
        except HTTPException:
            _naip_failed()
    elif fresh or path.exists():
        return Response(path.read_bytes(), media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})
    # NAIP slow or down and nothing cached: the coarser picture now; the browser asks again in 5 minutes.
    data = await _from_parent(z, x, y)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=300"})


# ---------------------------------------------------------------- pre-loading sharp imagery
# NAIP tiles take 1-8 s each the first time, so the 3D view starts blurry. A background job
# fetches the deep levels around every site ahead of time (slowly: 2 at a time), then once a day
# tops up anything that expired. Only network waits; the alarm path is unaffected.
PREFETCH_LEVELS = (17, 18, 19)
PREFETCH_RADIUS_M = 250
_prefetch_slots = asyncio.Semaphore(2)


def tiles_around(lat: float, lng: float, z: int, radius_m: float) -> list[tuple[int, int]]:
    n = 2 ** z
    fx = (lng + 180) / 360 * n
    fy = (1 - math.asinh(math.tan(math.radians(lat))) / math.pi) / 2 * n
    span = radius_m / (2 * _MERC * math.cos(math.radians(lat)) / n)     # radius in tiles at this latitude
    return [(x, y) for x in range(int(fx - span), int(fx + span) + 1) for y in range(int(fy - span), int(fy + span) + 1)
            if 0 <= x < n and 0 <= y < n]


async def prefetch_site(lat: float, lng: float) -> tuple[int, int]:
    """Fetch the sharp levels around a point into the cache. Returns (downloaded or refreshed, failed)."""
    settings = get_settings()
    got = failed = 0
    for z in PREFETCH_LEVELS:
        if z <= settings.sat_tile_max_tiled or z > settings.sat_tile_max_zoom:
            continue
        for x, y in tiles_around(lat, lng, z, PREFETCH_RADIUS_M):
            path = Path(settings.tile_cache_dir) / "sat" / str(z) / str(x) / f"{y}.jpg"
            if path.exists() and time.time() - path.stat().st_mtime < settings.sat_tile_cache_ttl_s:
                continue
            try:
                if naip_resting():
                    failed += 1
                    continue
                async with _prefetch_slots:          # at most 2 of NAIP's slots: people looking at the map come first
                    await _cached(path, settings.sat_tile_cache_ttl_s, settings.sat_hires_url.format(bbox=tile_bbox(z, x, y)),
                                  _naip_slots, NAIP_TIMEOUT_S)
                got += 1
            except Exception:  # noqa: BLE001 - outside the US, USGS down: the view falls back to coarser tiles
                _naip_failed()
                failed += 1
    return got, failed


async def run_prefetcher(every_s: float = 24 * 3600) -> None:
    from sqlalchemy import select
    from app.db import sessionmaker
    from app.models import Site
    await asyncio.sleep(30)          # let pollers and push settle after a restart first
    while True:
        try:
            async with sessionmaker()() as db:
                spots = (await db.execute(select(Site.id, Site.lat, Site.lng).where(
                    Site.archived_at.is_(None), Site.lat.is_not(None), Site.lng.is_not(None)))).all()
            for site_id, lat, lng in spots:
                got, failed = await prefetch_site(lat, lng)
                if got or failed:
                    log.info("imagery pre-load: site %s: %d tiles fetched, %d failed", site_id, got, failed)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("imagery pre-load failed: %s", exc)
        await asyncio.sleep(every_s)
