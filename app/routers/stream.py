"""Server-Sent Events stream and the NX image proxy."""

import asyncio
import json
from collections import OrderedDict

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.db import get_db, sessionmaker
from app.deps import current_user
from app.models import Alarm, User
from app.services import clips
from app.services.bus import bus
from app.services.poller import manager
from app.services.sites import describe_http_error

router = APIRouter()

KEEPALIVE_S = 5      # the page reconnects if it hears nothing for 12 s


@router.get("/api/events")
async def events(request: Request):
    # Look the user up with a short-lived session so an open stream doesn't pin a DB connection.
    uid = request.session.get("uid")
    async with sessionmaker()() as db:
        user = await db.get(User, uid) if uid else None
    if user is None or not user.is_active:
        raise HTTPException(401)
    sub = bus.subscribe(user.tenant_id)

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


async def _alarm_for(db: AsyncSession, user: User, alarm_id: int) -> Alarm:
    alarm = await db.scalar(select(Alarm).where(Alarm.id == alarm_id, Alarm.tenant_id == user.tenant_id))
    if alarm is None or not alarm.device_id:
        raise HTTPException(404)
    return alarm


async def _frame(alarm: Alarm, ts: int) -> bytes:
    try:
        return await manager.client_for(alarm.site).get_thumbnail(alarm.device_id, ts)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(502, str(describe_http_error(exc))) from exc


@router.get("/media/alarms/{alarm_id}/snapshot.jpg")
async def alarm_snapshot(alarm_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    alarm = await _alarm_for(db, user, alarm_id)
    data = _SNAP_CACHE.get(alarm.id)
    if data is None:
        data = await _frame(alarm, alarm.event_ts_ms)
        _SNAP_CACHE[alarm.id] = data
        while len(_SNAP_CACHE) > _SNAP_CACHE_MAX:
            _SNAP_CACHE.popitem(last=False)
    else:
        _SNAP_CACHE.move_to_end(alarm.id)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})


@router.get("/media/alarms/{alarm_id}/live.jpg")
async def alarm_live(alarm_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    alarm = await _alarm_for(db, user, alarm_id)
    data = await _frame(alarm, -1)
    return Response(data, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


# ------------------------------------------------------------------ video clips

async def _clip_alarm(db: AsyncSession, user: User, alarm_id: int) -> Alarm:
    alarm = await db.scalar(select(Alarm).where(Alarm.id == alarm_id, Alarm.tenant_id == user.tenant_id))
    if alarm is None:
        raise HTTPException(404)
    return alarm


@router.get("/api/alarms/{alarm_id}/clip")
async def alarm_clip(alarm_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                     pre: int | None = Query(None, ge=0, le=600), post: int | None = Query(None, ge=1, le=600),
                     quality: str = Query("sd", pattern="^(sd|hd)$")):
    """Clip status; poll until status == "ready", then play `url`."""
    alarm = await _clip_alarm(db, user, alarm_id)
    try:
        info = await clips.get_clip(alarm, pre, post, quality)
    except clips.ClipError as exc:
        raise HTTPException(400, str(exc)) from exc
    return info.to_dict()


_CLIP_NAME = re.compile(r"^a(\d+)_\d+_\d+_(sd|hd)$")


@router.get("/media/clips/{name}.mp4")
async def clip_file(name: str, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    m = _CLIP_NAME.match(name)
    if not m:
        raise HTTPException(404)
    await _clip_alarm(db, user, int(m.group(1)))          # tenant check
    path = clips.clip_path(name)
    if not path.exists():
        raise HTTPException(404, "Clip expired from cache; request it again")
    # FileResponse answers Range requests, which <video> needs for seeking and looping.
    return FileResponse(path, media_type="video/mp4", filename=f"alarm-{m.group(1)}.mp4",
                        content_disposition_type="inline", headers={"Cache-Control": "private, max-age=86400"})


@router.get("/api/alarms/{alarm_id}/objects")
async def alarm_objects(alarm_id: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db),
                        from_ms: int | None = None, to_ms: int | None = None):
    """Analytics object tracks (per-frame bounding boxes) around the alarm."""
    alarm = await _clip_alarm(db, user, alarm_id)
    s = get_settings()
    start = from_ms if from_ms is not None else alarm.event_ts_ms - s.clip_pre_s * 1000
    end = to_ms if to_ms is not None else alarm.event_ts_ms + s.clip_post_s * 1000
    if end <= start or end - start > s.clip_max_window_s * 1000 + 60_000:
        raise HTTPException(400, "Bad time range")
    try:
        return await clips.objects(alarm, start, end)
    except Exception as exc:  # noqa: BLE001 — analytics is best-effort; the clip still plays
        return JSONResponse({"detail": str(describe_http_error(exc))}, status_code=502)
