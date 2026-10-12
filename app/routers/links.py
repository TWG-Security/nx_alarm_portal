"""The no-sign-in alarm page a text message links to (app/alarm_links.py, app/sms.py).

- GET /a/{token}                               the page: one alarm's details, clip and live video
- GET /api/a/{token}/clip, /objects            clip status and analytics boxes (same as the signed-in ones)
- GET /media/a/{token}/snapshot.jpg, live.jpg, live.webm, clips/{name}.mp4

Everything is limited to the link's one alarm. An unknown token is recorded as a failed sign-in
(kind "alarm_link"), so an address that guesses gets banned (BanGate guards these paths). Opening the
page is audit-logged in the alarm's company under the name of the person it was texted to.
Pages are never cached or indexed and send no Referer (the token is the key).
"""

import re

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app import alarm_links, security_guard
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.deps import client_ip, render
from app.models import Alarm, User, utcnow
from app.routers import stream
from app.services import clips
from app.services.serialize import alarm_dict
from app.services.sites import describe_http_error

router = APIRouter()

PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer", "X-Robots-Tag": "noindex, nofollow"}


async def _resolve(request: Request, db: AsyncSession, token: str) -> tuple[str, object, Alarm | None]:
    status, link, alarm = await alarm_links.look_up(db, token)
    if status == "invalid" and link is None:
        await security_guard.record(db, client_ip(request), "alarm_link", "failure", reason="unknown alarm link")
        await db.commit()
    return status, link, alarm


async def _alarm(request: Request, db: AsyncSession, token: str) -> Alarm:
    status, _, alarm = await _resolve(request, db, token)
    if status != "ok":
        raise HTTPException(404 if status == "invalid" else 410, "This alarm link has expired" if status == "expired" else "Not found")
    return alarm


@router.get("/a/{token}")
async def alarm_page(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    status, link, alarm = await _resolve(request, db, token)
    if status != "ok":
        response = render(request, "alarm_link.html", state=status, alarm_id=alarm.id if alarm else None)
        response.status_code = 410 if status == "expired" else 404
        response.headers.update(PRIVATE)
        return response
    now = utcnow()
    link.opened_at = link.opened_at or now
    link.open_count += 1
    who = await db.get(User, link.user_id) if link.user_id else None
    audit(db, alarm.tenant_id, "alarm.link_opened", user_id=link.user_id, site_id=alarm.site_id, alarm_id=alarm.id,
          ip=client_ip(request), opens=link.open_count)
    await db.commit()
    data = alarm_dict(alarm)
    data.pop("nx_ack_result", None)
    response = render(request, "alarm_link.html", state="ok", alarm=data, token=token,
                      sent_to=who.label if who else "", expires_at=link.expires_at.isoformat())
    response.headers.update(PRIVATE)
    return response


@router.get("/api/a/{token}/clip")
async def link_clip(token: str, request: Request, db: AsyncSession = Depends(get_db),
                    pre: int | None = Query(None, ge=0, le=600), post: int | None = Query(None, ge=1, le=600),
                    quality: str = Query("sd", pattern="^(sd|hd)$")):
    alarm = await _alarm(request, db, token)
    await db.commit()
    try:
        info = await clips.get_clip(alarm, pre, post, quality)
    except clips.ClipError as exc:
        raise HTTPException(400, str(exc)) from exc
    out = info.to_dict()
    if out.get("url"):
        out["url"] = f"/media/a/{token}/clips/{out['url'].rsplit('/', 1)[-1]}"
    return JSONResponse(out, headers=PRIVATE)


@router.get("/api/a/{token}/objects")
async def link_objects(token: str, request: Request, db: AsyncSession = Depends(get_db),
                       from_ms: int | None = None, to_ms: int | None = None):
    alarm = await _alarm(request, db, token)
    s = get_settings()
    start = from_ms if from_ms is not None else alarm.event_ts_ms - s.clip_pre_s * 1000
    end = to_ms if to_ms is not None else alarm.event_ts_ms + s.clip_post_s * 1000
    if end <= start or end - start > s.clip_max_window_s * 1000 + 60_000:
        raise HTTPException(400, "Bad time range")
    await db.commit()
    try:
        return JSONResponse(await clips.objects(alarm, start, end), headers=PRIVATE)
    except Exception as exc:  # noqa: BLE001 - analytics is best-effort; the clip still plays
        return JSONResponse({"detail": str(describe_http_error(exc))}, status_code=502)


_CLIP_NAME = re.compile(r"^a(\d+)_\d+_\d+_(sd|hd)(_g\d+)?\.mp4$")


@router.get("/media/a/{token}/clips/{name}")
async def link_clip_file(token: str, name: str, request: Request, db: AsyncSession = Depends(get_db)):
    alarm = await _alarm(request, db, token)
    m = _CLIP_NAME.match(name)
    if not m or int(m.group(1)) != alarm.id:          # only this alarm's clips
        raise HTTPException(404)
    path = clips.clip_path(name[:-4])
    if not path.exists():
        raise HTTPException(404, "Clip expired from cache; request it again")
    return FileResponse(path, media_type="video/mp4", filename=f"alarm-{alarm.id}.mp4", content_disposition_type="inline",
                        headers={"Cache-Control": "private, no-store", "Referrer-Policy": "no-referrer"})


@router.get("/media/a/{token}/snapshot.jpg")
async def link_snapshot(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    alarm = await _alarm(request, db, token)
    if not alarm.device_id:
        raise HTTPException(404)
    await db.commit()
    return Response(await stream.snapshot_bytes(alarm), media_type="image/jpeg", headers=PRIVATE)


@router.get("/media/a/{token}/live.jpg")
async def link_live_still(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    alarm = await _alarm(request, db, token)
    if not alarm.device_id:
        raise HTTPException(404)
    await db.commit()
    return Response(await stream._frame(alarm, -1), media_type="image/jpeg", headers=PRIVATE)


@router.get("/media/a/{token}/live.webm")
async def link_live(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    alarm = await _alarm(request, db, token)
    if not alarm.device_id:
        raise HTTPException(404)
    await db.commit()          # a live stream runs for minutes: don't pin a DB connection
    return await stream._live_webm(alarm.site, alarm.device_id)
