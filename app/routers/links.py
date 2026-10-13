"""The no-sign-in alarm page a text message links to (app/alarm_links.py, app/sms.py).

- GET /a/{token}                               the page: one alarm's details, clip and live video
- GET /api/a/{token}/clip, /objects            clip status and analytics boxes (same as the signed-in ones)
- GET /media/a/{token}/snapshot.jpg, alarm.jpg (frame + boxes: the MMS picture), live.jpg, live.webm, clips/{name}.mp4
- GET /api/a/{token}/cameras, /media/a/{token}/cameras/{device}/live.jpg|live.webm   the site's other cameras, live
- POST /twilio/sms                            replies to our number (YES / STOP / HELP), Twilio-signed
- GET /sms-terms                              the public text-message terms (carriers and Twilio ask for it)
- POST /api/a/{token}/share                   "Share my location now": the texted person's location-link token
- POST /api/a/{token}/diag                    "Request location": what the phone's browser answered (troubleshooting)
- POST /api/a/{token}/status                  En route / On scene / Clear (an MDT's status buttons), logged on the timeline

Everything is limited to the link's one alarm and the live view of its site's cameras. An unknown token is recorded as a failed sign-in
(kind "alarm_link"), so an address that guesses gets banned (BanGate guards these paths). Opening the
page is audit-logged in the alarm's company under the name of the person it was texted to.
Pages are never cached or indexed and send no Referer (the token is the key).
"""

import asyncio
import logging
import re
import time

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import FileResponse, JSONResponse, Response
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app import alarm_links, platform_settings, security_guard, sms
from app.audit import audit
from app.config import get_settings
from app.db import get_db
from app.deps import client_ip, render
from app.models import Alarm, PlatformSettings, User, utcnow
from app.routers import stream
from app.services import clips
from app.services.serialize import alarm_dict
from app.services.sites import describe_http_error

router = APIRouter()
log = logging.getLogger(__name__)

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


@router.get("/a/{token}/")
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
                      sent_to=who.label if who else "", expires_at=link.expires_at.isoformat(),
                      directions=sms.directions_url(alarm.site),
                      guard_status=await _guard_status(db, alarm.id, link.user_id) if who else "")
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


