import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import JSONResponse, RedirectResponse
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app import security_guard as guard
from app.audit import audit
from app.db import get_db
from app.deps import client_ip, csrf_protect, render
from app.models import Tenant, User
from app.security import hash_password, new_csrf_token, verify_password

router = APIRouter()

INVALID = "Invalid email or password."
# Checked against when the email is unknown, so the answer takes as long as for a real account.
_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))


def banned_page(request: Request, ban, *, api: bool = False):
    """403 for a banned IP: the blocked page, or JSON for /api/ calls."""
    until = None if ban.permanent else guard.aware(ban.expires_at)
    msg = "This address is blocked after repeated failed sign-ins."
    if api:
        msg += f" Try again after {until.strftime('%Y-%m-%d %H:%M')} UTC." if until else " Contact TWG Security to lift it."
        return JSONResponse({"detail": msg}, status_code=403)
    if not until:
        msg += " The block is permanent."
    response = render(request, "banned.html", until=until.isoformat() if until else None, ip=ban.ip, message=msg)
    response.status_code = 403
    return response


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("uid"):
        return RedirectResponse("/", 303)
    return render(request, "login.html")


@router.post("/login", dependencies=[Depends(csrf_protect)])
async def login(request: Request, email: str = Form(...), password: str = Form(...),
                db: AsyncSession = Depends(get_db)):
    ip = client_ip(request)
    addr = email.strip().lower()
    # (A banned IP never gets here: app.main.BanGate answers it with the blocked page.)
    if await guard.is_account_locked(db, addr):
        verify_password(_DUMMY_HASH, password)
        await guard.record(db, ip, "login", "locked", email=addr, reason="account locked: too many failures")
        await db.commit()
        return render(request, "login.html", error=INVALID, email=email)
    user = await db.scalar(select(User).where(func.lower(User.email) == addr))
    if user is None:
        verify_password(_DUMMY_HASH, password)
        banned = await guard.record(db, ip, "login", "failure", email=addr, reason="unknown email")
        await db.commit()
        return banned_page(request, banned) if banned else render(request, "login.html", error=INVALID, email=email)
    if not verify_password(user.password_hash, password) or not user.is_active:
        reason = "wrong password" if user.is_active else "account deactivated"
        banned = await guard.record(db, ip, "login", "failure", email=addr, reason=reason, user=user)
        audit(db, user.tenant_id, "login.failed", user_id=user.id, ip=ip)
        await db.commit()
        return banned_page(request, banned) if banned else render(request, "login.html", error=INVALID, email=email)
    tenant = await db.get(Tenant, user.tenant_id)
    if tenant is None or not tenant.is_active:
        await guard.record(db, ip, "login", "denied", email=addr, reason="company sign-in disabled", user=user)
        audit(db, user.tenant_id, "login.failed", user_id=user.id, ip=ip, reason="company disabled")
        await db.commit()
        return render(request, "login.html", error="Your company's portal access is disabled. Contact TWG Security.",
                      email=email)
    request.session.clear()
    request.session.update({"uid": user.id, "tid": user.tenant_id, "csrf": new_csrf_token()})
    user.last_login_at = datetime.now(timezone.utc)
    await guard.record(db, ip, "login", "success", email=addr, user=user)
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
