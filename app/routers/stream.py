"""Server-Sent Events stream and the NX image proxy."""

import asyncio
import json
import time
from collections import OrderedDict

import re

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.config import get_settings
from app.db import get_db, sessionmaker
from app.deps import client_ip, current_user
from app.scope import Scope, get_scope
from app.models import Alarm, User
from app.services import clips, report
from app.services.bus import bus
from app.services.poller import manager
from app.services.sites import describe_http_error

router = APIRouter()

KEEPALIVE_S = 5      # the page reconnects if it hears nothing for 12 s


@router.get("/api/events")
async def events(request: Request, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    # The companies in view plus your own (its alarms must keep reaching you); None = all companies.
    ids = None if scope.tenant_ids is None else scope.tenant_ids | {scope.own_id}
    await db.commit()          # release the DB connection: an open stream mustn't pin one
    sub = bus.subscribe(ids)

    async def gen():
        try:
            yield "retry: 3000\n\n"
            while not await request.is_disconnected():
                try:
                    event, data = await asyncio.wait_for(sub.queue.get(), KEEPALIVE_S)
                except asyncio.TimeoutError:
                    yield "event: ping\ndata: {}\n\n"   # heartbeat the page's watchdog listens for
                    continue
                yield f"event: {event}\ndata: {json.dumps(data)}\n\n"
        finally:
            bus.unsubscribe(sub)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# Event-time frames never change, so keep recent ones in memory.
_SNAP_CACHE: OrderedDict[int, bytes] = OrderedDict()
_SNAP_CACHE_MAX = 300


async def _alarm_for(db: AsyncSession, scope: Scope, alarm_id: int) -> Alarm:
    alarm = await db.scalar(select(Alarm).where(Alarm.id == alarm_id, scope.alerting_where(Alarm.tenant_id)))
    if alarm is None or not alarm.device_id:
        raise HTTPException(404)
    return alarm


async def _frame(alarm: Alarm, ts: int) -> bytes:
    try:
        return await manager.client_for(alarm.site).get_thumbnail(alarm.device_id, ts)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(describe_http_error(exc))) from exc


