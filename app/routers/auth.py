import time
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, render
from app.models import User
from app.security import new_csrf_token, verify_password

router = APIRouter()

# Simple in-memory brute-force brake: 10 failures per IP per 15 minutes.
_FAILS: dict[str, list[float]] = {}
_WINDOW_S, _MAX_FAILS = 900, 10


def _too_many(ip: str) -> bool:
    now = time.time()
    recent = [t for t in _FAILS.get(ip, []) if now - t < _WINDOW_S]
    _FAILS[ip] = recent
    return len(recent) >= _MAX_FAILS


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("uid"):
        return RedirectResponse("/", 303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login(request: Request, email: str = Form(...), password: str = Form(...),
                db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    if _too_many(ip):
        return render(request, "login.html", error="Too many failed attempts. Try again in 15 minutes.", email=email)
    user = await db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    if user is None or not user.is_active or not verify_password(user.password_hash, password):
        _FAILS.setdefault(ip, []).append(time.time())
        if user is not None:
            audit(db, user.tenant_id, "login.failed", user_id=user.id, ip=ip)
            await db.commit()
        return render(request, "login.html", error="Invalid email or password.", email=email)
    _FAILS.pop(ip, None)
    request.session.clear()
    request.session.update({"uid": user.id, "tid": user.tenant_id, "csrf": new_csrf_token()})
    user.last_login_at = datetime.now(timezone.utc)
    audit(db, user.tenant_id, "login", user_id=user.id, ip=ip)
    await db.commit()
    return RedirectResponse("/", 303)


@router.post("/logout", dependencies=[Depends(csrf_protect)])
async def logout(request: Request, db: AsyncSession = Depends(get_db)):
    uid, tid = request.session.get("uid"), request.session.get("tid")
    if uid and tid:
        audit(db, tid, "logout", user_id=uid, ip=client_ip(request))
        await db.commit()
    request.session.clear()
    return RedirectResponse("/login", 303)
