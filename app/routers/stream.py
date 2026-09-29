"""Server-Sent Events stream and the NX image proxy."""

import asyncio
import json
from collections import OrderedDict

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_db, sessionmaker
from app.deps import current_user
from app.models import Alarm, User
from app.services.bus import bus
from app.services.poller import manager
from app.services.sites import describe_http_error

router = APIRouter()

KEEPALIVE_S = 15


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
                    yield ": keepalive\n\n"
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