@router.get("/media/alarms/{alarm_id}/snapshot.jpg")
async def alarm_snapshot(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    alarm = await _alarm_for(db, scope, alarm_id)
    data = _SNAP_CACHE.get(alarm.id)
    if data is None:
        data = await _frame(alarm, alarm.event_ts_ms)
        _SNAP_CACHE[alarm.id] = data
        while len(_SNAP_CACHE) > _SNAP_CACHE_MAX:
            _SNAP_CACHE.popitem(last=False)
    else:
        _SNAP_CACHE.move_to_end(alarm.id)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


LIVE_MAX_PER_SITE = 6          # ~0.8 Mbit/s each through the relay
LIVE_MAX_S = 20 * 60           # a forgotten tab stops after this; the page reconnects when it's looked at
_live_slots: dict[int, asyncio.Semaphore] = {}


@router.get("/media/alarms/{alarm_id}/live.webm")
async def alarm_live_video(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    """Live video from the alarm's camera: NX transcodes to VP8/WebM (~100 KB/s at 640x360), the portal relays it.

    Measured through the relay: first byte in 0.25 s. NX's MJPEG stream is ~17x the bandwidth.
    """
    alarm = await _alarm_for(db, scope, alarm_id)
    await db.commit()          # release the DB connection: a live stream runs for minutes (sessions don't expire on commit)
    slots = _live_slots.setdefault(alarm.site_id, asyncio.Semaphore(LIVE_MAX_PER_SITE))
    if slots.locked():
        raise HTTPException(429, "Too many live views of this site are open; close one and retry")
    await slots.acquire()
    client = manager.client_for(alarm.site)
    resp = None
    try:
        for attempt in (1, 2):
            if not client._token:
                await client._login()
            req = client._client.build_request("GET", f"{client.base_url}/media/{alarm.device_id}.webm",
                                               params={"resolution": "640x360"}, headers=client._headers(),
                                               timeout=httpx.Timeout(15, read=30))
            resp = await client._client.send(req, stream=True)
            if resp.status_code == 401 and attempt == 1:
                await resp.aclose()
                client._token = None
                continue
            break
        if resp.status_code != 200:
            code = resp.status_code
            await resp.aclose()
            raise HTTPException(502, f"NX refused the live stream (HTTP {code})")
    except HTTPException:
        slots.release()
        raise
    except Exception as exc:  # noqa: BLE001
        slots.release()
        if resp is not None:
            await resp.aclose()
        raise HTTPException(502, str(describe_http_error(exc))) from exc

    async def body():
        started = time.monotonic()
        try:
            async for chunk in resp.aiter_raw():
                yield chunk
                if time.monotonic() - started > LIVE_MAX_S:
                    break
        except httpx.HTTPError:
            pass                                    # NX dropped it; the page notices and reconnects
        finally:
            await resp.aclose()
            slots.release()

    return StreamingResponse(body(), media_type="video/webm",
                             headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


@router.get("/media/alarms/{alarm_id}/live.jpg")
async def alarm_live(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    alarm = await _alarm_for(db, scope, alarm_id)
    data = await _frame(alarm, -1)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ video clips

async def _clip_alarm(db: AsyncSession, scope: Scope, alarm_id: int) -> Alarm:
    alarm = await db.scalar(select(Alarm).where(Alarm.id == alarm_id, scope.alerting_where(Alarm.tenant_id)))
    if alarm is None:
        raise HTTPException(404)
    return alarm


@router.get("/api/alarms/{alarm_id}/clip")
async def alarm_clip(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db),
                     pre: int | None = Query(None, ge=0, le=600), post: int | None = Query(None, ge=1, le=600),
                     quality: str = Query("sd", pattern="^(sd|hd)$")):
    """Clip status; poll until status == "ready", then play `url`."""
    alarm = await _clip_alarm(db, scope, alarm_id)
    try:
        info = await clips.get_clip(alarm, pre, post, quality)
    except clips.ClipError as exc:
        raise HTTPException(400, str(exc)) from exc
    return info.to_dict()


_CLIP_NAME = re.compile(r"^a(\d+)_\d+_\d+_(sd|hd)(_g\d+)?$")


@router.get("/media/clips/{name}.mp4")
async def clip_file(name: str, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db)):
    m = _CLIP_NAME.match(name)
    if not m:
        raise HTTPException(404)
    await _clip_alarm(db, scope, int(m.group(1)))          # tenant check
    path = clips.clip_path(name)
    if not path.exists():
        raise HTTPException(404, "Clip expired from cache; request it again")
    # FileResponse answers Range requests, which <video> needs for seeking and looping.
    return FileResponse(path, media_type="video/mp4", filename=f"alarm-{m.group(1)}.mp4",
                        content_disposition_type="inline", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/alarms/{alarm_id}/export")
async def export_alarm(alarm_id: int, request: Request, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db),
                       format: str = Query("pdf", pattern="^(pdf|zip)$"), note: str = Query("", max_length=2000),
                       pre: int | None = Query(None, ge=0, le=600), post: int | None = Query(None, ge=1, le=600),
                       quality: str = Query("sd", pattern="^(sd|hd)$")):
    """Incident report (PDF) or evidence package (ZIP: PDF + clip + stills + checksums). Audit-logged."""
    alarm = await _clip_alarm(db, scope, alarm_id)
    user = scope.user
    try:
        data, filename, media_type, detail = await report.export(db, alarm, user, format, note, pre, post, quality)
    except report.ExportError as exc:
        raise HTTPException(exc.status, str(exc)) from exc
    except clips.ClipError as exc:
        raise HTTPException(400, str(exc)) from exc
    audit(db, alarm.tenant_id, "alarm.exported", user_id=user.id, site_id=alarm.site_id, alarm_id=alarm.id,
          ip=client_ip(request), size=len(data), **detail)
    await db.commit()
    return Response(data, media_type=media_type, headers={
        "Content-Disposition": f'attachment; filename="{filename}"', "Cache-Control": "private, no-store"})


@router.get("/api/alarms/{alarm_id}/objects")
async def alarm_objects(alarm_id: int, scope: Scope = Depends(get_scope), db: AsyncSession = Depends(get_db),
                        from_ms: int | None = None, to_ms: int | None = None):
    """Analytics object tracks (per-frame bounding boxes) around the alarm."""
    alarm = await _clip_alarm(db, scope, alarm_id)
    s = get_settings()
    start = from_ms if from_ms is not None else alarm.event_ts_ms - s.clip_pre_s * 1000
    end = to_ms if to_ms is not None else alarm.event_ts_ms + s.clip_post_s * 1000
    if end <= start or end - start > s.clip_max_window_s * 1000 + 60_000:
        raise HTTPException(400, "Bad time range")
    try:
        return await clips.objects(alarm, start, end)
    except Exception as exc:  # noqa: BLE001 — analytics is best-effort; the clip still plays
        return JSONResponse({"detail": str(describe_http_error(exc))}, status_code=502)
