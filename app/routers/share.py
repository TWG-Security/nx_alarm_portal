"""The no-sign-in location-sharing page for guards without a GPS router (app/guard_share.py).

- GET  /g/{token}            the page: Start / Stop sharing, how fresh the last position is
- POST /api/g/{token}/pos    one position from the browser {lat, lng, accuracy, speed, heading, ts}
- POST /api/g/{token}/stop   the guard stopped sharing (button, or the page closing: sendBeacon)

Unknown tokens count as failed sign-ins (kind "share_link"), so guessing gets the address banned. The link only
writes this one tracker's position; it shows nothing about alarms or other guards.
"""
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import guard_share, security_guard
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, render
from app.models import Tenant, utcnow
from app.services import trackers

router = APIRouter()
PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}
SEND_EVERY_S = 10            # the page sends at least this often while sharing (sooner when the guard moves)


async def _tracker(request: Request, db: AsyncSession, token: str):
    status, t = await guard_share.look_up(db, token)
    if status == "invalid":
        await security_guard.record(db, client_ip(request), "share_link", "failure", reason="unknown location link")
        await db.commit()
    return status, t


@router.get("/g/{token}/")
@router.get("/g/{token}")
async def share_page(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    status, t = await _tracker(request, db, token)
    if status != "ok":
        response = render(request, "share.html", state=status)
        response.status_code = 410 if status == "expired" else 404
    else:
        tenant = await db.get(Tenant, t.tenant_id)
        audit(db, t.tenant_id, "tracker.share_opened", user_id=t.user_id, ip=client_ip(request), name=t.name)
        await db.commit()
        response = render(request, "share.html", state="ok", token=token, guard_name=t.name,
                          company=tenant.label if tenant else "", every_s=SEND_EVERY_S)
    response.headers.update(PRIVATE)
    return response


def fix_time(ts) -> "datetime | None":
    """The browser's fix time, or None (use the arrival time). Safari's GeolocationPosition.timestamp came in a
    different unit (2026-10-13: a date in the year 58753 crashed this with a 500 on every position from an iPhone),
    so milliseconds and microseconds are both tried; anything not within 10 minutes of now is ignored."""
    from datetime import datetime, timezone
    if not ts:
        return None
    now = utcnow()
    for scale in (1_000, 1_000_000, 1):
        try:
            at = datetime.fromtimestamp(ts / scale, timezone.utc)
        except (ValueError, OverflowError, OSError):
            continue
        if abs((now - at).total_seconds()) <= 600:
            return at
    return None


class PosIn(BaseModel):
    lat: float = Field(ge=-90, le=90)
    lng: float = Field(ge=-180, le=180)
    accuracy: float | None = Field(default=None, ge=0, le=100_000)
    speed: float | None = Field(default=None, ge=0, le=150)
    heading: float | None = Field(default=None, ge=0, le=360)
    ts: float | None = None                        # the browser's fix time (ms since 1970; Safari's differs: fix_time)


async def _live(request: Request, db: AsyncSession, token: str):
    status, t = await _tracker(request, db, token)
    if status != "ok":
        raise HTTPException(410 if status == "expired" else 404, "This location link has expired" if status == "expired" else "Not found")
    tid = t.id
    await db.commit()                              # the position is written in its own short session
    return tid


@router.post("/api/g/{token}/pos")
async def share_position(token: str, body: PosIn, request: Request, db: AsyncSession = Depends(get_db)):
    tid = await _live(request, db, token)
    at = fix_time(body.ts)
    outcome = await trackers.phone_fix(tid, body.lat, body.lng, speed=body.speed, heading=body.heading,
                                       accuracy=body.accuracy, at=at)
    if outcome == "gone":
        raise HTTPException(404, "Not found")
    return {"ok": True, "taken": outcome == "ok", "every_s": SEND_EVERY_S}


@router.post("/api/g/{token}/stop")
async def share_stop(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    tid = await _live(request, db, token)
    await trackers.phone_stop(tid)
    return {"ok": True}