@router.api_route("/media/a/{token}/alarm.jpg", methods=["GET", "HEAD"])
async def link_picture(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The alarm frame with its boxes: the picture in the MMS (Twilio fetches it, and checks it with HEAD too)."""
    t0 = time.monotonic()
    alarm = await _alarm(request, db, token)
    if not alarm.device_id:
        raise HTTPException(404)
    await db.commit()
    pic = await sms.alarm_picture(alarm, budget_s=20)
    took = int((time.monotonic() - t0) * 1000)
    if pic is None:
        log.warning("alarm %s picture: %s from %s got none after %d ms", alarm.id, request.method, client_ip(request), took)
        raise HTTPException(502, "The camera picture isn't available")
    log.info("alarm %s picture: %s from %s, %d bytes in %d ms (%s)", alarm.id, request.method, client_ip(request),
             len(pic), took, (request.headers.get("user-agent") or "")[:60])
    return Response(pic, media_type="image/jpeg", headers=PRIVATE)


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


# ------------------------------------------------------------------ the site's other cameras, live
# A guard on a phone wants to look around, not only at the alarm camera. The link shows every camera of the
# alarm's site (nothing else): stills that refresh, and live video of the one they tap. Live video shares
# the 6-per-site cap with the portal; stills are limited per site so a phone can't crowd the NX relay.
CAMERA_LIST_TTL_S = 60
STILL_SLOTS = 4
_camera_lists: dict[int, tuple[float, dict[str, str]]] = {}
_still_slots: dict[int, asyncio.Semaphore] = {}


async def _cameras(site) -> dict[str, str]:
    """{device id: name} of the site's cameras (NX's list, at most CAMERA_LIST_TTL_S old)."""
    from app.routers.api import _site_cameras
    hit = _camera_lists.get(site.id)
    if hit and time.monotonic() - hit[0] < CAMERA_LIST_TTL_S:
        return hit[1]
    names = await _site_cameras(site)
    if names:
        _camera_lists[site.id] = (time.monotonic(), names)
    return names


async def _site_camera(request: Request, db: AsyncSession, token: str, device_id: str) -> tuple[Alarm, str]:
    alarm = await _alarm(request, db, token)
    device_id = device_id.strip("{}")
    await db.commit()
    if device_id != (alarm.device_id or "").strip("{}") and device_id not in await _cameras(alarm.site):
        raise HTTPException(404)
    return alarm, device_id


@router.get("/api/a/{token}/cameras")
async def link_cameras(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """The alarm's site cameras, the alarm's own camera first."""
    status, link, alarm = await _resolve(request, db, token)
    if status != "ok":
        raise HTTPException(404 if status == "invalid" else 410)
    audit(db, alarm.tenant_id, "alarm.link_cameras", user_id=link.user_id, site_id=alarm.site_id, alarm_id=alarm.id,
          ip=client_ip(request))
    await db.commit()
    names = await _cameras(alarm.site)
    own = (alarm.device_id or "").strip("{}")
    cams = sorted(({"device_id": d, "name": n or d, "alarm": d == own} for d, n in names.items()),
                  key=lambda c: (not c["alarm"], c["name"].lower()))
    if own and own not in names:
        cams.insert(0, {"device_id": own, "name": alarm.source_name or "Alarm camera", "alarm": True})
    return JSONResponse({"site": alarm.site.name if alarm.site else "", "cameras": cams}, headers=PRIVATE)


@router.get("/media/a/{token}/cameras/{device_id}/live.jpg")
async def link_camera_still(token: str, device_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    alarm, device_id = await _site_camera(request, db, token, device_id)
    slots = _still_slots.setdefault(alarm.site_id, asyncio.Semaphore(STILL_SLOTS))
    async with slots:
        data = await stream.camera_frame(alarm.site, device_id, -1)
    return Response(data, media_type="image/jpeg", headers=PRIVATE)


@router.get("/media/a/{token}/cameras/{device_id}/live.webm")
async def link_camera_live(token: str, device_id: str, request: Request, db: AsyncSession = Depends(get_db)):
    alarm, device_id = await _site_camera(request, db, token, device_id)
    return await stream._live_webm(alarm.site, device_id)


# ------------------------------------------------------------------ text-message consent (public)

TWIML_EMPTY = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


@router.post("/twilio/sms")
async def twilio_reply(request: Request, db: AsyncSession = Depends(get_db)):
    """Twilio's "A message comes in" webhook. Checked with X-Twilio-Signature (our Auth Token), so only
    Twilio can opt people in or out. Replies are sent through the API (logged), so the TwiML is empty."""
    form = await request.form()
    params = [(k, str(v)) for k, v in form.multi_items()]
    cfg = await sms.config(db)
    path = request.url.path
    urls = {f"{platform_settings.current().portal_url}{path}", str(request.url).split("?")[0],
            str(request.url.replace(scheme="https")).split("?")[0]}
    signature = request.headers.get("x-twilio-signature", "")
    if cfg is None or not any(sms.valid_signature(cfg.token, u, params, signature) for u in urls):
        log.warning("twilio reply rejected: %s (from %s)",
                                            "texts are off" if cfg is None else "signature didn't match", client_ip(request))
        if cfg is not None:
            row = await db.get(PlatformSettings, 1)
            row.sms_last_error = ("A reply from Twilio was refused: its signature didn't match. Check the webhook URL on the "
                                  f"Twilio number is exactly {platform_settings.current().portal_url}/twilio/sms and the Auth Token is current.")
            row.sms_last_error_at = utcnow()
            await db.commit()
        raise HTTPException(403, "Bad signature")
    await db.commit()
    data = dict(params)
    await sms.handle_reply(data.get("From", ""), data.get("Body", ""), data.get("MessageSid", ""))
    return Response(TWIML_EMPTY, media_type="application/xml")


@router.get("/sms-terms")
async def sms_terms(request: Request, db: AsyncSession = Depends(get_db)):
    row = await db.get(PlatformSettings, 1)
    contact = (row.sms_help_contact if row else "") or "twgsecurity.com"
    sender = row.twilio_from if row and row.twilio_from.startswith("+") else ""
    return render(request, "sms_terms.html", contact=contact, sender=sender,
                  portal=platform_settings.current().portal_url, updated="October 12, 2026")


@router.post("/api/a/{token}/share")
async def link_share_location(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """"Share my location now" on the alarm page a guard was texted: the location token of their phone tracker,
    made on the spot if they don't have one (then they show on the dispatcher's map like any guard). Only for links
    texted to a person; a link's alarm is in that person's company."""
    from sqlalchemy import select
    from app import guard_share
    from app.models import Tracker
    from app.services import trackers
    status, link, alarm = await _resolve(request, db, token)
    if status != "ok":
        raise HTTPException(404 if status == "invalid" else 410)
    if link.user_id is None:
        raise HTTPException(400, "This link wasn't sent to a person")
    who = await db.get(User, link.user_id)
    t = await db.scalar(select(Tracker).where(Tracker.tenant_id == alarm.tenant_id, Tracker.user_id == who.id,
                                              Tracker.source == "phone"))
    created = t is None
    if created:
        other = await db.scalar(select(Tracker.id).where(Tracker.tenant_id == alarm.tenant_id, Tracker.user_id == who.id))
        t = Tracker(tenant_id=alarm.tenant_id, name=(f"{who.label} (phone)" if other else who.label)[:100], kind="guard",
                    source="phone", user_id=who.id)
        db.add(t)
        await db.flush()
    elif not t.enabled:
        t.enabled = True
    url = guard_share.share_url(t)
    audit(db, alarm.tenant_id, "tracker.share_from_alarm", user_id=who.id, site_id=alarm.site_id, alarm_id=alarm.id,
          ip=client_ip(request), name=t.name, created=created)
    await db.commit()
    from app.services.bus import bus
    bus.publish(t.tenant_id, "tracker.moved", trackers.tracker_dict(t))
    return {"token": url.rstrip("/").rsplit("/", 1)[-1], "name": t.name}



async def _guard_status(db: AsyncSession, alarm_id: int, user_id: int) -> str:
    from sqlalchemy import select
    from app.models import AuditLog
    row = await db.scalar(select(AuditLog).where(AuditLog.alarm_id == alarm_id, AuditLog.user_id == user_id,
                                                 AuditLog.action == "guard.status").order_by(AuditLog.id.desc()).limit(1))
    return (row.detail or {}).get("status", "") if row else ""


class StatusIn(BaseModel):
    status: str = Field(pattern="^(en_route|on_scene|clear)$")


@router.post("/api/a/{token}/status")
async def link_guard_status(token: str, body: StatusIn, request: Request, db: AsyncSession = Depends(get_db)):
    """The guard taps En route / On scene / Clear. Logged on the alarm's timeline with where they were (their freshest
    tracked position under 3 minutes old) and how far that is from the alarm; the dispatcher's screen updates live."""
    from datetime import timedelta
    from sqlalchemy import select
    from app.models import Tracker
    from app.services import dispatch
    from app.services.trackers import aware, metres
    status, link, alarm = await _resolve(request, db, token)
    if status != "ok":
        raise HTTPException(404 if status == "invalid" else 410)
    if link.user_id is None:
        raise HTTPException(400, "This link wasn't sent to a person")
    where = {}
    recent = utcnow() - timedelta(minutes=3)
    t = await db.scalar(select(Tracker).where(Tracker.tenant_id == alarm.tenant_id, Tracker.user_id == link.user_id,
                                              Tracker.lat.is_not(None), Tracker.fix_at >= recent).order_by(Tracker.fix_at.desc()).limit(1))
    if t is not None:
        where = {"lat": round(t.lat, 6), "lng": round(t.lng, 6), "fix_at": aware(t.fix_at).isoformat()}
        target = dispatch.target(alarm)
        if target:
            where["distance"] = sms.distance_words(metres(target[0], target[1], t.lat, t.lng))
    audit(db, alarm.tenant_id, "guard.status", user_id=link.user_id, site_id=alarm.site_id, alarm_id=alarm.id,
          ip=client_ip(request), status=body.status, **where)
    await db.commit()
    return {"ok": True, "status": body.status, "position": bool(where)}



@router.post("/api/a/{token}/diag")
async def link_location_diag(token: str, request: Request, db: AsyncSession = Depends(get_db)):
    """"Request location" on the alarm page: what the phone's browser answered (audit "location.test")."""
    from app.routers.share import DiagIn, record_diag
    status, link, alarm = await _resolve(request, db, token)
    if status != "ok":
        raise HTTPException(404 if status == "invalid" else 410)
    try:
        body = DiagIn.model_validate(await request.json())
    except Exception:  # noqa: BLE001 - a malformed report is just refused
        raise HTTPException(422, "Bad report") from None
    record_diag(db, request, alarm.tenant_id, link.user_id, body, page="alarm", alarm=alarm.id)   # not alarm_id: not on its timeline
    await db.commit()
    return {"ok": True}
