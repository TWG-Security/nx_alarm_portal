"""The phone apps: /apps (how to install them) and /apps/android.apk (the newest Android APK, app/app_releases.py).

Any signed-in user, by the website cookie or the app's Bearer token. Each download is audit-logged (who, version).
"""

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import FileResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app import app_releases
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, current_user, render
from app.models import User

router = APIRouter()
APK_TYPE = "application/vnd.android.package-archive"


def released(iso: str) -> str:
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%B %-d, %Y")
    except ValueError:
        return ""


@router.get("/apps")
async def apps_page(request: Request, user: User = Depends(current_user)):
    rel = await app_releases.android()
    return render(request, "apps.html", user, page="apps", android=rel,
                  android_date=released(rel.published_at) if rel else "")


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
