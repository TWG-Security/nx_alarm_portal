"""The phone apps: /apps (how to install them), /apps/android.apk (the newest Android APK, app/app_releases.py),
GET /api/apps (what the apps and the page need) and POST /api/apps/invite (TestFlight invite / Android link).

Any signed-in user, by the website cookie or the app's Bearer token; inviting needs users.invite (admins have it).
Downloads and invites are audit-logged.
"""

import re
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import app_releases, appstore, mail, platform_settings
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, current_user, render
from app.models import AuditLog, User

router = APIRouter(dependencies=[Depends(csrf_protect)])
PUBLIC_URL = "https://alarmportal.twgsecurity.net"
INVITES_PER_HOUR = 30
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
APK_TYPE = "application/vnd.android.package-archive"


def released(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%B %-d, %Y")
    except ValueError:
        return ""


def apps_url() -> str:
    return f"{platform_settings.current().portal_url or PUBLIC_URL}/apps"


@router.get("/apps")
async def apps_page(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rel = await app_releases.android()
    row = await platform_settings.get_row(db)
    return render(request, "apps.html", user, page="apps", android=rel, testflight_url=row.testflight_url or "",
                  android_date=released(rel.published_at) if rel else "")


@router.get("/api/apps")
async def apps_info(user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rel = await app_releases.android()
    row = await platform_settings.get_row(db)
    return {"ios": {"testflight_url": row.testflight_url or None, "invites": appstore.config(row) is not None},
            "android": {"version": rel.version if rel else None, "released_at": rel.published_at if rel else None,
                        "size": rel.size if rel else None, "page": "/apps", "apk": "/apps/android.apk"},
            "can_invite": user.can("users.invite")}


class InviteIn(BaseModel):
    email: str = Field(max_length=320)
    name: str = Field(default="", max_length=120)
    platform: str = Field(pattern="^(ios|android|both)$")


@router.post("/api/apps/invite")
async def invite(body: InviteIn, request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    """iPhone: add them as a TestFlight tester (Apple emails the invite). Android: email them the /apps link, or hand
    the link back for the app to share when the portal has no email set up."""
    if not user.can("users.invite"):
        raise HTTPException(403, "Inviting people to the app needs the users.invite permission")
    email = body.email.strip().lower()
    if not EMAIL_RE.match(email):
        raise HTTPException(422, "That isn't an email address")
    since = datetime.now(timezone.utc) - timedelta(hours=1)
    sent = await db.scalar(select(func.count()).select_from(AuditLog).where(
        AuditLog.action == "app.invited", AuditLog.user_id == user.id, AuditLog.ts >= since))
    if sent >= INVITES_PER_HOUR:
        raise HTTPException(429, f"At most {INVITES_PER_HOUR} app invites an hour. Try again later.")
    out: dict = {}
    if body.platform in ("ios", "both"):
        asc = appstore.config(await platform_settings.get_row(db))
        if asc is None:
            out["ios"] = "App Store Connect isn't set up yet (Platform → Phone apps)"
        else:
            try:
                out["ios"] = await appstore.invite(asc, email, body.name)
            except appstore.InviteError as e:
                out["ios"] = str(e)
            except Exception as e:  # noqa: BLE001 - network: say so, never crash the request
                out["ios"] = f"App Store Connect couldn't be reached ({e.__class__.__name__})"
    if body.platform in ("android", "both"):
        url = apps_url()
        if await mail.config() is not None:
            html, text = mail.render(
                "Get the TWG Alarms app", "TWG Alarms on your Android phone",
                [f"{user.label} invited you to the TWG Alarms app: alarms, live video and dispatch on your phone.",
                 "Open the link on your Android phone, sign in with your portal account, and tap Download APK. "
                 "The page shows the three install steps."],
                button=("Get the app", url), footnote="Sent from the TWG Security Alarm Portal.")
            mail.send_later(email, "Get the TWG Alarms app", html, text, purpose="app_invite",
                            tenant_id=user.tenant_id, user_id=user.id)
            out["android"] = "emailed"
        else:
            out["android"] = "share_link"
        out["url"] = url
    audit(db, user.tenant_id, "app.invited", user_id=user.id, ip=client_ip(request), target=email,
          platform=body.platform, outcome={k: v for k, v in out.items() if k != "url"})
    await db.commit()
    return out


@router.get("/apps/android.apk")
async def android_apk(request: Request, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)):
    rel = await app_releases.android(need_file=True)
    if rel is None:
        raise HTTPException(503, "The Android app can't be downloaded right now. Try again in a few minutes.")
    audit(db, user.tenant_id, "app.downloaded", user_id=user.id, ip=client_ip(request), platform="android",
          version=rel.version, tag=rel.tag, sha256=rel.sha256, via="app" if request.scope.get("twg_mobile") else "website")
    await db.commit()
    return FileResponse(app_releases.apk_path(), media_type=APK_TYPE, filename=rel.filename,
                        headers={"Cache-Control": "private, no-store", "X-Content-SHA256": rel.sha256})
